"""
Boot-check test suite for email cleaning v2 parser + ingestion.

Run:  python scripts/test_email_clean_parser.py
Pass: every assertion green, exit 0.

Mirrors the discipline of scripts/test_pain_score.py — pure functions
exercised against hand-crafted fixtures covering every Truelist result
code we've observed in the smoke test + edge cases (empty, prefixed
forms, unknown variants, NULL).

This script self-stubs the api.* module imports so it can run without
booting FastAPI (we only need the pure parsing helpers).
"""

from __future__ import annotations

import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Stub out fastapi/httpx/pydantic/supabase so the route module imports cleanly.
# We're only testing PURE functions — parse_truelist_state, is_campaign_eligible,
# _parse_annotated_csv. No HTTP, no DB.

def _stub(name: str, attrs: dict | None = None) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in (attrs or {}).items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _Router:
    def __init__(self, *a, **kw): pass
    def get(self, *a, **kw): return lambda f: f
    def post(self, *a, **kw): return lambda f: f
    def delete(self, *a, **kw): return lambda f: f


_stub("fastapi", {
    "APIRouter": _Router,
    "Depends":   lambda fn: fn,
    "HTTPException": type("HTTPException", (Exception,), {"__init__": lambda self, **kw: None}),
    "Query":     lambda *a, **kw: None,
    "Header":    lambda *a, **kw: None,
    "Request":   object,
})
_stub("httpx", {
    "AsyncClient": object,
})
_stub("pydantic", {
    "BaseModel": type("BaseModel", (), {"__init_subclass__": lambda *a, **k: None}),
})
_stub("supabase", {
    "create_client": lambda *a, **kw: None,
    "Client":        object,
})

# Allow `from api.routes...` resolution against the real package on disk
api_pkg = _stub("api")
api_pkg.__path__ = [os.path.join(ROOT, "api")]
api_routes_pkg = _stub("api.routes")
api_routes_pkg.__path__ = [os.path.join(ROOT, "api", "routes")]
_stub("api.config", {"settings": types.SimpleNamespace(TRUELIST_API_KEY="")})
_stub("api.database", {"get_db": lambda: None})

# Now import the module under test
from api.routes.enrichment_email_clean import (  # noqa: E402
    parse_truelist_state,
    is_campaign_eligible,
    _parse_annotated_csv,
)


# ─────────────────────────────────────────────────────────────────────────────
# Test runner
# ─────────────────────────────────────────────────────────────────────────────

PASS = 0
FAIL = 0

def expect(label: str, got, want):
    global PASS, FAIL
    ok = got == want
    if ok:
        PASS += 1
        print(f"  [PASS] {label}: {got!r}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}: got {got!r}, want {want!r}")


# ─────────────────────────────────────────────────────────────────────────────
# parse_truelist_state — clean batch-CSV vocab
# ─────────────────────────────────────────────────────────────────────────────

print("\n# parse_truelist_state — batch CSV vocabulary")
expect("ok",         parse_truelist_state("ok"),       ("valid",   True))
expect("OK upper",   parse_truelist_state("OK"),       ("valid",   True))
expect("invalid",    parse_truelist_state("invalid"),  ("invalid", False))
expect("risky",      parse_truelist_state("risky"),    ("unknown", None))
expect("unknown",    parse_truelist_state("unknown"),  ("unknown", None))
expect("trimmed",    parse_truelist_state("  ok  "),   ("valid",   True))


# ─────────────────────────────────────────────────────────────────────────────
# parse_truelist_state — verify_inline prefixed forms (email_*)
# ─────────────────────────────────────────────────────────────────────────────

print("\n# parse_truelist_state — verify_inline prefixed forms")
expect("email_ok",        parse_truelist_state("email_ok"),      ("valid",   True))
expect("email_invalid",   parse_truelist_state("email_invalid"), ("invalid", False))
expect("email_risky",     parse_truelist_state("email_risky"),   ("unknown", None))
expect("email_unknown",   parse_truelist_state("email_unknown"), ("unknown", None))


# ─────────────────────────────────────────────────────────────────────────────
# parse_truelist_state — defensive defaults
# ─────────────────────────────────────────────────────────────────────────────

print("\n# parse_truelist_state — defensive defaults (None / empty / garbage)")
expect("None",           parse_truelist_state(None),           ("unknown", None))
expect("empty",          parse_truelist_state(""),             ("unknown", None))
expect("whitespace",     parse_truelist_state("   "),          ("unknown", None))
expect("unknown_label",  parse_truelist_state("greylisted"),   ("unknown", None))
expect("legacy_bad",     parse_truelist_state("bad"),          ("unknown", None))  # v1 vocab no longer matches anything
expect("number",         parse_truelist_state("42"),           ("unknown", None))


