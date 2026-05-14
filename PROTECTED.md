# 🔒 PROTECTED FILES — DO NOT MODIFY WITHOUT EXPLICIT JIM APPROVAL

These files represent production-critical empire infrastructure that has been
carefully built and verified. Modifying them without explicit user approval 
risks breaking the entire empire's compliance, data quality, or revenue pipeline.

## ABSOLUTELY OFF LIMITS (READ ONLY without explicit Jim approval)

### DNC Compliance
- api/routes/dnc.py
- api/routes/dnc_webhooks.py
- api/lib/dnc_provider.py
- api/lib/searchbug_provider.py
- api/lib/phone_utils.py
- api/lib/reply_parser.py

### Pain Scoring (formula + thresholds)
- scripts/ingest.py (compute_pain_score_v1 function)
- scripts/test_pain_score.py
- api/routes/segments_engine.py
- scripts/recompute_pain.py

### Email Cleaning (Truelist bulk architecture)
- api/routes/enrichment_email_clean.py
- api/routes/webhooks_truelist.py
- scripts/test_email_clean_parser.py

### ReachInbox Campaign Layer
- api/routes/reachinbox.py
- api/routes/campaigns.py
- api/routes/webhooks.py

## RULES FOR ANY SC OR CC SESSION

1. Before editing ANY file listed above, STOP and ask Jim explicitly.
2. If Jim is unavailable, do NOT proceed. Wait for approval.
3. Plan-first protocol applies: show the diff, get approval, then push.
4. NEVER use `git add .` or `git add -A` — always explicit file paths.
5. NEVER force-push to main.
6. If you find yourself rewriting one of these files from scratch, you 
   are doing it wrong. Surgical edits only.

## ENFORCEMENT

- Branch protection on main (force-push disabled)
- CODEOWNERS requires Jim approval for any of the above paths
- Pre-commit hook flags modifications to PROTECTED files (warns + blocks)
- Every CC session at startup loads this file (it's in the standard prompt)

Last updated: 2026-05-13