## Empire onboarding -- read every session
Single source of truth: jharriGH/kjle. On session start, fetch + follow:
- https://raw.githubusercontent.com/jharriGH/kjle/main/EMPIRE_SC_HANDOFF.md
- https://raw.githubusercontent.com/jharriGH/kjle/main/EMPIRE_INTEGRATION_STANDARD.md
Keep current at this repo root:
1. ROADMAP.md -- valid YAML front-matter (project, status, description, last_updated, repo,
   api_url, facts_doc, vault_key, integrates_with). The empire dashboard reads this; bump
   last_updated on any change.
2. PROJECT_FACTS.md -- integration contract (endpoints, schema, auth), verified against the
   live system. Vault key NAMES only, never secret values.
To learn about another project: brain_search for "<project> integration" or read EMPIRE_INDEX.md
in jharriGH/kjle -> that project's PROJECT_FACTS.md -> brain_vault_search for its key.
Repo edits: this repo only, explicit file paths only.

## 👑 Empire onboarding — read every session
Single source of truth: this repo (jharriGH/kjle). On session start, follow EMPIRE_SC_HANDOFF.md
and EMPIRE_INTEGRATION_STANDARD.md at the repo root.
KJLE's roadmap file is KJ_EMPIRE_ROADMAP.md (managed by the hands-free pipeline via
scripts/submit_roadmap_update.sh) — keep its front-matter current. Keep PROJECT_FACTS.md current.
Vault key NAMES only — never secret values. Repo edits: explicit paths, never `git add -A`.


# 🧠 KJLE — CLAUDE.md
# Auto-healed by claude_md_healer.py from Jim Brain state
# Last healed: 2026-05-12 00:00:25 UTC
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
KJLE n8n Phase 1 (bounced_hard) READY TO DEPLOY 2026-05-10. Workflow JSON drafted at kjle_n8n_phase1_bounced.json. DEPLOY SEQUENCE: (1) Import workflow JSON into n8n + set env vars REACHINBOX_WEBHOOK_SECRET / KJLE_API_BASE / KJLE_API_KEY. (2) Manual webhook fire test — verify email lands in email_suppressions with source=n8n_reachinbox_bounce; verify 401 on bad/missing secret; verify 200+processed=false on soft bounce. (3) Dispatch CC session to: (a) add parallel-run tap in dnc_webhooks.py — fire-and-forget async via asyncio.create_task, no-retry, no-block, ~6 lines; (b) fix API_SECRET_KEY hardcoded fallback to None in dnc_webhooks.py L71 + campaigns.py L37 so missing env fails loudly. (4) Deploy + observe 24-48h parallel-run; compare email_suppressions writes between KJLE direct path and n8n path — drift must be zero. (5) Cut over by updating ReachInbox dashboard webhook URL for the bounced event subscription to the n8n URL. Punch list (non-Phase-1): map_lead_to_ri firstName bug (reachinbox.py L170-172) must fix before Session 2 first campaign launch; multi-account RI deferred until DemoBoosterz rebuild; missing 2 of 8 events resolved at Phase 2 by inspecting RI dashboard event dropdown. Brain tags for retrieval: kjle_n8n_architecture_v1, kjle_n8n_code_review, kjle_n8n_phase_1_workflow, kjle_n8n_phase_1_decisions.

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

**Card:** KJE Orchestrator BUILD_STATE 2026-05-11
**Saved:** 2026-05-11T20:14:22.905640

# KJE Orchestrator — BUILD_STATE 2026-05-11

**Status:** LIVE
**URL:** https://kje-orchestrator.onrender.com
**Render Service:** srv-d813bjvavr4c73b223b0
**Repo:** https://github.com/jharriGH/kje-orchestrator
**Build SHA:** cee25b8799de (P3 v1.0.0)
**Plan:** starter ($7/mo)
**Region:** oregon

## Verified endpoints
| Endpoint | Result |
|----------|--------|
| `GET /health` | HTTP 200 `{"status":"ok","version":"1.0.0"}` |
| `GET /version` | HTTP 200 (build, poll_interval, stall_timeout) |
| `GET /status` | HTTP 500 — pending `kjcodedeck.wave_manifest` table creation |
| `POST /trigger-poll` | guarded by `x-trigger-key` header |

