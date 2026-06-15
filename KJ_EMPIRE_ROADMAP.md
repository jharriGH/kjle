---
project: KJLE
status: active
description: Lead engine — scraping, classification, DNC compliance, RI campaign creation. Data hub for the empire.
last_updated: 2026-06-14
integrates_with:
  - EmpireSenderz
  - TH
  - DemoEnginez
  - DemoBoosterz
  - Local Scraper workers
sc_contact: ""
current_sprint: "Day 3 done — LS pipeline proven; casing patch + DNC swap pending"
sprint_target_date: 2026-06-09
cost_spent: 10.92
cost_remaining: 4.08
notes: "LS pipeline proven end-to-end (job ed784559, 120 leads). One SC casing patch from leads landing. kjle-api flapping root-caused (scheduler loop blocking). DNC swap to RealValidito in flight."
repo: jharriGH/kjle
api_url: https://kjle-api.onrender.com
facts_doc: PROJECT_FACTS.md
vault_key: API_SECRET_KEY
---

# 👑 KJ EMPIRE — CENTRALIZED ROADMAP

**Owner:** Jim Harris, DevelopingRiches Inc
**Last refresh:** 2026-06-14 (AVA Smart Routing Phase C2 section added — Empire Inbound Router; outbound-lock-gated)
**Sprint goal:** Autonomous lead pumping live by **Monday 2026-06-09**
**Source of truth:** This file + `KJ_EMPIRE_ROADMAP.html`
**KJLE main HEAD:** `d42b54a` (last verified by this seat) — SC reports main now at `46ec14e`; SC to confirm before any pull.

---

## 🖼️ VISUAL DASHBOARD — KJ_EMPIRE_ROADMAP.html

Companion file: `KJ_EMPIRE_ROADMAP.html`. Same data, visual format. Save both in `Documents/GitHub/kjle/` and open the HTML for a glanceable status check.

**Update discipline — non-negotiable:**

1. **At session start** — read BOTH this markdown AND the HTML before doing anything else
2. **After EVERY successful task** — update BOTH files inline before moving on
3. **At session end** — final refresh of BOTH, deliver via outputs folder
4. **If only one updates** — task is NOT complete

---

## 🚦 STATUS LEGEND

| Symbol | Meaning |
|---|---|
| 🟢 | Done & verified |
| 🟡 | In progress now |
| 🔵 | Queued — next up |
| ⚪ | Scoped — later in sprint |
| ⏸️ | Parked — deferred post-sprint |
| ❌ | Blocked / needs decision |
| ⚠️ | Risk / watch item |

---

## 🎯 HEADLINE — DAY 3 (2026-06-07)

**The autonomous scrape→ingest pipeline is PROVEN end-to-end.** Job `ed784559` ran clean: Local Scraper exited rc 0, daemon loaded **120 leads** from JSON, KJLE ingest returned `status: processed` with **zero errors**. Every fix shipped tonight held.

**One blocker remains before leads actually land:** all 120 leads were filtered (`inserted_count: 0, filtered_count: 120`) due to a **field-name casing mismatch** in KJLE's ingest. Root-caused; one-block SC patch ready (see Blockers). Until patched, every scrape will filter 100%.

---

## 📅 SPRINT CALENDAR (Mon 6/2 → Mon 6/9)

| Day | Theme | Status | Cost |
|---|---|---|---|
| Day 1 (Mon 6/2) | Phase 4 close + ES bug hotfix | 🟢 DONE | $5.51 |
| Day 2 AM | Connection pool fix | 🟢 DONE | $0 |
| Day 2 PM | Phase 4.2 backend (scrape queue + worker daemon) | 🟢 DONE | $5.41 |
| Day 3 (landed 6/7) | LS install + daemon live + pipeline proven | 🟢 **DONE** (1 SC patch pending) | ~$0 |
| Day 4 | n8n workflow build — 1 niche end-to-end | ⚪ scoped | ~$2-4 |
| Day 5 | Multi-niche + scraper trigger logic | ⚪ scoped | ~$2-3 |
| Day 6-7 | Stress test + monitoring | ⚪ scoped | ~$2-5 |
| Day 8 (Mon 6/9) | **AUTONOMOUS LEAD PUMPING LIVE** | 🎯 target | ~$0-1 |

**Sprint spent so far:** $10.92 (Day 3 added $0 — all Tier 0/1 diagnostics)
**Sprint budget remaining:** $9-14 (target total: $15-25)

---

## ✅ COMPLETED — DAY 3 (Local Scraper pipeline, 2026-06-07)

