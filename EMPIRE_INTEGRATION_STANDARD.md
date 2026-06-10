# 👑 KJ EMPIRE — INTEGRATION STANDARD
Last updated: 2026-06-09

How any KJ Empire project publishes its integration contract, and how any SC
discovers and consumes another project's contract from one starting point.
Every integrable project follows this.

## 1. The three artifacts every integrable project ships
1. `PROJECT_FACTS.md` (repo root) — the contract: identity, auth, endpoints, boundary.
2. A row in `EMPIRE_INDEX.md` (in jharriGH/kjle) — the discovery entry.
3. Four discovery keys in the project's roadmap front-matter — machine-readable pointers.

## 2. PROJECT_FACTS.md format
Required sections, in order:
- **Header:** `# 👑 <PROJECT> — PROJECT FACTS` + `Last verified: YYYY-MM-DD`
- **Identity block (bullets):** one-liner, repo, live API base, auth header,
  vault key NAME, DB, integrates_with, boundary (who owns what I/O).
- **Endpoints table:** `| Endpoint | Auth | Status |`
  - Status vocabulary: `✅ LIVE — <note>`, `⚠️ UNVERIFIED — <why; do not integrate until confirmed>`, `⏸️ planned`.
  - Every endpoint is verified against the live API before commit. A 500/absent
    route is NEVER listed as LIVE — it carries ⚠️ until the owning seat clears it.
- **Domain notes:** provider, cost model, caching, rate limits, round-trip
  contracts (e.g. `account_ids: List[int]` — state int-vs-string explicitly).

Rules: reference credentials by vault key NAME only, never the value. Bump
`Last verified` on every change.

## 3. EMPIRE_INDEX.md row convention
One row per project; each SC appends its own during onboarding. Fixed columns:

`| Project | Repo | Live URL | Facts doc | Vault key | Status |`

- `Facts doc` = path within that repo (usually `PROJECT_FACTS.md`).
- `Vault key` = the NAME, resolved via `brain_vault_search`.
- `Status` = `active` | `wip` | `parked`.
- Append only — never rewrite another project's row.

## 4. Roadmap front-matter discovery keys
Every project's `*_ROADMAP.md` front-matter carries these 4, additive to existing keys:

repo: <owner>/<repo>
api_url: <live base URL>
facts_doc: PROJECT_FACTS.md
vault_key: <VAULT_KEY_NAME>

Additive only — never drop existing keys.

## 5. How an SC discovers a project's integration contract
1. Open `EMPIRE_INDEX.md` (jharriGH/kjle). Find the target project's row.
2. From the row: get Repo, Facts doc, Vault key name.
3. Open `<Repo>/<Facts doc>` → endpoints, auth header, boundary, ⚠️ flags.
4. Resolve the credential by NAME via `brain_vault_search` → use `reveal_url`
   internally; never echo the value to chat, logs, or files.
5. Call `api_url` with the stated auth header.
6. Honor ⚠️ UNVERIFIED flags — do not integrate against a flagged endpoint
   until the owning seat clears it.
7. Stay within the declared boundary — don't duplicate another project's owned I/O.

## 6. Maintenance
- Owning SC updates PROJECT_FACTS.md on any endpoint/auth/boundary change; bumps Last verified.
- Keep the EMPIRE_INDEX.md row and the 4 front-matter keys in sync with the facts doc.
- Verify against the live API before committing any status change.
