#!/usr/bin/env bash
#
# submit_roadmap_update.sh — hands-off roadmap ingest.
#
# An SC runs this ONE command at the end of a session, after editing
# KJ_EMPIRE_ROADMAP.md. It validates locally, branches, commits ONLY the .md,
# pushes, and opens an auto-merging PR. CI regenerates the .html and merges on green.
#
# PRECONDITION: run from the repo root, on an up-to-date `main`, with your edit to
# KJ_EMPIRE_ROADMAP.md present as an uncommitted working-tree change and nothing else changed.
#
# Usage: scripts/submit_roadmap_update.sh "short message about the update"
#
set -euo pipefail

MD="KJ_EMPIRE_ROADMAP.md"
MSG="${1:-automated roadmap update}"

[ -f "$MD" ] || { echo "ERROR: $MD not found — run from the repo root." >&2; exit 1; }

# Is there actually a change to submit?
if git diff --quiet -- "$MD" && git diff --quiet --cached -- "$MD"; then
  echo "No local changes to $MD — nothing to submit."
  exit 0
fi

# Local validation gate: refuse to push a roadmap whose YAML/project: is broken.
if ! python3 scripts/build_roadmap_html.py --md "$MD" --html /tmp/_roadmap_validate.html >/dev/null; then
  echo "ERROR: roadmap front-matter failed validation — not pushing. Fix the YAML / project: key and retry." >&2
  exit 2
fi

BRANCH="roadmap/update-$(date -u +%Y%m%dT%H%M%SZ)"
git checkout -b "$BRANCH"
git add -- "$MD"            # ONLY the .md — never -A, never the .html (CI regenerates it)
git commit -m "roadmap: ${MSG}"
git push -u origin "$BRANCH"

if command -v gh >/dev/null 2>&1; then
  gh pr create --base main --head "$BRANCH" \
    --title "roadmap: ${MSG}" \
    --body "Automated roadmap update. Changes KJ_EMPIRE_ROADMAP.md only; CI regenerates the .html and auto-merges on a green roadmap-sync check."
  gh pr merge "$BRANCH" --auto --squash
  echo "PR opened and auto-merge armed for ${BRANCH}."
else
  echo "Branch ${BRANCH} pushed. 'gh' is not installed — open the PR in the GitHub UI; it will auto-merge on green." >&2
fi
