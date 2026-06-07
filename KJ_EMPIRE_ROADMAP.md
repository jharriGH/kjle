---
project: KJLE
status: active
description: Lead engine — scraping, classification, DNC compliance, RI campaign creation. Data hub for the empire.
last_updated: 2026-06-06
integrates_with:
  - EmpireSenderz
  - TH
  - DemoEnginez
  - DemoBoosterz
  - Local Scraper workers
sc_contact: ""
current_sprint: "Phase 5 autonomous lead pumping"
sprint_target_date: 2026-06-09
cost_spent: 20.92
cost_remaining: 4.08
notes: "All 3 RI bugs fixed and ES SC verified end-to-end. Phase 4.2 backend complete. Empire dashboard shipped. Awaiting LS license for first end-to-end scrape test."
---

# 👑 KJ EMPIRE — CENTRALIZED ROADMAP

**Owner:** Jim Harris, DevelopingRiches Inc
**Last refresh:** 2026-06-04 (Day 2 PM close)
**Sprint goal:** Autonomous lead pumping live by **Monday 2026-06-09**
**Source of truth:** This file + `KJ_EMPIRE_ROADMAP.html`
**KJLE main HEAD:** `d42b54a` (worker daemon merge)

---

## 🖼️ VISUAL DASHBOARD — KJ_EMPIRE_ROADMAP.html

This roadmap has a companion file: `KJ_EMPIRE_ROADMAP.html`. Same data, visual format. Save both in `Documents/GitHub/kjle/` and open the HTML in any browser for a glanceable status check.

**Update discipline — non-negotiable:**

1. **At session start** — Claude reads BOTH this markdown AND the HTML before doing anything else
2. **After EVERY successful task** — Claude updates BOTH files inline, before moving to the next item
3. **At session end** — Final refresh of BOTH files, deliver updated versions to Jim via outputs folder
4. **If only one updates and the other doesn't** — task is NOT considered complete; Jim should call it out

---

## 💰 COST ANNOTATION KEY

Every task in this roadmap shows an estimated dispatch cost. Three categories:

| Marker | Meaning | When you'll see it |
|---|---|---|
| **$0** | Tier 0/1 — free (vps_exec or GitHub web UI edit) | Most diagnostic + small-edit work |
| **✓ $X** | Calibrated estimate — anchored to a real dispatch we've already run | Most code-shipping tasks |
| **? $X** | Guessed estimate — no real-world data point yet | New territory (n8n, etc.) |

**Calibration baseline (real Day 1+2 dispatch costs):**
- L5B bulk update patch (single endpoint refactor + migration): **$1.50**
- DNC Cost endpoints (4 read-only endpoints): **$1.51**
- ES bug hotfix (2 bugs + migration + rollback): **~$2.50**
- Phase 4.2 scrape queue (6 endpoints + migration + 2 auth helpers): **$2.91**
- Worker daemon (7 files, ~30KB code, 2 platforms): **~$2.50**
- **Mean: ~$2.30. Median: ~$2.50.** Two-dispatch days have totaled $5-6.

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

## 📅 SPRINT CALENDAR (Mon 6/2 → Mon 6/9)

| Day | Theme | Status | Cost |
|---|---|---|---|
| Day 1 (Mon 6/2) | Phase 4 close + ES bug hotfix | 🟢 DONE | $5.51 |
| Day 2 AM (Tue 6/3) | Connection pool fix | 🟢 DONE | $0 |
| Day 2 PM (Tue 6/3) | Phase 4.2 backend (scrape queue + worker daemon) | 🟢 DONE | $5.41 |
| **Day 3 (Wed 6/4 → Thu 6/5)** | **3 RI bug fixes (Bug 1.1, 2.1, 3) — all verified E2E + LS install pending license** | 🟢 **RI complete** | **~$3.00** |
| Day 4 (Thu 6/5) | n8n workflow build — 1 niche end-to-end | ⚪ scoped | ~$2-4 |
| Day 5 (Fri 6/6) | Multi-niche + scraper trigger logic | ⚪ scoped | ~$2-3 |
| Day 6-7 (Sat-Sun 6/7-6/8) | Stress test + monitoring | ⚪ scoped | ~$2-5 |
| Day 8 (Mon 6/9) | **AUTONOMOUS LEAD PUMPING LIVE** | 🎯 target | ~$0-1 |

**Sprint spent so far:** $10.92
**Sprint budget remaining:** $9-14 (target total: $15-25)

---

## ✅ COMPLETED — KJLE PHASE 4 (closed 2026-06-03)