| Item | Status | Notes |
|---|---|---|
| LS files onto Z820 | 🟢 | `C:\Users\King James\OneDrive\Desktop\workers\local_scraper_daemon\` (OneDrive-redirected Desktop); Python 3.13.5 |
| Daemon live on Z820 | 🟢 | Reverse-polling `/scrape/jobs/poll` every 15s; `X-Worker-API-Key` authenticates (returns `{"jobs":[]}`) |
| Env vars set (Z820) | 🟢 | `KJLE_API_BASE`, `WORKER_ID=z820`, `LOCAL_SCRAPER_SAVE_DIR=C:\KJLE\scrapes`, `LOCAL_SCRAPER_WEBHOOK_SECRET`, `KJLE_WORKER_API_KEY`, `LOCAL_SCRAPER_EXE_PATH` |
| **Backend gap 1 — ingest log table** | 🟢 | `local_scraper_ingest_log` table created in Supabase (ingest had been fail-closed on missing table → `ingest_log_write_failed`) |
| **Backend gap 2 — webhook secret** | 🟢 | `LOCAL_SCRAPER_WEBHOOK_SECRET` set in Render `kjle-api` env (HMAC verify was fail-closed) |
| **Backend gap 3 — LS output format** | 🟢 | Switched LS export CSV→JSON; daemon `_load_saved_lead_files` reads only `**/*.json`. LS JSON = top-level list of dicts ✓ |
| LS CLI contract verified | 🟢 | `--target "<exact scraper name>"` + `--keyword` + `--location`; daemon builds 13-arg cmd. Requires saved settings file (Tools→Save Settings) |
| **End-to-end run proven** | 🟢 | Job `ed784559`: rc 0 → 120 leads loaded → ingest `processed`, 0 errors |

**Day 3 dispatch cost: $0** (all Tier 0/1: vps_exec reads, Supabase SQL editor, Render env, GUI config)

---

## ❌ ACTIVE BLOCKER — 120/120 leads filtered (root-caused)

**Symptom:** Job `ed784559` ingest → `results_count: 120, inserted_count: 0, filtered_count: 120`.

**Root cause:** Field-name **casing mismatch**. `api/routes/local_scraper_ingest.py` → `_process_results` (L142-152) reads contact fields **lowercase** (`raw_lead.get("phone"/"email"/"website")`). Local Scraper JSON emits **capitalized** keys (`Phone`/`Email`/`Website`/`Name`/`FullAddress`). All three `.get()` return `None` per row → "no contact path" (L151) → every row filtered.

**Fix (SC domain — receiver code, off-limits to worker-daemon seat):** in `_process_results`, top of the `for raw_lead in results:` loop, after the `isinstance` check, add:
```python
            raw_lead = {
                (k.lower() if isinstance(k, str) else k): v
                for k, v in raw_lead.items()
            }
```
Commit + redeploy `kjle-api`, re-fire one scrape, expect `inserted_count > 0`. Alt locus: normalize in daemon before POST (ingest side is the cleaner contract boundary).

**Owner:** KJLE backend SC. **Status:** patch handed off, awaiting deploy.

---

## ⚠️ kjle-api FLAPPING — root-caused (coordinated fix)

**Symptom:** `kjle-api` goes "down/up dozens of times a day" — 502s on poll, `/health` itself timed out at 40s. Restored twice this session via Render restart (healthy ~130-195ms after).

**Root cause:** In-process `AsyncIOScheduler` (`api/routes/scheduler.py` `setup_scheduler`, ~13 jobs, started in `api/main.py` lifespan) runs **synchronous blocking** Supabase/external calls directly on the single event loop — **zero** `asyncio.to_thread` / `run_in_executor` / `ThreadPoolExecutor` offloading. A blocking job stalls the whole loop, so even static-dict `/health` hangs. Pool fix `813f75b` only added a 30s DB timeout — did not address loop blocking.

**Fix plan (SC domain — `scheduler.py` PROTECTED):**
- **Primary:** move scheduler to a separate Render **Background Worker** service.
- **Secondary:** wrap blocking DB calls in `asyncio.to_thread`; consider multiple uvicorn workers.
- **Coordination required:** ~10 API consumers, 4 of them real-time fail-closed (Telehealth/AVA, DemoBoosterz, KJ Sales Agentz, n8n `/dnc/check`). Flapping intermittently halts live outreach empire-wide. Needs SC sign-off + Render config change + one cost-approved T3 dispatch.

**Owner:** KJLE backend SC. **Status:** root cause documented, fix queued.

---

## 🔵 QUEUED — Phase 5 (Day 4-7)

| Item | Owner | Cost | Conf. |
|---|---|---|---|
| **SC: land the casing patch** (unblocks leads) | KJLE SC | $0 (T1) | High |
| Wire Find Leads "Start Scrape" button → `/scrape/start` (Lovable prompt) | Jim (paste) | $0 | High |
| Build `GET /kjle/v1/leads/eligible-for-campaign` helper | Claude (CC) | ✓ $1-2 | High |
| n8n workflow: 1 niche end-to-end (route → create → attach) | Jim + Claude | ? $0-1 | Med |
| Test campaign creates in RI manually first | Jim | $0 | High |
| Lead inventory monitor (triggers scraper if pool < N) | Claude (CC) | ✓ $2-3 | Med-High |
| Monitoring/alerting for autonomous failures | Claude (CC) | ? $2-5 | Low |

**Phase 5 total estimated cost: ~$6-15**

---

## 🏗️ ARCHITECTURE STATUS

| Component | URL/Location | Status |
|---|---|---|
| KJLE API | https://kjle-api.onrender.com | 🟢 Live ⚠️ flapping (root-caused — scheduler blocking loop) |
| KJLE Lead Finder UI | https://kjleadzempire.com | 🟢 Live, 4 tabs |
| KJLE Supabase | dhzpwobfihrprlcxqjbq | 🟢 Small compute tier |
| EmpireSenderz API | https://kjle-sender.onrender.com | 🟢 Live, CR.3 routing endpoints |
| Jim Brain | https://jim-brain-production.up.railway.app | 🟢 Live (intermittent 500s on memory writes → fall back to brain_log) |
| Brain MCP | https://kje-mcp.onrender.com | 🟢 Live |
| RackNerd VPS | 192.161.173.97 | 🟢 CC dispatcher on 8091 |
| CSV uploader cron | `/opt/kjle_csv_uploader.py` (10pm nightly) | 🟢 Live |
| Worker daemon code | GitHub `workers/local_scraper_daemon/` (commit `d42b54a`) | 🟢 Shipped |
| **Local Scraper (Z820)** | Z820 desktop | 🟢 **Installed + daemon live & polling** |
| Local Scraper (VPS) | RackNerd VPS | 🟡 Awaiting license #2 + Wine (later) |
| n8n autonomous orchestrator | Railway | ⚪ Not built yet |

---

## ⚠️ KNOWN RISKS / WATCH ITEMS

| Risk | Likelihood | Cost impact |
|---|---|---|
| SC casing patch slips → no leads land | — | $0 (blocks progress, not budget) |
| kjle-api flapping recurs during n8n testing | Medium-High | $0 to restart; fix is coordinated T3 |
| LS license #2 / Wine for VPS worker | Medium | $0 — install time |
| n8n learning curve eats Day 4-5 | Medium | +$2-3 |
| Scope creep — "just add X" | High | +$3-5 per off-roadmap dispatch — **biggest budget risk** |
| Brain memory 500s | Low | $0 — fall back to brain_log |

---

## 📡 AVA SMART ROUTING — PHASE C2 (Empire Inbound Router)

**Updated:** 2026-06-14
**Scope:** AVA-using products
**New repo:** `empire-inbound-router` (not yet deployed)
**Config schema:** `ava_router`

**Summary:** Empire inbound router for the shared 866. A call back to 866 is identified by caller number and routed to the correct AVA context (`reviewbombz_crisis_inbound`, `telehealth_closer_inbound`, `kjwidgetz_warm_inbound`) with IVR/default fallback for unknown callers. Built as a NEW standalone sidecar with **zero edits** to the live outbound AVA stack.

### ❌ HARD GATE — AVA outbound LOCKED Brain log missing

No AVA outbound LOCKED Brain log exists. **All deploy, flip, and test items are HELD** until the outbound AVA SC posts the lock.

### 🟢 Done & verified

| Item | Status |
|---|---|
| Sidecar `main.py` + `lookup.py` import-clean | 🟢 |
| Fail-safe to default IVR | 🟢 |
| Read-only parameterized lookup | 🟢 |
| AVA-only scope enforced in code | 🟢 |
| Routing precedence: recent outbound call → product tables → unknown/default | 🟢 |
| SIP target hardened off loopback to `sip:192.161.173.97:5060` with `/health` warning | 🟢 |
| Full GOAT admin config drafted (`ava_router` schema: `routes`, `personas`, `settings`, `route_log`; idempotent migration, **NOT applied**) | 🟢 |
| `config_store.py` loader + `render_contexts` generator drafted | 🟢 |
| `vps_exec` guard widened (`/home/ccrunner` readable live; `/opt/ava/src` + dotfile deny committed `86bb484`, Render cutover) | 🟢 |

### 🔵 / ⏸️ Pending — blocked / queued

| Item | Status |
|---|---|
| Admin panel UI — NEW AVA Router module on the KJLE Command Deck (`deck.kjle.com`); placement decided, **NOT** in ReviewBombz UI | 🔵 Queued |
| Apply `ava_router` migration to Supabase (manual rule) — gates the panel | 🔵 Queued |
| Inbound contexts into `ai-agent.local.yaml` | ⏸️ Parked on outbound lock |
| Asterisk reload | ⏸️ Parked on outbound lock |
| 866 `voice_url` flip | ⏸️ Parked on outbound lock |
| Inbound test calls | ⏸️ Parked on outbound lock |

### ⚙️ Admin panel option set

- **Voice / persona:** voice (alloy default; echo / shimmer / ash / ballad / coral / sage / verse), tone, agent_name, language, greeting, persona_prompt, temperature, speaking_rate
- **Audio / turn:** vad_threshold, noise_reduction (near_field), silence_hangup 8s, max_call
- **Routing:** product, category, direction, match_source, priority, business_hours, timezone, fallback
- **Ops:** route_log feed, health, unknown-caller rate; guarded 866 `voice_url` flip + rollback

### 🤝 SC coordination

- Outbound AVA SC owns the **AVA outbound LOCKED** signal
- Jim is confirming outbound status
- No C2 deploy moves until the lock lands

---

## 🤝 EXTERNAL DEPENDENCIES — SC COORDINATION

### KJLE backend SC (NEW — this seat's handoffs)
| Item | Status |
|---|---|
| Casing patch in `_process_results` (lands leads) | 🟡 Handed off |
| Scheduler split → Background Worker (flapping fix) | 🟡 Root cause documented |
| `/opt/kjle` working tree cleanup (uncommitted dnc.py +261, scheduler.py, .gitignore on `phase4-email-clean-poller-and-logging-fix`) | ⚠️ SC-owned — author commits/stashes; this seat will NOT git-write `/opt/kjle` |
| DNC vendor swap (RealValidation/RealValidito) — edits same `dnc.py` | ⏸️ Downstream of tree cleanup |

### EmpireSenderz SC
| Item | Status |
|---|---|
| Bug 1 (int→email) fixed at `1bbdeb0` | 🟢 |
| Bug 1.1 (set-accounts 500 on valid payload) | 🟡 capture RI UI request via DevTools |
| Bug 2.1 (rollback path / orphan campaigns) | 🟡 add logging around `ri_delete` |

---

## 💰 COST PROTOCOL — 5-TIER FRAMEWORK

| Tier | What | Cost |
|---|---|---|
| T0 | vps_exec / terminal / browser dev tools | $0 |
| T1 | GitHub web UI edit (2-20 lines) | $0 |
| T2 | Claude writes patch, Jim applies | $0 |
| T3 | Single combined CC dispatch (build+verify+merge) | $1.50-$3 |
| T4 | Multiple sequential dispatches (rare, explicit approval) | $5+ |

**5 absolute rules:** (1) never dispatch CC just to run curl; (2) never dispatch CC just to verify truth; (3) never dispatch CC for drain/batch/loop — use vps_exec; (4) never split chaperone + merge; (5) when uncertain, **ASK Jim before spending.** State verified cost BEFORE any dispatch; wait for explicit Y/N.

---

## 📌 STANDING REMINDERS

- 📌 Roadmap dual-update discipline — refresh both .md and .html after every successful task
- 📌 Cost protocol — state verified cost BEFORE any T3+ dispatch, get explicit Y/N
- 📌 Build card v9 refresh needed
- 📌 Add/test "Send to DemoBoosterz" from Lead Finder after Tour demo
- 📌 Schedule local backups for `Documents/GitHub`
- 📌 EmpireSenderz: prune 83 stale Instantly rows in mailbox_fleet
- 📌 EmpireSenderz: `reachinbox_ltd_1` empty workspace decision
- 📌 Promote Z820 daemon from foreground to Task Scheduler once stable
- 📌 vps_exec cannot send master `x-api-key` (secret denylist) — Jim runs master-auth POSTs himself

---

## 🎯 NEXT IMMEDIATE ACTION

1. **KJLE SC:** apply the `_process_results` casing patch + redeploy `kjle-api`.
2. Re-fire one test scrape → confirm `inserted_count > 0`.
3. Wire Find Leads "Start Scrape" button (Lovable prompt — Claude writes, Jim pastes).
4. Begin n8n 1-niche workflow.

---

*Centralized Empire Roadmap v1.3 — DevelopingRiches Inc — Jim Harris*
*Updated after every successful task — markdown + HTML kept in sync*
