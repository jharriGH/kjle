# KJLE Phase 4 — Layer 3 Design

**Status:** Approved by Jim 2026-05-28. Slice 3A implementation lands in branch
`phase4-slice-3A-review`. Slices 3B / 3C deferred to follow-on branches.

**Authors:** Jim Harris (vision), Claude Code (drafted from Jim's notes 2026-05-28).

---

## 1. Context

Phase 4 Layer 2 (Slices 2A–2B) shipped the per-consumer DNC observability layer
and the soft-TTL + background-refresh cache. The DNC pipeline is now:

1. Phone normalization → E.164
2. Whitelist short-circuit
3. Internal suppression check
4. Cache lookup (fresh / stale-served-with-background-refresh / hard-expired)
5. Budget guard
6. Searchbug provider call

Layer 3 adds **dedicated litigator + carrier-pattern + toll-free protections**
on top of that pipeline. These are categories the Searchbug provider can't
catch reliably (or at all):

- **TCPA litigators** — phones owned by known plaintiffs / serial filers.
  Treated as DNC regardless of consent, regardless of whether Searchbug knows
  about them. "Never contact under any circumstance."
- **Carrier patterns** — e.g. NPA-555 reserved blocks, NXX-0000 invalid blocks,
  and other static unreachable ranges that don't need a provider lookup.
- **Toll-free NPAs** — 800/822/833/844/855/866/877/888. KJLE is a cold-outbound
  product; we never legitimately dial toll-free.

This document covers all three Layer 3 components but only Section 3 (Slice 3A,
TCPA litigators) is being built in this branch.

---

## 2. Goals

1. **Pre-Searchbug pre-checks for hard categorical blocks.** TCPA litigators,
   carrier-reserved ranges, and toll-free NPAs should all halt the pipeline
   before paying for a Searchbug lookup *or* trusting a stale cache row.
2. **Litigator status overrides cache.** If a phone enters the TCPA litigator
   list after we cached a "clean" result, we must not serve the stale clean
   result. The cleanest way is to pre-check the litigator list before the cache.
3. **Vendor abstraction.** The TCPA list vendor landscape is unstable
   (see Section 3.7) — wrap the import in a small abstract base so the
   factory can swap vendors via `admin_settings`, identical pattern to
   `dnc_provider.py`.
4. **Dormant-safe ship.** Slice 3A ships fully wired but with empty env vars by
   default. No crash, no exception, no false positives until Jim subscribes to
   a vendor and populates `TCPA_LIST_API_KEY` / `TCPA_LIST_CSV_URL`.
5. **Weekly delta visibility.** A list refresh that silently adds 30 numbers is
   useful but invisible. Emit a weekly digest so Jim sees the trend.

---

## 3. Slice 3A — TCPA litigator list mirror + pre-check + weekly refresh

This is the slice landed in `phase4-slice-3A-review`.

### 3.1 Schema

New table `tcpa_litigators`:

```sql
CREATE TABLE tcpa_litigators (
  phone text PRIMARY KEY,
  added_at timestamptz DEFAULT now(),
  source text,
  name text,
  state text,
  case_count int,
  last_refreshed_at timestamptz,
  metadata jsonb DEFAULT '{}'::jsonb
);

CREATE INDEX tcpa_litigators_added_at_idx
  ON tcpa_litigators (added_at DESC);
```

PK is `phone` (E.164). Other columns optional — vendor schemas vary. Whatever
the vendor exports gets dumped into `metadata` so we don't lose provenance.

Three new rows in `admin_settings`:

- `tcpa_list_provider` (default `'tcpalitigatorlist'`)
- `tcpa_list_refresh_cadence` (default `'weekly'`)
- `tcpa_list_last_refresh_at` (default `''`)

### 3.2 Provider abstraction — `api/lib/tcpa_provider.py`

Mirror of `api/lib/dnc_provider.py`:

```
TCPAProvider (ABC)
  └── async fetch_latest_list() -> list[dict]
TCPALitigatorListProvider(TCPAProvider)
  └── reads TCPA_LIST_API_KEY + TCPA_LIST_CSV_URL from env
  └── empty env -> returns [] + WARNING log (dormant-safe)
  └── present env -> downloads CSV via httpx, parses, normalizes phones via
                     phone_utils.normalize_phone, drops invalid rows, returns
                     list[dict]{phone, name, state, case_count, metadata}
get_active_tcpa_provider() -> reads admin_settings.tcpa_list_provider, returns
                              the matching impl. Only TCPALitigatorListProvider
                              registered as of Slice 3A.
```

The factory pattern (not a singleton) lets Slice 3 swap vendors without code
edits to the call sites. If Jim picks StopLitigators.com or TCPA Black List
instead, add one new class + one registry entry.

### 3.3 Lookup helper — `api/lib/tcpa_check.py`

Pure DB read, no Searchbug, no cache:

```python
async def is_tcpa_litigator(phone_e164: str, db) -> dict:
    """
    Returns:
      {
        "is_litigator": bool,
        "matched_row": dict | None,
      }
    """
```

PK lookup against `tcpa_litigators`. Fast (indexed), free, no network.

### 3.4 Integration into `/dnc/check`

New order in `api/routes/dnc.py::_perform_check`:

1. Phone normalization (existing)
2. Whitelist short-circuit (existing)
3. Internal suppression check (existing)
4. **NEW — TCPA litigator check** → returns `result_source='tcpa_litigator_match'`,
   `is_dnc=True`, `tcpa_litigator=True`, audit row written with `cost_usd=0` and
   `metadata={litigator_row}`. **Bypasses cache** — cache invalidation when the
   list updates would be messy; pre-check is the cleaner cut.
5. Cache check (Slice 2B soft-TTL flow, existing)
6. Budget guard + Searchbug provider (existing)

Response schema gains the `tcpa_litigator: bool` field on every response.
Existing consumers ignore unknown fields, so this is backwards-compatible.

### 3.5 Weekly refresh job

`api/routes/scheduler.py` gains `job_tcpa_refresh_weekly`:

- **Cadence:** Sunday 04:00 UTC (1 hr after the daily Stage 4 cron at 03:00).
- **Flow:**
  1. Snapshot current phones: `SELECT phone FROM tcpa_litigators ORDER BY phone`.
  2. `get_active_tcpa_provider().fetch_latest_list()`.
  3. UPSERT rows into `tcpa_litigators` (preserve `added_at`, update
     `last_refreshed_at` + `metadata`).
  4. Compute delta `added = new − old`, `removed = old − new`.
  5. `admin_settings.tcpa_list_last_refresh_at = now()`.
  6. Send weekly digest (Section 3.6).
  7. Log via `_log_job` so it shows in `/scheduler/status`.

**Empty-list safety net:**

- Empty `→` Empty: log "no-op (list not yet provisioned)", success.
- NonEmpty `→` Empty: log WARNING, send "TCPA refresh returned empty — investigate"
  email, do NOT delete existing rows, skip digest.

### 3.6 Weekly delta digest

Plain text, terse. Same recipient as daily report
(`admin_settings.daily_cost_report_email`).

Subject: `KJLE TCPA list refresh — YYYY-MM-DD — +N added, -M removed`

Body lists added (with name + state if available) and removed phones, plus a
one-line "notable" pattern noting which state had the most additions. If both
added and removed are zero, body collapses to a one-liner
`TCPA list refresh: no changes. Total list size: N.`

### 3.7 Vendor research (2026-05-28)

Web check before build:

- `tcpalitigatorlist.com` operating. **Pricing has materially changed** from
  ~$99–150/yr (original spec assumption) to **$2,029/yr Basic / $2,870–$6,711/yr
  API tiers**. Site is healthy, API + CSV scrub still offered.
- Alternatives at lower tiers:
  - **StopLitigators.com** — free crowdsourced TCPA litigator DB. Data freshness
    / completeness vs paid services unknown. Plausible second choice.
  - **TCPA Black List (tcpablacklist.com)** — pay-as-you-go pricing tiers.
  - **TextP2P** — $0.01/phone, $5 min. Per-scrub, not list mirror; not a great
    fit for the local-mirror architecture in Slice 3A.

**Decision:** ship Slice 3A dormant-safe. The factory accepts any future vendor
class via `admin_settings.tcpa_list_provider`. Jim picks the vendor + price tier
after slice ships. No code changes required at switch time beyond a new provider
class + one registry entry.

### 3.8 `/dnc/stats` extension

Three new fields:

- `tcpa_litigator_list_size` — `COUNT(*) FROM tcpa_litigators`
- `tcpa_litigator_matches_24h` — `dnc_audit_log` rows with
  `result='tcpa_litigator_match'` in last 24h
- `tcpa_litigator_last_refreshed_at` — `admin_settings.tcpa_list_last_refresh_at`
  (null if never refreshed)

### 3.9 Daily report extension

New sub-section under DNC HEALTH in `api/lib/daily_report.py`:

```
TCPA LITIGATOR PROTECTION
-----
List size:                247 numbers
Matches blocked (24h):    0
Last refreshed:           2026-05-26 04:00 UTC (3 days ago)
-----
```

If list size is zero (not yet provisioned), block collapses to:

```
TCPA LITIGATOR PROTECTION
-----
Status: not provisioned (no subscription)
-----
```

---

## 4. Slice 3B (deferred) — Carrier-pattern + toll-free pre-checks

Not in this branch. Sketched here for continuity.

### 4.1 Reserved 555 numbers

The only assignable 555 numbers are **555-0100 through 555-0199** (reserved for
fictional/test use in entertainment/media). Everything else in 555-XXXX is
unallocated. Slice 3B will block all 555 NXX numbers with one regex; the
0100–0199 block is documented in the source comment for posterity (it doesn't
change the rule — the whole NXX-555 space is unsafe to dial).

### 4.2 Toll-free NPA list

Full list: `800, 822, 833, 844, 855, 866, 877, 888`. KJLE is a cold-outbound
B2B / B2C product; we never legitimately call toll-free. Hard block at the
pre-check layer.

### 4.3 Integration point

Sits between TCPA litigator check (Slice 3A step 4) and cache check (step 5).
A regex match in either component returns `result_source='carrier_pattern_block'`
or `'tollfree_block'` with `is_dnc=True`, `cost_usd=0`.

---

## 5. Slice 3C (deferred) — DNC list expiration / list-sync ops

Slice 3C will add:

- Automated diff alerts when the TCPA list shrinks more than 20% week-over-week
  (signal of vendor outage / scraping break).
- Per-NPA litigator density rollup on the daily report.
- An ops endpoint to re-snapshot the list on demand.

Out of scope for 3A and 3B.

---

## 6. Files touched (Slice 3A only)

New:

- `docs/PHASE_4_LAYER_3_DESIGN.md` (this file)
- `migrations/tcpa_litigators.sql`
- `api/lib/tcpa_provider.py`
- `api/lib/tcpa_check.py`

Edited:

- `api/routes/dnc.py` (Step 4 pre-check, response schema, `/dnc/stats` fields)
- `api/routes/scheduler.py` (refresh job + digest)
- `api/lib/daily_report.py` (TCPA LITIGATOR PROTECTION block)

Protected — must not be edited in this slice:

- `api/lib/carrier_lookup.py`, `api/lib/phone_utils.py`, `api/lib/dnc_check.py`,
  `api/lib/dnc_batch.py`, `api/lib/dnc_provider.py`, `api/lib/searchbug_provider.py`,
  `api/lib/cache_refresh.py`, `api/routes/dnc_webhooks.py`, `api/routes/dnc_channel.py`,
  `api/routes/local_scraper_ingest.py`, all Slice 1A–2B migrations.

---

## 7. Acceptance — Slice 3A

- `py_compile` clean across `api/`.
- App boots under dummy env (no `TCPA_LIST_*` set).
- `/dnc/check` on a random clean phone returns existing behavior + new
  `tcpa_litigator: false` field.
- Inserting a test row into `tcpa_litigators` and re-checking returns
  `result_source='tcpa_litigator_match'`, `is_dnc=true`, `tcpa_litigator=true`,
  and writes an audit row with `result='tcpa_litigator_match'`, `cost_usd=0`.
- Manual trigger of `tcpa_refresh_weekly` with empty env vars logs "not
  provisioned" and exits success.
- Manual trigger of `daily_cost_report` shows the new TCPA LITIGATOR
  PROTECTION block (in "not provisioned" state initially).
