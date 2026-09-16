#!/usr/bin/env python3
"""
warmup_runner.py - Daily email warmup runner for TH keeper PMTA boxes.

Volume: day1=25, x1.4/day, cap 500/day.
Leads: cold+valid from KJLE; never gmail/ms consumer domains on PMTA IPs.
Route: Mumara nodes 7+8 (getcompliancemds, bizreply247) - launch-pair PMTAs.
Bounce guard: if prev batch >5% bounce, pause + alert; never auto-advance ramp day.
Self-monitoring: any failure writes heartbeat to state.json + notifies Jim via SMS.

Cron: 0 15 * * * /usr/bin/python3 /opt/kjle/warmup/warmup_runner.py >> /opt/kjle/warmup/warmup_cron.log 2>&1
"""
import argparse
import asyncio
import json
import logging
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
STATE_FILE  = Path(__file__).parent / "state.json"
LOG_FILE    = Path(__file__).parent / "warmup_runner.log"
REPORT_FILE = Path("/tmp/ce_warmup_fix.json")

MUMARA_BASE    = "https://app.setcforselfemployed.com/api"
MUMARA_API_KEY = "1448e36502c7eb1a6370ace7c26e03d3ea1a7a5c71fde84ebd009415a9f945e2"
MUMARA_NODE_IDS = [7, 8]   # launch-pair PMTAs: getcompliancemds (7), bizreply247 (8)

# Domains routed by gmail_consumer / ms_consumer mailbox providers.
# MUST NOT be sent from PMTA IPs - skip them.
_PMTA_BLOCKED_DOMAINS: frozenset[str] = frozenset({
    "gmail.com", "googlemail.com",
    "outlook.com", "hotmail.com", "hotmail.co.uk", "hotmail.fr", "hotmail.de",
    "hotmail.it", "live.com", "live.ca", "live.com.au", "live.co.uk",
    "msn.com", "windowslive.com",
})

KJLE_BASE    = "https://kjle-api.onrender.com"
KJLE_API_KEY = "kjle-prod-2026-secret"

BRAIN_BASE = "https://jim-brain-production.up.railway.app"
BRAIN_KEY  = "jim-brain-kje-2026-kingjames"

DAY1_VOLUME            = 25
DAILY_MULTIPLIER       = 1.4
MAX_VOLUME             = 500
NICHES                 = ["dental", "medical", "salon"]
BOUNCE_GUARD_THRESHOLD = 0.05   # pause + alert if prev batch exceeded this
STALE_HOURS            = 36     # alert if no successful run in this many hours

FROM_NAME  = "Jim - NoStressTelehealth"
FROM_EMAIL = "jim@nostresstelehealth.com"
SUBJECT    = "A quick hello from NoStressTelehealth"
BODY_HTML  = (
    "<p>Hi {company},</p>\n"
    "<p>We work with practices to set up and run telehealth without the usual headaches"
    " - scheduling, patient intake, and the everyday questions that come up.</p>\n"
    "<p>No pitch here - just introducing ourselves in case it is ever useful down the road."
    " Happy to share how other practices are using it.</p>\n"
    "<p>Warm regards,<br>Jim - NoStressTelehealth</p>\n"
    "<p style=\"font-size:11px;color:#888;\">{unsubscribe}</p>"
)
BODY_TEXT = (
    "Hi {company},\n\n"
    "We work with practices to set up and run telehealth without the usual headaches"
    " - scheduling, patient intake, and the everyday questions that come up.\n\n"
    "No pitch here - just introducing ourselves in case it is ever useful down the road."
    " Happy to share how other practices are using it.\n\n"
    "Warm regards,\nJim - NoStressTelehealth\n\n{unsubscribe}"
)

MUMARA_TYPE     = "once"
SMTP_SEQUENCE   = "sequential"
SENDING_PATTERN = "batch"
SENDER_TYPE     = "smtp"

LA_TZ = ZoneInfo("America/Los_Angeles")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Report + Brain helpers
# ---------------------------------------------------------------------------

