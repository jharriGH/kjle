"""Card 3068: contacts:read scope for consumer keys + CEz campaign exclusion."""
import hashlib

import pytest
from fastapi import HTTPException

import api.routes.contacts as c


class _Q:
    def __init__(self, rows):
        self._rows = rows
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def limit(self, *a, **k): return self
    def range(self, *a, **k): return self
    def update(self, *a, **k): return self
    def execute(self):
        class R: pass
        r = R(); r.data = self._rows
        return r


class _DB:
    def __init__(self, keys=None, sends=None):
        self.keys, self.sends = keys or [], sends or []
    def table(self, name):
        return _Q(self.keys if name == "api_keys" else self.sends)


def test_master_key_passes():
    assert c.verify_contacts_read_key(c.API_SECRET_KEY, _DB()) == "master"


def test_consumer_key_needs_explicit_scope():
    db = _DB(keys=[{"id": 1, "label": "outreachenginez", "scope": ["read", "write"], "is_active": True}])
    with pytest.raises(HTTPException):
        c.verify_contacts_read_key("kjle_x", db)


def test_consumer_key_with_scope_passes():
    db = _DB(keys=[{"id": 1, "label": "outreachenginez", "scope": ["read", "contacts:read"], "is_active": True}])
    assert c.verify_contacts_read_key("kjle_x", db) == "outreachenginez"


def test_inactive_key_rejected():
    db = _DB(keys=[{"id": 1, "label": "oe", "scope": ["contacts:read"], "is_active": False}])
    with pytest.raises(HTTPException):
        c.verify_contacts_read_key("kjle_x", db)


def test_unknown_key_rejected():
    with pytest.raises(HTTPException):
        c.verify_contacts_read_key("nope", _DB())


def test_recently_contacted_lowercases():
    db = _DB(sends=[{"to_email": "A@B.com "}, {"to_email": "c@d.com"}])
    assert c._recently_contacted_emails(db, ["rek_showcase_w1"], 90) == {"a@b.com", "c@d.com"}
