#!/usr/bin/env python3
"""
KJLE — SearchbugProvider unit tests
File: scripts/test_searchbug_provider.py

Standalone fixture suite that locks in:
  - Canonical Federal+CA DNC verdict for +19095813010 (no error envelope)
  - 3xx → provider_redirect_denied with Location header captured (the F1 bug)
  - 401/403 → provider_auth_denied
  - 5xx → provider_http_error
  - Clean number → is_dnc=False, reason='clean'
  - Searchbug-style top-level error envelope → provider_error

Run:
    python scripts/test_searchbug_provider.py

Exit code 0 if all pass, 1 if any fail. No network calls — httpx is monkey-
patched with a fake AsyncClient that returns canned responses.
"""
from __future__ import annotations

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# api.config.Settings demands SUPABASE_URL / SUPABASE_SERVICE_KEY at import time;
# api.database imports supabase lazily but config validation runs eagerly.
os.environ.setdefault("SUPABASE_URL", "https://test.invalid")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test_key")
os.environ.setdefault("SEARCHBUG_API_KEY", "test_pass")

# Stub external libs so this runs in a minimal CI / fresh-checkout environment.
# Only the SearchbugProvider class is under test — none of these stubs are
# exercised by the assertions.
_STUB_MODULES = ["supabase", "pydantic_settings"]
for _mod_name in _STUB_MODULES:
    if _mod_name in sys.modules:
        continue
    try:
        __import__(_mod_name)
    except ImportError:
        _mod = types.ModuleType(_mod_name)
        if _mod_name == "supabase":
            class _Client: pass
            _mod.Client = _Client
            _mod.create_client = lambda *a, **kw: _Client()
        if _mod_name == "pydantic_settings":
            # BaseSettings just needs to be a class subclassable by api.config.Settings;
            # we manually populate the attrs Settings uses.
            class _BaseSettings:
                def __init__(self, **_kw):
                    for k, v in type(self).__dict__.items():
                        if not k.startswith("_") and not callable(v):
                            setattr(self, k, v)
                    self.SUPABASE_URL         = os.environ.get("SUPABASE_URL", "")
                    self.SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
                    self.SEARCHBUG_API_KEY    = os.environ.get("SEARCHBUG_API_KEY", "")
                class Config:  # noqa: D106
                    pass
            _mod.BaseSettings = _BaseSettings
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


# ── Fake httpx.AsyncClient ───────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, status_code: int, *, json_body=None, text: str = "",
                 headers: dict | None = None):
        self.status_code = status_code
        self._json_body  = json_body
        self.text        = text
        self.headers     = headers or {}

    def json(self):
        if self._json_body is None:
            raise ValueError("no JSON body")
        return self._json_body


class _FakeAsyncClient:
    """
    Drop-in for httpx.AsyncClient — captures the next canned response from
    the class-level _NEXT slot and returns it from .post().
    """
    _NEXT: _FakeResponse | None = None
    _LAST_POST: dict | None     = None

    def __init__(self, *a, **kw):
        self._init_kwargs = kw

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, data=None, **kw):
        _FakeAsyncClient._LAST_POST = {"url": url, "data": data, "kw": kw}
        resp = _FakeAsyncClient._NEXT
        if resp is None:
            raise RuntimeError("FakeAsyncClient: no canned response set")
        return resp


# ── Stub admin_settings.dnc_searchbug_co_code lookup + balance persistence ──

def _patch_provider_internals():
    """
    Patch SearchbugProvider's external dependencies:
      - _get_co_code → static value so is_available() / check_phone() proceed
      - _persist_and_alert_balance → no-op so the test doesn't touch the DB
    """
    from api.lib import searchbug_provider as sp

    sp._get_co_code = lambda: "FAKE_CO"

    async def _noop(self, raw_response):
        return None

    sp.SearchbugProvider._persist_and_alert_balance = _noop  # type: ignore[assignment]

    # Swap httpx.AsyncClient in the module's namespace for our fake.
    sp.httpx.AsyncClient = _FakeAsyncClient  # type: ignore[assignment]


