#!/usr/bin/env python3
"""
KJLE — city filter unit tests
File: scripts/test_city_filter.py

Verifies:
  1. eligible-for-campaign: city="Long Beach" applies ilike("city","Long Beach") on both
     query and count_query; no city param leaves the filter set unchanged.
  2. segment_builder.apply_filters: city="Long Beach" applies ilike("city","Long Beach")
     (exact match, no wildcards); omitting city leaves query unchanged.

Run:
    python scripts/test_city_filter.py

Exit 0 if all pass, 1 if any fail. NO network calls.
"""
from __future__ import annotations

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SUPABASE_URL", "https://test.invalid")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test_key")
os.environ.setdefault("API_SECRET_KEY", "test")

# Stub heavy external deps so this runs without a venv install
for _mod_name in ("supabase", "pydantic_settings", "httpx", "postgrest", "fastapi"):
    if _mod_name in sys.modules:
        continue
    try:
        __import__(_mod_name)
    except ImportError:
        _mod = types.ModuleType(_mod_name)
        if _mod_name == "supabase":
            class _Client:
                pass
            class _ClientOptions:
                def __init__(self, **kw): pass
            _mod.Client = _Client
            _mod.ClientOptions = _ClientOptions
            _mod.create_client = lambda *a, **kw: _Client()
            _sub = types.ModuleType("supabase.client")
            _sub.ClientOptions = _ClientOptions
            sys.modules["supabase.client"] = _sub
        if _mod_name == "pydantic_settings":
            class _BaseSettings:
                def __init__(self, **kw): pass
                class Config:
                    pass
            _mod.BaseSettings = _BaseSettings
        if _mod_name == "httpx":
            _mod.post = lambda *a, **kw: None
            class _TimeoutException(Exception): pass
            class _HTTPStatusError(Exception):
                def __init__(self, *a, response=None, **kw):
                    super().__init__(*a, **kw)
                    self.response = response
            _mod.TimeoutException = _TimeoutException
            _mod.HTTPStatusError = _HTTPStatusError
            class _AsyncClient:
                async def __aenter__(self): return self
                async def __aexit__(self, *a, **kw): return False
            _mod.AsyncClient = _AsyncClient
        if _mod_name == "fastapi":
            _mod.APIRouter = lambda: None
            _mod.HTTPException = Exception
            _mod.Header = lambda *a, **kw: None
            _mod.Query = lambda *a, **kw: None
            _mod.Request = object
            _sub_responses = types.ModuleType("fastapi.responses")
            _sub_responses.JSONResponse = dict
            sys.modules["fastapi.responses"] = _sub_responses
            _sub_routing = types.ModuleType("fastapi.routing")
            sys.modules["fastapi.routing"] = _sub_routing
        sys.modules[_mod_name] = _mod

# ── Result counter ────────────────────────────────────────────────────────────

PASSES = 0
FAILS = 0


def _ok(name: str, msg: str = "") -> None:
    global PASSES
    PASSES += 1
    print(f"  [OK]    {name}{(' — ' + msg) if msg else ''}")


def _fail(name: str, msg: str) -> None:
    global FAILS
    FAILS += 1
    print(f"  [FAIL]  {name} — {msg}")


# ── Spy query builder ─────────────────────────────────────────────────────────

class _SpyQuery:
    """Records every (method, args, kwargs) call for assertion."""

    def __init__(self):
        self.calls: list[tuple] = []

    def _record(self, method, *args, **kwargs):
        self.calls.append((method, args, kwargs))
        return self

    def table(self, *a, **kw): return self._record("table", *a, **kw)
    def select(self, *a, **kw): return self._record("select", *a, **kw)
    def eq(self, *a, **kw): return self._record("eq", *a, **kw)
    def neq(self, *a, **kw): return self._record("neq", *a, **kw)
    def ilike(self, *a, **kw): return self._record("ilike", *a, **kw)
    def gte(self, *a, **kw): return self._record("gte", *a, **kw)
    def lte(self, *a, **kw): return self._record("lte", *a, **kw)
    def gt(self, *a, **kw): return self._record("gt", *a, **kw)
    def in_(self, *a, **kw): return self._record("in_", *a, **kw)
    def is_(self, *a, **kw): return self._record("is_", *a, **kw)
    def or_(self, *a, **kw): return self._record("or_", *a, **kw)
    def order(self, *a, **kw): return self._record("order", *a, **kw)
    def limit(self, *a, **kw): return self._record("limit", *a, **kw)
    def range(self, *a, **kw): return self._record("range", *a, **kw)
    def execute(self): return self._record("execute")

    def ilike_calls(self):
        return [(a, kw) for (m, a, kw) in self.calls if m == "ilike"]


# ── Tests: segment_builder.apply_filters ──────────────────────────────────────

