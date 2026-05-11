# 🧠 KJLE — CLAUDE.md
# Auto-healed by claude_md_healer.py from Jim Brain state
# Last healed: 2026-05-11 20:00:16 UTC
# Repo: /opt/kjle

---

## WHO YOU WORK FOR

You are working for Jim Harris — King James Empire (KJE).
Empire-wide rules in `/opt/jim-brain/CLAUDE.md` (KJ_RULEZ) apply unless this
file explicitly supersedes them.

Brain endpoint: `https://jim-brain-production.up.railway.app`
Brain key: `jim-brain-kje-2026-kingjames` (header: `x-brain-key`, lowercase)

---

## PROJECT STATUS

- **Project:** KJLE 🎯
- **ID:** `kjle`
- **Group:** KJE SaaS
- **Status:** `live`
- **Description:** Lead empire backend — 32/32 done, 28,849 leads ready

### Next Action
Session 2B SHIPPED 2026-05-10. Truelist bulk-batches LIVE: batch-1 manual 4000 leads ingested clean (2795 valid / 76 invalid / 1129 unknown / 0 errors / 0 no_csv_match) - first nonzero invalid count ever, parser bug fix verified. Nightly cron auto-fired batch-2 with 15566 emails, poller will ingest within 30 min. 30-min poller cron running every 30 min. Two PostgREST gotchas fixed live (NULL semantics + 1000-row cap). Session 2C scope: (1) Configure Truelist dashboard webhook to fire at /webhooks/truelist/batch-complete?secret=TRUELIST_WEBHOOK_SECRET - drops batch-completion latency from 0-30min to seconds (5min Truelist UI work). (2) Re-verify the 17576 unknown leads from v1 verify_inline to recover hidden invalids using new architecture. (3) Cleanup 2786 orphan leads stuck in email_status=pending_batch with no parent batch_id - either flush back to NULL or sweep into next batch. (4) Watch first weekend of 4-batches/night cron to confirm 100K/day rate clears backlog in target 5 days.

---

## RECENT MEMORIES (top 6)

1. KJLE Session 2B shipped on 2026-05-10
2. KJLE pain v2 formula deployed and verified (commit 02673ac)
3. Tags: reviewbombz, kjle, dnc, phase25, voicemail
4. Saved card IDs: ReviewBombz 1776982890230, KJLE 1776982893503, KJWidgetz 1776982896422, DemoEnginez 1776982899202, DemoBoosterz 1776982901880, SiteEnginez 1776982904787, IASY 1776982907530, UnhideLocal 1776982910247, TestEnginez 1776982912894
5. KJLE stamped complete by CC overnight
6. New empire projects added on March 23, 2026: SiteEnginez (SiteEnginez.com + BuildEnginez.com — DemoEnginez one-click site fulfillment, Cloudflare Pages deploy), KJ Command Deck (KJLE React frontend, military sci-fi HUD, deck.kjle.com, Prompts 1-25 complete, AlertSystem re-enable pending), KJ Command Center (empire-wide dashboard Lovable app — all 6 products live metrics, Jim monitoring hub, planned build), KJ Autonomous (8-agent autonomous empire system — n8n + Vapi + Claude API, weeks 2-6 build plan), IAMStillHere (grief/memorial product — physical products pen-plotter handwriting laser engraved timelines canvas portraits sound wave jewelry memory boxes, ~250 home studio, referral via funeral homes hospices estate attorneys), DTF (Direct To Film printing business), DTG (Direct To Garment printing business)

---

## BUILD STATE

**Card:** KJLE BUILD_STATE 2026-05-10 (Session 2B closed)
**Saved:** 2026-05-11T01:21:09.193664

# KJLE Build State — Session 2B (Truelist Bulk Batches) — CLOSED 2026-05-10

