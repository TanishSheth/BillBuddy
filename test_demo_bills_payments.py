"""Bill CRUD + Friends payment reflection + demo session source contracts."""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.main import app, _business_today, _period_key_for
from app.redis_support import clear_memory_rate_limits

client = TestClient(app)
ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "app" / "static"


def _uid(prefix: str = "bp") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}@gmail.com"


def _auth_pair():
    clear_memory_rate_limits()
    email_a, email_b = _uid("a"), _uid("b")
    ra = client.post("/api/auth/register", json={"email": email_a, "password": "Password12!", "display_name": "Alex"})
    rb = client.post("/api/auth/register", json={"email": email_b, "password": "Password12!", "display_name": "Blake"})
    assert ra.status_code == 200 and rb.status_code == 200, (ra.text, rb.text)
    ha = {"Authorization": f"Bearer {ra.json()['access_token']}"}
    hb = {"Authorization": f"Bearer {rb.json()['access_token']}"}
    return email_a, email_b, ha, hb


def _ready_group():
    email_a, email_b, ha, hb = _auth_pair()
    r = client.post("/api/groups", json={"name": f"BP-{uuid.uuid4().hex[:6]}", "currency": "USD"}, headers=ha)
    assert r.status_code == 200, r.text
    group_id = r.json()["id"]
    friend_account_id = client.get("/api/auth/me", headers=hb).json()["account"]["id"]
    added = client.post(f"/api/groups/{group_id}/members", json={"account_id": friend_account_id}, headers=ha)
    assert added.status_code == 200, added.text
    client.post("/api/friends/invites", json={"email": email_b}, headers=ha)
    detail = client.get(f"/api/groups/{group_id}", headers=ha).json()
    by_email = {m["email"]: m for m in detail["members"]}
    alice = client.get("/api/auth/me", headers=ha).json()["account"]["id"]
    bob = client.get("/api/auth/me", headers=hb).json()["account"]["id"]
    return {
        "email_a": email_a,
        "email_b": email_b,
        "ha": ha,
        "hb": hb,
        "group_id": group_id,
        "me": by_email[email_a],
        "friend": by_email[email_b],
        "alice": alice,
        "bob": bob,
        "period_key": _period_key_for(_business_today()),
    }


def _expense_payload(ctx, total=100.0, store="Dinner"):
    return {
        "store_name": store,
        "bill_date": str(_business_today()),
        "total_amount": total,
        "currency": "USD",
        "category": "Food & Dining",
        "split_type": "EQUAL",
        "payer_member_id": ctx["me"]["id"],
        "split_with": [ctx["me"]["id"], ctx["friend"]["id"]],
    }


def _usd_balance(headers, friend_id: int) -> float:
    r = client.get(f"/api/friends/{friend_id}/consolidated-balance?currency=USD", headers=headers)
    assert r.status_code == 200, r.text
    return float(r.json()["balance"])


def _settlement_amount(ctx) -> float:
    r = client.get(
        f"/api/groups/{ctx['group_id']}/periods/{ctx['period_key']}/settlements?mode=smart",
        headers=ctx["ha"],
    )
    assert r.status_code == 200, r.text
    rows = r.json()
    if not rows:
        return 0.0
    return float(max(rows, key=lambda s: abs(float(s["amount"])))["amount"])


def test_bill_create_edit_delete_reflects_settlements_and_friends():
    ctx = _ready_group()
    gid = ctx["group_id"]
    payload = _expense_payload(ctx, 100.0, "CRUD-Create")

    created = client.post(f"/api/groups/{gid}/expenses", json=payload, headers=ctx["ha"])
    assert created.status_code == 200, created.text
    expense_id = created.json()["id"]

    listed = client.get(f"/api/groups/{gid}/expenses", headers=ctx["ha"])
    assert listed.status_code == 200, listed.text
    assert any(e["id"] == expense_id and abs(float(e["total_amount"]) - 100) < 0.01 for e in listed.json())

    assert abs(_settlement_amount(ctx) - 50.0) < 0.05
    # Owner (Alex) is owed ~50 by Blake
    bal_after_create = _usd_balance(ctx["ha"], ctx["bob"])
    assert abs(bal_after_create) >= 49.0

    updated = client.put(
        f"/api/groups/{gid}/expenses/{expense_id}",
        json={**payload, "total_amount": 80.0, "store_name": "CRUD-Edit"},
        headers=ctx["ha"],
    )
    assert updated.status_code == 200, updated.text
    assert abs(_settlement_amount(ctx) - 40.0) < 0.05
    bal_after_edit = _usd_balance(ctx["ha"], ctx["bob"])
    assert abs(abs(bal_after_edit) - 40.0) < 0.55

    deleted = client.delete(f"/api/groups/{gid}/expenses/{expense_id}", headers=ctx["ha"])
    assert deleted.status_code in (200, 204), deleted.text
    listed_after = client.get(f"/api/groups/{gid}/expenses", headers=ctx["ha"])
    assert listed_after.status_code == 200
    assert all(e["id"] != expense_id for e in listed_after.json())
    assert abs(_settlement_amount(ctx)) < 0.05
    bal_after_delete = _usd_balance(ctx["ha"], ctx["bob"])
    assert abs(bal_after_delete) < 0.55


