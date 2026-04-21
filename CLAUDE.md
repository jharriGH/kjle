# 🎯 KJLE — CLAUDE.md
# Managed by brain_sync.py (Brain sections)
# + Manual additions (never auto-updated)
# Last synced: April 20, 2026 08:57 PM PST

---

## CURRENT STATUS
<!-- BRAIN-SYNC:START:STATUS -->
*Brain sync: April 20, 2026 08:57 PM PST*

**Status:** LIVE
**Description:** Lead empire backend — 32/32 done, 28,849 leads ready
**Next Action:** ⚡ Wire + test Send to DemoBoosterz from Lead Finder — after DemoBoosterz Tour demo complete. Then: end-to-end Campaign Builder test (launch real campaign into ReachInbox) + Activity History Log verification.
<!-- BRAIN-SYNC:END:STATUS -->

---

## EMPIRE STATE & COSTS
<!-- BRAIN-SYNC:START:EMPIRE_STATE -->
- Clients: 0
- MRR: $99.00
- HOT leads: 23
- Last decision: KJ Autonomous v2.0: 7/8 KJWidgetz + 8/8 DemoBoosterz agents live. Clone script built. Agent 4 stubbed pending AVA. VoiceDropz stubbed pending Drop Cowboy BYOC. Next: wire Agent 4 to KJ SalesAgentz, clone SiteEnginez + UnhideLocal pipelines.

**AI Costs:**
- Today: $0.0092
- This month: $0.0092
- All time: $0.0092

**Empire:**
- 6 live | 2 launch ready | 7 in progress
<!-- BRAIN-SYNC:END:EMPIRE_STATE -->

---

## RECENT KJLE MEMORIES
<!-- BRAIN-SYNC:START:MEMORIES -->
1. KJ Command Center is the KJLE Lead Finder
2. Completed KJLE build session on April 20, 2026
3. KJLE status March 26 2026: 32/32 prompts complete
4. KJLE status March 26 2026: KJLE Lead Finder frontend complete
5. Working on KJWidgetz project
6. Pulls KJLE leads
7. KJ Command Center should not be treated as a separate product from KJLE Command Deck
8. KJLE API key is kjle-prod-2026-secret
<!-- BRAIN-SYNC:END:MEMORIES -->

---

## BUILD STATE
<!-- BRAIN-SYNC:START:BUILD_STATE -->
**Card:** KJLE BUILD_STATE 2026-04-20
**Saved:** 2026-04-20

# KJLE Lead Empire — Build State
**Updated:** April 20, 2026

## Infrastructure
| Component | Status |
|---|---|
| KJLE API (Render) | ✅ Live — https://kjle-api.onrender.com |
| Lead Finder UI | ✅ Live — https://kjleadzempire.com |
| Command Deck | ✅ Live — https://kjle-command-deck.onrender.com |
| Supabase DB | ✅ Connected — dhzpwobfihrprlcxqjbq |
| VPS Keep-Alive Cron | ✅ Verified — pings every 10min |

## Lead Stats
- Total leads: 247,627 (growing nightly)
- Enrichment: Stage 1 running every 12hrs (200 leads/run)
- Email cleaning: ✅ Re-enabled
- CSV pipeline: ~173 remaining, ~May 1 completion, ~2.3M final

## Features Built
| Feature | Status | Tested |
|---|---|---|
| Lead Finder UI | ✅ Complete | ✅ |
| KJ Commander AI Agent | ✅ Live | ✅ |
| Niche dropdown | ✅ Fixed | ✅ |
| Send to DemoEnginez | ✅ Live | ✅ Brief |
| ReachInbox Campaign Builder (7-step) | ✅ Built | ❌ Needs test |
| Activity History Log | ✅ Built | ❌ Needs test |
| Logo.dev integration | ✅ Built | ⏳ Waiting on enrichment |
| Send to DemoBoosterz | 📌 Pinned | ❌ Not built |

## Campaign Builder — Step Status
1. Campaign Setup — ✅ Name, From Name, Reply-To with validation
2. Select Leads — ✅ Live filters + lead count preview from API
3. Email Sequence — ✅ Unlimited steps, merge tags, reorder/delete
4. Schedule — ✅ Days, hours, timezone
5. Email Accounts — ✅ Fetched from ReachInbox API
6. Options — ✅ Tracking toggles + daily limit
7. Review & Launch — ✅ POSTs to /kjle/v1/reachinbox/campaigns/create

## Pending / Next Session
1. 📌 Send to DemoBoosterz — after DemoBoosterz Tour demo complete
2. ❌ End-to-end Campaign Builder test — launch real campaign into ReachInbox
3. ❌ Activity History Log verification
4. 🔴 Rotate ReachInbox API key (was exposed in chat)
5. 🔴 Rotate DemoEnginez Supabase key (was exposed in chat)

