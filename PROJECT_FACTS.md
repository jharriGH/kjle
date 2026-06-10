# KJLE — PROJECT FACTS

**Repo:** jharriGH/kjle
**Live URL:** https://kjle-api.onrender.com
**Status:** active
**Owner SC:** KJLE SC
**Last verified:** 2026-06-10

## What it does
Master lead database + Lead Finder backend, and the empire's DNC source of truth.
Owns lead scraping/classification, DNC/TCPA compliance, and ReachInbox campaign I/O.

## Auth
- Header: `x-api-key`
- Vault key NAME: `API_SECRET_KEY`   (resolve via brain_vault_search; never echo value)

## Endpoints other apps call
| Method + path | Purpose | Auth |
|---|---|---|
| GET /kjle/v1/dnc/check/{phone} | DNC/TCPA/line-type check (scrub-on-call) | public |
| GET /kjle/v1/dnc/check-email/{email} | email suppression check | public |
| POST /kjle/v1/dnc/add | add a suppression / opt-out | x-api-key |
| GET /kjle/v1/reachinbox/accounts | live mailbox capacity for routing | x-api-key |
| POST /kjle/v1/reachinbox/campaigns/create | create campaign (account_ids: List[int]) | x-api-key |
| GET /kjle/v1/leads | paginated/filtered leads (returns `total`) | x-api-key |
| GET /kjle/v1/leads/eligible-for-campaign | campaign-ready leads | x-api-key — **UNVERIFIED: being built; confirm path + shape before relying** |
| POST /kjle/v1/scrape/start | queue a scrape job | worker key |
| POST /kjle/v1/push/demoenginez/batch | push leads to DemoEnginez | x-api-key |

## Integration points
- **DNC:** every empire app calls `/dnc/check/{phone}` before contacting a number. TH is wired in.
- **EmpireSenderz:** pulls `/reachinbox/accounts` for routing; all campaign create/launch/pause go through `/reachinbox/*`.
- **DemoEnginez:** receives leads via `/push/demoenginez/batch`.

## Data
Supabase `dhzpwobfihrprlcxqjbq` — KJLE owns leads / segments / DNC / campaign tables.

## Gotchas
- `account_ids` is `List[int]` from `/reachinbox/accounts` (round-trip contract).
- FIRECRAWL_API_KEY must carry the `fc-` prefix.
- Scheduler runs in a separate Render Background Worker (kjle-scheduler, RUN_SCHEDULER-gated); the API process runs no jobs — flapping fixed 2026-06-10.
