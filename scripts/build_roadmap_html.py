#!/usr/bin/env python3
"""
build_roadmap_html.py — single source of truth for KJ_EMPIRE_ROADMAP.html

The .md is the ONLY file a human edits. This script regenerates the .html from it.

Two responsibilities:
  1. VALIDATE the YAML front-matter at the top of KJ_EMPIRE_ROADMAP.md.
     - Front-matter must be present (delimited by `---` ... `---`).
     - It must contain a non-empty `project:` key.
     The empire dashboard (jharriGH.github.io/empire-dashboard) parses this YAML to
     render KJLE's card. A dropped/blank `project:` silently breaks that dashboard,
     so this is a HARD failure.
  2. RENDER the markdown body into a themed, glanceable HTML dashboard that reuses the
     existing KJ_EMPIRE_ROADMAP.html color theme. The .html is a build artifact and is
     never hand-edited.

Usage:
  python scripts/build_roadmap_html.py            # validate + write the .html
  python scripts/build_roadmap_html.py --check    # validate + verify .html is in sync
                                                   #   (exit 1 if missing/out-of-date; no write)

Exit codes:
  0  success (written, or --check passed)
  2  front-matter invalid (missing block or missing/blank `project:`)
  3  --check found the .html missing or out of sync
  4  input .md not found / dependency missing
"""
from __future__ import annotations

import argparse
import html
import sys
from pathlib import Path

try:
    import yaml  # pyyaml
    import markdown  # markdown
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(
        f"ERROR: missing dependency ({exc}). Install with: pip install pyyaml markdown\n"
    )
    sys.exit(4)

REPO_ROOT = Path(__file__).resolve().parent.parent
MD_PATH = REPO_ROOT / "KJ_EMPIRE_ROADMAP.md"
HTML_PATH = REPO_ROOT / "KJ_EMPIRE_ROADMAP.html"

# Status value -> CSS color token (matches the existing theme palette).
STATUS_COLORS = {
    "active": "var(--green)",
    "live": "var(--green)",
    "in_progress": "var(--yellow)",
    "blocked": "var(--red)",
    "parked": "var(--pause)",
    "paused": "var(--pause)",
    "scoped": "var(--blue)",
}


def split_front_matter(text: str) -> tuple[dict, str]:
    """Return (front_matter_dict, body). Raises ValueError if the block is malformed."""
    if not text.startswith("---"):
        raise ValueError(
            "YAML front-matter block is missing. The file must start with a `---` line, "
            "the YAML keys, then a closing `---` line."
        )
    # First line is the opening `---`. Find the closing `---` on its own line.
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        raise ValueError(
            "YAML front-matter is not closed. Add a `---` line after the YAML keys."
        )
    raw_yaml = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    try:
        data = yaml.safe_load(raw_yaml) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML front-matter failed to parse: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("YAML front-matter must be a mapping of key: value pairs.")
    return data, body


def validate_front_matter(data: dict) -> None:
    """Hard-fail if the dashboard contract is broken."""
    project = data.get("project")
    if project is None or (isinstance(project, str) and not project.strip()):
        raise ValueError(
            "Required `project:` key is missing or blank in the YAML front-matter. "
            "The empire dashboard parses this to render the project card — it cannot be dropped."
        )


def _esc(v) -> str:
    return html.escape(str(v))