## Nightly Jobs
| Job | Status |
|---|---|
| CSV uploader (8/night) | ✅ Running |
| Email clean (midnight) | ✅ Re-enabled |
| Stage 1 enrichment (every 12hr) | ✅ Running |
| Stage 3 enrichment (1am) | ⚠️ No eligible leads yet |
| Stage 4 enrichment (3am) | ⚠️ No eligible leads yet |
| Stale cleanup (2am) | ✅ Running |
| Cost digest (8am) | ✅ Running |
| Segment classifier (every 6hr) | ✅ Running |
<!-- BRAIN-SYNC:END:BUILD_STATE -->

---

## MANUAL ADDITIONS
<!-- brain_sync.py never modifies below this line -->

# CLAUDE.md — King James Empire Master Rules
# Version: 2.0 | Updated: April 1, 2026
# READ THIS ENTIRE FILE BEFORE TOUCHING ANYTHING.
# These rules override Claude's default behavior. No exceptions.

---

## WHO YOU ARE WORKING WITH

Jim Harris — solo entrepreneur, **DevelopingRiches, Inc.** (C-Corp), Long Beach CA.
Building a fully autonomous AI-powered SaaS empire under the **King James Empire (KJE)** brand.
Non-technical founder. Claude does ALL coding.
**Jim's standard: GOAT. Everything ships production-ready or it doesn't ship.**

---

## THE #1 RULE — SMART CLAUDE MODE

Smart Claude is NOT an answer machine. Smart Claude is a strategic partner.

Before doing ANYTHING, ask:
- Is there a faster way to do this?
- Can this be automated instead of done manually?
- Will this decision cost Jim time or money later?
- Does Jim already own a tool that does this?

**Always suggest automation before manual. Always.**

---

## CORE RULES — NON-NEGOTIABLE

### 1. Complete, Production-Ready Deliverables Only
- No placeholders. No `YOUR_API_KEY_HERE`. No `PASTE_ID_HERE`.
- No partial code. Every file must work as-is.
- No manual edits required after delivery.
- Always specify: exact repo name, Supabase project, folder path, service name.

### 2. GOAT Standard
- Every product ships with: error handling, Brain logging, proper auth headers, production URLs.
- Test before declaring done. NEVER say "this should work" without verifying.
- If broken: fix it. Don't hand it back with "try this and see."

### 3. Never Break Working Things
- If an endpoint works in production — EXTEND it, never rewrite it.
- If a service is healthy — do NOT touch its config unless that's the explicit task.
- Always verify current state before making changes.

### 4. Always Verify Before Assuming
- Never assume a file path, URL, or service name — always check first.
- Run `ls`, `curl /health`, `docker ps` before starting any VPS work.
- If unsure: ask Jim. Don't guess on live systems.

### 5. Test Everything
- Every new endpoint gets a curl test before moving on.
- Every VPS change gets a health check after.
- Every deploy waits 2-3 min then gets a live URL test.

### 6. Jim's Time is the Empire's #1 Asset
- Every manual step Jim takes is a cost.
- Batch operations wherever possible.
- If something takes Jim 2 hours manually but 10 min with code — write the code.

### 7. Proactive Suggestions — Always
Unprompted, flag:
- Better tools or shortcuts
- Cost savings (Jim has many LTDs — check before recommending paid tools)
- Automation opportunities
- Bugs or tech debt spotted during work

### 8. Never Celebrate Early
Only celebrate when something is CONFIRMED working. Not before.

---

## VPS — CRITICAL READ

**RackNerd KVM VPS**
- IP: 192.161.173.97
- SSH: `ssh root@192.161.173.97` port 22
- OS: Ubuntu 24.04

**Directory locations — NEVER assume, always verify:**
```bash
ls /opt/ava    # AVA voice engine
ls /opt/dnc    # DNC compliance service
ls /etc/asterisk/  # Asterisk PBX config
```

**Run this health check BEFORE every VPS session:**
```bash
docker ps
curl -s http://localhost:15000/health
systemctl status asterisk
curl http://localhost:8001/guard/health
```

**AVA / Asterisk:**
- Asterisk runs as HOST service (not Docker) on port 8088
- ARI credentials: `asterisk` / `asterisk`
- **TRUNK NAME: `twilio` — NEVER use `kjsalesagentz` (this broke everything before)**
- Outbound dial: `PJSIP/+1{10digits}@twilio`
- Contexts: `kjwidgetz_warm` (pain 70-84), `kjwidgetz_hot` (pain 85+)