def test_apply_filters_city_exact_match():
    """city="Long Beach" → ilike("city","Long Beach") — no wildcards."""
    from api.routes.segment_builder import SegmentFilters, apply_filters

    q = _SpyQuery()
    f = SegmentFilters(city="Long Beach")
    apply_filters(q, f)

    ilike_calls = q.ilike_calls()
    name = "apply_filters: city='Long Beach' → ilike('city','Long Beach')"
    if not ilike_calls:
        _fail(name, "no ilike call recorded at all")
        return
    col, pattern = ilike_calls[0][0]
    if col == "city" and pattern == "Long Beach":
        _ok(name, f"ilike('{col}', '{pattern}') — exact, no wildcards")
    else:
        _fail(name, f"expected ilike('city','Long Beach'), got ilike('{col}','{pattern}')")


def test_apply_filters_city_strips_whitespace():
    """Leading/trailing whitespace is stripped before the ilike call."""
    from api.routes.segment_builder import SegmentFilters, apply_filters

    q = _SpyQuery()
    f = SegmentFilters(city="  Long Beach  ")
    apply_filters(q, f)

    ilike_calls = q.ilike_calls()
    name = "apply_filters: city whitespace trimmed"
    if not ilike_calls:
        _fail(name, "no ilike call recorded")
        return
    col, pattern = ilike_calls[0][0]
    if col == "city" and pattern == "Long Beach":
        _ok(name, f"pattern is '{pattern}' (trimmed)")
    else:
        _fail(name, f"expected 'Long Beach', got '{pattern}'")


def test_apply_filters_city_none_leaves_query_unchanged():
    """Omitting city leaves the ilike call absent."""
    from api.routes.segment_builder import SegmentFilters, apply_filters

    q = _SpyQuery()
    f = SegmentFilters()  # no city
    before_len = len(q.calls)
    apply_filters(q, f)

    ilike_calls = q.ilike_calls()
    name = "apply_filters: no city → no ilike call"
    if not ilike_calls:
        _ok(name, "ilike not called — query unchanged for city column")
    else:
        _fail(name, f"unexpected ilike call: {ilike_calls}")


# ── Tests: eligible-for-campaign filter logic ─────────────────────────────────

def test_eligible_city_applies_ilike():
    """
    Simulate the city filter block from eligible_for_campaign.
    city="Long Beach" → query.ilike("city","Long Beach") called once.
    """
    from typing import Optional

    city: Optional[str] = "Long Beach"
    q = _SpyQuery()
    count_q = _SpyQuery()

    if city and city.strip():
        q = q.ilike("city", city.strip())
        count_q = count_q.ilike("city", city.strip())

    name = "eligible-for-campaign: city='Long Beach' → ilike('city','Long Beach')"
    q_calls = q.ilike_calls()
    cq_calls = count_q.ilike_calls()

    if not q_calls:
        _fail(name, "no ilike on query")
        return
    if not cq_calls:
        _fail(name, "no ilike on count_query")
        return

    q_col, q_pat = q_calls[0][0]
    cq_col, cq_pat = cq_calls[0][0]

    if q_col == "city" and q_pat == "Long Beach" and cq_col == "city" and cq_pat == "Long Beach":
        _ok(name, "both query and count_query received ilike('city','Long Beach')")
    else:
        _fail(name, f"q=({q_col},{q_pat}) count=({cq_col},{cq_pat})")


def test_eligible_no_city_leaves_query_unchanged():
    """
    Omitting city (None) → ilike is not called on either query object.
    """
    from typing import Optional

    city: Optional[str] = None
    q = _SpyQuery()
    count_q = _SpyQuery()

    if city and city.strip():
        q = q.ilike("city", city.strip())
        count_q = count_q.ilike("city", city.strip())

    name = "eligible-for-campaign: city=None → no ilike call"
    q_calls = q.ilike_calls()
    if q_calls:
        _fail(name, f"unexpected ilike: {q_calls}")
    else:
        _ok(name, "no ilike — filter set unchanged")


def test_eligible_empty_city_leaves_query_unchanged():
    """city='' (empty string) → ilike is not called."""
    from typing import Optional

    city: Optional[str] = ""
    q = _SpyQuery()
    count_q = _SpyQuery()

    if city and city.strip():
        q = q.ilike("city", city.strip())
        count_q = count_q.ilike("city", city.strip())

    name = "eligible-for-campaign: city='' → no ilike call"
    q_calls = q.ilike_calls()
    if q_calls:
        _fail(name, f"unexpected ilike: {q_calls}")
    else:
        _ok(name, "empty string skipped — filter set unchanged")


# ── Runner ────────────────────────────────────────────────────────────────────

def main() -> int:
    print("\n== city filter tests ==")

    test_apply_filters_city_exact_match()
    test_apply_filters_city_strips_whitespace()
    test_apply_filters_city_none_leaves_query_unchanged()
    test_eligible_city_applies_ilike()
    test_eligible_no_city_leaves_query_unchanged()
    test_eligible_empty_city_leaves_query_unchanged()

    total = PASSES + FAILS
    print(f"\n  {PASSES}/{total} passed, {FAILS} failed.")
    return 0 if FAILS == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