def build_status_card(data: dict) -> str:
    project = _esc(data.get("project", "?"))
    status = str(data.get("status", "unknown")).lower()
    color = STATUS_COLORS.get(status, "var(--text-dim)")

    rows = []

    def row(label: str, value) -> None:
        if value in (None, "", []):
            return
        if isinstance(value, list):
            value = ", ".join(_esc(x) for x in value)
        else:
            value = _esc(value)
        rows.append(
            f'<div class="meta-row"><span class="meta-k">{_esc(label)}</span>'
            f'<span class="meta-v">{value}</span></div>'
        )

    row("Sprint", data.get("current_sprint"))
    row("Target date", data.get("sprint_target_date"))
    row("Last updated", data.get("last_updated"))
    row("Integrates with", data.get("integrates_with"))

    spent = data.get("cost_spent")
    remaining = data.get("cost_remaining")
    if spent is not None or remaining is not None:
        parts = []
        if spent is not None:
            parts.append(f"${_esc(spent)} spent")
        if remaining is not None:
            parts.append(f"${_esc(remaining)} remaining")
        rows.append(
            '<div class="meta-row"><span class="meta-k">Budget</span>'
            f'<span class="meta-v">{" · ".join(parts)}</span></div>'
        )

    sc = data.get("sc_contact")
    if sc:
        row("SC contact", sc)

    notes = data.get("notes")
    desc = data.get("description")
    notes_html = ""
    if desc:
        notes_html += f'<p class="card-desc">{_esc(desc)}</p>'
    if notes:
        notes_html += f'<p class="card-notes">{_esc(notes)}</p>'

    return f"""<div class="status-card">
  <div class="card-head">
    <span class="proj">{project}</span>
    <span class="status-badge" style="color:{color};border-color:{color}">{_esc(status)}</span>
  </div>
  {notes_html}
  <div class="meta-grid">
    {''.join(rows)}
  </div>
</div>"""


def render_body(body_md: str) -> str:
    md = markdown.Markdown(
        extensions=["tables", "fenced_code", "sane_lists", "attr_list", "toc"]
    )
    return md.convert(body_md)


# Theme reproduced from the existing KJ_EMPIRE_ROADMAP.html (:root tokens + base styles).
THEME_CSS = """
  :root {
    --bg:#0a0e1a; --panel:#111827; --panel-2:#1a2236; --border:#233048;
    --text:#e5e7eb; --text-dim:#9ca3af; --text-faint:#6b7280;
    --green:#10b981; --yellow:#f59e0b; --blue:#3b82f6; --grey:#6b7280;
    --pause:#a78bfa; --red:#ef4444; --warn:#f97316; --cyan:#06b6d4; --accent:#fbbf24;
  }
  * { margin:0; padding:0; box-sizing:border-box; }
  html, body { background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; line-height:1.55; }
  .wrap { max-width:1100px; margin:0 auto; padding:32px 24px 64px; }
  a { color:var(--cyan); text-decoration:none; }
  a:hover { text-decoration:underline; }

  header h1 { font-size:28px; font-weight:600; color:var(--accent); letter-spacing:-0.5px; margin-bottom:4px; }
  header .gen { color:var(--text-faint); font-size:12px; font-family:'SF Mono',Menlo,monospace; }

  .status-card { background:var(--panel); border:1px solid var(--border); border-radius:8px;
    padding:18px 20px; margin:18px 0 28px; }
  .status-card .card-head { display:flex; align-items:center; gap:12px; margin-bottom:8px; }
  .status-card .proj { font-size:20px; font-weight:600; color:var(--text); }
  .status-card .status-badge { font-size:11px; text-transform:uppercase; letter-spacing:0.5px;
    border:1px solid; border-radius:10px; padding:2px 10px; font-weight:600; }
  .card-desc { color:var(--text-dim); font-size:13px; margin-bottom:6px; }
  .card-notes { color:var(--text-faint); font-size:12px; margin-bottom:12px; font-style:italic; }
  .meta-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:4px 24px; }
  .meta-row { display:flex; justify-content:space-between; gap:12px; font-size:12px;
    padding:3px 0; border-bottom:1px dashed var(--border); }
  .meta-k { color:var(--text-faint); }
  .meta-v { color:var(--text-dim); text-align:right; }

  .body h1 { font-size:22px; color:var(--accent); margin:28px 0 10px; }
  .body h2 { font-size:16px; font-weight:600; color:var(--text); margin:26px 0 12px;
    padding-bottom:8px; border-bottom:1px solid var(--border); }
  .body h3 { font-size:14px; font-weight:600; color:var(--text-dim); margin:18px 0 8px; }
  .body p { color:var(--text-dim); font-size:13px; margin:8px 0; }
  .body ul, .body ol { color:var(--text-dim); font-size:13px; margin:8px 0 8px 22px; }
  .body li { margin:3px 0; }
  .body strong { color:var(--text); }
  .body hr { border:0; border-top:1px solid var(--border); margin:24px 0; }
  .body blockquote { border-left:3px solid var(--accent); padding:4px 14px; margin:12px 0;
    color:var(--text-dim); background:var(--panel); border-radius:0 6px 6px 0; }
  .body code { background:var(--panel-2); color:var(--cyan); padding:2px 6px; border-radius:4px;
    font-family:'SF Mono',Menlo,monospace; font-size:12px; }
  .body pre { background:var(--panel); border:1px solid var(--border); border-radius:6px;
    padding:12px 14px; overflow-x:auto; margin:12px 0; }
  .body pre code { background:none; padding:0; color:var(--text); }

  .body table { width:100%; border-collapse:collapse; margin:14px 0; font-size:12.5px; }
  .body th, .body td { text-align:left; padding:7px 10px; border:1px solid var(--border);
    vertical-align:top; }
  .body th { background:var(--panel-2); color:var(--accent); font-weight:600; }
  .body td { color:var(--text-dim); background:var(--panel); }
  .body tr:hover td { background:var(--panel-2); }

  footer { margin-top:40px; padding-top:16px; border-top:1px solid var(--border);
    color:var(--text-faint); font-size:11px; }
"""

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<!-- GENERATED FILE — do not edit by hand. Source: KJ_EMPIRE_ROADMAP.md -->
<!-- Regenerate with: python scripts/build_roadmap_html.py -->
<style>{css}</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>👑 KJ Empire — Roadmap</h1>
    <div class="gen">generated from KJ_EMPIRE_ROADMAP.md — do not edit this file by hand</div>
  </header>
  {status_card}
  <div class="body">
{body}
  </div>
  <footer>Auto-generated by scripts/build_roadmap_html.py · edit the .md, the .html rebuilds itself.</footer>