## What shipped (commits)
- b8b877d feat(email-clean): Truelist bulk batches architecture (Session 2B)
- be3c39b fix(email-clean): NULL-semantics bug in select_uncleaned_leads
- 0076569 fix(email-clean): paginate select_uncleaned_leads past PostgREST 1000-row cap
- 14 repos updated via brain_sync for new status-banner rule
- KJ_RULEZ.md commit 031a0c3 on kj-bridgedeck/main

## Architecture changes
- New migration: migrations/2026_05_10_truelist_batches.sql (audit table + email_sub_state + email_truelist_batch_id + 2 admin_settings keys)
- Rewrite api/routes/enrichment_email_clean.py: parse_truelist_state (handles ok/invalid/risky/unknown + email_* prefixed variants), is_campaign_eligible (positive whitelist only), select_uncleaned_leads (HOT>WARM>COLD priority with .range() pagination), submit_batch (POST /api/v1/batches + audit), ingest_batch_result (idempotent cursor + id-range UPDATE), 4 new endpoints
- New api/routes/webhooks_truelist.py: receiver shim at /webhooks/truelist/batch-complete?secret=... ready for Truelist dashboard webhook
- scheduler.py: replaced v1 verify_inline loop with batch submitter; new job_email_clean_poll_batches cron every 30 min
- 41-fixture boot check at scripts/test_email_clean_parser.py covering parser vocab, prefixed forms, defensive defaults, positive-whitelist campaign eligibility, CSV ingestion, reordered headers

## Batch-1 verification (LIVE)
- batch_id: 9292028e-f0e3-401a-ac5e-390e92838f9a
- 4000 KJLE leads submitted, 3907 unique emails after Truelist dedup
- Truelist completed in ~95 min wallclock
- Auto-ingest counts: valid=2795, invalid=76, unknown=1129, error=0, no_csv_match=0
- CSV reconciliation: 1:1 with ingest (CSV 2722 ok + 74 invalid + 1111 risky/unknown = 3907 unique; ingest 2795 + 76 + 1129 = 4000 = 3907 + 93 duplicates ✓)
- Per-lead spot-check: 7/7 verified leads matched expected mapping AND scoped to batch-1
- Sub-state preservation working: email_ok 2250, is_role 816, accept_all 326, failed_mx_check 42, failed_no_mailbox 19, unknown 547

## Live segment + email state (post-batch-1)
- total: 560,057 (was 556,830, +3,227 new from CSV import last night)
- hot: 20,596 / warm: 129,524 / cold: 406,710 / unclassified: 3,227
- by_email_status: valid 70,559 / invalid 76 (was 0!) / unknown 17,576 / error 3,490 / pending_batch 18,352

## Bugs found and fixed mid-session
- NULL-semantics bug: select_uncleaned_leads filtered .neq('email_status','pending_batch') which excludes NULL email_status rows in PostgREST. Switched to .is_('email_truelist_batch_id','null'). Same gotcha family as pain Session 1.
- 1000-row response cap: .limit(25000) capped at 1000 per segment so first batch only marked 4000 leads. Switched to .range()-based pagination per segment, drains 1000 at a time.
- Parser bug from v1: checked state=='bad' but Truelist returns 'invalid'. Fixed in parse_truelist_state; verified live by first-ever nonzero invalid count.

## Investigation surprises (worth remembering)
- Truelist's POST /batches accepts but silently ignores webhook_url field. Webhook is dashboard-side config only. Receiver shim built, awaiting dashboard wire-up.
- verify_inline returns inconsistent prefixed/unprefixed state strings (email_invalid vs risky). Parser handles both defensively now.

## Open anomalies for Session 2C
- 2786 orphan leads with email_status='pending_batch' and no parent batch_id. Likely leftovers from earlier aborted submits. Cleanup: flush back to NULL or sweep into next batch.
- 17576 'unknown' leads from v1 verify_inline include hidden invalids (the v1 parser miscategorized state=='invalid' as unknown). Re-verify via new architecture to recover them.
- Truelist dashboard webhook not yet configured. 30-min poller is currently the only completion path. Latency 0-30 min instead of seconds.