# ── Canonical Searchbug response shapes ─────────────────────────────────────

def _fed_ca_dnc_body() -> dict:
    """
    Searchbug api_lnd2 response for +19095813010 — Federal + California DNC,
    landline, Frontier carrier (matches the live cache row).
    """
    return {
        "Status": "Success",
        "Error":  None,
        "Data": {
            "PHONE": {
                "DNC":     "FED,CA",
                "TCPA":    "NO",
                "TYPE":    "LANDLINE",
                "VOIP":    "NO",
                "CARRIER": "FRONTIER COMMUNICATIONS OF THE SOUTHWEST INC",
                "OCN":     "0218",
                "PORTED":  "NO",
            },
            "STATS": {"BALANCE": "49.82"},
        },
    }


def _clean_body() -> dict:
    return {
        "Status": "Success",
        "Error":  None,
        "Data": {
            "PHONE": {
                "DNC":     "NO",
                "TCPA":    "NO",
                "TYPE":    "CELLULAR",
                "VOIP":    "NO",
                "CARRIER": "VERIZON WIRELESS",
            },
            "STATS": {"BALANCE": "49.80"},
        },
    }


def _envelope_error_body() -> dict:
    return {
        "Status": "Error",
        "Error":  "Invalid API key",
        "Data":   {},
    }


# ── Tests ────────────────────────────────────────────────────────────────────

async def test_known_dnc_phone():
    """+19095813010 must return is_dnc=True with reason containing FED and CA, error=None."""
    from api.lib.searchbug_provider import SearchbugProvider

    _FakeAsyncClient._NEXT = _FakeResponse(200, json_body=_fed_ca_dnc_body())
    result = await SearchbugProvider().check_phone("+19095813010")

    name = "known_dnc_phone_+19095813010"
    if not result.is_dnc:
        _fail(name, f"is_dnc should be True, got {result.is_dnc}")
        return
    if result.error is not None:
        _fail(name, f"error should be None, got {result.error!r}")
        return
    if "FED" not in result.reason.upper() or "CA" not in result.reason.upper():
        _fail(name, f"reason should contain FED and CA, got {result.reason!r}")
        return
    if result.line_type != "landline":
        _fail(name, f"line_type should be 'landline', got {result.line_type!r}")
        return
    if "FRONTIER" not in result.carrier.upper():
        _fail(name, f"carrier should contain FRONTIER, got {result.carrier!r}")
        return
    _ok(name, f"is_dnc=True reason={result.reason!r} line_type={result.line_type!r}")


async def test_clean_phone():
    """Clean phone (DNC=NO) must return is_dnc=False, reason='clean'."""
    from api.lib.searchbug_provider import SearchbugProvider

    _FakeAsyncClient._NEXT = _FakeResponse(200, json_body=_clean_body())
    result = await SearchbugProvider().check_phone("+13105550001")

    name = "clean_phone"
    if result.is_dnc:
        _fail(name, f"is_dnc should be False, got {result.is_dnc}")
        return
    if result.reason != "clean":
        _fail(name, f"reason should be 'clean', got {result.reason!r}")
        return
    if result.line_type != "mobile":
        _fail(name, f"line_type should be 'mobile', got {result.line_type!r}")
        return
    if result.error is not None:
        _fail(name, f"error should be None, got {result.error!r}")
        return
    _ok(name)


