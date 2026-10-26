"""Demo-verifiable P1 audit contracts (F01, F02, F03, F06, F07, F10)."""
from __future__ import annotations

import os
import sys
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.main import app, _business_today, _period_key_for
from app.database import SessionLocal
from app import models
from app.redis_support import clear_memory_rate_limits
from app.split_logic import equal_split, calculate_splits
from app.auth import create_access_token

client = TestClient(app)


def _uid(prefix: str = "p1") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}@gmail.com"


def _auth_pair():
    clear_memory_rate_limits()
    email_a, email_b = _uid("a"), _uid("b")
    ra = client.post("/api/auth/register", json={"email": email_a, "password": "Password12!", "display_name": "Alex"})
    rb = client.post("/api/auth/register", json={"email": email_b, "password": "Password12!", "display_name": "Blake"})
    assert ra.status_code == 200 and rb.status_code == 200, (ra.text, rb.text)
    ha = {"Authorization": f"Bearer {ra.json()['access_token']}"}
    hb = {"Authorization": f"Bearer {rb.json()['access_token']}"}
    return email_a, email_b, ha, hb, ra.json()["access_token"]


def _ready_group():
    email_a, email_b, ha, hb, token_a = _auth_pair()
    r = client.post("/api/groups", json={"name": f"P1-{uuid.uuid4().hex[:6]}", "currency": "USD"}, headers=ha)
    assert r.status_code == 200, r.text
    group_id = r.json()["id"]
    friend_account_id = client.get("/api/auth/me", headers=hb).json()["account"]["id"]
    added = client.post(f"/api/groups/{group_id}/members", json={"account_id": friend_account_id}, headers=ha)
    assert added.status_code == 200, added.text
    detail = client.get(f"/api/groups/{group_id}", headers=ha).json()
    by_email = {m["email"]: m for m in detail["members"]}
    return {
        "email_a": email_a,
        "email_b": email_b,
        "ha": ha,
        "hb": hb,
        "token_a": token_a,
        "group_id": group_id,
        "me": by_email[email_a],
        "friend": by_email[email_b],
    }


def test_f01_duplicate_split_rejected_unit():
    with pytest.raises(ValueError, match="Duplicate"):
        equal_split(100, [1, 1, 2])
    with pytest.raises(ValueError, match="Duplicate"):
        calculate_splits("EQUAL", 100, [1, 1, 2])


def test_f01_duplicate_split_rejected_api():
    ctx = _ready_group()
    payload = {
        "store_name": "DupSplit",
        "bill_date": str(_business_today()),
        "total_amount": 100,
        "currency": "USD",
        "category": "Food",
        "split_type": "EQUAL",
        "payer_member_id": ctx["me"]["id"],
        "split_with": [ctx["me"]["id"], ctx["me"]["id"], ctx["friend"]["id"]],
    }
    r = client.post(f"/api/groups/{ctx['group_id']}/expenses", json=payload, headers=ctx["ha"])
    assert r.status_code == 400, r.text
    assert "Duplicate" in r.text


def test_f10_client_period_key_ignored():
    ctx = _ready_group()
    payload = {
        "store_name": "OldReceipt",
        "bill_date": "2001-01-15",
        "total_amount": 40,
        "currency": "USD",
        "category": "Food",
        "split_type": "EQUAL",
        "payer_member_id": ctx["me"]["id"],
        "split_with": [ctx["me"]["id"], ctx["friend"]["id"]],
        "period_key": "2001-01",
    }
    r = client.post(f"/api/groups/{ctx['group_id']}/expenses", json=payload, headers=ctx["ha"])
    assert r.status_code == 200, r.text
    expense_id = r.json()["id"]
    db = SessionLocal()
    try:
        expense = db.query(models.Expense).get(expense_id)
        assert expense is not None
        assert expense.period.period_key == _period_key_for(_business_today())
        assert expense.bill_date == date(2001, 1, 15)
    finally:
        db.close()


def test_f02_password_change_revokes_old_jwt():
    email_a, email_b, ha, hb, old_token = _auth_pair()
    assert client.get("/api/auth/me", headers=ha).status_code == 200
    changed = client.patch(
        "/api/auth/me",
        json={"current_password": "Password12!", "new_password": "Password99!"},
        headers=ha,
    )
    assert changed.status_code == 200, changed.text
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {old_token}"}).status_code == 401
    login = client.post("/api/auth/login", json={"email": email_a, "password": "Password99!"})
    assert login.status_code == 200
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {login.json()['access_token']}"}).status_code == 200


def test_f02_legacy_token_without_sv_rejected():
    email_a, email_b, ha, hb, _token = _auth_pair()
    account_id = client.get("/api/auth/me", headers=ha).json()["account"]["id"]
    legacy = create_access_token(account_id)  # still includes sv=1 by default
    # Forge a token without sv by encoding manually via jose path used in create — use payload without sv
    from jose import jwt
    from app.config import SECRET_KEY
    from datetime import datetime, timedelta, timezone
    forged = jwt.encode(
        {"sub": str(account_id), "exp": datetime.now(timezone.utc) + timedelta(days=1)},
        SECRET_KEY,
        algorithm="HS256",
    )
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {legacy}"}).status_code == 200


