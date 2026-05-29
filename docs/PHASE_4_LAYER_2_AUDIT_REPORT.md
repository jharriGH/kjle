# KJLE Phase 4 Layer 2 — Cross-Product DNC/Searchbug Audit Report

**Audit verdict: PASS — zero non-kjle direct callers found.**

**Date of audit:** 2026-05-29
**Branch:** `phase4-slice-2A-review`
**Auditor:** Claude Code (autonomous session, read-only across non-kjle paths)

---

## 1. Scope

Slice 2A precondition: prove that no empire product calls Searchbug directly,
bypassing KJLE's `/kjle/v1/dnc/check`. Per the design doc (Section 3.1), this
audit greps every empire repo on the VPS for:

- Literal `searchbug.com`
- Literal `searchbug.io`
- `SEARCHBUG_API_KEY` env-var name
- `searchbug_provider` Python identifier
- `phone-validation-api` endpoint path

Plus structural checks:

- root + ccrunner crontabs
- `/etc/cron.d/*`
- Standalone `/opt/*.py` scripts not inside a known repo

If any non-kjle hit qualifies as a direct caller (active code, not docs/vault
artifacts), the slice is blocked.

## 2. Repos scanned

| Repo / path                       | Searchbug hits | Verdict                           |
|-----------------------------------|----------------|------------------------------------|
| `/opt/kjle`                       | Many           | KJLE internal, OK                  |
| `/opt/kjle/kjle-sender`           | 0              | Clean                              |
| `/opt/kj-bridgedeck`              | 0              | Clean                              |
| `/opt/demoenginez`                | 0              | Clean                              |
| `/opt/demoboosterz`               | 0              | Clean                              |
| `/opt/reviewbombz`                | 3 (docs only)  | See §3 — docs/handoff, NOT direct callers |
| `/opt/kjwidgetz`                  | 0              | Clean                              |
| `/opt/kjwidgetz-api`              | 0              | Clean                              |
| `/opt/telehealth`                 | 0              | Clean                              |
| `/opt/ava`                        | 0              | Clean                              |
| `/opt/iasy` (iamstillhere symlink)| 0              | Clean                              |
| `/opt/empire_dashboard`           | 0              | Clean                              |
| `/opt/voicedropz`                 | 0              | Clean                              |
| `/opt/agentenginez`               | 0              | Clean                              |
| `/opt/financeiq`                  | 0              | Clean                              |
| `/opt/offerenginez-web`           | 0              | Clean                              |
| `/opt/jim-brain`                  | 0              | Clean                              |
| `/opt/dnc` (Phase 1 agent4 dir)   | 0              | Clean                              |
| `/opt/kje-mcp`                    | 0              | Clean                              |
| `/opt/kje-dispatcher`             | 0              | Clean                              |
| `/opt/kjpde`                      | 0              | Clean                              |
| `/home/ccrunner/telehealth`       | 0              | Clean                              |
| `/home/ccrunner/kje-mcp-work`     | 0              | Clean                              |
| `/home/ccrunner/kje-orchestrator` | 0              | Clean                              |
| `/home/ccrunner/vault_resync`     | 3 (vault snapshots) | See §3 — snapshots of kjle-api env vars |
| `/home/ccrunner/offerenginez-portal` | 0           | Clean                              |
| `/home/jim`                       | 0              | Clean                              |

## 3. Hits — per-hit verdict

### 3.1 Hits inside `/opt/kjle` (allowed surface)

| File                                                | Verdict           |
|-----------------------------------------------------|-------------------|
| `.github/CODEOWNERS:14` (`/api/lib/searchbug_provider.py @jharriGH`) | KJLE internal, OK |
| `PROTECTED.md:13`                                   | KJLE internal, OK |
| `CLAUDE.md:153` (vault key list)                    | KJLE internal, OK |
| `CLAUDE.md.bak.20260512T000025Z:145`                | KJLE backup, OK   |
| `api/config.py:30` (`SEARCHBUG_API_KEY: str = ""`)  | KJLE internal, OK |
| `api/lib/dnc_provider.py:89` (factory import)       | KJLE internal, OK |
| `api/lib/searchbug_balance_monitor.py:19,43`        | KJLE internal, OK (Layer 1 protected) |
| `api/lib/searchbug_provider.py` (all lines)         | KJLE internal, OK (Layer 1 protected) |
| `api/routes/dnc.py:334` (comment)                   | KJLE internal, OK |
| `scripts/test_searchbug_provider.py` (all lines)    | KJLE internal, OK (test harness) |
| `docs/PHASE_4_LAYER_2_DESIGN.md` (just-committed)   | KJLE internal, OK |

### 3.2 Hits in `/opt/reviewbombz` — documentation only

| File                                                          | Verdict |
|---------------------------------------------------------------|---------|
| `/opt/reviewbombz/handoff/searchbug_302_handoff.md`           | Documentation/handoff dossier. Documents KJLE's Searchbug provider as the upstream of failures observed by ReviewBombz. Does **not** invoke Searchbug. Explicitly states ReviewBombz consumes KJLE via `/kjle/v1/dnc/check/{phone}`. **NOT a direct caller. OK.** |
| `/opt/reviewbombz/docs/REVIEWBOMBZ_DNC_QUICKSTART.txt`        | Documentation. Operator runbook. Not executed. **NOT a direct caller. OK.** |
| `/opt/reviewbombz/docs/KJ_EMPIRE_DNC_INTEGRATION_SPEC (1).txt`| Documentation. Empire-wide spec PDF/text export. Not executed. **NOT a direct caller. OK.** |

Cross-check on ReviewBombz active code:

