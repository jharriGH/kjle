# 👑 KING JAMES LEAD EMPIRE — MASTER INFRASTRUCTURE CARD
**Last Updated:** March 15, 2026
**Owner:** Jim Harris — King James Enterprises LLC

---

## 🗺️ INFRASTRUCTURE MAP

| App | Repo | Deployed To | URL | Database |
|---|---|---|---|---|
| **KJLE API** | `github.com/jharriGH/kjle` | Render → `kjle-api` | `https://kjle-api.onrender.com` | Supabase `dhzpwobfihrprlcxqjbq` |
| **KJLE Command Deck** | `github.com/jharriGH/kjle-command-deck` | Render → `kjle-command-deck` | `https://kjle-command-deck.onrender.com` | Via KJLE API |
| **DemoEnginez** | `github.com/jharriGH/demoenginez` | Render | TBD | Shared Supabase (demoenginez schema) |
| **DemoBoosterz** | TBD | Render | `https://demoboosterz-screenshot-server.onrender.com` | Shared Supabase (demoboosterz schema) |
| **KJWidgetz** | TBD | Render → `kjwidgetz-api` | `https://kjwidgetz-api.onrender.com` | Shared Supabase (kjwidgetz schema) |
| **UnhideLocal** | TBD | TBD | `UnhideLocal.com` | TBD |
| **KJVoiceDropz** | TBD | Railway | `KJVoiceDropz.com` | Supabase (voicedrop schema) |

---

## 🗄️ SUPABASE

- **Project ID:** `dhzpwobfihrprlcxqjbq`
- **URL:** `https://dhzpwobfihrprlcxqjbq.supabase.co`
- **Used by:** KJLE API, DemoEnginez, DemoBoosterz, KJWidgetz (all separate schemas on same project)
- **Auth:** Service role key stored in Render env vars as `SUPABASE_SERVICE_KEY`

### Key Tables (KJLE schema)
| Table | Purpose |
|---|---|
| `leads` | Master lead database — 28,849 records |
| `api_cost_log` | All enrichment cost tracking |
| `scheduler_log` | APScheduler job run history |
| `budget_guardrails` | Cost alert thresholds |
| `saved_segments` | Named lead segments |
| `webhooks` | Registered webhook endpoints |
| `export_log` | CSV/ReachInbox export history |

### Permissions Required
All tables need: `GRANT ALL ON TABLE <table> TO service_role;`

---

## ⚙️ KJLE API (FastAPI Backend)

- **Local path:** `C:\Users\Jim\Desktop\kjle`
- **GitHub:** `github.com/jharriGH/kjle`
- **Live URL:** `https://kjle-api.onrender.com`
- **Docs:** `https://kjle-api.onrender.com/kjle/docs`
- **API prefix:** `/kjle/v1`
- **Framework:** FastAPI + Uvicorn
- **Python:** 3.11.15
- **Hosting:** Render (free tier — spins down after 15min inactivity)

### Render Environment Variables
| Key | Value |
|---|---|
| `SUPABASE_URL` | `https://dhzpwobfihrprlcxqjbq.supabase.co` |
| `SUPABASE_SERVICE_KEY` | `eyJ...` (service role key) |
| `API_SECRET_KEY` | `kjle-prod-2026-secret` |
| `PYTHON_VERSION` | `3.11.15` |

### Key Routes
| Route | Purpose |
|---|---|
| `GET /kjle/v1/health` | Health check |
| `GET /kjle/v1/segments/summary` | Hot/warm/cold counts |
| `GET /kjle/v1/segments/by-niche` | Per-niche breakdown |
| `GET /kjle/v1/pain/by-niche` | Avg pain per niche |
| `GET /kjle/v1/pain/distribution` | Pain score buckets |
| `GET /kjle/v1/costs` | MTD cost summary + by service |
| `GET /kjle/v1/scheduler/status` | 4 job statuses |
| `GET /kjle/v1/scheduler/log` | Job run history |
| `POST /kjle/v1/scheduler/run/{job}` | Manual job trigger |
| `GET /kjle/v1/push/demoenginez/status` | DE eligible leads |
| `GET /kjle/v1/push/voicedrop/status` | VD eligible leads |