| Layer | Item | Status | Cost |
|---|---|---|---|
| L1 | Core ingest + DNC restructure | 🟢 | prior |
| L2 | Cache optimization (Slice 2A audit + 2B soft TTL) | 🟢 | prior |
| L3 | Protective filters (TCPA + Searchbug harvest + leadcrap + carrier-pattern) | 🟢 | prior |
| L4 — Tab 1 | Find Leads UI shipped | 🟢 | prior |
| L4 — Tab 2 | Campaign Push UI shipped | 🟢 | prior |
| L4 — Tab 3 | Cost Command Center UI shipped | 🟢 | prior |
| L4 — Tab 4 | DNC Center UI shipped + 4 endpoints | 🟢 | ✓ $1.51 |
| L5 | Bulk DNC scrub: 596,764 leads classified, 1,760 flagged (100% coverage) | 🟢 | prior |
| L5B | Bulk-update endpoint patch (50s → 2s per chunk) | 🟢 | ✓ $1.50 |
| Hotfix | ES Bug 1 fix (set-accounts emails translation) | 🟢 | ✓ $2.50 (combined w/ Bug 2) |
| Hotfix | ES Bug 2 fix (lead-fetch + composite index + orphan rollback) | 🟢 | (above) |
| Infra | Render kjle-api: Starter → Standard ($25/mo) | 🟢 | $0 |
| Infra | Supabase compute: Micro → Small (~$5/mo net) | 🟢 | $0 |
| Day 2 AM | Connection pool fix in database.py | 🟢 | $0 (T1 GitHub edit) |

**Phase 4 total dispatch cost: $5.51**

---

## ✅ COMPLETED — DAY 2 PM (closed 2026-06-04)

| Item | Status | Commit | Cost |
|---|---|---|---|
| `/scrape/start` + queue endpoints + `scrape_jobs` table | 🟢 | `ee3f774` | ✓ $2.91 |
| Bug 1 diagnostic logging | 🟢 | `cb1f49b` | $0 (T1) |
| Worker daemon code (workers/local_scraper_daemon/) | 🟢 | `d42b54a` | ✓ $2.50 |
| WORKER_API_KEY in Render env | 🟢 | (Jim) | $0 |
| scrape_jobs migration applied | 🟢 | (Jim) | $0 |

**Day 2 PM total dispatch cost: $5.41**

---

## 🟡 IN PROGRESS — DAY 3 (Wed 6/4)

| Item | Owner | Effort | Cost |
|---|---|---|---|
| LS license email arrival | Support team | unknown | $0 |
| Install Local Scraper on Z820 | Jim | ~30 min | $0 |
| Install Local Scraper on VPS (Wine setup likely) | Jim + Claude SSH walk | ~30-60 min | $0 |
| Set env vars on each worker | Jim | ~10 min | $0 |
| Deploy daemon on Z820 (Task Scheduler) | Jim + Claude | ~20 min | $0 |
| Deploy daemon on VPS (systemd) | Jim + Claude | ~15 min | $0 |
| First end-to-end test (queue a small scrape, watch leads arrive) | Claude | ~30 min | $0 |
| Wire Find Leads "Start Scrape" button → `/scrape/start` (Lovable prompt) | Jim (paste) | ~10 min | $0 |

**Day 3 total estimated cost: ~$0-1** (mostly install + Tier 0/1 work)

---

## 🔵 QUEUED — Phase 5 (Day 4-7)

| Item | Owner | Effort | Cost | Conf. |
|---|---|---|---|---|
| Build `GET /kjle/v1/leads/eligible-for-campaign` helper endpoint | Claude (CC dispatch) | ~1 hr | ✓ $1-2 | High |
| n8n workflow: 1 niche end-to-end (route → create → attach) | Jim + Claude | ~1 day | ? $0-1 | Med (n8n is mostly config) |
| Test campaign actually creates in RI manually first | Jim | ~30 min | $0 | High |
| Schedule the cron only after manual success | Jim | ~5 min | $0 | High |
| Wire 2-3 more niches into n8n | Jim + Claude | ~1 day | ? $0-1 | Med |
| Add lead inventory monitor (triggers scraper if pool < N) | Claude (CC dispatch) | ~2 hr | ✓ $2-3 | Med-High |
| Add monitoring/alerting for autonomous failures | Claude (CC dispatch) | unclear | ? $2-5 | Low (scope unclear) |
| Sat-Sun stress test fixes (buffer for anything that breaks) | Claude | varies | ? $0-3 | Low (buffer) |

**Phase 5 total estimated cost: ~$6-15** (depending on how much n8n + monitoring scope grows)

---

## ⏸️ PARKED — Post-week-1

