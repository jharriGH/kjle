---
## AUTONOMOUS EXECUTION — READ THIS FIRST

You are part of the King James Empire CC fleet.
Jim Harris is NEVER the middleman. Ever.

BEFORE ANYTHING ELSE:
brain_session_start(focus="[task]", product="[project]")

GET CREDENTIALS (never ask Jim):
brain_vault_search("what you need")

DISPATCH ANOTHER CC (never ask Jim to do it):
run_build_task(project="[project]", prompt="[task]")

LOG EVERYTHING:
brain_log(content, project)     — events
brain_memory(content, tags)     — decisions

END EVERY SESSION:
brain_session_end(product, what_shipped,
  decisions, next_action)
brain_save_card(title, project, content)

ONLY INTERRUPT JIM FOR:
+ Business decisions requiring his judgment
+ Credentials genuinely not in vault after search
+ Task complete — here are the results
+ Truly blocked with specific reason

NEVER:
- Ask Jim for credentials
- Ask Jim to copy/paste anything
- Present options and wait
- Ask Jim to run any command
- Be the middleman between SC and CC

KJE MCP: https://kje-mcp.onrender.com/mcp/T24NM1Sxbh7txJs-unNIjblaXMqA1OZW6gNU-Ud5Yjk/
VPS: 192.161.173.97 (claude at /usr/local/bin/claude)
Brain: https://jim-brain-production.up.railway.app
Key: jim-brain-kje-2026-kingjames
---

# 🎯 KJLE — CLAUDE.md
# Managed by brain_sync.py (Brain sections)
# + Manual additions (never auto-updated)
# Last synced: May 05, 2026 02:45 PM PST

---

## CURRENT STATUS
<!-- BRAIN-SYNC:START:STATUS -->
*Brain sync: May 05, 2026 02:45 PM PST*

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
- Today: $0.0083
- This month: $0.0083
- All time: $0.0083

**Empire:**
- 7 live | 3 launch ready | 6 in progress
<!-- BRAIN-SYNC:END:EMPIRE_STATE -->

---

## RECENT KJLE MEMORIES
<!-- BRAIN-SYNC:START:MEMORIES -->
1. KJLE prioritized as easiest
2. KJ Command Center is the KJLE Lead Finder
3. Completed KJLE build session on April 20, 2026
4. KJLE status March 26 2026: 32/32 prompts complete
5. KJLE status March 26 2026: KJLE Lead Finder frontend complete
6. KJLE profile includes internal tool at kjle-command-deck.onrender.com and kjle-api.onrender.com
7. Working on KJWidgetz project
8. KJ Command Center should not be treated as a separate product from KJLE Command Deck
<!-- BRAIN-SYNC:END:MEMORIES -->

---

## BUILD STATE
<!-- BRAIN-SYNC:START:BUILD_STATE -->
**Card:** KJLE Update-5-2-26
**Saved:** 2026-05-03

Update KJ Brain with the following architectural decisions and current state:

1. DNC ARCHITECTURE LOCKED IN
- KJLE becomes the DNC source of truth for the entire KJ empire
- Every app (Telehealth, DemoBoosterz, KJ Sales Agentz, future) calls KJLE's DNC endpoints rather than building their own
- Architecture pattern: scrub-on-call, not scrub-on-export (saves ~$2,000+/year)
- Provider: Searchbug API (~$0.003-0.03 per lookup)
- Cache TTL: 14 days for B2B
- Endpoints to be built: GET /kjle/v1/dnc/check/{phone}, POST /kjle/v1/dnc/add, POST /kjle/v1/dnc/scrub-batch (for future cold-dial campaigns)
- Cost: ~$15-50/month at realistic empire scale

2. KJLE CURRENT STATE (as of May 2)
- 514,534 leads, 100% email coverage, 99.7% phone coverage
- 9 scheduler jobs running cleanly
- $20/day budget cap, $500/month, currently using ~$0.15/day (mostly Commander chats)
- Stage 1 unthrottled to 1000/run (was 50)
- Stage 3 + Stage 4 nightly jobs paused via admin_settings flags (preserve cap for deliberate use)
- Daily cost reports landing at sales@mobilewebmds.com from kjle@kjreportz.com
- Segments-by-niche bug fixed (now shows 480K leads correctly, was showing 590)

3. KNOWN ISSUES NOT YET FIXED
- Pain scoring formula is degenerate: 97% of leads cluster in 41-50 band, max=78.8, only 6 leads ever reach pain≥70 (HOT). Needs full redesign in scripts/ingest.py compute_pain_score_v1. Estimated 2-3 hour session.
- 'other' niche bucket has 15,439 leads, of which only ~1,800 are recoverable from existing search_keyword/niche_raw signals. Recovery SQL is committed at migrations/recover_other_niche.sql but not run. The other ~12,838 leads have empty niche metadata entirely (ingestion bug documented at migrations/INGESTION_BUG_empty_niche_metadata.md).
- 393K+ leads still unclassified (classifier processes 40K/day, will catch up over ~10 days now that Stage 1 is unthrottled)

4. NEXT SESSION PRIORITIES (DNC Day)
- Build full DNC architecture in KJLE (3-4 hours): cache table, audit table, /dnc/check endpoint, /dnc/add endpoint, Searchbug provider, provider abstraction for future swaps
- Wire Telehealth to KJLE DNC endpoint (~30 min)
- Build inbound suppression webhooks (~1-2 hours): ReachInbox unsubscribes, TH bridge form opt-outs feed into KJLE's master DNC list
- Total target: ~6 hours for full empire-wide DNC compliance

5. SUBSEQUENT SESSION (Day 2)
- Pain scoring formula redesign
- First KJLE test campaign drafted in ReachInbox (interest probe pattern, no offer needed)
- Build Card v9 update reflecting reality

6. PINNED ITEMS
- Send to DemoBoosterz from KJLE Lead Finder (after tour rebuild)
- Schedule local backups for Documents\GitHub folder
- Rotate exposed ReachInbox API key on Render
- Rotate exposed DemoEnginez Supabase service role key on Render
- Click "Reclassify All Leads" in Lead Finder Admin Settings (catchup on 393K backlog)

7. STRATEGIC FRAME
KJLE is evolving from "lead database" to "lead concierge service" for the empire. Cross-cutting concerns (DNC, campaign tracking, suppression, contact frequency) live in KJLE so each app can stay focused on its product. This is the GOAT architecture pattern that mature CRMs use internally.
<!-- BRAIN-SYNC:END:BUILD_STATE -->

---

## MANUAL ADDITIONS
<!-- brain_sync.py never modifies below this line -->

---

## EMPIRE STATE & COSTS

---

## RECENT KJLE MEMORIES

---

## BUILD STATE


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

*Synced: May 05, 2026 02:45 PM PST*
*Refresh: `python brain_sync.py kjle`*