async def test_302_redirect_denied():
    """3xx must classify as provider_redirect_denied with Location captured (the F1 bug)."""
    from api.lib.searchbug_provider import SearchbugProvider

    _FakeAsyncClient._NEXT = _FakeResponse(
        302,
        text='<html><head><title>Object moved</title></head><body>'
             '<h2>Object moved to <a href="https://www.searchbug.com/access-denied.aspx?TYPE=F1">here</a>.</h2>'
             '</body></html>',
        headers={"Location": "https://www.searchbug.com/access-denied.aspx?TYPE=F1"},
    )
    result = await SearchbugProvider().check_phone("+14155551234")

    name = "302_redirect_classified"
    if not result.is_dnc:
        _fail(name, "fail-closed: is_dnc should be True on 3xx")
        return
    if result.reason != "provider_redirect_denied":
        _fail(name, f"reason should be provider_redirect_denied, got {result.reason!r}")
        return
    if not result.error or "access-denied.aspx" not in result.error:
        _fail(name, f"error must include Location URL, got {result.error!r}")
        return
    if "provider_http_302" not in (result.error or ""):
        _fail(name, f"error must include provider_http_302, got {result.error!r}")
        return
    _ok(name, f"reason={result.reason} error contains access-denied.aspx")


async def test_403_auth_denied():
    """403 must classify as provider_auth_denied (distinct from generic http_error)."""
    from api.lib.searchbug_provider import SearchbugProvider

    _FakeAsyncClient._NEXT = _FakeResponse(403, text="Forbidden")
    result = await SearchbugProvider().check_phone("+12125550100")

    name = "403_auth_denied"
    if not result.is_dnc:
        _fail(name, "fail-closed: is_dnc should be True on 403")
        return
    if result.reason != "provider_auth_denied":
        _fail(name, f"reason should be provider_auth_denied, got {result.reason!r}")
        return
    if "provider_http_403" not in (result.error or ""):
        _fail(name, f"error must include provider_http_403, got {result.error!r}")
        return
    _ok(name)


async def test_500_generic_http_error():
    """5xx falls through to generic provider_http_error."""
    from api.lib.searchbug_provider import SearchbugProvider

    _FakeAsyncClient._NEXT = _FakeResponse(500, text="Internal Server Error")
    result = await SearchbugProvider().check_phone("+18005551212")

    name = "500_generic_http_error"
    if not result.is_dnc:
        _fail(name, "fail-closed: is_dnc should be True on 500")
        return
    if result.reason != "provider_http_error":
        _fail(name, f"reason should be provider_http_error, got {result.reason!r}")
        return
    _ok(name)


async def test_envelope_error():
    """Searchbug-shape error envelope (200 body with Status=Error) → provider_error."""
    from api.lib.searchbug_provider import SearchbugProvider

    _FakeAsyncClient._NEXT = _FakeResponse(200, json_body=_envelope_error_body())
    result = await SearchbugProvider().check_phone("+19495555555")

    name = "envelope_error_status_error"
    if not result.is_dnc:
        _fail(name, "fail-closed: is_dnc should be True on envelope error")
        return
    if result.reason != "provider_error":
        _fail(name, f"reason should be provider_error, got {result.reason!r}")
        return
    if not result.error or "Invalid API key" not in result.error:
        _fail(name, f"error should surface envelope message, got {result.error!r}")
        return
    _ok(name)


async def test_phone_format_strips_plus1():
    """Searchbug F param must receive 10 digits (no leading +1)."""
    from api.lib.searchbug_provider import SearchbugProvider

    _FakeAsyncClient._NEXT = _FakeResponse(200, json_body=_clean_body())
    await SearchbugProvider().check_phone("+19095813010")

    name = "phone_format_strips_plus1"
    last = _FakeAsyncClient._LAST_POST
    if not last:
        _fail(name, "no POST captured")
        return
    f_param = (last.get("data") or {}).get("F")
    if f_param != "9095813010":
        _fail(name, f"F param should be '9095813010', got {f_param!r}")
        return
    _ok(name)


# ── Runner ───────────────────────────────────────────────────────────────────

async def _run_all():
    _patch_provider_internals()

    print("\n== SearchbugProvider tests ==")
    await test_known_dnc_phone()
    await test_clean_phone()
    await test_302_redirect_denied()
    await test_403_auth_denied()
    await test_500_generic_http_error()
    await test_envelope_error()
    await test_phone_format_strips_plus1()


def main() -> int:
    asyncio.run(_run_all())
    total = PASSES + FAILS
    print(f"\n  {PASSES}/{total} passed, {FAILS} failed.")
    return 0 if FAILS == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