### APScheduler Jobs
| Job | Schedule | Purpose |
|---|---|---|
| `classify_segments` | Every 6hrs | Labels leads hot/warm/cold (1000/batch) |
| `enrich_stage1` | Every 12hrs | Free enrichment on unenriched leads |
| `cost_digest` | Daily 8am UTC | Cost summary + guardrail check |
| `stale_cleanup` | Daily 2am UTC | Soft-delete old low-pain leads |

### File Structure
```
C:\Users\Jim\Desktop\kjle\
├── api/
│   ├── main.py          ← FastAPI app + CORS + lifespan
│   ├── config.py        ← Settings (pydantic)
│   ├── database.py      ← Supabase client init (init_db() called in lifespan)
│   └── routes/
│       ├── health.py
│       ├── leads.py
│       ├── segments.py
│       ├── segments_engine.py   ← classify + summary (registered BEFORE segments.py)
│       ├── segment_manager.py
│       ├── pipeline.py
│       ├── costs.py             ← includes GET /costs root endpoint
│       ├── pain.py
│       ├── enrichment.py
│       ├── enrichment_stage2.py
│       ├── enrichment_stage3.py
│       ├── enrichment_stage4.py
│       ├── export.py
│       ├── push_demoenginez.py
│       ├── push_voicedrop.py
│       ├── webhooks.py
│       └── scheduler.py         ← SCHEDULER_LOG_TABLE = "scheduler_log" required
```

### Critical Notes
- `init_db()` MUST be called in lifespan before scheduler starts
- `segments_engine` router MUST be registered before `segments` router in main.py
- CORS: `allow_origins=["*"]`, `allow_credentials=False` (credentials must be False with wildcard)
- Router prefix pattern: enrichment uses `f"{PREFIX}/enrichment"`, all others use `PREFIX`

---

## 🖥️ KJLE COMMAND DECK (React Frontend)

- **Local path:** `C:\Users\Jim\Desktop\kjle-command-deck`
- **GitHub:** `github.com/jharriGH/kjle-command-deck`
- **Live URL:** `https://kjle-command-deck.onrender.com`
- **Framework:** React 18 + Vite 5
- **Hosting:** Render Static Site

### Render Build Settings
| Setting | Value |
|---|---|
| Build Command | `npm ci --include=dev && npx vite build` |
| Publish Directory | `dist` |

### File Structure
```
C:\Users\Jim\Desktop\kjle-command-deck\
├── src/
│   ├── main.jsx
│   ├── KJLECommandDeck.jsx       ← Root component (fully integrated)
│   ├── TopRowPanels.jsx          ← Lead Inventory, Pain Score, Product Fit, Data Quality, Niche Reading
│   ├── MidRowPanels.jsx          ← Niche Inventory, Lead Radar
│   ├── HealthAndPipelinePanels.jsx ← Product Pipeline, Health & Diagnostics
│   ├── BotRowPanels.jsx          ← System Log, Saved Segments
│   ├── CostIntelligencePanel.jsx ← Cost Intelligence
│   └── AlertSystem.jsx           ← useAlerts, AlertToastStack, AlertWarningStrip
├── index.html
├── package.json                  ← vite + @vitejs/plugin-react in dependencies (not devDependencies)
├── vite.config.js                ← base: "./"
├── render.yaml
└── .gitignore
```

