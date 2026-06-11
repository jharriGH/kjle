# 👑 EMPIRE SC HANDOFF — Roadmap Automation + Cross-SC Sharing

**Owner:** Jim Harris / DevelopingRiches Inc.
**Issued:** 2026-06-10

This is the single handoff to give every project SC. It tells each seat how to
(1) be discoverable empire-wide, (2) find out about any other project without asking
Jim, and (3) — for the kjle seat — publish roadmap updates hands-free.

---

## A. WHAT'S NOW POSSIBLE (the new capabilities)

### Roadmap automation (live on `jharriGH/kjle`)
1. **Hands-free roadmap publishing.** An SC edits `KJ_EMPIRE_ROADMAP.md`, runs ONE
   script, and it auto-merges to `main` — no PR clicks, no Jim in the loop.
2. **Self-regenerating HTML.** `KJ_EMPIRE_ROADMAP.html` is rebuilt automatically on
   every roadmap change (CI does it; nobody hand-edits the HTML).
3. **Safety-gated.** Only the two roadmap files auto-merge. Any PR that also touches
   code (`api/`, anything else) is HELD for human review. A validator blocks a bad
   front-matter (missing/blank `project:` key) before it can merge.
4. **No wasteful redeploys.** A Render Ignored-Paths filter stops roadmap/doc/workflow
   merges from rebuilding `kjle-api` or the `kjle` worker.

### Global dashboard (auto-aggregating)
5. **One glanceable empire view** that auto-pulls every project's `ROADMAP.md`
   front-matter — a project's card appears automatically once its front-matter is in
   place. (URL in section D.)

### Cross-SC sharing (the standard — capability live, content fills in as seats onboard)
6. **Any SC can self-serve any project's integration facts** — URL, endpoints, schema,
   auth — without asking Jim.
7. **Central index** (`EMPIRE_INDEX.md` in `jharriGH/kjle`): the phone book of every
   project → repo → facts doc → vault key.
8. **Every project becomes self-describing** via its `ROADMAP.md` discovery front-matter.
9. **Credential self-service**: docs carry vault key *names* only; any SC resolves the
   value via `brain_vault_search`. Nobody asks Jim for keys.

---

## B. THE UNIVERSAL SC HANDOFF  ← paste this into EVERY project's SC, once

```
EMPIRE ONBOARDING — make your project discoverable + learn to find others (one-time).

PART 1 — PUBLISH YOUR PROJECT (so every other SC + the global dashboard can see you):

1. VERIFY GROUND TRUTH first — your live URL, the endpoints other apps call, their
   request/response shapes, the tables/payloads other apps touch, your auth header —
   against the ACTUAL running system, not memory. Mark anything unconfirmed UNVERIFIED.

2. CREATE PROJECT_FACTS.md at your repo root (template: EMPIRE_INTEGRATION_STANDARD.md).
   Include: what it does, auth (vault key NAME only — never the value), cross-app
   endpoints, data shapes, integration points, gotchas. Set "Last verified" = today.

3. ADD these discovery keys to your ROADMAP.md YAML front-matter (this is also what puts
   your card on the global dashboard):
     project: <ProjectName>      # REQUIRED, non-blank
     repo: jharriGH/<repo>
     status: active|building|parked|launched
     api_url: https://...        # or N/A
     facts_doc: PROJECT_FACTS.md
     vault_key: <VAULT_KEY_NAME>
     integrates_with: [ ... ]
     last_updated: YYYY-MM-DD

4. COMMIT to YOUR repo only, explicit paths, never `git add -A`. Use an isolated
   worktree off origin/main if your repo shares a working tree with other seats.

5. LOG ONE BRAIN POINTER (not the whole doc — Brain shreds long prose):
     brain_memory(
       content="<ProjectName> integration contract + facts live in PROJECT_FACTS.md in
         jharriGH/<repo>. Vault key: <VAULT_KEY_NAME>. Status: <status>.",
       tags=["<projectslug>", "integration_contract", "facts_doc"])

6. APPEND your row to EMPIRE_INDEX.md in jharriGH/kjle
   (Project | Repo | Live URL | Facts doc | Vault key NAME | Status). If you can't reach
   that repo, report your row to Jim.

PART 2 — HOW TO FIND OUT ABOUT ANY OTHER PROJECT (no asking Jim):
   a. brain_search("<other-project> integration")  -> returns the pointer, OR read
      EMPIRE_INDEX.md in jharriGH/kjle to find its repo + facts doc.
   b. Read that repo's PROJECT_FACTS.md (git show / raw fetch with the empire PAT).
   c. Need its API key? brain_vault_search("<project> API key") -> masked value +
      reveal_url. Pull internally; never echo the value in chat.

HARD RULES:
- Your repo only. Never modify another project's files or shared tables.
- Secret VALUES never appear anywhere — vault key NAMES only.
- Verify before you assert. UNVERIFIED beats wrong.
- Report what you published (repo, commit, Brain pointer) when done.
```

---

## C. ROADMAP-UPDATE HANDOFF  ← for the kjle SC now; for other SCs only after the pipeline is replicated into their repo

```
PUBLISH A ROADMAP UPDATE (hands-free, kjle repo):
1. Isolated worktree off origin/main:
     git -C /opt/kjle fetch origin
     git -C /opt/kjle worktree add /tmp/roadmap-edit origin/main && cd /tmp/roadmap-edit
2. Edit ONLY KJ_EMPIRE_ROADMAP.md (keep the front-matter; project: must stay non-blank).
3. Submit:  scripts/submit_roadmap_update.sh "one-line summary"
   -> validates, branches, commits only the .md, pushes, opens a PR, auto-merges on green.
   CI regenerates KJ_EMPIRE_ROADMAP.html. Never hand-edit the .html. Never bundle code.
4. Clean up:  cd / && git -C /opt/kjle worktree remove /tmp/roadmap-edit
```

---

## D. THE GLOBAL FILE TO WATCH (hands-free)

- **Global empire view:** `https://jharrigh.github.io/empire-dashboard`
  Auto-aggregates every project's `ROADMAP.md` front-matter (VPS cron + GitHub webhook).
  A project shows up automatically once it has the `project:` front-matter key (Part 1,
  step 3). It fills out as each SC onboards.
- **KJLE roadmap (per-repo):** `KJ_EMPIRE_ROADMAP.html` in `jharriGH/kjle` — auto-regenerated
  on every roadmap change.

---

## E. JIM'S ACTIONS — just one

**Fan out Section B (and the EMPIRE_INTEGRATION_STANDARD.md template) into each project's
SC seat, once.** Start with the active seats: KJLE, EmpireSenderz, ReviewBombz, Telehealth,
DemoEnginez, DemoBoosterz, SiteEnginez, KJ Command Deck, kje-orchestrator, and any others.
After that, the SCs do the rest and it's self-sustaining. Everything else — the kjle roadmap
lane, the HTML regen, the Render filter, the dashboard aggregation — is already automatic.

## F. OPTIONAL (not required)

- Replicate the hands-free roadmap lane (3 files + 1 CODEOWNERS line) into other repos if
  you want non-kjle SCs to publish *their* roadmaps hands-free too. Small one-time setup per
  repo; until then those SCs update their roadmaps the normal way (their dashboard card still
  works as long as the front-matter is present).

---
*Empire SC Handoff — DevelopingRiches, Inc. — Jim Harris*