</div>
</body>
</html>
"""


def generate_html(text: str) -> str:
    data, body_md = split_front_matter(text)
    validate_front_matter(data)
    title = f"KJ Empire — {data.get('project', 'Roadmap')} Roadmap"
    return PAGE_TEMPLATE.format(
        title=_esc(title),
        css=THEME_CSS,
        status_card=build_status_card(data),
        body=render_body(body_md),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build KJ_EMPIRE_ROADMAP.html from the .md")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate front-matter and verify the .html is in sync. Does not write. "
        "Exit 3 if the .html is missing or out of date.",
    )
    parser.add_argument("--md", default=str(MD_PATH), help="Path to the roadmap .md")
    parser.add_argument("--html", default=str(HTML_PATH), help="Path to the roadmap .html")
    args = parser.parse_args()

    md_path = Path(args.md)
    html_path = Path(args.html)

    if not md_path.exists():
        sys.stderr.write(f"ERROR: roadmap markdown not found at {md_path}\n")
        return 4

    text = md_path.read_text(encoding="utf-8")

    try:
        new_html = generate_html(text)
    except ValueError as exc:
        sys.stderr.write(f"FRONT-MATTER VALIDATION FAILED: {exc}\n")
        return 2

    if args.check:
        if not html_path.exists():
            sys.stderr.write(
                f"OUT OF SYNC: {html_path.name} does not exist. "
                "Run: python scripts/build_roadmap_html.py\n"
            )
            return 3
        current = html_path.read_text(encoding="utf-8")
        if current != new_html:
            sys.stderr.write(
                f"OUT OF SYNC: {html_path.name} does not match the regenerated output. "
                "It was likely hand-edited or the .md changed. "
                "Run: python scripts/build_roadmap_html.py\n"
            )
            return 3
        print(f"OK: front-matter valid and {html_path.name} is in sync.")
        return 0

    html_path.write_text(new_html, encoding="utf-8")
    print(f"OK: front-matter valid. Wrote {html_path.name} ({len(new_html):,} bytes).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
