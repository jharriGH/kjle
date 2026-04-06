# 👑 KING JAMES LEAD EMPIRE — COMPLETE BUILD STATE
**Last Updated:** April 5, 2026
**Owner:** Jim Harris — DevelopingRiches, Inc.
**Status:** 🟢 100% COMPLETE — FULLY AUTONOMOUS

---

## 🗺️ INFRASTRUCTURE MAP

| Component | Location | URL | Status |
|---|---|---|---|
| KJLE API | Render Starter ($7/mo) | https://kjle-api.onrender.com | ✅ Live |
| KJLE Command Deck | Render Static (Starter) | https://kjle-command-deck.onrender.com | ✅ Live |
| KJLE Lead Finder | Lovable (standalone app) | Lovable preview URL | ✅ Live |
| Database | Supabase dhzpwobfihrprlcxqjbq | https://dhzpwobfihrprlcxqjbq.supabase.co | ✅ Connected |

---

## 🔑 CREDENTIALS

| Item | Value |
|---|---|
| Master API Key | `kjle-prod-2026-secret` |
| Supabase Project ID | `dhzpwobfihrprlcxqjbq` |
| Command Deck API Key | `kjle_49c0989b46ab43da6c3d424b0db7c504` |
| Truelist API Key | Stored in admin_settings (masked) |
| Outscraper API Key | Set in Render env var `OUTSCRAPER_API_KEY` |
| Firecrawl API Key | Set in Render env var `FIRECRAWL_API_KEY` (must include `fc-` prefix) |

---

## ⚙️ KJLE API — ROUTE INVENTORY (32 endpoints)

### Core
| Route | Purpose |
|---|---|
| `GET /kjle/v1/health` | Health check |
| `GET /kjle/v1/leads` | Paginated lead list + filters |
| `GET /kjle/v1/leads/stats` | Total counts, segment breakdown |
| `GET /kjle/v1/leads/{id}` | Single lead full detail |
| `POST /kjle/v1/leads/{id}/reclassify` | Re-score single lead |
| `POST /kjle/v1/leads/{id}/dnc` | Mark do not contact |
| `DELETE /kjle/v1/leads/{id}` | Hard delete |
| `POST /kjle/v1/leads/bulk` | Bulk reclassify/delete/dnc |

### Segments
| Route | Purpose |
|---|---|
| `GET /kjle/v1/segments/summary` | Hot/warm/cold counts |
| `GET /kjle/v1/segments/by-niche` | Per-niche breakdown (Lead Finder source) |
| `GET /kjle/v1/segments/saved` | All saved segments |
| `POST /kjle/v1/segments/saved` | Create saved segment |
| `POST /kjle/v1/segments/preview` | Live preview without saving |

### Pain Score
| Route | Purpose |
|---|---|
| `GET /kjle/v1/pain/by-niche` | Avg pain score per niche |
| `GET /kjle/v1/pain/distribution` | Pain score buckets |
| `GET /kjle/v1/pain/top` | Top N leads by pain |
| `GET /kjle/v1/pain/benchmarks` | Niche-level benchmarks |

### Enrichment
| Route | Purpose |
|---|---|
| `POST /kjle/v1/enrichment/stage1/single/{id}` | Stage 1 single lead |
| `POST /kjle/v1/enrichment/stage1/batch` | Stage 1 batch |
| `GET /kjle/v1/enrichment/status` | Pipeline stage counts |
| `POST /kjle/v1/enrichment/stage3/single/{id}` | Stage 3 Outscraper |
| `POST /kjle/v1/enrichment/stage3/batch` | Stage 3 batch |
| `GET /kjle/v1/enrichment/stage3/cost-estimate` | Stage 3 cost estimate |
| `POST /kjle/v1/enrichment/stage4/single/{id}` | Stage 4 Firecrawl |
| `POST /kjle/v1/enrichment/stage4/batch` | Stage 4 batch |
| `GET /kjle/v1/enrichment/stage4/cost-estimate` | Stage 4 cost estimate |
| `POST /kjle/v1/enrichment/email-clean` | Manual Truelist batch |
| `GET /kjle/v1/enrichment/email-clean/status` | Email coverage stats |