## Session 2C scope
1. Configure Truelist dashboard webhook → /webhooks/truelist/batch-complete?secret=... (5 min Truelist UI work)
2. Re-verify the 17576 v1-unknown leads via batch architecture
3. Cleanup 2786 orphan pending_batch rows
4. Watch first full weekend of 4-batches/night nightly cron — expect backlog clear in ~5 days at 100K/day
5. Build Card v9 update reflecting Truelist-v2-as-canonical state

## Pinned items (carry-over)
- Send to DemoBoosterz from Lead Finder (after tour rebuild)
- Schedule local backups for Documents/GitHub folder
- Rotate exposed ReachInbox + DemoEnginez Supabase service role keys on Render


---

## EMPIRE-WIDE RULES (excerpt)

1. **Brain Endpoint Verification** — always hit `/health` then the real
   endpoint with `x-brain-key` header BEFORE coding against it. Document
   actual response shape. No assumptions from convention.

2. **Empire Cost Logging** — any LLM call must be instrumented via
   `kje-cost-logger` per `docs/EMPIRE_COST_LOGGING_BUILD_CARD.md`.

3. **Env Var Automation** — CC never asks Jim to manually click env vars
   into a dashboard. Use Render / Railway / Cloudflare APIs. Tokens live
   in CC env (`RENDER_API_KEY`, `RAILWAY_TOKEN`, `CF_API_TOKEN`).

4. **Gotcha Logging** — log any bug / workaround to Brain via
   `POST /memory` with tags `["kjle", "gotcha", "lesson"]` the
   moment context is fresh.

5. **Session Start / End** — every CC session begins with
   `brain_session_start(focus="...", product="kjle")` and ends
   with `brain_session_end(...)` + `brain_save_card(...)`.

---

## VAULT KEYS AVAILABLE FOR THIS PROJECT

Use `GET /vault/kjle/<KEY>/reveal` with header
`x-brain-key: jim-brain-kje-2026-kingjames` to fetch real values.

| Key | Masked | Service |
|---|---|---|
| `SUPABASE_URL` | `` | render |
| `API_SECRET_KEY` | `` | render |
| `BRIDGEDECK_URL` | `` | render |
| `PYTHON_VERSION` | `` | render |
| `RESEND_API_KEY` | `` | render |
| `TRUELIST_API_KEY` | `` | render |
| `ANTHROPIC_API_KEY` | `` | render |
| `FIRECRAWL_API_KEY` | `` | render |
| `SEARCHBUG_API_KEY` | `` | render |
| `OUTSCRAPER_API_KEY` | `` | render |
| `REACHINBOX_API_KEY` | `` | render |
| `SUPABASE_SERVICE_KEY` | `` | render |
| `BRIDGEDECK_INGEST_KEY` | `` | render |
| `DEMOENGINEZ_SUPABASE_KEY` | `` | render |
| `DEMOENGINEZ_SUPABASE_URL` | `` | render |
| `REACHINBOX_WEBHOOK_SECRET` | `` | render |

Empire-wide shared keys (always available):

- `GITHUB_PAT_VPS` — VPS automation PAT (contents:write)
- `SUPABASE-PAT-SHARED` — Supabase DDL automation token
- `SUPABASE_PERSONAL_ACCESS_TOKEN` — Supabase PAT (44+ chars)

---

## SESSION END PROTOCOL

Before closing the chat, run:

```
POST /memory   tags=["kjle", "session_end"]
               content="<what shipped, what's next>"
POST /log      tags=["kjle", "session_complete"]
               content="<one-liner>"
POST /cards    title="<Project> BUILD_STATE <date>"
               project="kjle"
               content="<full markdown spec>"
```

If anything broke, log a gotcha memory FIRST so the next session inherits
the lesson.

---

*Synced from Brain state at 2026-05-11 20:00:16 UTC.*
*This file is auto-regenerated every 4h. Manual edits will be overwritten
on the next heal if the rebuilt content differs by >20% of lines.*
