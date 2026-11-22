"""
Tests for api/routes/outreach_contract.py and api/lib/tz_backfill.py.

Coverage:
  - _registrable_domain / _business_key (pure unit, no I/O)
  - _get_iana_tz from tz_backfill (pure unit, no I/O)
  - Pydantic model validation (DncCheckRequest <=500 cap, LeadSearchRequest limit cap)
  - _outreach_auth (via TestClient: missing bearer, wrong token, table key, service header)
  - Endpoint shape: POST /dnc/check, POST /dnc, POST /outreach/signals,
                    POST /outreach/leads/search, POST /outreach/leads/count
"""
from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError


# ── Pure unit: _registrable_domain + _business_key ───────────────────────────

from api.routes.outreach_contract import _registrable_domain, _business_key


class TestRegistrableDomain:
    def test_basic(self):
        assert _registrable_domain("https://www.example.com/path") == "example.com"

    def test_strips_www(self):
        assert _registrable_domain("www.acme.co") == "acme.co"

    def test_no_scheme(self):
        assert _registrable_domain("acme.com") == "acme.com"

    def test_two_part_public_suffix(self):
        # co.uk is a 2-part public suffix → keep 3 labels
        assert _registrable_domain("https://shop.acme.co.uk") == "acme.co.uk"

    def test_subdomain_stripped(self):
        assert _registrable_domain("https://app.staging.example.com") == "example.com"

    def test_none_returns_none(self):
        assert _registrable_domain(None) is None

    def test_empty_string_returns_none(self):
        assert _registrable_domain("") is None

    def test_no_dot_returns_none(self):
        assert _registrable_domain("localhost") is None


class TestBusinessKey:
    def test_uses_website_domain(self):
        assert _business_key("https://www.acme.com", "owner@acme.com", "123") == "acme.com"

    def test_falls_back_to_email_domain(self):
        assert _business_key(None, "owner@acme.com", "123") == "acme.com"

    def test_falls_back_to_lead_id(self):
        assert _business_key(None, None, "abc-123") == "lead:abc-123"

    def test_free_mail_domain_not_excluded(self):
        # Free-mail exclusion is handled upstream (OE); KJLE passes email domain through
        result = _business_key(None, "user@gmail.com", "xyz")
        assert result == "gmail.com"

    def test_bad_email_falls_back(self):
        assert _business_key(None, "not-an-email", "xyz") == "lead:xyz"

    def test_website_takes_priority_over_email(self):
        assert _business_key("https://biz.com", "owner@other.com", "id1") == "biz.com"

    def test_bad_website_falls_back_to_email(self):
        # unparseable host
        assert _business_key("https://", "owner@fallback.com", "id2") == "fallback.com"


# ── Pure unit: _get_iana_tz ───────────────────────────────────────────────────

from api.lib.tz_backfill import _get_iana_tz


class TestGetIanaTz:
    def test_single_tz_state(self):
        assert _get_iana_tz("CA", None, None) == "America/Los_Angeles"
        assert _get_iana_tz("NY", None, None) == "America/New_York"
        assert _get_iana_tz("TX", None, None) == "America/Chicago"  # TX majority

    def test_case_insensitive(self):
        assert _get_iana_tz("ca", None, None) == "America/Los_Angeles"

    def test_unknown_state_returns_none(self):
        assert _get_iana_tz("XX", None, None) is None
        assert _get_iana_tz(None, None, None) is None
        assert _get_iana_tz("", None, None) is None

    def test_split_state_lng_west(self):
        # FL panhandle (west of -86.5) → America/Chicago
        assert _get_iana_tz("FL", None, -87.0) == "America/Chicago"

    def test_split_state_lng_east(self):
        # FL east of -86.5 → America/New_York (majority)
        assert _get_iana_tz("FL", None, -80.0) == "America/New_York"

    def test_split_state_zip_override(self):
        # SD Black Hills (zip 577xx) → America/Denver
        assert _get_iana_tz("SD", "57701", None) == "America/Denver"

    def test_split_state_fallback_no_lng_no_zip(self):
        # TX with no zip, no lng → state majority (America/Chicago)
        assert _get_iana_tz("TX", None, None) == "America/Chicago"

    def test_tx_el_paso_lng(self):
        # TX west of -104.5 → America/Denver
        assert _get_iana_tz("TX", None, -105.0) == "America/Denver"

    def test_hi_ak(self):
        assert _get_iana_tz("HI", None, None) == "Pacific/Honolulu"
        assert _get_iana_tz("AK", None, None) == "America/Anchorage"