### CSV Import
| Route | Purpose |
|---|---|
| `POST /kjle/v1/csv/upload` | Upload CSV — returns upload_id |
| `POST /kjle/v1/csv/import/{upload_id}` | Import with field mapping |
| `GET /kjle/v1/csv/imports` | Full import history |

### Admin
| Route | Purpose |
|---|---|
| `GET /kjle/v1/admin/settings` | All admin settings |
| `POST /kjle/v1/admin/settings` | Update settings |
| `GET /kjle/v1/integrations` | All integrations + configured status |
| `GET /kjle/v1/budget/summary` | MTD spend vs guardrails |
| `GET /kjle/v1/budget/guardrails` | All guardrails |
| `POST /kjle/v1/budget/guardrails` | Create/upsert guardrail |
| `DELETE /kjle/v1/budget/guardrails/{id}` | Soft delete guardrail |
| `GET /kjle/v1/scheduler/status` | All 7 job statuses |
| `POST /kjle/v1/scheduler/run/{job}` | Manual job trigger |
| `GET /kjle/v1/scheduler/log` | Job run history |
| `GET /kjle/v1/pipeline/status` | Data quality coverage |
| `GET /kjle/v1/auth/keys` | API keys (masked) |
| `POST /kjle/v1/auth/keys` | Create API key |

---

## 🗄️ SUPABASE SCHEMA — leads table columns

### Core fields
`id, business_name, phone, email, website, address, city, state, zip, niche_slug, is_active, created_at, updated_at`

### Pain score fields
`pain_score, segment_label, segmented_at, fit_demoenginez`

### Google/Social
`google_stars, google_review_count, g_maps_claimed, facebook_url, instagram_url, linkedin_url`

### Website intel
`mobile_friendly, seo_schema_present, website_reachable, has_schema_markup, schema_types, has_phone_on_page, has_address_on_page`

### Email cleaning
`email_valid, email_status, email_cleaned_at, do_not_contact`

### Stage 3 — Outscraper
`outscraper_place_id, outscraper_rating, outscraper_reviews_count, outscraper_phone, outscraper_full_address, outscraper_business_status, outscraper_website, outscraper_latitude, outscraper_longitude`

### Stage 4 — Firecrawl
`firecrawl_markdown, firecrawl_owner_name, firecrawl_services, firecrawl_service_areas, firecrawl_years_in_business, firecrawl_usp`

### Enrichment tracking
`enrichment_stage, enriched_at`

### Other tables
`api_cost_log` (lead_id, service, cost_per_unit, records_processed, source_system, created_at)
`budget_guardrails` (id, service, period, limit_amount, action, active)
`admin_settings` (key, value, updated_at)
`scheduler_log` (job_name, leads_processed, hot, warm, cold, duration_seconds, status, notes, ran_at)
`saved_segments, export_log, import_log, api_keys, api_audit_log, webhooks, niches, category_mappings`

---

## 🔧 ENRICHMENT PIPELINE

### Stage 1 — Free (website scrape)
- Cost: $0.00
- What it does: checks website reachability, extracts JSON-LD schema, detects phone + address on page
- Gate: any lead with a website URL
- ⚠️ Fix applied: removed `.single()` bug (PGRST116 crash)
- ⚠️ SQL fix applied: added `website_reachable, has_schema_markup, schema_types, has_phone_on_page, has_address_on_page, enriched_at` columns

### Stage 2 — Yelp
- ❌ REMOVED — $221/mo too expensive
- Leads flow directly from Stage 1 → Stage 3

