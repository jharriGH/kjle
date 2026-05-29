"""
KJLE — TCPA Litigator List Provider abstraction
File: api/lib/tcpa_provider.py

Mirror of api/lib/dnc_provider.py — abstract base + factory that returns the
active TCPA list provider based on admin_settings.tcpa_list_provider.

Slice 3A ships with one concrete impl (TCPALitigatorListProvider). It is
dormant-safe: when TCPA_LIST_API_KEY / TCPA_LIST_CSV_URL are unset (the
default until Jim subscribes to a vendor), fetch_latest_list() logs a
warning and returns []. No crash, no exception, no false positives.

Adding a new vendor (e.g. StopLitigators.com, TCPA Black List):
  1. Subclass TCPAProvider in a new module / new class here.
  2. Register it in _PROVIDER_REGISTRY below.
  3. Set admin_settings.tcpa_list_provider to the new name.
No call sites change.
"""
from __future__ import annotations

import csv
import io
import logging
import os
from abc import ABC, abstractmethod
from typing import Optional

import httpx

from .phone_utils import normalize_phone

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Abstract base
# ─────────────────────────────────────────────────────────────────────────────

class TCPAProvider(ABC):
    """Base class for TCPA litigator-list providers."""

    name: str = "abstract"

    @abstractmethod
    async def fetch_latest_list(self) -> list[dict]:
        """
        Returns the current vendor list as a list of dicts with at least
        the key 'phone' (E.164). Optional keys: name, state, case_count,
        metadata.

        Implementations MUST normalize phones via phone_utils.normalize_phone
        and drop rows that fail to normalize (logging the count).

        Dormant-safe contract: if credentials are missing, return [] and log
        a warning rather than raise. The weekly refresh job interprets an
        empty list correctly (Section 3.5 of the design doc).
        """
        ...


# ─────────────────────────────────────────────────────────────────────────────
# TCPALitigatorListProvider (tcpalitigatorlist.com)
#
# Two configuration shapes supported:
#   - Hosted CSV export at TCPA_LIST_CSV_URL (preferred for v1 — simplest)
#   - API key at TCPA_LIST_API_KEY (placeholder for follow-on; not used yet)
# ─────────────────────────────────────────────────────────────────────────────