# ── Pydantic model validation ─────────────────────────────────────────────────

from api.routes.outreach_contract import DncCheckRequest, DncCheckItem, LeadSearchRequest


class TestDncCheckRequestValidation:
    def test_accepts_up_to_500(self):
        items = [DncCheckItem(email=f"user{i}@example.com") for i in range(500)]
        req = DncCheckRequest(items=items)
        assert len(req.items) == 500

    def test_rejects_over_500(self):
        items = [DncCheckItem(email=f"u{i}@x.com") for i in range(501)]
        with pytest.raises(ValidationError):
            DncCheckRequest(items=items)

    def test_empty_items_allowed(self):
        req = DncCheckRequest(items=[])
        assert req.items == []

    def test_mixed_types(self):
        items = [
            DncCheckItem(email="a@b.com"),
            DncCheckItem(phone="+15550001234"),
            DncCheckItem(domain="spam.com"),
        ]
        req = DncCheckRequest(items=items)
        assert len(req.items) == 3


class TestLeadSearchRequestValidation:
    def test_limit_capped_at_1000(self):
        req = LeadSearchRequest(limit=9999)
        assert req.limit == 1000

    def test_limit_min_is_1(self):
        req = LeadSearchRequest(limit=0)
        assert req.limit == 1

    def test_default_limit(self):
        req = LeadSearchRequest()
        assert req.limit == 100

    def test_exclude_dnc_default(self):
        assert LeadSearchRequest().exclude_dnc is True


# ── TestClient fixture ────────────────────────────────────────────────────────

def _make_mock_db(api_keys_data=None, email_suppression_data=None,
                  dnc_suppression_data=None, leads_data=None,
                  idempotency_data=None):
    """Return a MagicMock that simulates the Supabase chaining API."""
    db = MagicMock()

    def _chain(table_name):
        tbl = MagicMock()
        execute_result = MagicMock()

        # configure per-table responses
        if table_name == "api_keys":
            execute_result.data = api_keys_data if api_keys_data is not None else []
        elif table_name == "email_suppressions":
            execute_result.data = email_suppression_data if email_suppression_data is not None else []
        elif table_name == "dnc_suppressions":
            execute_result.data = dnc_suppression_data if dnc_suppression_data is not None else []
        elif table_name == "leads":
            execute_result.data = leads_data if leads_data is not None else []
            execute_result.count = len(leads_data) if leads_data is not None else 0
        elif table_name == "outreach_idempotency_keys":
            execute_result.data = idempotency_data if idempotency_data is not None else []
        elif table_name == "stats_cache":
            execute_result.data = []
        else:
            execute_result.data = []
            execute_result.count = 0

        tbl.select.return_value = tbl
        tbl.insert.return_value = tbl
        tbl.update.return_value = tbl
        tbl.upsert.return_value = tbl
        tbl.delete.return_value = tbl
        tbl.eq.return_value = tbl
        tbl.neq.return_value = tbl
        tbl.in_.return_value = tbl
        tbl.not_.is_.return_value = tbl
        tbl.not_ = tbl
        tbl.is_.return_value = tbl
        tbl.lt.return_value = tbl
        tbl.lte.return_value = tbl
        tbl.gt.return_value = tbl
        tbl.gte.return_value = tbl
        tbl.like.return_value = tbl
        tbl.order.return_value = tbl
        tbl.limit.return_value = tbl
        tbl.range.return_value = tbl
        tbl.or_.return_value = tbl
        tbl.execute.return_value = execute_result
        return tbl

    db.table.side_effect = _chain
    return db