# ─────────────────────────────────────────────────────────────────────────────
# is_campaign_eligible — POSITIVE WHITELIST ONLY
#
# This is the bug-prevention test the user explicitly asked for. If anyone
# ever changes the helper to `!= 'invalid'`, these fixtures will fail loudly.
# ─────────────────────────────────────────────────────────────────────────────

print("\n# is_campaign_eligible — POSITIVE WHITELIST ONLY")
expect("valid -> eligible",   is_campaign_eligible("valid"),         True)
expect("invalid -> NO",       is_campaign_eligible("invalid"),       False)
expect("unknown -> NO",       is_campaign_eligible("unknown"),       False)
expect("error -> NO",         is_campaign_eligible("error"),         False)
expect("pending_batch -> NO", is_campaign_eligible("pending_batch"), False)
expect("None -> NO",          is_campaign_eligible(None),            False)
expect("empty -> NO",         is_campaign_eligible(""),              False)
expect("ok (raw) -> NO",      is_campaign_eligible("ok"),            False)  # only KJLE 'valid' counts


# ─────────────────────────────────────────────────────────────────────────────
# _parse_annotated_csv — real CSV from smoke test 2026-05-10
# ─────────────────────────────────────────────────────────────────────────────

print("\n# _parse_annotated_csv — smoke-test CSV format")
SMOKE_CSV = (
    ",Email Address,Did you mean,Email State,Email Sub-State\n"
    "jim@developingriches.com,jim@developingriches.com,,invalid,failed_mx_check\n"
    "test@example.com,test@example.com,,invalid,failed_mx_check\n"
    "noreply@google.com,noreply@google.com,,risky,is_role\n"
    "ceo@apple.com,ceo@apple.com,,risky,accept_all\n"
    "fake-non-existent-domain-xyz123@nowhere-invalid.com,fake-non-existent-domain-xyz123@nowhere-invalid.com,,invalid,failed_mx_check\n"
)
m = _parse_annotated_csv(SMOKE_CSV)
expect("row count",            len(m), 5)
expect("jim state",             m["jim@developingriches.com"]["state"], "invalid")
expect("jim sub",               m["jim@developingriches.com"]["sub_state"], "failed_mx_check")
expect("noreply state",         m["noreply@google.com"]["state"], "risky")
expect("noreply sub",           m["noreply@google.com"]["sub_state"], "is_role")
expect("ceo sub",               m["ceo@apple.com"]["sub_state"], "accept_all")

# Empty CSV
expect("empty csv",             _parse_annotated_csv(""), {})
expect("header only",           _parse_annotated_csv(",Email Address,Did you mean,Email State,Email Sub-State\n"), {})

# Lowercased keys for case-insensitive lookup
expect("lowercased keys",       all(k == k.lower() for k in m), True)


# ─────────────────────────────────────────────────────────────────────────────
# _parse_annotated_csv — defensive against reordered columns
# ─────────────────────────────────────────────────────────────────────────────

print("\n# _parse_annotated_csv — defensive header lookup")
REORDERED_CSV = (
    "Email Sub-State,Email State,Did you mean,Email Address,leading\n"
    "is_role,risky,,info@example.com,info@example.com\n"
)
m2 = _parse_annotated_csv(REORDERED_CSV)
expect("reordered row count",   len(m2), 1)
expect("reordered state",       m2["info@example.com"]["state"], "risky")
expect("reordered sub",         m2["info@example.com"]["sub_state"], "is_role")


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end fixture: simulate ingest mapping for 5 known emails
# ─────────────────────────────────────────────────────────────────────────────

print("\n# End-to-end mapping (parse_truelist_state(csv state) for each row)")
for email, expected in [
    ("jim@developingriches.com",                              ("invalid", False)),
    ("test@example.com",                                       ("invalid", False)),
    ("noreply@google.com",                                     ("unknown", None)),
    ("ceo@apple.com",                                          ("unknown", None)),
    ("fake-non-existent-domain-xyz123@nowhere-invalid.com",    ("invalid", False)),
]:
    state = m[email]["state"]
    expect(f"map[{email}]", parse_truelist_state(state), expected)


# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────

print(f"\n{'='*60}\nRESULTS: {PASS} passed, {FAIL} failed\n{'='*60}")
sys.exit(0 if FAIL == 0 else 1)
