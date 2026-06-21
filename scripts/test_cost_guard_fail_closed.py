#!/usr/bin/env python3
"""
KJLE — cost_guard fail-closed unit tests
File: scripts/test_cost_guard_fail_closed.py

Proves that the F-constraint fix in api/lib/cost_guard.py behaves correctly:
  - Spend-read DB failure → float("inf"), never 0.0 (the old floor-to-zero bug)
  - check_budget → False when spend cannot be read (paid job BLOCKED)
  - log_cost failure → logger.critical fires (loud, not a silent swallow)

Run:
    python scripts/test_cost_guard_fail_closed.py

Exit 0 if all pass, 1 if any fail. NO network calls. Mocks at the DB-client
boundary (forces .execute() to raise) so the REAL except paths in cost_guard
run — not the mocks themselves. _get_today_spend / _get_month_spend are never
patched directly.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# api.config.Settings demands SUPABASE_URL / SUPABASE_SERVICE_KEY at import time.
os.environ.setdefault("SUPABASE_URL", "https://test.invalid")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test_key")

# Stub external libs so this runs in a minimal / CI environment.
# Only the cost_guard module is under test.
_STUB_MODULES = ["supabase", "pydantic_settings", "httpx"]
for _mod_name in _STUB_MODULES:
    if _mod_name in sys.modules:
        continue
    try:
        __import__(_mod_name)
    except ImportError:
        _mod = types.ModuleType(_mod_name)
        if _mod_name == "supabase":
            class _Client: pass
            class _ClientOptions:
                def __init__(self, **kw): pass
            _mod.Client         = _Client
            _mod.ClientOptions  = _ClientOptions
            _mod.create_client  = lambda *a, **kw: _Client()
            # Register the submodule so `from supabase.client import ClientOptions` works.
            # Without this, Python can't traverse the stub (plain Module, not a package).
            _client_submod                  = types.ModuleType("supabase.client")
            _client_submod.ClientOptions    = _ClientOptions
            sys.modules["supabase.client"]  = _client_submod
        if _mod_name == "pydantic_settings":
            class _BaseSettings:
                def __init__(self, **_kw):
                    for k, v in type(self).__dict__.items():
                        if not k.startswith("_") and not callable(v):
                            setattr(self, k, v)
                    self.SUPABASE_URL         = os.environ.get("SUPABASE_URL", "")
                    self.SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
                class Config:
                    pass
            _mod.BaseSettings = _BaseSettings
        if _mod_name == "httpx":
            # cost_guard uses httpx.post for Twilio halt SMS (never reached without
            # TWILIO env vars). Stub so the import succeeds in minimal envs.
            _mod.post = lambda *a, **kw: None
            class _TimeoutException(Exception): pass
            class _HTTPStatusError(Exception):
                def __init__(self, *a, response=None, **kw):
                    super().__init__(*a, **kw)
                    self.response = response
            _mod.TimeoutException = _TimeoutException
            _mod.HTTPStatusError  = _HTTPStatusError
            class _AsyncClient:
                def __init__(self, *a, **kw): pass
                async def __aenter__(self): return self
                async def __aexit__(self, *a, **kw): return False
                async def post(self, *a, **kw): raise RuntimeError("httpx stub")
                async def get(self,  *a, **kw): raise RuntimeError("httpx stub")
            _mod.AsyncClient = _AsyncClient
        sys.modules[_mod_name] = _mod


# ── Result counter ───────────────────────────────────────────────────────────

PASSES = 0
FAILS  = 0


def _ok(name: str, msg: str = "") -> None:
    global PASSES
    PASSES += 1
    print(f"  [OK]    {name}{(' — ' + msg) if msg else ''}")


def _fail(name: str, msg: str) -> None:
    global FAILS
    FAILS += 1
    print(f"  [FAIL]  {name} — {msg}")


# ── Exploding DB client (DB-boundary mock) ───────────────────────────────────

class _ExplodingQueryBuilder:
    """
    Every fluent builder method returns self; .execute() raises RuntimeError.

    This forces the REAL except paths inside cost_guard to execute.
    We are NOT patching _get_today_spend or _get_month_spend themselves —
    we are simulating the DB being unreachable at the lowest possible boundary.
    """
    def table(self,  *a, **kw): return self
    def select(self, *a, **kw): return self
    def insert(self, *a, **kw): return self
    def gte(self,    *a, **kw): return self
    def lte(self,    *a, **kw): return self
    def eq(self,     *a, **kw): return self
    def limit(self,  *a, **kw): return self
    def execute(self):
        raise RuntimeError("Simulated DB connection failure — exploding test client")


_EXPLODING_DB = _ExplodingQueryBuilder()


def _install_exploding_db() -> None:
    """Patch cost_guard.get_db to return the exploding client."""
    import api.lib.cost_guard as cg
    cg.get_db = lambda: _EXPLODING_DB


# ── Critical-log capture ─────────────────────────────────────────────────────

class _CriticalCapture(logging.Handler):
    """Captures CRITICAL-level records emitted by the attached logger."""

    def __init__(self) -> None:
        super().__init__(level=logging.CRITICAL)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


# ── Tests ────────────────────────────────────────────────────────────────────

def test_get_today_spend_returns_inf_on_db_error() -> None:
    """
    WHY THIS WOULD FAIL AGAINST OLD CODE:
        The old except block ended with `return 0.0`.
        `assert result == float("inf")` would fail because 0.0 != inf.
        The guard would then see svc_spent=0.0 and allow the paid call through —
        the system would spend real money even though the spend ledger was
        completely unreadable (fail-OPEN behaviour).

    NEW behaviour: returns float("inf") so check_budget treats the cap as
    exhausted and blocks the call (fail-CLOSED).
    """
    import api.lib.cost_guard as cg

    result = cg._get_today_spend(service="outscraper")

    name = "_get_today_spend returns inf on DB error"
    if result == float("inf"):
        _ok(name, "returned float('inf'), NOT 0.0")
    else:
        _fail(name, f"expected float('inf'), got {result!r} — old floor-to-zero bug present")


def test_get_month_spend_returns_inf_on_db_error() -> None:
    """
    WHY THIS WOULD FAIL AGAINST OLD CODE:
        _get_month_spend also returned 0.0 on exception in the old code.
        The monthly cap check would pass silently even when the ledger was down,
        letting unlimited spending accumulate if the daily checks also failed.

    NEW behaviour: returns float("inf").
    """
    import api.lib.cost_guard as cg

    result = cg._get_month_spend()

    name = "_get_month_spend returns inf on DB error"
    if result == float("inf"):
        _ok(name, "returned float('inf'), NOT 0.0")
    else:
        _fail(name, f"expected float('inf'), got {result!r} — old floor-to-zero bug present")


async def test_check_budget_returns_false_when_spend_unreadable() -> None:
    """
    WHY THIS WOULD FAIL AGAINST OLD CODE:
        Old code: _get_today_spend() → 0.0 on DB error.
        check_budget would see svc_spent=0.0, compute 0.0+0.002 ≤ 30.00 → True.
        The paid Outscraper call would PROCEED even though we have zero visibility
        into actual spend — classic fail-open behaviour, money at risk.

    NEW behaviour: _get_today_spend() → inf → inf+0.002 > any cap → returns False.
    The paid job is BLOCKED. Money is not spent when the ledger is unreadable.
    """
    import api.lib.cost_guard as cg

    result = await cg.check_budget(
        service="outscraper",
        estimated_cost_usd=0.002,
        job_name="failclosed_test",
        leads_affected=1,
    )

    name = "check_budget returns False when DB spend unreadable"
    if result is False:
        _ok(name, "returned False — paid call BLOCKED (fail-closed)")
    else:
        _fail(name, f"expected False, got {result!r} — guard is FAIL-OPEN, money at risk")


async def test_log_cost_fires_critical_on_insert_failure() -> None:
    """
    WHY THIS WOULD FAIL AGAINST OLD CODE:
        Old code: `logger.error(...)` — level 40, below CRITICAL (50).
        The _CriticalCapture handler only accepts level >= CRITICAL, so nothing
        would be captured and the assertion would fail.
        This confirms the failure was previously logged below the alerting
        threshold, making it easy to miss in a busy log stream.

    NEW behaviour: `logger.critical(...)` fires with explicit
    "spend row NOT recorded" message — captured by the handler.
    """
    import api.lib.cost_guard as cg

    cg_logger = logging.getLogger("api.lib.cost_guard")
    capture   = _CriticalCapture()
    cg_logger.addHandler(capture)
    old_level = cg_logger.level
    cg_logger.setLevel(logging.DEBUG)   # ensure critical records aren't filtered

    try:
        await cg.log_cost(
            stage="website_audit",
            service="website_audit",
            cost_usd=0.005,
            lead_id="test-lead-id",
            items_fetched=1,
        )
    finally:
        cg_logger.removeHandler(capture)
        cg_logger.setLevel(old_level)

    name = "log_cost fires logger.critical on INSERT failure"
    if any(
        "log_cost FAILED" in m or "spend row NOT recorded" in m
        for m in capture.messages
    ):
        _ok(name, f"critical fired: {capture.messages[0][:80]!r}")
    elif capture.messages:
        _fail(name, f"critical fired but message unexpected: {capture.messages[0][:120]!r}")
    else:
        _fail(
            name,
            "no CRITICAL log captured — failure silently swallowed "
            "or logged below CRITICAL (old logger.error behaviour)"
        )


# ── Runner ───────────────────────────────────────────────────────────────────

async def _run_all() -> None:
    _install_exploding_db()

    print("\n== cost_guard fail-closed tests ==")

    # Synchronous spend-read tests — verify the raw helper return values
    test_get_today_spend_returns_inf_on_db_error()
    test_get_month_spend_returns_inf_on_db_error()

    # Async budget-gate test — end-to-end: DB down → check_budget → False
    await test_check_budget_returns_false_when_spend_unreadable()

    # Async log-cost test — INSERT fails → CRITICAL fired (not silently swallowed)
    await test_log_cost_fires_critical_on_insert_failure()


def main() -> int:
    asyncio.run(_run_all())
    total = PASSES + FAILS
    print(f"\n  {PASSES}/{total} passed, {FAILS} failed.")
    return 0 if FAILS == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