| Item | Notes | Future cost |
|---|---|---|
| Phase 6 reply handling | Replies sit in RI inbox week 1 | ? $5-10 |
| AVA voice handoff | Week 2+ | ? $5-10 |
| Calendar booking integration | Week 2+ | ? $3-5 |
| Landing pages per product | Open-ended polish | Open-ended |
| Pricing pages | Open-ended polish | Open-ended |
| Conversion tracking | Open-ended | ? $2-3 |
| Promote roadmap to Lovable tab in Lead Finder | Post-launch upgrade | ? $3-5 |
| Build card v9 refresh | After autonomy lives | $0 |

---

## 🤝 EXTERNAL DEPENDENCIES — SC COORDINATION

### EmpireSenderz SC

| Item | Status | Due | Cost impact if delayed |
|---|---|---|---|
| Sprint brief delivered | 🟢 | — | — |
| ES bugs fixed at `1bbdeb0` | 🟢 | — | — |
| Bug 1 diagnostic logging at `cb1f49b` | 🟢 | — | — |
| ES SC re-runs Soft-Test A with diagnostic logs | 🟡 Awaiting | This week | +$1-3 if Bug 2.1 hotfix needed |
| ES SC answers to 4 questions | 🟡 Awaiting | EOD Wed | Slips n8n by 1 day if late |

### DemoBoosterz / DemoEnginez SCs

| Item | Status | Cost when started |
|---|---|---|
| Coordination briefs drafted | ⚪ Not started | $0 (Claude writes inline) |
| Decision: include in week-1 sprint or defer? | ❌ Jim | Adds $3-5 if pulled into week 1 |

---

## 🏗️ ARCHITECTURE STATUS

| Component | URL/Location | Status |
|---|---|---|
| KJLE API | https://kjle-api.onrender.com | 🟢 Live, ~200ms response |
| KJLE Lead Finder UI | https://kjleadzempire.com | 🟢 Live, 4 tabs |
| KJLE Supabase | dhzpwobfihrprlcxqjbq | 🟢 Small compute tier |
| EmpireSenderz API | https://kjle-sender.onrender.com | 🟢 Live, CR.3 routing endpoints |
| Jim Brain | https://jim-brain-production.up.railway.app | 🟢 Live (intermittent 500s on memory writes — fall back to brain_log) |
| Brain MCP | https://kje-mcp.onrender.com | 🟢 Live |
| RackNerd VPS | 192.161.173.97 | 🟢 CC dispatcher on 8091 |
| CSV uploader cron | `/opt/kjle_csv_uploader.py` (10pm nightly) | 🟢 Live |
| Worker daemon code | `/opt/kjle/workers/local_scraper_daemon/` | 🟢 Shipped, awaiting deployment |
| Local Scraper (Z820) | Z820 desktop | 🟡 Awaiting license |
| Local Scraper (VPS) | RackNerd VPS | 🟡 Awaiting license #2 + Wine |
| n8n autonomous orchestrator | Railway | ⚪ Not built yet |

---

## ⚠️ KNOWN RISKS / WATCH ITEMS (with cost impact)

| Risk | Likelihood | Cost impact |
|---|---|---|
| LS license email delayed | Medium | $0 — blocks progress, not budget |
| LS on Linux VPS needs Wine | Medium | $0 — install time, not dispatch |
| n8n learning curve eats Day 4-5 | Medium | +$2-3 if extra scaffolding dispatches needed |
| ES Bug 1 root cause is RI-side / structural | Medium | +$1-3 if a Bug 2.1 hotfix needed |
| Some untested endpoint breaks at scale | Medium-High | +$2-5 for hotfix dispatches |
| Scope creep — "just add X" | High | +$3-5 per off-roadmap dispatch — **this is the biggest budget risk** |
| Brain memory 500s | Low | $0 — fall back to brain_log |

---

## 💰 COST PROTOCOL — 5-TIER FRAMEWORK