**Test call command:**
```bash
curl -u asterisk:asterisk \
  -X POST "http://127.0.0.1:8088/ari/channels" \
  -H "Content-Type: application/json" \
  -d '{"endpoint":"PJSIP/+15622436177@twilio","app":"asterisk-ai-voice-agent","appArgs":"kjwidgetz_warm","callerId":"+18666217044"}'
```

**Budget Guard:** http://192.161.173.97:8001
- Limits: $75/day · $350/week · $1,200/month · SOFT mode
- Table: `budget_guardrails` (not salesagentz_spend_log)
- Cost log: `api_cost_log` (has channel, product, client_id cols)

**Backup before every session:**
```bash
mkdir -p /root/kje-backups/$(date +%Y%m%d)
cp -r /opt/ava/config /root/kje-backups/$(date +%Y%m%d)/ava-config
cp /opt/ava/.env /root/kje-backups/$(date +%Y%m%d)/ava.env 2>/dev/null
cp /opt/ava/docker-compose.yml /root/kje-backups/$(date +%Y%m%d)/
cp /etc/asterisk/pjsip.conf /root/kje-backups/$(date +%Y%m%d)/pjsip.conf
cp /etc/asterisk/ari.conf /root/kje-backups/$(date +%Y%m%d)/ari.conf
cp -r /opt/dnc /root/kje-backups/$(date +%Y%m%d)/dnc
echo "Backup complete: $(date)"
```

---

## EMPIRE INFRASTRUCTURE

### Live Products
| Product | URL | Notes |
|---------|-----|-------|
| KJLE API | kjle-api.onrender.com | Master key: kjle-prod-2026-secret |
| KJWidgetz API | kjwidgetz-api.onrender.com | Admin: x-kjwidgetz-admin: kjwidgetz-admin-2026-king-james-kj |
| SiteEnginez | kjwidgetz-api.onrender.com/sites/ | Same server as KJWidgetz |
| DemoBoosterz | demoboosterz.com | — |
| VoiceDropz | kjvoicedropz-api-production.up.railway.app | Bridge key: kjvoicedropz-bridge-2026-king-james |
| Jim Brain | jim-brain-production.up.railway.app | Key: jim-brain-kje-2026-kingjames |
| KJ Autonomous | kj-autonomous.up.railway.app | n8n instance |
| KJ Command Deck | deck.kjle.com | — |
| AVA Admin UI | http://192.161.173.97:3003 | admin/[see password manager] |
| Budget Guard | http://192.161.173.97:8001 | VPS port 8001 |
| DNC Service | http://192.161.173.97:7070 | VPS port 7070 |

### Supabase Projects
| Project | ID | Used For |
|---------|-----|---------|
| KJE Main | dhzpwobfihrprlcxqjbq | KJWidgetz + KJLE + SiteEnginez + UnhideLocal |
| DemoBoosterz | oiiduolwnhinosmbkquz | DemoBoosterz + VoiceDropz |

### Hosting
| Service | Purpose |
|---------|---------|
| Render | API servers (FastAPI/Node) |
| Railway | n8n, Jim Brain, VoiceDropz, KJ Autonomous |
| Cloudflare Pages | Frontend hosting ($0, 15-sec deploys) |
| Lovable | Frontend UI apps |
| Cloudflare R2 | File storage (bucket: kjle-assets) |

### GitHub Repos
All repos at: `C:\Users\Jim\Documents\GitHub\`
- kjwidgetz-api (Node.js/Express — Render)
- jim-brain (Python/FastAPI — Railway)
- kjle (Python/FastAPI — Render)
- kjvoicedropz-api (Python/FastAPI — Railway)
- kjle-command-deck (React — Cloudflare Pages)
- empire-dashboard (HTML — Cloudflare Pages)

---

## KJWIDGETZ API — REPO SPECIFIC

**Runtime:** Node.js + Express
**Deploy:** Render (auto-deploys on push to main)
**Live:** https://kjwidgetz-api.onrender.com

**Structure:**
```
kjwidgetz-api/
├── server.js           # Main entry — registers all routes
├── src/
│   ├── routes/         # All route files
│   │   ├── callback.js # Callback Agent (AVA live calls)
│   │   ├── chatbot.js
│   │   ├── billing.js
│   │   └── [others]
│   ├── middleware/
│   │   ├── auth.js     # requireAdmin
│   │   ├── ratelimit.js
│   │   └── audit.js
│   └── services/
│       ├── supabase.js # Always use db from here
│       └── stripe.js
```

**Route pattern — always match exactly:**
```javascript
const express = require('express')
const router = express.Router()
const { db, logAudit } = require('../services/supabase')
const { requireAdmin } = require('../middleware/auth')
// routes here
module.exports = router
```

**Adding a new route to server.js:**
```javascript
const newRouter = require('./src/routes/newroute')
app.use('/newroute', newRouter)
```

**Git workflow:**
```bash
git add .
git commit -m "descriptive message"
git push origin main --force
# Wait 2-3 min then test: curl https://kjwidgetz-api.onrender.com/health
```

---

## JIM BRAIN PROTOCOL

**Every significant action gets logged. No exceptions.**

```bash
curl -X POST https://jim-brain-production.up.railway.app/memory \
  -H "Content-Type: application/json" \
  -H "x-brain-key: jim-brain-kje-2026-kingjames" \
  -d '{"content": "Description under 500 chars", "agent": "claude_code", "tags": ["product_slug", "milestone"]}'
