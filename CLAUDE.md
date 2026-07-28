# 🎯 KJLE — CLAUDE.md
# Managed by brain_sync.py (Brain sections)
# + Manual additions (never auto-updated)
# Last synced: July 27, 2026 09:57 PM PST

---

# ═══════════════════════════════════════════════════════════
#  KING JAMES EMPIRE — SC / CC OPERATING INSTRUCTIONS
#  Read first, every session. This is the single source of truth.
# ═══════════════════════════════════════════════════════════

## IDENTITY
I am Jim Harris, King James Empire, Long Beach CA. DevelopingRiches Inc (C-Corp). Solo founder.
Standard: GOAT — production-ready, no half-measures.
Doctrine: decide-and-proceed, but VERIFY LIVE — never trust a self-report.

# ───────────────────────────────────────────────────────────
# 0. EXECUTION & COST — HIGHEST AUTHORITY (overrides everything below)
# ───────────────────────────────────────────────────────────
Before ANY build task, STATE these three, then STOP for Jim's explicit "go":
1. Cheapest path. DEFAULT = a terminal-ready prompt Jim runs on his Max plan (~$0 marginal).
   Only name run_build_task if a local terminal genuinely can't do it (must run
   unattended/overnight, or on the VPS) — and say WHY.
2. Model + one-line reason. Default Sonnet; Opus only if earned.
3. Rough cost: ~$0 (Jim's terminal) vs ~$2-3 (metered dispatch).
NEVER fire run_build_task without Jim's explicit "go."

COST REALITY:
- run_build_task / kje-cc-dispatch = METERED API key (~$2-3 each, hourly invoices).
- Jim's local terminals = Max subscription, flat fee = the cheap path. Default to it.
- FREE tools (use automatically, no permission needed): brain_status, brain_search,
  brain_get_project, brain_vault_search, brain_log, brain_memory, vps_exec, curl, /logs.
- ONLY run_build_task costs money and needs Jim's go. Diagnostics are free — use freely.

# ───────────────────────────────────────────────────────────
# 1. MODEL TIERING
# ───────────────────────────────────────────────────────────
- Haiku 4.5  — trivial/mechanical (lookups, simple edits)
- Sonnet 4.6 — DEFAULT for real build work
- Opus 4.8   — only when the task earns it (hard reasoning/architecture). Sparingly.
- Dispatcher default model = claude-sonnet-4-6. NEVER spawn `claude -p` without an explicit
  --model AND --max-turns (an env-var default alone is not enough — pin it in the command).
- Prompt caching is automatic in Claude Code — keep context stable to maximize it.

# ───────────────────────────────────────────────────────────
# 2. GIT SAFETY
# ───────────────────────────────────────────────────────────
- Normal push only: `git push origin main`. `git push --force` is BANNED empire-wide.
  A push-rejected error is the safety net — never override it.
- Resolve conflicts by KEEPING BOTH SIDES (never --ours, which drops other SCs' work).
  CLAUDE.md is brain_sync-auto-generated, so --theirs is safe for THAT file only.
- Jim sees the exact unified diff before any file change is approved.

# ───────────────────────────────────────────────────────────
# 3. CREDENTIALS
# ───────────────────────────────────────────────────────────
- The vault holds most keys: brain_vault_search or GET /vault/{project}. Use them first.
- If a key is genuinely missing, or a NEW secret must be created (e.g. issuing a fresh API
  key), ASK JIM. Never guess, never fabricate, never paste a placeholder.

# ───────────────────────────────────────────────────────────
# 4. TERMINAL PROMPT SKELETON (what to hand Jim)
# ───────────────────────────────────────────────────────────
Fill this in and hand Jim the COMPLETED version — never leave <...> placeholders in a
prompt a CC actually receives:

Model: Sonnet (Haiku if trivial / Opus if flagged)
Repo + exact files: <repo + specific file paths>
Task: <one specific, bounded task>
Rules: minimal diff · don't explore beyond the named files · stop when done
Report: unified diff + one-line summary

# ───────────────────────────────────────────────────────────
# 5. BRAIN TOOLS — which when (+ gotchas)
# ───────────────────────────────────────────────────────────
- brain_memory → SEARCHABLE (mem0/Qdrant). Decisions, state, milestones, gotchas.
  Retry on 500; do NOT fall back to /log.
- brain_log → AUDIT ONLY (Supabase empire_logs), not searchable. Read via /logs?limit=N.
- brain_search → RECENCY-BLIND (ranks relevance, not freshness). Check created_at/age_days.
  <0.45 = noise; 0.65+ = real. ~30-min embedding lag — not for confirming recent work.
- Real-time "what just happened" → GET /logs?limit=N, never brain_search.
- Vault existence checks → GET /vault/{project}; brain_vault_search is loose-ranked.
- mem0 dedup can NOOP silently (success:true but nothing embedded) — for critical state,
  also write a deterministic record (build card / next_action), don't rely on search alone.
- "Tool not found" on a brain_* tool = stale MCP session after a kje-mcp (Render) restart →
  reconnect / fresh session, then retry.
- CRITICAL WRITES: For state that MUST be findable later (decisions, gotchas, session summaries), write via POST /memory/raw - it embeds deterministically into Qdrant. Plain POST /memory can silently dedup (returns added:false) and lose the write. Always verify the /memory/raw response shows embedded:true plus an id.
- UNREGISTERED PROJECT: If PATCH /projects returns 404 for a slug that has memories, the project is missing from the empire_state.projects registry. Run register_missing.py in the jim-brain repo (it mirrors brain_register read-append-insert). Do NOT hand-write empire_state.

# ───────────────────────────────────────────────────────────
# 6. DEPLOY-AND-VERIFY
# ───────────────────────────────────────────────────────────
- Detect a live deploy by polling a NEWLY-ADDED endpoint (200 = new code, 404 = old) —
  NOT /health 200 (it serves the old container during a 7-15 min build).
- Never combine push + verify in one dispatch. Never add auto-rollback timers.

# ───────────────────────────────────────────────────────────
# 7. SPEED & PARALLELISM (Jim's default preference)
# ───────────────────────────────────────────────────────────
- Move as FAST as safely possible — bias toward shipping. PROACTIVELY offer to split
  independent work into separate prompts Jim runs in parallel local terminals (Max plan =
  flat fee, so parallel = faster at ~$0 extra). Offer the parallel breakdown by default.
- Parallelize across DIFFERENT repos/files only — never parallel sessions on the same files
  (merge conflicts). If work is genuinely sequential, say so.
- "When safe" is the gate: speed never skips verify-live, diff review, git safety, or the
  cost recommendation. Fast AND safe — not fast INSTEAD of safe.

# ───────────────────────────────────────────────────────────
# SESSION START — load context automatically (free, read-only)
# ───────────────────────────────────────────────────────────
On every new chat, load empire context (no permission needed — these are free reads):
1. brain_status  (cheap pulse)
2. GET /context
3. GET /projects
4. GET /cards
5. GET /logs?limit=10
Header for all REST calls: x-brain-key: jim-brain-kje-2026-kingjames
Then confirm: "Jim Brain loaded. I can see [X] projects, [X] cards, and [X] memories.
Ready to build. What are we working on?"

# ───────────────────────────────────────────────────────────
# BRAIN — connection reference
# ───────────────────────────────────────────────────────────
Brain URL: https://jim-brain-production.up.railway.app   (current version: v1.9.0)
Key:       jim-brain-kje-2026-kingjames
MCP:       https://kje-mcp.onrender.com/mcp/T24NM1Sxbh7txJs-unNIjblaXMqA1OZW6gNU-Ud5Yjk/
VPS:       192.161.173.97 (Ubuntu 24.04)   |   Notify: POST /notify (channel: sms|email|both)
GitHub:    jharriGH   |   Local repos: C:\Users\Jim\Documents\GitHub\

Tools available: brain_status, brain_search, brain_get_project, brain_vault_search,
brain_log, brain_memory, run_build_task (METERED — Jim's go required).

Two logging endpoints:
- POST /log    → instant (<1s), audit only, NOT searchable. Automated/frequent events.
- POST /memory → semantic (60-90s), searchable. Decisions, milestones, lessons.

# ───────────────────────────────────────────────────────────
# TECH STACK (never suggest third-party alternatives like GHL/Zapier)
# ───────────────────────────────────────────────────────────
Frontend: Lovable | Backend: Python/FastAPI | DB: Supabase (PostgreSQL)
API hosting: Render | Workers/crons: Railway | Storage: Cloudflare R2 | CDN: Cloudflare Pages
AI: Claude API (claude-sonnet-4-6 default) | Memory: Jim Brain (mem0 + Qdrant)
Orchestration: n8n on Railway | Voice: Vapi.ai + AVA v6.4.0 | RVM: VoiceDropz
Email: ReachInbox + Instantly | LinkedIn: HeyReach | SMS: Twilio (866-621-7044 A2P)
Payments: Stripe

EMPIRE PRODUCTS: KJWidgetz | KJLE | Jim Brain | DemoBoosterz | DemoEnginez | VoiceDropz |
KJ SalesAgentz | UnhideLocal | SiteEnginez | ReviewBombz | KJ Autonomous |
KJ CodeDeck/BridgeDeck | IAMStillHere | AgentEnginez | TestEnginez | DaycareMarketerz |
FinanceIQ | IASY

# ───────────────────────────────────────────────────────────
# CODING RULES — ALWAYS
# ───────────────────────────────────────────────────────────
- Complete files only, no snippets or placeholders.
- Specify exact repo, Supabase project, service on every command.
- `git push origin main`  (NORMAL push — never --force; see §2).
- service_role key, never anon key.
- supabase.schema('name') not constructor chaining.
- .maybeSingle() not .single().
- await + try/catch on Supabase queries.
- Run curl from the RackNerd VPS, not Windows CMD.
- /log for fast logging; /memory for semantic decisions.
- PATCH /projects for project updates; PATCH /state for single metric updates.

# NEVER DO:
- Suggest GHL, Zapier, or stack alternatives.
- Use placeholders or PASTE_ID_HERE.
- Produce snippets — always complete files.
- Modify existing n8n workflows (new workflows only).
- Single quotes or em dashes in Brain content (use double quotes and hyphens).
- POST /state to update arrays (use the PATCH endpoints).

# ───────────────────────────────────────────────────────────
# SESSION END RULE
# ───────────────────────────────────────────────────────────
Every session ends with all three:
1. POST /memory — full session summary (BUILT / DECISIONS / BUGS FIXED / GOTCHAS / NEXT),
   tags ["[project]", "session_end"].
2. PATCH /projects — update next_action.
3. POST /cards — save updated BUILD_STATE card.
Confirm each succeeded, then give Jim a plain-English summary.

# GOTCHA LOGGING RULE:
Any bug, workaround, or lesson learned → log immediately via POST /memory (brain_memory),
tags ["[project]", "gotcha", "lesson"].

# ───────────────────────────────────────────────────────────
# BRAIN SYNC + ROADMAP (Brain-derived — do NOT hand-edit)
# ───────────────────────────────────────────────────────────
brain_sync.py runs daily at 7am, regenerating every repo's CLAUDE.md AND ROADMAP.md from Brain.
Manual run: python brain_sync.py  (location: C:\Users\Jim\Documents\GitHub\)
- The operating rules in this document live in the brain_sync CLAUDE.md template — edit them
  THERE, not per-repo.
- ROADMAP.md is RENDERED FROM BRAIN (each project's next_action + latest BUILD_STATE card +
  recent session summaries). Do NOT hand-edit per-repo ROADMAP.md — multiple writers are what
  caused the CLAUDE.md/ROADMAP.md merge conflicts we already hit. The roadmap updates itself
  because session-end already writes next_action + card + summary to Brain; brain_sync renders
  the file. One writer, no conflicts, always current.

# ───────────────────────────────────────────────────────────
# MANDATORY FOOTER — end EVERY response with this block
# ───────────────────────────────────────────────────────────
📁 [PROJECT] · [TAG] · [DIR]
🎯 Task: [TASK]
📍 Now: [what you are doing this response]
🕐 Started [STARTED]

To fill PROJECT/TAG/DIR/STARTED: read the context file the cc launcher wrote at kickoff:
  PowerShell: Get-Content "$env:TEMP\cc_context_<TAG>.txt"
where <TAG> is your session tag (e.g. TEST-9). Lines: TAG=, TASK=, PROJECT=, DIR=, STARTED=.
Use TASK for the Task line. Fill Now with your current action.
If the file is not found, fall back to working directory + repo name; note "context file not found" once.

# ───────────────────────────────────────────────────────────
# TEMPLATE — UNIVERSAL SESSION ENDER (paste to a CC to close out)
# ───────────────────────────────────────────────────────────
Session complete. Do all three now and confirm each:
1. POST /memory — SESSION SUMMARY [project]: BUILT / DECISIONS / BUGS FIXED / GOTCHAS / NEXT
   tags ["[project]", "session_end"]
2. PATCH /projects — next_action = [most important next task]  (match_by: id)
3. POST /cards — title "[PROJECT] BUILD_STATE [date]", project "[project]", content [markdown]
Then give a plain-English summary of what we accomplished.

---

## CURRENT STATUS
<!-- BRAIN-SYNC:START:STATUS -->
*Brain sync: July 27, 2026 09:57 PM PST*

**Status:** ACTIVE
**Description:** Lead empire backend — 32/32 done, 28,849 leads ready
**Next Action:** ⚡ Clarify project goals and context for kjle before resuming work.
<!-- BRAIN-SYNC:END:STATUS -->

---

## EMPIRE STATE & COSTS
<!-- BRAIN-SYNC:START:EMPIRE_STATE -->
- Clients: 0
- MRR: $0.00
- HOT leads: 0
- Last decision: None

**AI Costs:**
- Today: $0.0093
- This month: $0.0093
- All time: $0.0093

**Empire:**
- 4 live | 2 launch ready | 23 in progress
<!-- BRAIN-SYNC:END:EMPIRE_STATE -->

---

## RECENT KJLE MEMORIES
<!-- BRAIN-SYNC:START:MEMORIES -->
1. KJLE prioritized as easiest
2. Status of KJLE is active
3. kjle docs: f76a186 + a2b10fc
4. KJLE UI QA on 2026-06-17
5. KJ Command Center is the KJLE Lead Finder
6. KJLE's origin URL was overwritten and restored to https://github.com/jharriGH/kjle.git
7. Canonical GitHub origin for KJLE is https://github.com/jharriGH/kjle
8. Completed KJLE build session on April 20, 2026
<!-- BRAIN-SYNC:END:MEMORIES -->

---

## BUILD STATE
<!-- BRAIN-SYNC:START:BUILD_STATE -->
**Card:** KJLE SESSION 2026-07-24
**Saved:** 2026-07-24

# KJLE Session Hand-off
**Date:** 2026-07-24
**Status:** completed
**Session ID:** kjle_slice19_contact_hygiene_8c5a7793

## Summary
CC session 'kjle_slice19_contact_hygiene_8c5a7793' on kjle completed (rc=0, 575s). Prompt: MODEL: Run this task on SONNET, not Opus. (Standing cost rule.)

TASK: KJLE Slice 19 — add contact-data hygiene to the campaign-prep engine so cold-email batches never contain cross-contaminated or mismatched contact rows. A real 50-lead pull surfaced: the same email mapped to multiple different com

## Files Touched
- api/worker_scheduler.py
- api/routes/campaign_prep.py
- api/routes/scheduler.py

<!-- BRAIN-SYNC:END:BUILD_STATE -->

---

## MANUAL ADDITIONS
<!-- brain_sync.py never modifies below this line -->

---

## EMPIRE STATE & COSTS

---

## RECENT KJLE MEMORIES

---

## BUILD STATE


---

## FIRST THING — DO THIS AUTOMATICALLY

```
brain_session_start(focus="[today's task]", product="kjle")
brain_search(query='kjle')
brain_list_cards()   # find build card
brain_get_card(id)   # load full spec
# THEN ask Jim what to tackle
```

**Do not wait to be asked. Always do this first.**

---

## SESSION END — DO THIS AUTOMATICALLY

```
brain_session_end(
  product="kjle",
  what_shipped="[what was built]",
  decisions="[key decisions]",
  next_action="[most important next task]"
)
brain_save_card(
  title="KJLE BUILD_STATE [date]",
  project="kjle",
  content="[full build state md]"
)
```

---

*Synced: July 27, 2026 09:57 PM PST*
*Refresh: `python brain_sync.py kjle`*