## Files shipped (11)
1. `main.py` — FastAPI app, lifespan boots BrainClient + WaveEngine + Poller
2. `poller.py` — APScheduler 60s tick, calls `wave.process_logs(logs)`
3. `wave_engine.py` — wave_manifest reader, complete/blocked/stalled detection, VPS dispatch
4. `notify.py` — retry+backoff wrappers around `/notify`, `/memory`, `/log`
5. `brain_client.py` — httpx async client with lowercase `x-brain-key`
6. `requirements.txt` — fastapi 0.115.5, supabase 2.9.1, apscheduler 3.10.4, etc.
7. `Procfile` — `web: uvicorn main:app --host 0.0.0.0 --port $PORT`
8. `render.yaml` — service blueprint w/ `sync: false` secrets
9. `Dockerfile` — python:3.11-slim, non-root user, healthcheck
10. `.env.example` — every env var documented
11. `README.md` — full runbook + schema + ops guide
   (+ `.python-version` and `runtime.txt` pinning 3.11.10 after Render's default of 3.14 broke first deploy)

## Wave-chaining logic
- Poll Brain `/logs?limit=50` every 60s (configurable).
- Group entries by `tags + job_id`. Match against active wave's `jobs[]`.
- **Complete:** every job logged `task_complete` → mark wave `done`, chain next queued.
- **Blocked:** any job tagged `blocker`/`fatal`/`halt` → mark wave `blocked`, SMS + memory.
- **Stalled:** >30 min without job-tagged log entries → mark `stalled`, SMS.
- **Chain:** lowest-priority queued wave promoted to `active`, each job POSTed to VPS_DISPATCH_URL with X-Dispatch-Key.

## Env vars on Render (14)
PYTHON_VERSION=3.11.10, BRAIN_URL, BRAIN_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY,
VPS_DISPATCH_URL=http://192.161.173.97:8091/dispatch, VPS_DISPATCH_KEY (64-char),
TRIGGER_KEY (auto-generated, stored on VPS at /tmp/p3_orch_trigger.env),
POLL_INTERVAL=60, POLL_LOG_LIMIT=50, STALL_TIMEOUT=1800, DISPATCH_TIMEOUT=20,
PROJECT_SLUG=kje_orchestrator, LOG_LEVEL=INFO.

## Gotchas captured
- **Render Python default:** first deploy used Python 3.14.3 (latest) and crashed — must pin via `.python-version` + `PYTHON_VERSION` env var + `runtime.txt`.
- **Render envVars via POST /v1/services:** the envVars array inside serviceDetails was silently dropped on create. Fix: `PUT /v1/services/{id}/env-vars` separately, then trigger redeploy.
- **Brain has no POST /projects:** new projects must be registered through the Brain UI; API exposes only GET/PATCH. `PATCH /projects` for kje_orchestrator currently 404s until Jim adds it.
- **/status 500:** `kjcodedeck.wave_manifest` table does not yet exist in Supabase. Create it before promoting orchestrator to active duty (DDL in README.md).

## Decisions
- Starter plan, oregon, autoDeploy=yes — same shape as kjle/jim-brain.
- 30-min stall timeout (configurable via env).
- Trigger key generated via `secrets.token_urlsafe(40)`, not stored in repo.
- Force-push to main per spec; competing P3v3 build script also pushed (commit 71aff4b7) — currently running container is still `cee25b87` build until a later redeploy promotes the newer commit.

## Next actions
1. Register `kje_orchestrator` project in Brain UI so PATCH /projects works.
2. Run DDL to create `kjcodedeck.wave_manifest` table.
3. Insert first test wave (one harmless job) and watch `/status` cycle through queued → active → done.
4. Tune `STALL_TIMEOUT` after observing first 24h of throughput.

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

*Synced from Brain state at 2026-05-12 00:00:25 UTC.*
*This file is auto-regenerated every 4h. Manual edits will be overwritten
on the next heal if the rebuilt content differs by >20% of lines.*

<!-- KJE-ONBOARD-V1 -->
## KJ Empire — SC Onboarding
This repo belongs to the KJ Empire (DevelopingRiches Inc, owner Jim Harris / jharriGH).
- Central repo: jharriGH/kjle. Brain: https://jim-brain-production.up.railway.app
- New SC seats: run brain_status, brain_search this repo's slug, and verify live state before declaring anything done.
- Decide-and-proceed. Cost-gate chargeable dispatches. Never echo secrets — pull keys from the Brain vault.
- See ROADMAP.md for status.
<!-- /KJE-ONBOARD-V1 -->