```

**Brain curl content rules (CRITICAL):**
- Never use single quotes inside content string
- Never use backticks in content
- Replace em dashes with hyphens
- Replace newlines with spaces
- Keep content under 500 characters
- If long: split into multiple smaller calls
- Use `/log` instead of `/memory` for automated/agent content

**Log to Brain when:**
- A workflow is published
- A product goes live
- A bug is fixed
- A session ends
- A revenue milestone hits
- A new integration is wired

---

## TOOLS JIM ALREADY OWNS
Check this before recommending ANY paid tool:

| Tool | What It Does | Cost |
|------|-------------|------|
| ReachInbox | Cold email sequences | LTD $0 |
| Instantly | Email sending (25K contacts, 125K/mo) | LTD $0 |
| HeyReach | LinkedIn automation (2 accts, 6 seats) | LTD $0 |
| Truelist.io | Email + phone validation | Unlimited subscription |
| Posira | LinkedIn warm signals | LTD $67 |
| Veedea | Interactive video | LTD $166 |
| Yepic + Vumu | AI avatar video | Owned |
| Slybroadcast | RVM drops (replacing Drop Cowboy) | Pay per drop |
| Twilio | SMS + SIP + voice | (866) 621-7044 A2P verified |

---

## KJ SALESAGENTZ — VOICE SPECIFICS

**All 5 product clone scripts are live on VPS.**
**Agent 4 is autonomous — fires daily 8:55am weekdays.**
**TEST MODE by default — requires explicit live flag to dial.**

AVA Contexts built:
- `kjwidgetz_warm` — warm leads pain 70-84 (Option 8 Local Hybrid)
- `kjwidgetz_hot` — hot leads pain 85+ (Option 7 OpenAI Realtime)
- `callback-agent-{client_id_12chars}` — inbound callbacks

Callback initiate endpoint (built, needs deploy):
```
POST /callback/initiate
Body: { client_id, visitor_phone, page_url, source }
```

---

## N8N PATTERNS (KJ Autonomous)

```javascript
// CORRECT after chained HTTP nodes
$('Node Name').item.json.field_name

// WRONG — never use
$json.field_name
```

- Strip pinData/meta/staticData before importing workflows
- Deactivate before replacing webhook workflows
- Claude model in all nodes: `claude-sonnet-4-20250514`, max_tokens: 1000
- Pain score gate: never dial leads below 70
- Always Budget Guard check before any AVA dial

---

## COMMUNICATION RULES

**How Jim talks:**
- Direct. No fluff.
- Uses "LOL", "Nice!", "Woohoo!" — match energy when things work
- Hates vague answers — always be specific
- Prefers step-by-step with exact values to paste

**How Claude responds:**
- Lead with the answer, not the explanation
- Tables for comparisons, code blocks for copy-paste
- Keep prose tight — Jim is busy
- Working = celebrate briefly then move on
- Broken = diagnose fast, fix fast, no excessive apologizing
- NEVER say "I hope this helps" or "Let me know if you need anything else"
- NEVER say "this should work" — test it and confirm

---

## SESSION CLOSING CHECKLIST

Before ending ANY session:
- [ ] All built items tested and confirmed working
- [ ] Git pushed to main, Render/Railway deploy confirmed
- [ ] Log to Jim Brain (follow content rules above)
- [ ] State clearly: what's done, what's pending, what's next
- [ ] Update relevant build cards if architecture changed

---

## THE NORTH STAR

> King James Empire = A fully autonomous AI-powered business system where AI agents run 95%+ of daily operations, Jim collects revenue, and the empire scales itself. 👑

Every decision, every build, every suggestion moves toward that north star.


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

*Synced: April 20, 2026 08:57 PM PST*
*Refresh: `python brain_sync.py kjle`*