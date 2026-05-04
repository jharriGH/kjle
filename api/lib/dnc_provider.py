"""
KJLE — DNC Provider abstraction
File: api/lib/dnc_provider.py

Defines the contract every DNC provider implementation must satisfy and a
factory that returns the active provider based on admin_settings.dnc_provider.

Adding a new provider:
  1. Implement DNCProvider in a new module under api/lib/
  2. Register it in _PROVIDER_REGISTRY below
  3. Set admin_settings.dnc_provider to its name
No call sites change.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class DNCResult:
    """
    Canonical DNC lookup result. Provider implementations map their native
    response shapes onto this. Fail-closed semantics: when error is set,
    is_dnc must default to True so callers don't accidentally dial.
    """
    is_dnc:          bool
    reason:          str = ""              # raw provider codes (e.g., "FED,CA,CPL") or our internal reason
    tcpa_litigator:  bool = False
    line_type:       str = "unknown"       # 'mobile' | 'landline' | 'voip' | 'unknown'
    carrier:         str = ""
    raw_response:    dict = field(default_factory=dict)
    provider:        str = ""
    error:           Optional[str] = None  # None on success; non-empty string on any failure
    fetched_at:      str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class DNCProvider(ABC):
    """Base class for DNC providers."""

    name: str = "abstract"

    @abstractmethod
    async def check_phone(self, phone_e164: str) -> DNCResult:
        """Look up a single phone. phone_e164 is guaranteed valid (+1 + 10 digits)."""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """True if the provider has the credentials/config it needs to make calls."""
        ...


class _UnavailableProvider(DNCProvider):
    """
    Fallback returned by get_active_provider() when no provider is configured
    or the requested provider name doesn't match any registered impl.
    Every call returns a fail-closed error result.
    """

    name = "unavailable"

    def __init__(self, reason: str = "no provider configured"):
        self._reason = reason

    def is_available(self) -> bool:
        return False

    async def check_phone(self, phone_e164: str) -> DNCResult:
        return DNCResult(
            is_dnc   = True,                 # fail-closed
            reason   = "provider_unavailable",
            provider = "unavailable",
            error    = f"provider_unavailable: {self._reason}",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Provider registry — extend here to add new providers
# ─────────────────────────────────────────────────────────────────────────────

def _build_searchbug() -> DNCProvider:
    # Lazy import so this module has no hard dep on httpx / the impl.
    from .searchbug_provider import SearchbugProvider
    return SearchbugProvider()


_PROVIDER_REGISTRY = {
    "searchbug": _build_searchbug,
}


def get_active_provider() -> DNCProvider:
    """
    Returns the DNCProvider matching admin_settings.dnc_provider.
    Falls back to _UnavailableProvider on any error (missing setting, unknown
    name, provider construction failure, etc.) — never raises.
    """
    try:
        # Lazy import to avoid circular: dnc_provider has no DB dep itself.
        from ..database import get_db
        db = get_db()
        res = db.table("admin_settings").select("value").eq("key", "dnc_provider").execute()
        provider_name = (res.data[0].get("value") if res.data else "") or "searchbug"
    except Exception as e:
        logger.warning(f"get_active_provider: admin_settings read failed ({e}); defaulting to 'searchbug'")
        provider_name = "searchbug"

    builder = _PROVIDER_REGISTRY.get(provider_name)
    if builder is None:
        logger.warning(f"get_active_provider: unknown provider '{provider_name}'")
        return _UnavailableProvider(reason=f"unknown provider name: {provider_name}")

    try:
        provider = builder()
    except Exception as e:
        logger.error(f"get_active_provider: failed to construct '{provider_name}': {e}")
        return _UnavailableProvider(reason=f"construction failed: {e}")

    if not provider.is_available():
        logger.warning(f"get_active_provider: '{provider_name}' reports not available")
        return _UnavailableProvider(reason=f"{provider_name} not configured")

    return provider