| Tier | What | When | Cost |
|---|---|---|---|
| **T0** | vps_exec / terminal / browser dev tools | Diagnostics, verification, reads | $0 |
| **T1** | GitHub web UI edit | 2-20 line code changes (Jim's preferred manual path) | $0 |
| **T2** | Claude writes patch, Jim applies | Small file changes Jim prefers to apply | $0 |
| **T3** | Single combined CC dispatch (build+verify+merge) | Real architectural slices | $1.50-$3 typical |
| **T4** | Multiple sequential dispatches | Rare, requires explicit Jim approval | $5+ |

**5 absolute rules:**
1. Never dispatch CC just to run curl
2. Never dispatch CC just to verify truth
3. Never dispatch CC for drain/batch/loop work — use vps_exec
4. Never split chaperone + merge into separate dispatches
5. When uncertain, **ASK Jim before spending**

State verified cost BEFORE any dispatch. Wait for explicit Y/N.

---

## 💰 SPRINT COST TRACKING (real numbers)

| Item | Status | Cost |
|---|---|---|
| Phase 4 Layer 5B bulk update patch | 🟢 paid | $1.50 |
| Phase 4 DNC Cost endpoints | 🟢 paid | $1.51 |
| Phase 4 ES bug hotfix | 🟢 paid | ~$2.50 |
| Day 2 AM connection pool fix | 🟢 free (Tier 1) | $0 |
| Day 2 PM scrape queue endpoints | 🟢 paid | $2.91 |
| Day 2 PM worker daemon | 🟢 paid | ~$2.50 |
| Day 2 PM Bug 1 diagnostic logging | 🟢 free (Tier 1) | $0 |
| Day 3 Bug 2.1 logging (rollback path) | 🟢 free (Tier 1) | $0 |
| Day 3 Bug 1.1 fix (set-accounts key) | 🟢 paid | $1.50 |
| Day 3 Bug 3 fix (add-leads endpoint + shape) | 🟢 paid | ~$1.50 |
| **Sprint total so far** | — | **~$13.92** |
| Estimated remaining budget | — | $6-11 |
| Worst-case total | — | ~$25 |
| Recurring infra delta | 🟢 paid | +$23/mo |

---

## 📌 STANDING REMINDERS

- 📌 **Roadmap dual-update discipline** — refresh both .md and .html after every successful task
- 📌 **Cost protocol** — state verified cost BEFORE any T3+ dispatch, get explicit Y/N
- 📌 Build card v9 refresh needed
- 📌 Add/test "Send to DemoBoosterz" from Lead Finder after Tour demo
- 📌 Schedule local backups for `Documents/GitHub`
- 📌 EmpireSenderz: prune 83 stale Instantly rows in mailbox_fleet
- 📌 EmpireSenderz: `reachinbox_ltd_1` empty workspace decision

---


---

## 🏆 DAY 3 RESOLVED — RI CAMPAIGN PATH FULLY UNBLOCKED (2026-06-06)

**Three RI bugs identified, root-caused via official docs, fixed, and verified end-to-end by ES SC.**

| Bug | Description | Resolution | Commit | Verified |
|---|---|---|---|---|
| **Bug 2.1** | Rollback path diagnostic logging missing — couldn't tell if rollback fired on set-accounts failure | Added entry logging + DELETE attempt logging | `f791cce` | Live, hasn't fired yet |
| **Bug 1.1** | set-accounts 500'd on valid payload. RI docs show body key must be `accountsToUse` not `emails` | One-word rename | `140f4b4` | ES SC: 5 telehealth + 1 control assigned cleanly |
| **Bug 3** | add-leads called `/leads/addLeadsToCampaign` (doesn't exist) with firstName nested in attributes. RI docs say endpoint is `/leads/add`, firstName/lastName at top level | Endpoint + shape fix + diagnostic logging | `ad87b1a` | ES SC: 5 added / 0 skipped on re-test |

**Lessons reinforced:**
- RI's own docs (docs.reachinbox.ai) are the authoritative source — not packet captures, not memory
- Root-cause-via-docs THEN ship-via-CC-dispatch is the repeatable pattern
- Bug 1 (int→email translation) was correctly fixed but was masking Bug 1.1; fixing one surfaces the next

**Phase 4.2 backend: COMPLETE.** Queue endpoints + worker daemon + all three RI bugs resolved. The entire campaign creation path (create + assign accounts + add leads) is now programmatically callable end-to-end. This is the spine of Phase 5 autonomous lead pumping.

**Day 3 dispatch cost:** ~$3.00 (Bug 1.1 + Bug 3, mean $1.50 each — calibrated baseline holding).

**Queued next:** Orphan cleanup script for 125659, 125660, 125877, 125878 + SOFTTEST/RERUN/VERIFY pile via DELETE /api/v1/campaigns/{id} ($0, ~5 min).

## 🎯 NEXT IMMEDIATE ACTION

**Awaiting LS license email.** When it arrives, Day 3 work is mostly free (Tier 0/1):
1. Install LS on Z820 (~30 min)
2. Save settings file in LS Tools tab
3. Activate second license, install LS on VPS (Wine setup likely)
4. Set env vars on both workers
5. Run daemon foreground for first test
6. Submit a small test scrape via `POST /kjle/v1/scrape/start`
7. Verify leads arrive in KJLE
8. **Update both roadmap files with each completed step**

**Optional parallel work while waiting** ($0):
- Lovable prompt for Find Leads UI wire (Claude writes, Jim pastes)
- n8n workflow design sketch (no dispatch, just architecture thinking)

---

*Centralized Empire Roadmap v1.2 — DevelopingRiches Inc — Jim Harris*
*With calibrated cost annotations · ✓ = real dispatch data · ? = no calibration yet*
*Updated after every successful task — markdown + HTML kept in sync*
