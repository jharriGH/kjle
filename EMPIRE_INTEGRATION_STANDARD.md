# 👑 KJ EMPIRE — INTEGRATION STANDARD v1

**Purpose:** any SC seat can find out everything it needs about any other empire
project — URL, endpoints, schema, auth, integration points — **without asking Jim
and without guessing.** This standard makes the Brain + GitHub you already have
actually carry usable cross-project information.

The problem it fixes: Brain shreds long prose into ~10-word fragments on retrieval,
so detail doesn't survive there. The fix is a fixed division of labor:

- **Substance lives in a repo markdown file** (survives intact): `PROJECT_FACTS.md`.
- **Discovery lives in two light, shred-proof places:** a short Brain pointer, and
  the project's `ROADMAP.md` YAML front-matter.
- **A central index** (`EMPIRE_INDEX.md`, hosted in `jharriGH/kjle`) is the phone book.

---

## 1. Every project repo carries `PROJECT_FACTS.md`

One file at the repo root. The integration contract. Template:

```markdown
# <ProjectName> — PROJECT FACTS

**Repo:** jharriGH/<repo>
**Live URL:** https://<service>.onrender.com   (or N/A)
**Status:** active | building | parked | launched
**Owner SC:** <seat name/label>
**Last verified:** YYYY-MM-DD   (date you confirmed this against the live system)

## What it does
2–4 sentences. Plain language. What problem it solves, what it produces/consumes.

## Auth
- API key header: `x-api-key` (or Bearer, etc.)
- Vault key NAME (never the value): `<VAULT_KEY_NAME>`   ← look up via brain_vault_search

## Endpoints other apps call
| Method + path | Purpose | Request shape | Response shape |
|---|---|---|---|
| GET /v1/... | ... | ... | ... |
(Only the endpoints meant for cross-app use. Internal-only routes can be omitted.)

## Data shapes / tables other apps touch
Table or payload schemas a consuming app needs. Field names + types.

## Integration points
"To send X to this project, call ... ." "This project expects ... from KJLE."
The explicit hand-shake other SCs will wire against.

## Gotchas
Anything that bit us (Cloudflare UA blocks, payload-shape traps, rate limits, etc.).
```

**Rule:** write it from **ground truth** — verify against the live system / actual repo
before asserting a route or field. Do not transcribe from memory or old docs. If you
can't verify something, mark it `UNVERIFIED` rather than stating it as fact.

**Rule:** never put a secret VALUE in this file. Only the vault key NAME.

---

## 2. Every project's `ROADMAP.md` front-matter carries discovery keys

The empire dashboard already parses `ROADMAP.md` front-matter. Extend it so the same
block doubles as machine-readable discovery. Required keys:

```yaml
---
project: <ProjectName>          # REQUIRED, non-blank (dashboard breaks silently without it)
repo: jharriGH/<repo>           # REQUIRED — where the facts doc lives
status: active                  # active | building | parked | launched
api_url: https://...            # or N/A
facts_doc: PROJECT_FACTS.md     # REQUIRED — path to the contract in this repo
vault_key: <VAULT_KEY_NAME>     # the key name (NOT value) for this project's API
integrates_with:                # other empire projects this one talks to
  - KJLE
  - EmpireSenderz
last_updated: YYYY-MM-DD
---
```

---

## 3. Brain pointer — one line per project (shred-proof)

After publishing `PROJECT_FACTS.md`, log exactly one pointer so `brain_search` surfaces it:

```
brain_memory(
  content="<ProjectName> integration contract + facts live in PROJECT_FACTS.md in jharriGH/<repo>. Vault key: <VAULT_KEY_NAME>. Status: <status>.",
  tags=["<projectslug>", "integration_contract", "facts_doc"]
)
```

Brain holds the *pointer + status* (short facts survive shredding); the repo file holds
the *substance*. Never rely on Brain alone for the detail.

---

## 4. Central index — `EMPIRE_INDEX.md` (the phone book)

Hosted in `jharriGH/kjle` (the data hub; also a registered dispatch slug). One row per
project. Either hand-maintained or auto-generated from every repo's ROADMAP front-matter.

```markdown
| Project | Repo | Live URL | Facts doc | Vault key | Status |
|---|---|---|---|---|---|
| KJLE | jharriGH/kjle | https://kjle-api.onrender.com | PROJECT_FACTS.md | API_SECRET_KEY | active |
| EmpireSenderz | jharriGH/kjle-sender | https://kjle-sender.onrender.com | PROJECT_FACTS.md | ... | active |
| ReviewBombz | jharriGH/reviewbombz-monitor | ... | PROJECT_FACTS.md | ... | launched |
| ... | ... | ... | ... | ... | ... |
```

---

## 5. CONSUMER RECIPE — how any SC finds out about any project

Example: **TH SC needs info about ReviewBombz.**

1. Start at the index: read `EMPIRE_INDEX.md` in `jharriGH/kjle` → find the ReviewBombz row
   → get its repo + facts-doc path + vault key name.
   (Or skip to Brain: `brain_search("reviewbombz integration")` → returns the pointer.)
2. Read the contract: fetch `PROJECT_FACTS.md` from `jharriGH/reviewbombz-monitor`
   (git show / GitHub raw with Jim's PAT — every SC operates with the PAT; never ask Jim).
3. Need the API key? `brain_vault_search("ReviewBombz API key")` → masked value + reveal_url.
   Pull internally; never echo the value.

Done — full integration knowledge, zero questions to Jim.

---

## Credential rule (non-negotiable, empire-wide)
- Secret **values** never appear in any facts doc, index, Brain memory, or chat.
- Only ever record the **vault key name**; consumers resolve it via `brain_vault_search`.

*KJ Empire Integration Standard v1 — DevelopingRiches, Inc. — Jim Harris*