class TCPALitigatorListProvider(TCPAProvider):
    """
    Concrete impl that pulls the canonical TCPA litigator list from
    tcpalitigatorlist.com.

    Env vars (both optional; either being empty puts the provider in
    dormant mode):
      TCPA_LIST_API_KEY  — vendor API key, sent as Authorization header
      TCPA_LIST_CSV_URL  — direct URL to a CSV export of the list

    Expected CSV header columns (case-insensitive, flexible):
      phone | phone_number | number   (required)
      name  | full_name                (optional)
      state | st                       (optional)
      case_count | cases               (optional, int)
    Any other columns are preserved in metadata.

    Other vendors that publish a similar CSV can subclass this provider
    and override the URL/headers — no code changes elsewhere.
    """

    name = "tcpalitigatorlist"

    # Module-level constants so a subclass can override
    REQUEST_TIMEOUT_SECS = 60.0
    MAX_ROWS = 1_000_000  # paranoia cap — vendor list is typically ~hundreds-to-thousands

    def __init__(self) -> None:
        self._api_key = os.environ.get("TCPA_LIST_API_KEY", "").strip()
        self._csv_url = os.environ.get("TCPA_LIST_CSV_URL", "").strip()

    def is_dormant(self) -> bool:
        """True when no credentials present — fetch_latest_list will no-op."""
        return not self._csv_url

    async def fetch_latest_list(self) -> list[dict]:
        if self.is_dormant():
            logger.warning(
                "tcpa_provider: TCPALitigatorListProvider is dormant "
                "(TCPA_LIST_CSV_URL not set) — returning empty list. "
                "Subscribe to a vendor + set env vars to activate."
            )
            return []

        try:
            csv_text = await self._download_csv()
        except Exception as e:
            logger.error(
                f"tcpa_provider: CSV download from {self._csv_url!r} failed: "
                f"{type(e).__name__}: {e}"
            )
            return []

        rows, dropped = self._parse_csv(csv_text)
        if dropped:
            logger.warning(
                f"tcpa_provider: dropped {dropped} row(s) with un-normalizable phones"
            )
        logger.info(
            f"tcpa_provider: fetched {len(rows)} valid rows from "
            f"{self.name} (after normalization, dropped={dropped})"
        )
        return rows

    async def _download_csv(self) -> str:
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        async with httpx.AsyncClient(timeout=self.REQUEST_TIMEOUT_SECS) as client:
            r = await client.get(self._csv_url, headers=headers)
            r.raise_for_status()
            return r.text

    def _parse_csv(self, csv_text: str) -> tuple[list[dict], int]:
        """Parse a CSV string into normalized row dicts + dropped count."""
        dropped = 0
        out: list[dict] = []

        reader = csv.DictReader(io.StringIO(csv_text))
        for raw_row in reader:
            if len(out) >= self.MAX_ROWS:
                logger.error(
                    f"tcpa_provider: hit MAX_ROWS={self.MAX_ROWS} cap during "
                    "CSV parse — vendor export unexpectedly large; aborting parse"
                )
                break

            phone_raw = _first(raw_row, "phone", "phone_number", "number", "phonenumber")
            phone_e164 = normalize_phone(phone_raw)
            if not phone_e164:
                dropped += 1
                continue

            name  = _first(raw_row, "name", "full_name", "fullname")
            state = _first(raw_row, "state", "st")
            case_count_raw = _first(raw_row, "case_count", "cases", "case_total")
            case_count: Optional[int]
            try:
                case_count = int(case_count_raw) if case_count_raw else None
            except (TypeError, ValueError):
                case_count = None

            # Surface raw vendor row in metadata so we don't lose provenance.
            metadata = {
                k: v for k, v in raw_row.items()
                if k and k.strip().lower() not in {
                    "phone", "phone_number", "number", "phonenumber",
                    "name", "full_name", "fullname",
                    "state", "st",
                    "case_count", "cases", "case_total",
                }
            }

            out.append({
                "phone":      phone_e164,
                "name":       (name or "").strip() or None,
                "state":      (state or "").strip().upper()[:2] or None,
                "case_count": case_count,
                "metadata":   metadata,
            })

        return out, dropped


def _first(row: dict, *keys: str) -> Optional[str]:
    """Case-insensitive lookup over a list of candidate keys."""
    lower = {(k or "").strip().lower(): v for k, v in row.items()}
    for k in keys:
        v = lower.get(k.lower())
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

_PROVIDER_REGISTRY = {
    "tcpalitigatorlist": TCPALitigatorListProvider,
}


def get_active_tcpa_provider() -> TCPAProvider:
    """
    Returns the TCPAProvider matching admin_settings.tcpa_list_provider.
    Falls back to TCPALitigatorListProvider on missing setting / unknown name.
    Never raises — failures upstream become dormant no-ops.
    """
    provider_name = "tcpalitigatorlist"
    try:
        # Lazy import to avoid circular: tcpa_provider has no DB dep itself.
        from ..database import get_db
        db = get_db()
        res = (
            db.table("admin_settings")
            .select("value")
            .eq("key", "tcpa_list_provider")
            .execute()
        )
        if res.data and res.data[0].get("value"):
            provider_name = str(res.data[0]["value"]).strip() or provider_name
    except Exception as e:
        logger.warning(
            f"get_active_tcpa_provider: admin_settings read failed ({e}); "
            f"defaulting to {provider_name!r}"
        )

    cls = _PROVIDER_REGISTRY.get(provider_name)
    if cls is None:
        logger.warning(
            f"get_active_tcpa_provider: unknown provider {provider_name!r}; "
            "falling back to tcpalitigatorlist"
        )
        cls = TCPALitigatorListProvider

    try:
        return cls()
    except Exception as e:
        logger.error(
            f"get_active_tcpa_provider: failed to construct {provider_name!r}: {e}"
        )
        # Last resort: return a dormant TCPALitigatorListProvider so refresh
        # job sees an empty list and logs "not provisioned".
        return TCPALitigatorListProvider()