### Stage 3 — Outscraper (Google Maps)
- Cost: $0.002/lead
- What it does: Google Maps data — stars, reviews, phone, address, business status, coordinates
- Gate: enrichment_stage >= 1, pain_score >= 60
- Key: `OUTSCRAPER_API_KEY` Render env var
- ⚠️ Fix applied: removed `.single()` bug, lowered stage gate from 2→1, added `async=false` param
- ⚠️ SQL fix applied: added all `outscraper_*` columns

### Stage 4 — Firecrawl (deep website crawl)
- Cost: $0.005/lead
- What it does: crawls full website, extracts owner name, services, service areas, years in business, USP
- Gate: enrichment_stage >= 3, pain_score >= 75
- Key: `FIRECRAWL_API_KEY` Render env var (must include `fc-` prefix)
- ⚠️ Fix applied: removed `.single()` bug
- ⚠️ SQL fix applied: added all `firecrawl_*` columns

---

## ⏰ NIGHTLY SCHEDULER — 7 Jobs

| Job | Schedule | Batch | Gate | Cost |
|---|---|---|---|---|
| `email_clean_nightly` | Midnight UTC | 2,500 leads | Any | $0.00 |
| `enrich_stage3_nightly` | 1:00 AM UTC | 25 leads | Pain ≥ 60 | ~$0.05 |
| `enrich_stage4_nightly` | 3:00 AM UTC | 10 leads | Pain ≥ 75 | ~$0.05 |
| `cost_digest` | 8:00 AM UTC | — | — | $0.00 |
| `stale_cleanup` | 2:00 AM UTC | — | Pain < 30, age > 180d | $0.00 |
| `classify_segments` | Every 6hrs | 10,000 leads | All | $0.00 |
| `enrich_stage1` | Every 12hrs | 200 leads | Any + website | $0.00 |

**Max nightly cost: ~$0.10**

### Budget Guard
All paid jobs check budget before running. Configurable via admin_settings:

| Key | Default | Purpose |
|---|---|---|
| `enrich_stage3_nightly_limit` | 25 | Leads per Stage 3 run |
| `enrich_stage3_min_pain` | 60 | Stage 3 pain gate |
| `enrich_stage3_daily_budget_usd` | 0.10 | Stage 3 daily cap |
| `enrich_stage4_nightly_limit` | 10 | Leads per Stage 4 run |
| `enrich_stage4_min_pain` | 75 | Stage 4 pain gate |
| `enrich_stage4_daily_budget_usd` | 0.05 | Stage 4 daily cap |

---

## 🔗 INTEGRATIONS

| Integration | Status | Notes |
|---|---|---|
| Supabase | ✅ Connected | Primary database |
| Truelist.io | ✅ Connected | Stored in admin_settings |
| Outscraper | ✅ Connected | Render env var |
| Firecrawl | ✅ Connected | Render env var (needs `fc-` prefix) |
| ReachInbox | ⚠️ Key needed | Campaign 108639 |
| Yelp | ❌ Removed | Too expensive ($221/mo) |

---

## 🖥️ KJLE COMMAND DECK — Certified ✅

**URL:** https://kjle-command-deck.onrender.com
**Repo:** `C:\Users\Jim\Documents\GitHub\kjle-command-deck`

### Panels (all live)
| Panel | Status |
|---|---|
| Lead Inventory | ✅ |
| Avg Pain Score | ✅ |
| Product Fit | ✅ |
| Data Quality | ✅ |
| Niche Reading | ✅ |
| Niche Inventory | ✅ |
| Lead Radar | ✅ |
| Product Pipeline | ✅ |
| Health & Diagnostics | ✅ |
| System Log | ✅ |
| Saved Segments | ✅ |
| Cost Intelligence | ✅ |
| AlertSystem (Warning Strip + Toast Stack) | ✅ |

---

## 🔍 KJLE LEAD FINDER — Certified ✅

**Built in:** Lovable (standalone app)
**Connects to:** https://kjle-api.onrender.com/kjle/v1