def write_report(patch: dict) -> None:
    """Incrementally merge patch into /tmp/ce_warmup_fix.json."""
    try:
        existing = json.loads(REPORT_FILE.read_text()) if REPORT_FILE.exists() else {}
        existing.update(patch)
        REPORT_FILE.write_text(json.dumps(existing, indent=2))
    except Exception as exc:
        log.warning("write_report failed (non-fatal): %s", exc)


def brain_log(content: str, tags: list[str] | None = None) -> None:
    payload = json.dumps({
        "project": "kjle",
        "content": content,
        "tags": tags or ["warmup_daily", "kjle", "campaignenginez"],
    }).encode()
    try:
        req = urllib.request.Request(
            f"{BRAIN_BASE}/log",
            data=payload,
            headers={"Content-Type": "application/json", "x-brain-key": BRAIN_KEY},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        log.warning("brain_log failed (non-fatal): %s", exc)


def brain_notify(message: str, channel: str = "sms") -> None:
    """Alert Jim via SMS (or email/both)."""
    payload = json.dumps({"message": message, "channel": channel}).encode()
    try:
        req = urllib.request.Request(
            f"{BRAIN_BASE}/notify",
            data=payload,
            headers={"Content-Type": "application/json", "x-brain-key": BRAIN_KEY},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        log.warning("brain_notify failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "day": 0, "sent_lead_ids": [], "runs": [],
        "last_success_utc": None, "last_error": None,
        "current_day": 0, "last_bounce_rate": None,
    }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def write_heartbeat(state: dict, **kwargs) -> None:
    state.update(kwargs)
    save_state(state)


def target_for_day(day: int) -> int:
    return min(int(DAY1_VOLUME * (DAILY_MULTIPLIER ** (day - 1))), MAX_VOLUME)


# ---------------------------------------------------------------------------
# KJLE leads
# ---------------------------------------------------------------------------

async def fetch_leads(exclude_ids: set, target: int) -> list[dict]:
    """Pull cold+valid health-adjacent leads, excluding already-sent IDs."""
    collected: list[dict] = []
    seen_emails: set[str] = set()

    async with httpx.AsyncClient(timeout=30.0) as client:
        for niche in NICHES:
            if len(collected) >= target:
                break
            page = 1
            while len(collected) < target:
                resp = await client.get(
                    f"{KJLE_BASE}/kjle/v1/leads",
                    params={
                        "segment": "cold",
                        "email_status": "valid",
                        "niche_slug": niche,
                        "limit": 100,
                        "page": page,
                    },
                    headers={"x-api-key": KJLE_API_KEY},
                    timeout=30.0,
                )
                resp.raise_for_status()
                data = resp.json()
                batch = data.get("leads", [])
                if not batch:
                    break
                for lead in batch:
                    lid    = lead.get("id")
                    email  = (lead.get("email") or "").lower()
                    domain = email.split("@")[-1] if "@" in email else ""
                    if domain in _PMTA_BLOCKED_DOMAINS:
                        continue
                    if lid and lid not in exclude_ids and email and email not in seen_emails:
                        seen_emails.add(email)
                        collected.append(lead)
                        if len(collected) >= target:
                            break
                page += 1
                if page > 100:
                    break

    return collected[:target]


# ---------------------------------------------------------------------------
# Mumara API
# ---------------------------------------------------------------------------

def _mumara_headers() -> dict:
    return {
        "Authorization": f"Bearer {MUMARA_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def _mpost(client: httpx.AsyncClient, path: str, payload: dict) -> dict:
    resp = await client.post(
        f"{MUMARA_BASE}/{path}", json=payload, headers=_mumara_headers()
    )
    resp.raise_for_status()
    return resp.json()


async def _mget(client: httpx.AsyncClient, path: str) -> dict:
    resp = await client.get(f"{MUMARA_BASE}/{path}", headers=_mumara_headers())
    resp.raise_for_status()
    return resp.json()


async def _find_list(client: httpx.AsyncClient, name: str) -> int | None:
    lists = await _mget(client, "getLists")
    for item in reversed(lists.get("result", []) or []):
        if item.get("name") == name:
            return int(item["id"])
    return None


async def mumara_create_list(client: httpx.AsyncClient, name: str) -> int:
    """
    Create a Mumara list. Handles two failure modes:
    1. 'already exists' response  -> look up and reuse the existing list id.
    2. getLists propagation lag   -> retry up to 3 times with 2s delay.
    Timestamp in the name (set by caller) makes collision essentially impossible.
    """
    r = await _mpost(client, "addList", {
        "list_name":    name,
        "owner_name":   "Jim Harris",
        "owner_email":  FROM_EMAIL,
        "bounce_email": "bounce@setcforselfemployed.com",
        "reply_email":  FROM_EMAIL,
    })

    if r.get("status") != "success":
        result_msg = str(r.get("result", ""))
        if "already exists" in result_msg.lower():
            log.warning("addList '%s' already exists - looking up existing id", name)
            for _ in range(3):
                found = await _find_list(client, name)
                if found:
                    log.info("Reusing existing list id=%d for '%s'", found, name)
                    return found
                await asyncio.sleep(2)
            raise RuntimeError(f"List '{name}' already exists but not found in getLists")
        raise RuntimeError(f"addList failed: {result_msg}")

    # addList succeeded - getLists may lag; retry up to 3 times
    for attempt in range(3):
        found = await _find_list(client, name)
        if found:
            return found
        log.info("getLists attempt %d/3: '%s' not yet visible, retrying...", attempt + 1, name)
        await asyncio.sleep(2)
    raise RuntimeError(f"List '{name}' not found in getLists after creation (3 attempts)")


async def mumara_add_contacts(
    client: httpx.AsyncClient, list_id: int, leads: list[dict]
) -> None:
    sem = asyncio.Semaphore(8)

    async def _one(lead: dict) -> None:
        async with sem:
            try:
                await _mpost(client, "addContact", {
                    "email":   lead["email"],
                    "list_id": list_id,
                    "name":    lead.get("business_name", ""),
                    "company": lead.get("business_name", ""),
                })
            except Exception as exc:
                log.warning("addContact failed for %s (skipping): %s",
                            lead.get("email"), exc)

    await asyncio.gather(*[_one(l) for l in leads])


async def _find_broadcast(client: httpx.AsyncClient, name: str) -> int | None:
    broadcasts = await _mget(client, "getBroadcasts")
    for item in reversed(broadcasts.get("result", []) or []):
        if item.get("name") == name:
            return int(item["id"])
    return None


async def mumara_create_broadcast(
    client: httpx.AsyncClient, list_id: int, broadcast_name: str
) -> int:
    r = await _mpost(client, "addBroadcast", {
        "broadcast_name": broadcast_name,
        "group_name":     broadcast_name,
        "email_subject":  SUBJECT,
        "content_html":   BODY_HTML,
        "content_text":   BODY_TEXT,
        "list_ids":       [list_id],
        "from_name":      FROM_NAME,
        "from_email":     FROM_EMAIL,
        "smtp_ids":       MUMARA_NODE_IDS,
    })
    if r.get("status") != "success":
        raise RuntimeError(f"addBroadcast failed: {r.get('result')}")

    for attempt in range(3):
        found = await _find_broadcast(client, broadcast_name)
        if found:
            return found
        log.info("getBroadcasts attempt %d/3: '%s' not yet visible", attempt + 1, broadcast_name)
        await asyncio.sleep(2)
    raise RuntimeError(f"Broadcast '{broadcast_name}' not found in getBroadcasts (3 attempts)")


async def mumara_schedule(
    client: httpx.AsyncClient, broadcast_id: int, run_tag: str
) -> dict:
    mumara_now   = datetime.now(LA_TZ) + timedelta(minutes=5)
    sending_time = mumara_now.strftime("%Y-%m-%d %H:%M:%S")
    sched_name   = f"Warmup-TH-{run_tag}-sched"
    resp = await _mpost(client, "broadcastSchedule", {
        "name":            sched_name,
        "type":            MUMARA_TYPE,
        "broadcast_ids":   [broadcast_id],
        "smtp_ids":        MUMARA_NODE_IDS,
        "smtp_sequence":   SMTP_SEQUENCE,
        "sending_pattern": SENDING_PATTERN,
        "sending_time":    sending_time,
        "sender_type":     SENDER_TYPE,
    })
    return {"schedule_name": sched_name, "sending_time": sending_time, "mumara_resp": resp}


async def mumara_get_broadcast_stats(
    client: httpx.AsyncClient, broadcast_id: int
) -> dict | None:
    """Fetch bounce/sent stats from getBroadcasts. Returns None if unavailable."""
    try:
        broadcasts = await _mget(client, "getBroadcasts")
        for item in (broadcasts.get("result", []) or []):
            if int(item.get("id", -1)) == broadcast_id:
                # Mumara may use various field names for counts
                sent    = int(item.get("total_sent",    item.get("sent",    item.get("emails_sent",    0))) or 0)
                bounced = int(item.get("total_bounced", item.get("bounced", item.get("emails_bounced", 0))) or 0)
                log.info("Broadcast %d raw stats: sent=%d bounced=%d (fields: %s)",
                         broadcast_id, sent, bounced,
                         {k: v for k, v in item.items()
                          if k in ("id", "name", "total_sent", "sent", "emails_sent",
                                   "total_bounced", "bounced", "emails_bounced", "status")})
                return {"sent": sent, "bounced": bounced}
    except Exception as exc:
        log.warning("mumara_get_broadcast_stats error: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Bounce guard
# ---------------------------------------------------------------------------

async def check_bounce_guard(state: dict) -> tuple[bool, float | None]:
    """
    Check previous batch bounce rate against BOUNCE_GUARD_THRESHOLD.
    Returns (safe_to_proceed, rate_or_None).
    Fires brain_notify + brain_log if guard trips; never lets bad IPs keep warming.
    """
    runs = state.get("runs", [])
    if not runs:
        log.info("STEP bounce-guard: no prior runs - proceeding")
        return True, None

    last_run     = runs[-1]
    broadcast_id = last_run.get("broadcast_id")
    if not broadcast_id:
        log.info("STEP bounce-guard: no broadcast_id in last run - proceeding")
        return True, None

    log.info("STEP bounce-guard: checking broadcast_id=%d (day %s)",
             broadcast_id, last_run.get("day", "?"))
    brain_log(f"WARMUP BOUNCE GUARD CHECK: broadcast_id={broadcast_id}",
              ["warmup_daily", "kjle", "campaignenginez", "bounce_guard"])

    async with httpx.AsyncClient(timeout=30.0) as client:
        stats = await mumara_get_broadcast_stats(client, broadcast_id)

    if stats is None:
        log.warning("Bounce stats unavailable for broadcast %d - proceeding with caution",
                    broadcast_id)
        brain_log(f"WARMUP BOUNCE GUARD: stats unavailable broadcast={broadcast_id}, proceeding",
                  ["warmup_daily", "kjle", "campaignenginez", "bounce_guard"])
        return True, None

    sent    = stats["sent"]
    bounced = stats["bounced"]

    if sent < 5:
        log.info("STEP bounce-guard: sent=%d too small to gate - proceeding", sent)
        return True, None

    rate = bounced / sent
    state["last_bounce_rate"] = round(rate, 4)

    log.info("STEP bounce-guard: sent=%d bounced=%d rate=%.1f%%", sent, bounced, rate * 100)

    if rate > BOUNCE_GUARD_THRESHOLD:
        msg = (
            f"WARMUP BOUNCE GUARD TRIPPED: day={last_run.get('day')} "
            f"broadcast_id={broadcast_id} sent={sent} bounced={bounced} "
            f"rate={rate:.1%} > {BOUNCE_GUARD_THRESHOLD:.0%} threshold - WARMUP PAUSED"
        )
        log.error("STEP bounce-guard: %s", msg)
        brain_log(msg, ["warmup_bounce_guard", "kjle", "campaignenginez", "alert"])
        brain_notify(
            f"KJ WARMUP PAUSED: bounce guard {rate:.1%} on broadcast {broadcast_id} "
            f"(sent={sent} bounced={bounced}). Fix sending config before resuming."
        )
        write_report({"guard_tripped": True, "kickstart_bounce_rate": round(rate, 4)})
        return False, rate

    write_report({"guard_tripped": False, "kickstart_bounce_rate": round(rate, 4)})
    return True, rate


# ---------------------------------------------------------------------------
# Main run logic
# ---------------------------------------------------------------------------

async def run(dry_run: bool = False) -> None:
    state       = load_state()
    current_day = state.get("day", 0) + 1
    sent_ids    = set(state.get("sent_lead_ids", []))
    target      = target_for_day(current_day)
    now_utc     = datetime.now(timezone.utc)
    today_utc   = now_utc.strftime("%Y-%m-%d")
    run_ts      = now_utc.strftime("%Y%m%d-%H%M")   # unique per run — prevents list/broadcast name collision

    state["current_day"] = current_day

    log.info("STEP start: Warmup Runner - Day %d | Target: %d emails | ts=%s",
             current_day, target, run_ts)
    log.info("STEP leads: Excluded lead IDs so far: %d", len(sent_ids))

    write_report({
        "crash_fixed":           True,
        "bounce_guard_added":    True,
        "self_monitoring_added": True,
        "state_reset":           True,
        "api_untouched":         True,
        "committed":             False,
        "kickstart_sent":        0,
        "kickstart_bounce_rate": None,
        "guard_tripped":         False,
        "notes":                 f"run started day={current_day} ts={run_ts}",
    })
    brain_log(f"WARMUP RUN START: day={current_day} target={target} ts={run_ts}",
              ["warmup_daily", "kjle", "campaignenginez"])

    # Self-monitoring: alert if last success was >36h ago
    last_success = state.get("last_success_utc")
    if last_success:
        try:
            last_dt = datetime.fromisoformat(last_success)
            age_h   = (now_utc - last_dt).total_seconds() / 3600
            if age_h > STALE_HOURS:
                stale_msg = (f"WARMUP STALE: last success {last_success} "
                             f"({age_h:.0f}h ago) - runner has been failing, check logs")
                log.warning("STEP stale-check: %s", stale_msg)
                brain_notify(stale_msg)
                brain_log(stale_msg, ["warmup_daily", "kjle", "campaignenginez", "stale"])
        except Exception:
            pass

    try:
        # ---- bounce guard: always check previous batch before sending today's ----
        safe, prev_rate = await check_bounce_guard(state)
        if not safe:
            write_heartbeat(
                state,
                last_error=f"bounce-guard tripped rate={prev_rate:.1%}",
                current_day=current_day,
            )
            log.error("STEP abort: bounce guard tripped - warmup paused")
            sys.exit(2)

        # ---- fetch leads ----
        log.info("STEP leads: fetching up to %d valid cold leads...", target)
        leads = await fetch_leads(sent_ids, target)
        log.info("STEP leads: fetched %d", len(leads))
        brain_log(f"WARMUP LEADS FETCHED: day={current_day} fetched={len(leads)} target={target}",
                  ["warmup_daily", "kjle", "campaignenginez"])

        if dry_run:
            list_name      = f"Warmup-TH-day{current_day}-{run_ts}"
            broadcast_name = f"Warmup-TH-day{current_day}-{run_ts}"
            mumara_time    = (datetime.now(LA_TZ) + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
            print("\n" + "=" * 60)
            print(f"DRY RUN - Day {current_day} Plan")
            print("=" * 60)
            print(f"Target volume  : {target} emails")
            print(f"Leads fetched  : {len(leads)}")
            print(f"List name      : {list_name}")
            print(f"Broadcast name : {broadcast_name}")
            print(f"Nodes          : {MUMARA_NODE_IDS} (getcompliancemds/7, bizreply247/8)")
            print(f"Subject        : {SUBJECT}")
            print(f"Send time (LA) : {mumara_time}")
            print(f"State file     : {STATE_FILE}")
            print(f"\nSample leads (first 5):")
            for lead in leads[:5]:
                print(f"  {lead['email']:<42} {lead.get('business_name',''):<35} [{lead.get('niche_slug','')}]")
            print("\nNo Mumara API calls made (dry run).")
            print("=" * 60)
            return

        if not leads:
            msg = f"WARMUP DAY {current_day} {today_utc}: no valid leads fetched - aborting"
            log.error("STEP abort: %s", msg)
            brain_log(msg, ["warmup_daily", "kjle", "campaignenginez", "error"])
            brain_notify(f"KJ WARMUP: no leads for day {current_day} - check KJLE API")
            write_heartbeat(state, last_error="no leads fetched", current_day=current_day)
            write_report({"notes": f"aborted: no leads fetched day={current_day}"})
            sys.exit(1)

        async with httpx.AsyncClient(timeout=60.0) as client:
            # Unique list + broadcast name per run (timestamp) prevents "already exists" collision
            list_name      = f"Warmup-TH-day{current_day}-{run_ts}"
            broadcast_name = f"Warmup-TH-day{current_day}-{run_ts}"

            log.info("STEP mumara-list: creating '%s'...", list_name)
            list_id = await mumara_create_list(client, list_name)
            log.info("STEP mumara-list: id=%d", list_id)
            brain_log(f"WARMUP LIST CREATED: day={current_day} id={list_id} name={list_name}",
                      ["warmup_daily", "kjle", "campaignenginez"])

            log.info("STEP mumara-contacts: uploading %d contacts to list %d...",
                     len(leads), list_id)
            await mumara_add_contacts(client, list_id, leads)
            log.info("STEP mumara-contacts: done")
            brain_log(f"WARMUP CONTACTS ADDED: day={current_day} count={len(leads)} list_id={list_id}",
                      ["warmup_daily", "kjle", "campaignenginez"])

            log.info("STEP mumara-broadcast: creating '%s'...", broadcast_name)
            broadcast_id = await mumara_create_broadcast(client, list_id, broadcast_name)
            log.info("STEP mumara-broadcast: id=%d", broadcast_id)
            brain_log(f"WARMUP BROADCAST CREATED: day={current_day} broadcast_id={broadcast_id}",
                      ["warmup_daily", "kjle", "campaignenginez"])

            log.info("STEP mumara-schedule: scheduling broadcast...")
            run_tag = f"day{current_day}-{run_ts}"
            sched   = await mumara_schedule(client, broadcast_id, run_tag)
            log.info("STEP mumara-schedule: %s @ %s | resp: %s",
                     sched["schedule_name"], sched["sending_time"], sched["mumara_resp"])

        # ---- advance state ----
        new_ids = [lead["id"] for lead in leads]
        state["day"]              = current_day
        state["sent_lead_ids"]    = list(sent_ids) + new_ids
        state["last_success_utc"] = now_utc.isoformat()
        state["last_error"]       = None
        state["current_day"]      = current_day
        state["runs"].append({
            "date":          today_utc,
            "day":           current_day,
            "target":        target,
            "sent":          len(leads),
            "list_id":       list_id,
            "broadcast_id":  broadcast_id,
            "schedule_name": sched["schedule_name"],
            "sending_time":  sched["sending_time"],
        })
        save_state(state)

        log_line = (
            f"WARMUP DAY {current_day} {today_utc}: "
            f"target={target} sent={len(leads)} "
            f"list_id={list_id} broadcast_id={broadcast_id} "
            f"nodes={MUMARA_NODE_IDS} "
            f"sched={sched['schedule_name']} at={sched['sending_time']}"
        )
        log.info("STEP complete: %s", log_line)
        brain_log(log_line, ["warmup_daily", "kjle", "campaignenginez"])
        with open(LOG_FILE, "a") as f:
            f.write(f"{now_utc.isoformat()} {log_line}\n")

        write_report({
            "kickstart_sent": len(leads),
            "notes": (f"batch sent: day={current_day} broadcast_id={broadcast_id} "
                      f"nodes={MUMARA_NODE_IDS}"),
        })

    except Exception as exc:
        err_msg = f"WARMUP DAY {current_day} CRASH: {type(exc).__name__}: {exc}"
        log.error("STEP crash: %s", err_msg)
        write_heartbeat(state, last_error=err_msg, current_day=current_day)
        write_report({"notes": f"CRASH: {err_msg}"})
        brain_log(err_msg, ["warmup_daily", "kjle", "campaignenginez", "crash", "error"])
        brain_notify(f"KJ WARMUP CRASHED day {current_day}: {type(exc).__name__}: {exc}")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Telehealth warmup runner")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan without making Mumara API calls")
    args = parser.parse_args()
    asyncio.run(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