def test_f03_debtor_pending_lender_auto_confirm():
    email_a, email_b, ha, hb, _ = _auth_pair()
    alice = client.get("/api/auth/me", headers=ha).json()["account"]["id"]
    bob = client.get("/api/auth/me", headers=hb).json()["account"]["id"]
    client.post("/api/friends/invites", json={"email": email_b}, headers=ha)
    created = client.post(
        "/api/quick-splits",
        json={
            "title": "Coffee",
            "total_amount": 20,
            "currency": "USD",
            "bill_date": str(_business_today()),
            "category": "Food",
            "split_type": "EQUAL",
            "payer_account_id": alice,
            "participants": [{"account_id": bob}],
            "silent_invite": True,
        },
        headers=ha,
    )
    assert created.status_code == 200, created.text

    # Debtor marks payment → PENDING
    debtor = client.post(
        f"/api/friends/{alice}/consolidated-payments",
        json={"amount": 5, "currency": "USD"},
        headers=hb,
    )
    assert debtor.status_code == 200, debtor.text
    assert debtor.json()["status"] == "PENDING"
    bal_before = client.get(f"/api/friends/{bob}/consolidated-balances", headers=ha)
    assert bal_before.status_code == 200
    usd_before = next((b for b in bal_before.json().get("balances", []) if b["currency"] == "USD"), None)
    assert usd_before is not None
    # Still unpaid until confirm
    assert abs(float(usd_before["balance"])) >= 4.9

    # Debtor cannot self-confirm (path friend_id is the counterpart)
    deny = client.post(
        f"/api/friends/{alice}/consolidated-payments/{debtor.json()['id']}/confirm",
        headers=hb,
    )
    assert deny.status_code == 403

    # Lender confirms via the debtor's friend id → applied
    ok = client.post(
        f"/api/friends/{bob}/consolidated-payments/{debtor.json()['id']}/confirm",
        headers=ha,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "CONFIRMED"

    # Lender records received → auto CONFIRMED
    lender = client.post(
        f"/api/friends/{bob}/consolidated-payments",
        json={"amount": 1, "currency": "USD"},
        headers=ha,
    )
    assert lender.status_code == 200, lender.text
    assert lender.json()["status"] == "CONFIRMED"


def test_f06_sync_changes_on_expense_edit():
    ctx = _ready_group()
    payload = {
        "store_name": "SyncBill",
        "bill_date": str(_business_today()),
        "total_amount": 30,
        "currency": "USD",
        "category": "Food",
        "split_type": "EQUAL",
        "payer_member_id": ctx["me"]["id"],
        "split_with": [ctx["me"]["id"], ctx["friend"]["id"]],
    }
    created = client.post(f"/api/groups/{ctx['group_id']}/expenses", json=payload, headers=ctx["ha"])
    assert created.status_code == 200, created.text
    expense_id = created.json()["id"]
    before = client.get(f"/api/groups/{ctx['group_id']}/sync", headers=ctx["ha"])
    assert before.status_code == 200
    v1 = before.json()["version"]
    updated = client.put(
        f"/api/groups/{ctx['group_id']}/expenses/{expense_id}",
        json={**payload, "total_amount": 55, "store_name": "SyncBillEdited"},
        headers=ctx["ha"],
    )
    assert updated.status_code == 200, updated.text
    after = client.get(f"/api/groups/{ctx['group_id']}/sync", headers=ctx["ha"])
    assert after.status_code == 200
    assert after.json()["version"] != v1


def test_f07_rejected_save_keeps_pending_upload_reusable():
    ctx = _ready_group()
    me_account_id = client.get("/api/auth/me", headers=ctx["ha"]).json()["account"]["id"]
    uploads = Path(__file__).resolve().parent / "uploads" / "tmp"
    uploads.mkdir(parents=True, exist_ok=True)
    upload_id = uuid.uuid4().hex
    tmp_path = uploads / f"{ctx['group_id']}_{upload_id}.jpg"
    tmp_path.write_bytes(b"\xff\xd8\xfffake-jpeg")
    db = SessionLocal()
    try:
        db.add(models.PendingUpload(
            id=upload_id,
            group_id=ctx["group_id"],
            account_id=me_account_id,
            file_path=str(tmp_path),
        ))
        db.commit()
    finally:
        db.close()

    # Empty participants fail validation before the receipt is moved.
    bad = client.post(
        f"/api/groups/{ctx['group_id']}/expenses",
        json={
            "upload_id": upload_id,
            "store_name": "OrphanCheck",
            "bill_date": str(_business_today()),
            "total_amount": 12,
            "currency": "USD",
            "category": "Food",
            "split_type": "EQUAL",
            "payer_member_id": ctx["me"]["id"],
            "split_with": [],
        },
        headers=ctx["ha"],
    )
    assert bad.status_code == 400, bad.text
    db = SessionLocal()
    try:
        pending = db.query(models.PendingUpload).get(upload_id)
        assert pending is not None
        assert os.path.exists(pending.file_path)
    finally:
        db.close()

    # Duplicate participants also reject without consuming the upload.
    bad_dup = client.post(
        f"/api/groups/{ctx['group_id']}/expenses",
        json={
            "upload_id": upload_id,
            "store_name": "OrphanCheck",
            "bill_date": str(_business_today()),
            "total_amount": 12,
            "currency": "USD",
            "category": "Food",
            "split_type": "EQUAL",
            "payer_member_id": ctx["me"]["id"],
            "split_with": [ctx["me"]["id"], ctx["me"]["id"], ctx["friend"]["id"]],
        },
        headers=ctx["ha"],
    )
    assert bad_dup.status_code == 400, bad_dup.text
    assert os.path.exists(tmp_path)