### Lead Finder Tab — All Features
| Feature | Status |
|---|---|
| Dark military aesthetic | ✅ |
| Dynamic niche dropdown (API-fed, searchable) | ✅ |
| Pain score range slider | ✅ |
| Segment checkboxes (HOT/WARM/COLD/Unclassified) | ✅ |
| Website quality filters | ✅ |
| Social presence filters | ✅ |
| Google presence filters | ✅ |
| Email status filters | ✅ |
| Contact info filters | ✅ |
| Location filters (state/city) | ✅ |
| Exclude DNC (default ON) | ✅ |
| Sort dropdown | ✅ |
| Lead results table | ✅ |
| Pain score colored badges | ✅ |
| Segment badges (HOT/WARM/COLD) | ✅ |
| Page size selector (25/50/100) | ✅ |
| Pagination | ✅ |
| Lead detail drawer (eye icon) | ✅ |
| Save Segment button | ✅ |
| Export CSV button | ✅ |
| Push to ReachInbox button | ✅ |
| ONLINE status indicator | ✅ |
| UTC clock | ✅ |

### Admin Settings Tab — All Sections
| Section | Status |
|---|---|
| Email Cleaning | ✅ |
| Budget Control | ✅ |
| Scheduler (7 jobs with Run Now) | ✅ |
| Integrations | ✅ |
| API Keys | ✅ |
| Lead Stats | ✅ |
| CSV Pipeline | ✅ |

---

## 📂 CSV PIPELINE

**Source folder:** `C:\Users\Jim\Downloads\`
**Total CSVs found:** 398
**Completed:** ~10 (growing nightly)
**Rate:** 8 CSVs per night
**Est. completion:** ~50 nights
**Uploader script:** `C:\Users\Jim\Desktop\kjle_csv_uploader.py`
**Log file:** `C:\Users\Jim\Desktop\kjle_upload_log.json`
**Task Scheduler:** Runs nightly at 10:00 PM local time

### Standard field mapping (Outscraper format — confirmed working)
```
name→business_name, phone, email, Email Address→email, website,
category→niche_slug, address, city, region→state, zip,
googlestars→google_stars, googlereviewscount→google_review_count,
g_maps_claimed, facebook→facebook_url, instagram→instagram_url,
linkedin→linkedin_url, mobilefriendly→mobile_friendly,
seo_schema→seo_schema_present
```

---

## 📊 LIVE METRICS (April 5, 2026)

| Metric | Value |
|---|---|
| Total leads | 56,136 |
| HOT (pain ≥ 70) | 0 (enrichment still in progress) |
| WARM (pain 40-69) | 932 |
| COLD (pain < 40) | 68 |
| Unclassified | 26,792 |
| Email valid | 23,427 (49.5% coverage) |
| Email unknown | 4,334 |
| Phone coverage | 97.3% |
| Website coverage | 97.2% |
| Stage 0 (unenriched) | ~56,000+ |
| Stage 1 enriched | ~50 |
| Stage 3 enriched | ~25 |
| Stage 4 enriched | ~10 |
| MTD spend | $0.048 |

---

## 🗂️ FILE STRUCTURE

```
C:\Users\Jim\Documents\GitHub\kjle\
├── api/
│   ├── main.py                    ← CRITICAL — never regenerate from scratch
│   ├── config.py                  ← OUTSCRAPER_API_KEY + FIRECRAWL_API_KEY defined here
│   ├── database.py
│   └── routes/
│       ├── health.py
│       ├── leads.py
│       ├── lead_management.py     ← registered BEFORE leads.py
│       ├── segments.py
│       ├── segments_engine.py     ← registered BEFORE segments.py
│       ├── segment_manager.py
│       ├── segment_builder.py     ← registered BEFORE segments.py
│       ├── pipeline.py
│       ├── costs.py
│       ├── pain.py
│       ├── enrichment.py          ← Stage 1 (.single() bug fixed)
│       ├── enrichment_stage3.py   ← Stage 3 (.single() fixed, stage gate 1, async=false)
│       ├── enrichment_stage4.py   ← Stage 4 (.single() fixed)
│       ├── enrichment_email_clean.py
│       ├── csv_import.py
│       ├── integration_hub.py
│       ├── budget_control.py
│       ├── access_auth.py
│       ├── export.py
│       ├── push_demoenginez.py
│       ├── push_voicedrop.py
│       ├── webhooks.py
│       ├── scheduler.py           ← 7 jobs + Budget Guard
│       └── admin_settings.py