def _make_client(mock_db):
    """Create a minimal TestClient with only the outreach_contract router."""
    from api.routes.outreach_contract import router
    app = FastAPI()
    app.include_router(router, prefix="/kjle/v1")
    with patch("api.routes.outreach_contract.get_db", return_value=mock_db):
        client = TestClient(app, raise_server_exceptions=False)
    return client, mock_db


# ── Auth tests ────────────────────────────────────────────────────────────────

VALID_HEADERS = {
    "Authorization": "Bearer test-outreach-key",
    "X-KJE-Service": "outreachenginez",
}


class TestOutreachAuth:
    def _client(self):
        db = _make_mock_db()
        return db, TestClient(
            _make_fastapi_with_patched_db(db),
            raise_server_exceptions=False,
        )

    def test_missing_bearer_returns_401(self):
        db = _make_mock_db()
        with patch("api.routes.outreach_contract.get_db", return_value=db):
            from api.routes.outreach_contract import router
            app = FastAPI()
            app.include_router(router, prefix="/kjle/v1")
            c = TestClient(app, raise_server_exceptions=False)
            resp = c.post("/kjle/v1/outreach/leads/count", json={},
                          headers={"X-KJE-Service": "outreachenginez"})
            assert resp.status_code == 401

    def test_wrong_bearer_no_table_key_returns_401(self):
        db = _make_mock_db(api_keys_data=[])  # no matching key in table
        with patch("api.routes.outreach_contract.get_db", return_value=db):
            from api.routes.outreach_contract import router
            app = FastAPI()
            app.include_router(router, prefix="/kjle/v1")
            c = TestClient(app, raise_server_exceptions=False)
            resp = c.post(
                "/kjle/v1/outreach/leads/count", json={},
                headers={"Authorization": "Bearer wrong-key", "X-KJE-Service": "oe"},
            )
            assert resp.status_code == 401

    def test_per_consumer_table_key_accepted(self):
        consumer_raw_key = "kjle_abcdef1234567890abcdef1234567890"
        consumer_hash = hashlib.sha256(consumer_raw_key.encode()).hexdigest()
        db = _make_mock_db(
            api_keys_data=[{"id": "uuid-1", "is_active": True}],
            leads_data=[],
        )
        # Ensure the hash lookup returns the active key
        def _chain(table_name):
            tbl = MagicMock()
            result = MagicMock()
            if table_name == "api_keys":
                result.data = [{"id": "uuid-1", "is_active": True}]
            elif table_name == "leads":
                result.data = []
                result.count = 0
            elif table_name == "stats_cache":
                result.data = []
            else:
                result.data = []
                result.count = 0
            tbl.select.return_value = tbl
            tbl.eq.return_value = tbl
            tbl.limit.return_value = tbl
            tbl.execute.return_value = result
            return tbl
        db.table.side_effect = _chain

        with patch("api.routes.outreach_contract.get_db", return_value=db):
            from api.routes.outreach_contract import router
            app = FastAPI()
            app.include_router(router, prefix="/kjle/v1")
            c = TestClient(app, raise_server_exceptions=False)
            resp = c.post(
                "/kjle/v1/outreach/leads/count", json={},
                headers={
                    "Authorization": f"Bearer {consumer_raw_key}",
                    "X-KJE-Service": "outreachenginez",
                },
            )
            assert resp.status_code == 200

    def test_missing_x_kje_service_returns_401(self):
        db = _make_mock_db()
        with patch("api.routes.outreach_contract.get_db", return_value=db):
            from api.routes.outreach_contract import router
            app = FastAPI()
            app.include_router(router, prefix="/kjle/v1")
            c = TestClient(app, raise_server_exceptions=False)
            resp = c.post("/kjle/v1/outreach/leads/count", json={},
                          headers={"Authorization": "Bearer test-outreach-key"})
            assert resp.status_code == 401


# ── Endpoint tests ────────────────────────────────────────────────────────────