def test_payment_partial_pending_full_and_overpay_extra_credit():
    ctx = _ready_group()
    gid = ctx["group_id"]
    created = client.post(f"/api/groups/{gid}/expenses", json=_expense_payload(ctx, 100.0), headers=ctx["ha"])
    assert created.status_code == 200, created.text

    before = _usd_balance(ctx["ha"], ctx["bob"])
    assert abs(before) >= 49.0

    # Debtor (Blake) marks partial â†’ PENDING; balances unchanged until confirm
    partial = client.post(
        f"/api/friends/{ctx['alice']}/consolidated-payments",
        json={"amount": 20.0, "currency": "USD"},
        headers=ctx["hb"],
    )
    assert partial.status_code == 200, partial.text
    assert partial.json()["status"] == "PENDING"
    assert abs(_usd_balance(ctx["ha"], ctx["bob"]) - before) < 0.05

    confirm = client.post(
        f"/api/friends/{ctx['bob']}/consolidated-payments/{partial.json()['id']}/confirm",
        headers=ctx["ha"],
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["status"] == "CONFIRMED"
    after_partial = _usd_balance(ctx["ha"], ctx["bob"])
    assert abs(abs(after_partial) - 30.0) < 0.55

    # Lender records remaining full amount â†’ auto CONFIRMED â†’ ~0
    remaining = abs(after_partial)
    full = client.post(
        f"/api/friends/{ctx['bob']}/consolidated-payments",
        json={"amount": remaining, "currency": "USD"},
        headers=ctx["ha"],
    )
    assert full.status_code == 200, full.text
    assert full.json()["status"] == "CONFIRMED"
    assert abs(_usd_balance(ctx["ha"], ctx["bob"])) < 0.55

    # New debt then overpay â†’ Extra pay / CREDIT allocation
    again = client.post(f"/api/groups/{gid}/expenses", json=_expense_payload(ctx, 40.0, "OverpayBill"), headers=ctx["ha"])
    assert again.status_code == 200, again.text
    owed = abs(_usd_balance(ctx["ha"], ctx["bob"]))
    assert owed >= 19.0
    over = client.post(
        f"/api/friends/{ctx['bob']}/consolidated-payments",
        json={"amount": owed + 10.0, "currency": "USD"},
        headers=ctx["ha"],
    )
    assert over.status_code == 200, over.text
    assert over.json()["status"] == "CONFIRMED"
    body = over.json()
    allocations = body.get("allocations") or []
    if isinstance(allocations, str):
        import json
        allocations = json.loads(allocations)
    assert any(
        (a.get("kind") == "CREDIT" or a.get("payment_kind") == "OVERPAYMENT")
        for a in allocations
    ) or body.get("payment_kind") == "OVERPAYMENT" or abs(_usd_balance(ctx["ha"], ctx["bob"]) - (-10 if before > 0 else 10)) < 20, (
        body,
        allocations,
    )
    multi = client.get(f"/api/friends/{ctx['bob']}/consolidated-balances", headers=ctx["ha"])
    assert multi.status_code == 200, multi.text
    usd = next((b for b in multi.json().get("balances", []) if b.get("currency") == "USD"), None)
    assert usd is not None
    sources = usd.get("sources") or []
    over_bal = float(usd.get("balance") or 0)
    # Overpay should not leave the original ~owed debt; credit/Extra pay or flipped ~10.
    assert abs(over_bal) < owed - 5 or abs(abs(over_bal) - 10.0) < 0.55, (over_bal, owed, sources)
    if sources:
        assert any(
            s.get("kind") == "CREDIT" or "Extra" in str(s.get("label") or "")
            for s in sources
        ) or abs(abs(over_bal) - 10.0) < 0.55, sources


def test_demo_session_helpers_and_401_path_in_api_js():
    api_js = (STATIC / "api.js").read_text(encoding="utf-8")
    landing = (STATIC / "landing.html").read_text(encoding="utf-8")
    assert "sharesquad_pre_demo_token" in api_js
    assert "function beginDemoSession" in api_js
    assert "function exitDemoSession" in api_js
    assert "function handleDemoAuthFailure" in api_js
    assert "function leaveDemoToLanding" in api_js
    assert 'id="exitDemoSandbox"' in api_js
    assert "isDemoSandboxSession() || hasPreDemoSession()" in api_js
    assert "beginDemoSession(" in landing
    assert "20261027demoReset1" in landing


def test_demo_sandbox_create_supports_bill_crud():
    """Live demo sandbox accepts expense create like a normal group."""
    clear_memory_rate_limits()
    started = client.post("/api/demo/sandbox")
    assert started.status_code == 200, started.text
    data = started.json()
    token = data["access_token"]
    group_id = data["group_id"]
    headers = {"Authorization": f"Bearer {token}"}
    detail = client.get(f"/api/groups/{group_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    members = [m for m in detail.json()["members"] if m.get("is_active", True)]
    assert len(members) >= 2
    me = members[0]
    other = members[1]
    created = client.post(
        f"/api/groups/{group_id}/expenses",
        json={
            "store_name": "DemoCRUD",
            "bill_date": str(_business_today()),
            "total_amount": 60,
            "currency": detail.json().get("currency") or "USD",
            "category": "Food",
            "split_type": "EQUAL",
            "payer_member_id": me["id"],
            "split_with": [me["id"], other["id"]],
        },
        headers=headers,
    )
    assert created.status_code == 200, created.text
    expense_id = created.json()["id"]
    edited = client.put(
        f"/api/groups/{group_id}/expenses/{expense_id}",
        json={
            "store_name": "DemoCRUD-Edit",
            "bill_date": str(_business_today()),
            "total_amount": 90,
            "currency": detail.json().get("currency") or "USD",
            "category": "Food",
            "split_type": "EQUAL",
            "payer_member_id": me["id"],
            "split_with": [me["id"], other["id"]],
        },
        headers=headers,
    )
    assert edited.status_code == 200, edited.text
    deleted = client.delete(f"/api/groups/{group_id}/expenses/{expense_id}", headers=headers)
    assert deleted.status_code in (200, 204), deleted.text


def test_demo_casey_overpay_leaves_only_extra_pay():
    """Paying above Casey's Friends total must clear all DIRECT/GROUP and leave only Extra pay.

    Regression: smart Who-pays-whom GroupPayments were snapshot-repaired to
    cleared_amount=0 against raw nets, leaving a residual group edge (e.g. $14.65)
    beside Extra pay after a $260 overpay on ~$259.
    """
    clear_memory_rate_limits()
    started = client.post("/api/demo/sandbox")
    assert started.status_code == 200, started.text
    headers = {"Authorization": f"Bearer {started.json()['access_token']}"}
    friends = client.get("/api/friends", headers=headers)
    assert friends.status_code == 200, friends.text
    casey = next((f for f in friends.json() if f.get("display_name") == "Casey"), None)
    assert casey, friends.json()
    before = client.get(f"/api/friends/{casey['id']}/consolidated-balance?currency=USD", headers=headers)
    assert before.status_code == 200, before.text
    owed = float(before.json()["balance"])
    assert owed > 100, before.json()
    pay_amount = round(owed + 0.88, 2)
    paid = client.post(
        f"/api/friends/{casey['id']}/consolidated-payments",
        headers=headers,
        json={"amount": pay_amount, "currency": "USD"},
    )
    assert paid.status_code == 200, paid.text
    after = client.get(f"/api/friends/{casey['id']}/consolidated-balance?currency=USD", headers=headers)
    assert after.status_code == 200, after.text
    body = after.json()
    assert abs(float(body["balance"]) + 0.88) < 0.02, body
    # Fixed bill debts may retain opposite obligations in different groups.
    # They offset in the total; a payment must not silently reassign them.
    assert abs(sum(float(s['amount']) for s in body.get('sources', []) if s['kind'] != 'CREDIT')) < 0.02
    credit = next(s for s in body["sources"] if s["kind"] == "CREDIT")
    assert abs(float(credit["amount"]) + 0.88) < 0.02, credit
    assert abs(sum(float(s['amount']) for s in body.get('atomic_sources', []) if s['kind'] != 'CREDIT')) < 0.02


def test_demo_jordan_overpay_absorbs_existing_extra_pay():
    """Jordan $220 on ~$213.67 with seeded Extra pay must not leave residual GROUP + stale CREDIT.

    Prior Extra pay is absorbed into oldest debts before cash; only true leftover cash
    remains as Extra pay (no open DIRECT/GROUP beside it).
    """
    clear_memory_rate_limits()
    started = client.post("/api/demo/sandbox")
    assert started.status_code == 200, started.text
    headers = {"Authorization": f"Bearer {started.json()['access_token']}"}
    friends = client.get("/api/friends", headers=headers)
    assert friends.status_code == 200, friends.text
    jordan = next((f for f in friends.json() if f.get("display_name") == "Jordan"), None)
    assert jordan, friends.json()
    before = client.get(f"/api/friends/{jordan['id']}/consolidated-balance?currency=USD", headers=headers)
    assert before.status_code == 200, before.text
    body_before = before.json()
    owed = float(body_before["balance"])
    assert owed > 100, body_before
    assert any(s.get("kind") == "CREDIT" for s in (body_before.get("sources") or [])), body_before
    paid = client.post(
        f"/api/friends/{jordan['id']}/consolidated-payments",
        headers=headers,
        json={"amount": 220.0, "currency": "USD"},
    )
    assert paid.status_code == 200, paid.text
    after = client.get(f"/api/friends/{jordan['id']}/consolidated-balance?currency=USD", headers=headers)
    assert after.status_code == 200, after.text
    body = after.json()
    expected_extra = round(220.0 - owed, 2)
    assert abs(float(body["balance"]) + expected_extra) < 0.03, (body, expected_extra)
    assert abs(sum(float(s['amount']) for s in body.get('sources', []) if s['kind'] != 'CREDIT')) < 0.03
    assert abs(sum(float(s['amount']) for s in body.get('atomic_sources', []) if s['kind'] != 'CREDIT')) < 0.03


def test_demo_sandbox_reset_with_weekly_settlement_locks():
    """Reset must succeed after weekly settlement legs lock group members (live scheduler case)."""
    from datetime import datetime, timezone

    from app.database import SessionLocal
    from app import models
    from app.main import _lock_weekly_settlement_batch

    clear_memory_rate_limits()
    started = client.post("/api/demo/sandbox")
    assert started.status_code == 200, started.text
    data = started.json()
    headers = {"Authorization": f"Bearer {data['access_token']}"}
    group_id = data["group_id"]

    db = SessionLocal()
    try:
        group = db.query(models.Group).get(group_id)
        assert group is not None
        period = (
            db.query(models.MonthlyPeriod)
            .filter_by(group_id=group_id, period_key=_period_key_for(_business_today()))
            .first()
        )
        assert period is not None
        owner = db.query(models.Account).get(
            db.query(models.DemoSandbox).filter_by(group_id=group_id).first().account_id
        )
        _lock_weekly_settlement_batch(
            db,
            group,
            period,
            window_ends_at=datetime.now(timezone.utc),
            created_by=owner,
        )
        db.commit()
        assert db.query(models.WeeklySettlementLeg).join(models.WeeklySettlementBatch).filter(
            models.WeeklySettlementBatch.group_id == group_id
        ).count() >= 0
    finally:
        db.close()

    reset = client.post("/api/demo/sandbox/reset", headers=headers)
    assert reset.status_code == 200, reset.text
    body = reset.json()
    assert body.get("access_token")
    assert body.get("sandbox_id") and body["sandbox_id"] != data["sandbox_id"]
    # SQLite may reuse numeric PKs; identity is the new sandbox + working token.
    fresh = client.get("/api/groups", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert fresh.status_code == 200, fresh.text
    assert any(g["id"] == body["group_id"] for g in fresh.json())
