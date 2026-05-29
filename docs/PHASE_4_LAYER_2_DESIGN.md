# KJLE Phase 4 — Layer 2 Design

**Status:** Draft committed for Slice 2A (cross-product DNC cache audit + per-consumer
observability). Other layers (2B–2E) sketched here for context; their slices will be
broken out separately.

**Authors:** Jim Harris (vision), Claude Code (drafted from Jim's notes 2026-05-28).

**Branch this lands on first:** `phase4-slice-2A-review`.

---

## 1. Context

Phase 4 Layer 1 (Slices 1A–1D) shipped the foundation for KJLE's empire-wide DNC /
phone-validation cache:

- Slice 1A — Searchbug-backed provider + `dnc_cache` + `dnc_suppressions` schema.
- Slice 1B — `verify_api_key`-gated routes for `/dnc/check`, `/dnc/add`,
  `/dnc/scrub-batch`, `/dnc/stats`. Audit logging via `dnc_audit_log`.
- Slice 1C — Local-scraper ingest path, channel-aware DNC, push gating, monthly
  scheduler jobs (federal DNC refresh, NANPA refresh).
- Slice 1D — NANPA classification hardening (v3 carrier heuristic + assigned-to column).

The cache layer is now the single source of phone DNC truth for KJLE. The next layer
extends that role to **the rest of the empire** — every consumer (telehealth, RB,
KJWidgetz, AVA, DemoBoosterz) should hit the same cache so we never pay for the same
phone twice.

---

## 2. Goals

1. **Single source of truth for phone DNC across the empire.** No other empire
   product calls Searchbug directly. All callers go through KJLE's
   `/kjle/v1/dnc/check`.
2. **Per-consumer observability.** When the daily report shows a $7.20 DNC bill,
   Jim should see *who* drove it.
3. **Cost attribution.** Future-proof the ability to bill internal consumers
   (or at minimum understand cost growth per product).
4. **Cache hit-rate visibility per consumer.** A consumer with a 5% hit rate is
   either misusing the cache or has a fundamentally cold dataset — we need to
   see which.

---

## 3. Slice 2A — Cross-product cache audit + per-consumer endpoint + daily report extension

This is the slice being implemented in `phase4-slice-2A-review`.

### 3.1 Cross-product audit (precondition)

Before per-consumer reporting is meaningful, we have to prove that no other
empire app is bypassing KJLE's cache by calling Searchbug directly. We audit
every repo on the VPS for:

- Literal `searchbug.com`, `searchbug.io` URLs
- `SEARCHBUG_API_KEY` env var references
- `searchbug_provider` imports
- `phone-validation-api` endpoint references
- Crontabs (root + ccrunner)
- Render env vars (`SEARCHBUG_API_KEY` should only be set on the `kjle-api` service)
- One-off Python scripts under `/opt/*.py`

Repos in scope: kjle (allowed), kjle-sender, kjle-command-deck, kjle-empire-hub,
kj-bridgedeck, demoenginez, demoboosterz, reviewbombz, kjwidgetz, telehealth /
kj-telehealth, kj-sales-agentz, ava, reachinbox-related repos.

**Acceptance:** zero non-kjle hits. Any direct caller found = audit FAILS, slice
is blocked pending migration, no endpoint/report work happens.

Deliverable: `docs/PHASE_4_LAYER_2_AUDIT_REPORT.md` with PASS / FAIL verdict at
the top and per-hit detail below.

### 3.2 Per-consumer endpoint

Add `GET /kjle/v1/dnc/stats/by-consumer` — same protection as `/dnc/stats`
(`verify_api_key`). Cuts the existing audit-log rollup along the `source` column
so each empire consumer gets its own row.

**Schema dependency:** the `dnc_audit_log.source` column already exists and is
populated by `_perform_check` (path/query routes), `_perform_email_check`, and
`POST /dnc/add`. Slice 2A inspects a 7-day sample to confirm the column is
populated with stable, low-cardinality identifiers. If null/garbage values are
found:

- **Preferred (Option A, lighter touch):** add a NOT NULL constraint with a
  default of `'unknown_legacy'`, backfill nulls in one migration:
  `migrations/dnc_audit_source_not_null.sql`.
- Option B (heavier, deferred): normalize sources into a `dnc_consumers` FK.
  Out of scope for 2A.

**Endpoint contract:**

```
GET /kjle/v1/dnc/stats/by-consumer?period_hours=24
Auth: x-api-key header

200 OK
{
  "period_hours": 24,
  "as_of": "2026-05-28T22:14:00+00:00",
  "consumers": [
    {
      "consumer_app": "telehealth",
      "calls": 120,
      "fresh_lookups": 26,
      "cache_hits": 94,
      "internal_suppressions": 0,
      "errors": 0,
      "hit_rate_pct": 78.33,
      "cost_usd": 0.5564
    },
    ...
  ],
  "totals": { ...same shape, aggregate across all consumers... }
}
```

`period_hours` clamps to `[1, 720]` (30 days). Default 24.

`hit_rate_pct` = `cache_hits / (cache_hits + fresh_lookups) * 100`, rounded to
2 decimals. Internal suppressions are excluded from the denominator — they're
not "the cache deciding," they're a separate pre-cache layer.

### 3.3 Daily report extension

`api/lib/daily_report.py::_dnc_section` (and any wrapper in
`api/routes/scheduler.py::job_daily_cost_report`) gets a new sub-block
**DNC BY CONSUMER (last 24h)** appended under the existing DNC HEALTH block:

```
DNC BY CONSUMER (last 24h)
-----
consumer_app          calls    hit_rate    cost
telehealth            120      78%         $0.51
reviewbombz           45       82%         $0.17
ava                   30       60%         $0.26
kjle_internal         12       45%         $0.14
-----
total                 207      75%         $1.08
```

Implementation rule: the route and the report share **one** helper function
(`_per_consumer_breakdown(period_hours: int) -> dict`) so the SQL lives in
exactly one place. No copy/paste between the route and the report.

If there's no DNC activity in the 24h window, render:
`DNC by consumer: no activity in window.`

### 3.4 Acceptance criteria

- Audit report committed with PASS verdict.
- `/kjle/v1/dnc/stats/by-consumer` returns valid JSON for `period_hours=24`
  and `period_hours=168` (7d).
- Daily report email contains the new BY CONSUMER block with correct totals.
- No protected files modified.
- Boots clean under py_compile and dummy env import.

---

## 4. Slice 2B — Bridge enforcement (future slice, sketched)

Once 2A confirms zero direct callers, BridgeDeck (the empire's gateway) should
**actively block** any request from a non-allowlisted app to Searchbug's domain
or the `phone-validation-api` path. This prevents regressions — a new product
spinning up and accidentally calling Searchbug directly will get a 403 at the
bridge instead of quietly running up KJLE's bill.

Out of scope for 2A.

## 5. Slice 2C — Per-consumer rate limiting (future slice, sketched)

If one consumer goes rogue and starts firing 10k lookups/min, the cache absorbs
most of it but the cold-cache tail will still drive Searchbug cost. Add per-
consumer token-bucket rate limits, configurable via `admin_settings`. Cost guard
already provides the global cap; this is the per-tenant cap.

Out of scope for 2A.

## 6. Slice 2D — Cost chargeback (future slice, sketched)

Once 2A's per-consumer cost data is reliable, surface a monthly chargeback line
per consumer. Useful for internal P&L attribution as the empire grows.

Out of scope for 2A.

## 7. Slice 2E — Cross-product email suppression mirror (future slice, sketched)

Same audit + endpoint pattern, applied to `email_suppressions` instead of phone
DNC. Phase 1 already centralized ReachInbox bounce ingestion; 2E ensures other
empire products consume that suppression list rather than maintaining their own.

Out of scope for 2A.

---

## 8. Non-goals

- No schema redesign of `dnc_audit_log`. The `source` column is sufficient.
- No new auth model — same `verify_api_key` posture as the rest of `/dnc/*`.
- No frontend (Command Deck) work in 2A. Surface raw JSON; visualization comes
  later.
- No backfill of old audit rows beyond the NOT NULL migration's default value.

## 9. Protected surface

The following files are load-bearing for Phase 4 Layer 1 and are **not** touched
in Slice 2A:

- `api/lib/carrier_lookup.py`
- `api/lib/phone_utils.py`
- `api/lib/dnc_check.py`
- `api/lib/dnc_batch.py`
- `api/lib/dnc_provider.py`
- `api/lib/searchbug_provider.py`
- `api/routes/dnc_webhooks.py`
- `api/routes/dnc_channel.py`
- `migrations/dnc_schema.sql`
- `migrations/dnc_admin_settings.sql`
- `migrations/nanpa_carrier_prefixes.sql`
- `migrations/leads_dnc_status.sql`
- `migrations/local_scraper_ingest.sql`
- `migrations/email_suppressions.sql`

Changes in 2A are confined to:

- `api/routes/dnc.py` — add endpoint + shared helper.
- `api/lib/daily_report.py` (or `api/routes/scheduler.py` wrapper) — extend
  email body with BY CONSUMER block.
- `migrations/dnc_audit_source_not_null.sql` — only if the source-column
  inspection (3.2) finds bad data.

---

*Last updated 2026-05-28.*
