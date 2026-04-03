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