### Dashboard Layout
```
┌──────────────────────── TOP BAR (56px) ────────────────────────────┐
│  Alert Warning Strip (dynamic — hidden when no alerts)             │
├──────────┬───────────┬────────────┬────────────┬───────────────────┤
│  Lead    │ Avg Pain  │ Product    │   Data     │  Niche            │
│ Inventory│  Score    │   Fit      │  Quality   │  Reading          │
├──────────┴───────────┴────────────┴────────────┴───────────────────┤
│  Niche Inventory (30%)  │  Lead Radar (40%)  │ Product Pipeline(30%)│
├─────────────────────────┼────────────────────┼──────────────────────┤
│  System Log (35%)       │ Health & Diag (30%)│ Cost Intel (35%)     │
└─────────────────────────┴────────────────────┴──────────────────────┘
│                      BOTTOM BAR (32px)                              │
```

### Palette (LOCKED — never deviate)
```
Background:       #010810
Panel BG:         #000D1A
Panel Border:     #002233
Primary (Cyan):   #00E5FF
Secondary (Gold): #FFD700
Hot (Green):      #00FF88
Warm (Amber):     #FFAA00
Alert (Red):      #FF2244
Label Text:       #004455
Font Display:     Rajdhani (400, 600, 700)
Font Mono:        Share Tech Mono
```

---

## 🔧 KJWIDGETZ

- **Local path:** TBD
- **API URL:** `https://kjwidgetz-api.onrender.com`
- **Admin Header:** `x-kjwidgetz-admin: kjwidgetz-admin-2026-king-james-kj`
- **Supabase Schema:** `kjwidgetz` (on shared DemoBoosterz Supabase project)
- **Tiers:** $49 / $99 / $149 / $297
- **Status:** Prompts 1–4 complete. Prompt 5 next (Admin Dashboard UI in Lovable)

---

## 📋 OPEN PUNCH LIST

| # | Item | Where | Priority |
|---|---|---|---|
| 1 | `/pipeline/status` endpoint missing — DataQualityPanel uses hardcoded fallback | ⚙️ kjle-api | Prompt 26 |
| 2 | Verify `niche` vs `niche_slug` field consistency across `/segments/by-niche` and `/pain/by-niche` | ⚙️ kjle-api | Test |
| 3 | `/push/demoenginez/status` and `/push/voicedrop/status` returning 500 + CORS blocked | ⚙️ kjle-api | Fix next |
| 4 | Truelist.io email cleaning integration — `email_valid` + `email_status` columns, POST `/enrichment/email-clean` | ⚙️ kjle-api | Prompt 26B |
| 5 | KJWidgetz Prompt 5 — Admin Dashboard UI in Lovable | 🔧 KJWidgetz | Next session |
| 6 | UnhideLocal schema generator engine — next build priority | TBD | Pending |
| 7 | KJVoiceDropz — deploy Feb 2026 codebase to Railway | Railway | After KJWidgetz |

---

## 🚀 REMAINING BUILD ROADMAP (KJLE)

| Prompt | Feature | Status |
|---|---|---|
| 26 | Admin: System Settings + pipeline/status endpoint | 🔲 Next |
| 26B | Truelist.io email cleaning integration | 🔲 |
| 27 | Admin: CSV Management | 🔲 |
| 28 | Admin: Lead Management | 🔲 |
| 29 | Admin: Segment Builder | 🔲 |
| 30 | Admin: Integration Hub | 🔲 |
| 31 | Admin: Budget Control | 🔲 |
| 32 | Admin: Access & Auth | 🔲 |

---

## 💡 KEY DECISIONS & PATTERNS

- **Consolidated Supabase:** All apps share one Supabase project with separate schemas — saves ~$84/mo
- **Consolidated Render API:** DemoEnginez + DemoBoosterz share one Render API server with namespaced routes
- **Screenshot server:** `https://demoboosterz-screenshot-server.onrender.com` (Docker, Render Starter)
- **Outreach stack:** Instantly + ReachInbox (unlimited) + HeyReach (2x AppSumo LTDs, 6 LinkedIn senders)
- **Video:** Yepic/Vumu for personalized video (check before recommending HeyGen/Loom), Veedea for interactive
- **SMS:** Twilio toll-free 866-621-7044, email-first strategy with SMS as warm follow-up
- **Reminder:** At KJWidgetz Prompt 15 — share UnhideLocal frontend build card