C:\Users\Jim\Documents\GitHub\kjle-command-deck\
├── src/
│   ├── main.jsx
│   ├── KJLECommandDeck.jsx        ← AlertSystem fully wired
│   ├── TopRowPanels.jsx
│   ├── MidRowPanels.jsx
│   ├── HealthAndPipelinePanels.jsx
│   ├── BotRowPanels.jsx
│   ├── CostIntelligencePanel.jsx
│   └── AlertSystem.jsx
```

---

## ⚠️ CRITICAL NOTES

- **Never regenerate main.py from scratch** — router registration order is critical
- **FIRECRAWL_API_KEY must include `fc-` prefix** in Render env var
- **Outscraper requires `async=false`** param or returns Pending status
- **admin_settings endpoint only accepts known keys** — add new keys directly via Supabase SQL
- **enrichment_stage3_nightly budget cap** — checked against `outscraper` service in api_cost_log
- **enrichment_stage4_nightly budget cap** — checked against `firecrawl` service in api_cost_log
- **Stage gate**: Stage 3 requires stage >= 1 (Yelp removed), Stage 4 requires stage >= 3
- **niche_slug FK dropped** — no longer blocks imports from mixed-niche CSVs

---

## ✅ COMPLETE PROMPT HISTORY

| Prompts | Feature | Status |
|---|---|---|
| 1–10 | Core API, enrichment stages 1–4, pain score engine | ✅ Done |
| 11–18 | Segmentation, segment manager, export, push integrations | ✅ Done |
| 19–25 | Command Deck frontend — all 12 panels | ✅ Done |
| 26 | Pipeline status + Admin Settings API | ✅ Done |
| 26B | Truelist email cleaning (manual + nightly) | ✅ Done |
| 27 | CSV import management | ✅ Done |
| 28 | Lead Management | ✅ Done |
| 29 | Segment Builder | ✅ Done |
| 30 | Integration Hub | ✅ Done |
| 31 | Budget Control | ✅ Done |
| 32 | Access & Auth | ✅ Done |
| CD | Command Deck + AlertSystem | ✅ Done |
| LF | KJLE Lead Finder (Lovable) — fully certified | ✅ Done |
| BG | Budget Guard + Stage 3/4 nightly jobs | ✅ Done |
| CSV | Nightly CSV uploader script + Windows Task Scheduler | ✅ Done |

---

## 🚀 WHAT RUNS AUTOMATICALLY EVERY NIGHT

| Time UTC | Action | Cost |
|---|---|---|
| 12:00 AM | Email clean — 2,500 leads via Truelist | $0.00 |
| 1:00 AM | Stage 3 enrichment — 25 leads via Outscraper | ~$0.05 |
| 2:00 AM | Stale lead cleanup | $0.00 |
| 3:00 AM | Stage 4 enrichment — 10 leads via Firecrawl | ~$0.05 |
| 8:00 AM | Cost digest + guardrail check | $0.00 |
| 10:00 PM | CSV uploader — 8 new CSVs imported | $0.00 |
| Every 6hrs | Classify all leads HOT/WARM/COLD | $0.00 |
| Every 12hrs | Stage 1 enrichment — 200 leads free | $0.00 |

**Max nightly cost: $0.10 — empire runs itself. 👑**

---

*King James Lead Empire — Final Build State — April 5, 2026*
*DevelopingRiches, Inc. — Jim Harris — Confidential*