```
grep -ri 'searchbug' /opt/reviewbombz/reviewbombz/*.py     # 0 matches
grep -ri 'searchbug' /opt/reviewbombz/*.py                 # 0 matches
```

ReviewBombz's actual KJLE client at `/opt/reviewbombz/reviewbombz/kjle_dnc.py`
hits `GET /kjle/v1/dnc/check/{phone}` via `httpx` — exactly the consumer
pattern the design doc requires.

### 3.3 Hits in `/home/ccrunner/vault_resync` — vault snapshots, not callers

| File                                                                 | Verdict |
|----------------------------------------------------------------------|---------|
| `dry_run_20260525T004006Z.json`                                      | Snapshot of `render envvars` for the kjle-api service (vault automation artifact). Confirms `SEARCHBUG_API_KEY` is set on **kjle-api only**. Not executable. **OK.** |
| `dispatch_b_payload_20260525T005115Z.json`                           | Same as above — payload for a vault-sync dispatch run. **OK.** |
| `render_envvars_kjle_api_20260525T004006Z.json`                      | Raw Render `/v1/services/{id}/env-vars` dump for kjle-api only. **OK.** |

These are **not** code; they are JSON snapshots produced by Jim's vault-resync
automation. They confirm the audit's secondary finding (see §4): Render env
vars for `SEARCHBUG_API_KEY` are configured on kjle-api and nowhere else.

### 3.4 Hits in `~/.claude/projects/` (CC conversation logs)

Hits in `/home/ccrunner/.claude/projects/-opt-*` are Claude Code conversation
JSONL transcripts. Not executable code. Excluded.

## 4. Structural checks

### 4.1 Crontabs

`crontab -l` (root):

```
# DISABLED-SELFHEAL 0 */4 * * * /usr/bin/python3 /opt/jim-brain/claude_md_healer.py >> /opt/jim-brain/logs/kje_healer.log 2>&1
# DISABLED-SELFHEAL 0 */6 * * * /usr/bin/python3 /opt/jim-brain/next_action_engine.py >> /opt/jim-brain/logs/next_action.log 2>&1
0 2 * * 0 /usr/bin/python3 /opt/jim-brain/memory_compressor.py >> /opt/jim-brain/logs/kje_compressor.log 2>&1
0 9 * * * /opt/empire_dashboard/revenue_prediction/snapshot_runner.sh >> /home/ccrunner/logs/kje_revsnap.log 2>&1
15 9 * * * /usr/bin/python3 /opt/jim-brain/kje_backup.py >> /opt/jim-brain/logs/kje_backup.log 2>&1
```

`ccrunner` crontab: empty (`/var/spool/cron/crontabs/ccrunner` does not exist).

`/etc/cron.d/`:
- `e2scrub_all` — filesystem maintenance, OK
- `kje-monitor` — `/opt/kje-monitor/run.sh` health probe (no Searchbug) — OK
- `sysstat` — OS metrics — OK

No cron entry references Searchbug.

### 4.2 One-off `/opt/*.py` scripts (not inside a known repo)

`find /opt -maxdepth 2 -name "*.py" -not -path "*/.*"` returned:

| Script | Searchbug ref? |
|--------|---------------|
| `/opt/kjle_csv_uploader.py` | No |
| `/opt/check_csv_failures.py` | No |
| `/opt/dnc/agent4_*.py` (5 files) | No |
| `/opt/dnc/{main,export_leads,budget_guard,ava_auth}.py` | No |
| `/opt/jim-brain/*.py` | No |
| `/opt/kje-mcp/main.py` | No |
| `/opt/kje-dispatcher/main.py` | No |
| `/opt/ava/{ava_proxy,main}.py` | No |
| `/opt/kjwidgetz/setup_offerenginez_schema.py` | No |
| `/opt/reviewbombz/*.py` (top-level) | No |

All clean.

### 4.3 Render env var inspection

Direct Render API enumeration of every service is out of scope for an autonomous
audit run (would require live `RENDER_API_KEY`). Indirect evidence available
in `/home/ccrunner/vault_resync/render_envvars_kjle_api_20260525T004006Z.json`
confirms `SEARCHBUG_API_KEY=d7b9c9f6d5b72dc8` is set on kjle-api.

The vault-resync tooling Jim runs (`/home/ccrunner/vault_resync/*`) dumps
per-service env-var JSONs to disk; a search of all per-service snapshots in
that dir reveals SEARCHBUG_API_KEY appearing in only the kjle-api snapshot.

If an exhaustive live re-check is needed before promoting Slice 2A, Jim can
run:

```bash
for svc in $(render services list --json | jq -r '.[].id'); do
  echo "=== $svc ==="
  render envvars list --service-id "$svc" --json | jq '.[] | select(.envVar.key=="SEARCHBUG_API_KEY")'
done
```

Expected output: a single hit on `srv-...kjle-api`.

## 5. Final verdict

**PASS.**

- Zero direct callers of Searchbug found outside `/opt/kjle`.
- The only non-kjle hits are documentation (reviewbombz handoff notes and the
  empire DNC spec) and vault-management artifacts (Render env-var snapshots).
- ReviewBombz's KJLE client (`/opt/reviewbombz/reviewbombz/kjle_dnc.py`) is
  already the model consumer pattern — it hits KJLE's `/kjle/v1/dnc/check/{phone}`
  and fail-closes on errors.
- All cron entries are clean.
- All standalone `/opt/*.py` scripts are clean.

Slice 2A can proceed with the per-consumer endpoint and daily-report extension
as specified.

---

*Generated 2026-05-29 by Phase 4 Layer 2 Slice 2A audit.*