def _authed_client(mock_db=None):
    if mock_db is None:
        mock_db = _make_mock_db()
    from api.routes.outreach_contract import router
    app = FastAPI()
    app.include_router(router, prefix="/kjle/v1")
    with patch("api.routes.outreach_contract.get_db", return_value=mock_db):
        pass
    # Return a closure so the patch is applied per-request via fixture
    return app, mock_db


class TestDncCheckEndpoint:
    def _post(self, payload, mock_db=None):
        if mock_db is None:
            mock_db = _make_mock_db()
        from api.routes.outreach_contract import router
        app = FastAPI()
        app.include_router(router, prefix="/kjle/v1")
        with patch("api.routes.outreach_contract.get_db", return_value=mock_db):
            c = TestClient(app, raise_server_exceptions=False)
            return c.post("/kjle/v1/dnc/check", json=payload, headers=VALID_HEADERS)

    def test_empty_items_returns_200(self):
        resp = self._post({"items": []})
        assert resp.status_code == 200
        body = resp.json()
        assert body["results"] == []

    def test_501_items_returns_422(self):
        items = [{"email": f"u{i}@x.com"} for i in range(501)]
        resp = self._post({"items": items})
        assert resp.status_code == 422

    def test_clean_email_not_blocked(self):
        db = _make_mock_db(email_suppression_data=[])
        resp = self._post({"items": [{"email": "clean@example.com"}]}, mock_db=db)
        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["blocked"] is False

    def test_suppressed_email_blocked(self):
        db = _make_mock_db(email_suppression_data=[{"email": "bad@spam.com", "reason": "complaint"}])
        resp = self._post({"items": [{"email": "bad@spam.com"}]}, mock_db=db)
        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["blocked"] is True

    def test_multiple_items(self):
        db = _make_mock_db(email_suppression_data=[{"email": "bad@spam.com", "reason": "unsubscribe"}])
        resp = self._post(
            {"items": [{"email": "clean@example.com"}, {"email": "bad@spam.com"}]},
            mock_db=db,
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert results[0]["blocked"] is False
        assert results[1]["blocked"] is True


class TestDncWriteEndpoint:
    def _post(self, payload, mock_db=None):
        if mock_db is None:
            mock_db = _make_mock_db()
        from api.routes.outreach_contract import router
        app = FastAPI()
        app.include_router(router, prefix="/kjle/v1")
        with patch("api.routes.outreach_contract.get_db", return_value=mock_db):
            c = TestClient(app, raise_server_exceptions=False)
            return c.post("/kjle/v1/dnc", json=payload, headers=VALID_HEADERS)

    def test_missing_all_fields_returns_400(self):
        resp = self._post({"reason": "manual", "source": "test"})
        assert resp.status_code == 400
        # FastAPI wraps HTTPException detail under {"detail": ...}
        assert resp.json()["detail"]["error"]["code"] == "bad_request"

    def test_missing_reason_returns_422(self):
        resp = self._post({"email": "x@y.com", "source": "test"})
        assert resp.status_code == 422

    def test_valid_email_write_returns_201(self):
        resp = self._post({"email": "opt@out.com", "reason": "unsubscribe", "source": "test"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "created"

    def test_invalid_reason_returns_422(self):
        resp = self._post({"email": "x@y.com", "reason": "not_a_valid_reason", "source": "test"})
        assert resp.status_code == 422

    def test_domain_write_returns_201(self):
        resp = self._post({"domain": "spam.com", "reason": "complaint", "source": "test"})
        assert resp.status_code == 201


class TestSignalsEndpoint:
    def _post(self, payload, mock_db=None):
        if mock_db is None:
            mock_db = _make_mock_db()
        from api.routes.outreach_contract import router
        app = FastAPI()
        app.include_router(router, prefix="/kjle/v1")
        with patch("api.routes.outreach_contract.get_db", return_value=mock_db):
            c = TestClient(app, raise_server_exceptions=False)
            return c.post("/kjle/v1/outreach/signals", json=payload, headers=VALID_HEADERS)

    def test_empty_items(self):
        resp = self._post({"items": []})
        assert resp.status_code == 200
        body = resp.json()
        assert body["updated"] == 0

    def test_single_item_all_fields(self):
        resp = self._post({
            "items": [{
                "kjle_lead_id": "lead-abc",
                "form_status": "found",
                "site_dead": False,
                "site_copyright_year": 2020,
                "ssl": True,
                "mobile_ok": True,
                "builder": "wordpress",
                "socials": {"facebook": "fb.com/acme"},
            }]
        })
        assert resp.status_code == 200
        assert resp.json()["updated"] == 1

    def test_no_fields_skips_update(self):
        # Only kjle_lead_id with no signal fields — should skip (no update)
        resp = self._post({"items": [{"kjle_lead_id": "lead-xyz"}]})
        assert resp.status_code == 200
        assert resp.json()["updated"] == 0


class TestLeadsCountEndpoint:
    def _post(self, payload, mock_db=None):
        if mock_db is None:
            mock_db = _make_mock_db(leads_data=[])
        from api.routes.outreach_contract import router
        app = FastAPI()
        app.include_router(router, prefix="/kjle/v1")
        with patch("api.routes.outreach_contract.get_db", return_value=mock_db):
            c = TestClient(app, raise_server_exceptions=False)
            return c.post("/kjle/v1/outreach/leads/count", json=payload, headers=VALID_HEADERS)

    def test_empty_filters_returns_count(self):
        resp = self._post({})
        assert resp.status_code == 200
        body = resp.json()
        assert "count" in body
        assert "as_of" in body

    def test_niche_filter(self):
        resp = self._post({"filters": {"niche_slugs": ["hvac", "plumbing"]}})
        assert resp.status_code == 200


class TestLeadsSearchEndpoint:
    def _post(self, payload, mock_db=None):
        if mock_db is None:
            mock_db = _make_mock_db(leads_data=[])
        from api.routes.outreach_contract import router
        app = FastAPI()
        app.include_router(router, prefix="/kjle/v1")
        with patch("api.routes.outreach_contract.get_db", return_value=mock_db):
            c = TestClient(app, raise_server_exceptions=False)
            return c.post("/kjle/v1/outreach/leads/search", json=payload, headers=VALID_HEADERS)

    def test_empty_request_returns_leads_and_cursor(self):
        resp = self._post({})
        assert resp.status_code == 200
        body = resp.json()
        assert "leads" in body
        assert "next_cursor" in body

    def test_no_cursor_on_partial_page(self):
        # 0 results < default limit → next_cursor should be None
        resp = self._post({"limit": 100})
        assert resp.status_code == 200
        assert resp.json()["next_cursor"] is None

    def test_lead_shape(self):
        lead_row = {
            "id": "uuid-lead-1",
            "business_name": "Acme HVAC",
            "email": "owner@acme.com",
            "email_status": "valid",
            "website": "https://acme.com",
            "phone": "+15550001234",
            "niche_slug": "hvac",
            "city": "Los Angeles",
            "state": "CA",
            "zip": "90001",
            "timezone": "America/Los_Angeles",
            "do_not_contact": False,
            "google_review_count": 10,
            "google_stars": 4.5,
            "form_status": "found",
            "site_dead": False,
            "site_copyright_year": 2022,
            "website_has_ssl": True,
            "mobile_friendly": True,
            "builder": "wordpress",
            "outreach_socials": None,
        }
        db = _make_mock_db(leads_data=[lead_row])
        # Need limit=1 to trigger cursor
        resp = self._post({"limit": 1}, mock_db=db)
        assert resp.status_code == 200
        leads = resp.json()["leads"]
        assert len(leads) == 1
        lead = leads[0]
        # Required fields per contract §6
        assert lead["kjle_lead_id"] == "uuid-lead-1"
        assert lead["business_name"] == "Acme HVAC"
        assert lead["niche_slug"] == "hvac"
        assert lead["tz"] == "America/Los_Angeles"
        assert lead["business_key"] == "acme.com"
        assert "signals" in lead
        assert lead["signals"]["form_status"] == "found"

    def test_exclude_dnc_default(self):
        # Confirm exclude_dnc=True is the default (DNC leads excluded)
        resp = self._post({})
        assert resp.status_code == 200
