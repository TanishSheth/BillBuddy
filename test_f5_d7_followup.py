"""Follow-up fixes: FX backfill, delete double-credit, ID reuse, settlement ccy, past-month adj, history UI."""
import os
from datetime import date, timedelta
from decimal import Decimal

os.environ.setdefault("BILLBUDDY_ENV", "development")

from fastapi.testclient import TestClient

from app import models
from app.auth import hash_password
from app.database import Base, SessionLocal, engine
from app.fx_service import get_conversion_rate
from app.main import _backfill_expense_fx_rates, app, get_group_dashboard

client = TestClient(app)


def _pair(pid: str):
    email_a = f"f5a_{pid}@example.com"
    email_b = f"f5b_{pid}@gmail.com"
    ra = client.post("/api/auth/register", json={"email": email_a, "password": "Password12!", "display_name": "A"})
    rb = client.post("/api/auth/register", json={"email": email_b, "password": "Password12!", "display_name": "B"})
    if ra.status_code != 200:
        ra = client.post("/api/auth/login", json={"email": email_a, "password": "Password12!"})
        rb = client.post("/api/auth/login", json={"email": email_b, "password": "Password12!"})
    ha = {"Authorization": f"Bearer {ra.json()['access_token']}"}
    hb = {"Authorization": f"Bearer {rb.json()['access_token']}"}
    alice = client.get("/api/auth/me", headers=ha).json()["account"]["id"]
    bob = client.get("/api/auth/me", headers=hb).json()["account"]["id"]
    client.post("/api/friends/invites", json={"email": email_b}, headers=ha)
    return ha, hb, alice, bob


def test_fx_backfill_fixes_old_inr_expense():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        owner = models.Account(
            email=f"fxbf_owner_{os.getpid()}@example.com",
            password_hash=hash_password("Password12!"),
            display_name="BF Owner",
        )
        db.add(owner)
        db.commit()
        db.refresh(owner)
        group = models.Group(name="BF FX", currency="USD", created_by_id=owner.id)
        db.add(group)
        db.commit()
        db.refresh(group)
        member = models.GroupMember(
            group_id=group.id, account_id=owner.id, display_name="O", role="owner", is_active=True,
        )
        db.add(member)
        db.commit()
        db.refresh(member)
        period = models.MonthlyPeriod(group_id=group.id, period_key=date.today().strftime("%Y-%m"), status="ACTIVE")
        db.add(period)
        db.commit()
        db.refresh(period)
        expense = models.Expense(
            period_id=period.id,
            store_name="Old Delhi",
            bill_date=date.today(),
            total_amount=Decimal("1000.00"),
            currency="INR",
            category="Food & Dining",
            split_type="EQUAL",
            fx_rate_to_group=Decimal("1.0"),  # pre-D7 bug
            payer_member_id=member.id,
        )
        db.add(expense)
        db.flush()
        db.add(models.ExpenseSplit(expense_id=expense.id, member_id=member.id, share_amount=Decimal("1000.00")))
        db.commit()
        expense_id, group_id, owner_id = expense.id, group.id, owner.id
    finally:
        db.close()

    _backfill_expense_fx_rates()

    db = SessionLocal()
    try:
        expense = db.query(models.Expense).get(expense_id)
        rate = float(expense.fx_rate_to_group or 0)
        expected = get_conversion_rate("INR", "USD")
        assert abs(rate - 1.0) > 0.01
        assert abs(rate - expected) < 0.01
        owner = db.query(models.Account).get(owner_id)
        dash = get_group_dashboard(group_id, date.today().strftime("%Y-%m"), owner, db)
        assert dash.total_spent < 50
    finally:
        db.close()


def test_delete_after_monthly_settlement_does_not_double_credit():
    pid = f"{os.getpid()}_del"
    ha, hb, alice, bob = _pair(pid)
    bill_date = (date.today().replace(day=1) - timedelta(days=1))  # last month
    month_key = bill_date.strftime("%Y-%m")

    created = client.post(
        "/api/quick-splits",
        headers=ha,
        json={
            "title": "Settled dinner",
            "total_amount": 40,
            "currency": "CAD",
            "bill_date": bill_date.isoformat(),
            "category": "Food & Dining",
            "split_type": "EQUAL",
            "payer_account_id": alice,
            "participants": [{"account_id": bob}],
            "silent_invite": True,
        },
    )
    assert created.status_code == 200, created.text
    split_id = created.json()["id"]

    settled = client.post(
        f"/api/friends/{alice}/settlements/confirm",
        headers=hb,
        json={"month_key": month_key, "action": "paid"},
    )
    assert settled.status_code == 200, settled.text
    assert settled.json()["currency"] == "CAD"

    before = client.get(f"/api/friends/{alice}/consolidated-balances", headers=hb).json()
    cad_before = next((b for b in before["balances"] if b["currency"] == "CAD"), None)
    bal_before = float(cad_before["balance"]) if cad_before else 0.0

    deleted = client.delete(f"/api/quick-splits/{split_id}", headers=ha)
    assert deleted.status_code == 200, deleted.text

    after = client.get(f"/api/friends/{alice}/consolidated-balances", headers=hb).json()
    cad_after = next((b for b in after["balances"] if b["currency"] == "CAD"), None)
    bal_after = float(cad_after["balance"]) if cad_after else 0.0
    # Bob paid 20 CAD share; after delete he should get ~20 credit (not ~40).
    # From Bob's view: positive = Alice owes Bob.
    credit = bal_after - bal_before
    assert 15 <= credit <= 25, f"expected ~20 credit, got {credit} (before={bal_before}, after={bal_after})"


def test_reused_bill_id_does_not_inherit_paid_edit_state():
    pid = f"{os.getpid()}_reuse"
    ha, hb, alice, bob = _pair(pid)
    created = client.post(
        "/api/quick-splits",
        headers=ha,
        json={
            "title": "Old paid",
            "total_amount": 50,
            "currency": "USD",
            "bill_date": date.today().isoformat(),
            "category": "Food & Dining",
            "split_type": "EQUAL",
            "payer_account_id": alice,
            "participants": [{"account_id": bob}],
            "silent_invite": True,
        },
    )
    assert created.status_code == 200, created.text
    old_id = created.json()["id"]
    paid = client.post(
        f"/api/friends/{alice}/consolidated-payments",
        headers=hb,
        json={"amount": 25, "currency": "USD"},
    )
    assert paid.status_code == 200, paid.text
    assert client.delete(f"/api/quick-splits/{old_id}", headers=ha).status_code == 200

    again = client.post(
        "/api/quick-splits",
        headers=ha,
        json={
            "title": "Brand new",
            "total_amount": 30,
            "currency": "USD",
            "bill_date": date.today().isoformat(),
            "category": "Food & Dining",
            "split_type": "EQUAL",
            "payer_account_id": alice,
            "participants": [{"account_id": bob}],
            "silent_invite": True,
        },
    )
    assert again.status_code == 200, again.text
    body = again.json()
    # SQLite often reuses the id; either way the new bill must be clean.
    assert body["edited_after_payment"] is False
    assert body.get("edit_history") in ([], None)

    edited = client.put(
        f"/api/quick-splits/{body['id']}",
        headers=ha,
        json={
            "title": "Brand new",
            "total_amount": 36,
            "currency": "USD",
            "bill_date": date.today().isoformat(),
            "category": "Food & Dining",
            "split_type": "EQUAL",
            "payer_account_id": alice,
            "participants": [{"account_id": bob}],
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["edited_after_payment"] is False


def test_monthly_settlement_uses_bill_currency():
    pid = f"{os.getpid()}_ccy"
    ha, hb, alice, bob = _pair(pid)
    bill_date = date.today().replace(day=1) - timedelta(days=1)
    month_key = bill_date.strftime("%Y-%m")
    created = client.post(
        "/api/quick-splits",
        headers=ha,
        json={
            "title": "CAD only",
            "total_amount": 40,
            "currency": "CAD",
            "bill_date": bill_date.isoformat(),
            "category": "Food & Dining",
            "split_type": "EQUAL",
            "payer_account_id": alice,
            "participants": [{"account_id": bob}],
            "silent_invite": True,
        },
    )
    assert created.status_code == 200, created.text

    # Poison an existing PENDING settlement with the wrong currency label.
    db = SessionLocal()
    try:
        one, two = sorted((alice, bob))
        row = models.FriendMonthlySettlement(
            account_one_id=one,
            account_two_id=two,
            month_key=month_key,
            payer_account_id=bob,
            recipient_account_id=alice,
            amount=Decimal("20.00"),
            currency="GBP",
            status="PENDING",
        )
        db.add(row)
        db.commit()
    finally:
        db.close()

    settled = client.post(
        f"/api/friends/{alice}/settlements/confirm",
        headers=hb,
        json={"month_key": month_key, "action": "paid"},
    )
    assert settled.status_code == 200, settled.text
    assert settled.json()["currency"] == "CAD"
    assert float(settled.json()["amount"]) == 20.0


def test_past_month_combined_pay_edit_creates_current_adjustment():
    pid = f"{os.getpid()}_past"
    ha, hb, alice, bob = _pair(pid)
    bill_date = date.today().replace(day=1) - timedelta(days=1)
    created = client.post(
        "/api/quick-splits",
        headers=ha,
        json={
            "title": "Past cafe",
            "total_amount": 100,
            "currency": "USD",
            "bill_date": bill_date.isoformat(),
            "category": "Food & Dining",
            "split_type": "EQUAL",
            "payer_account_id": alice,
            "participants": [{"account_id": bob}],
            "silent_invite": True,
        },
    )
    assert created.status_code == 200, created.text
    split_id = created.json()["id"]
    paid = client.post(
        f"/api/friends/{alice}/consolidated-payments",
        headers=hb,
        json={"amount": 50, "currency": "USD"},
    )
    assert paid.status_code == 200, paid.text

    edited = client.put(
        f"/api/quick-splits/{split_id}",
        headers=ha,
        json={
            "title": "Past cafe",
            "total_amount": 120,
            "currency": "USD",
            "bill_date": bill_date.isoformat(),
            "category": "Food & Dining",
            "split_type": "EQUAL",
            "payer_account_id": alice,
            "participants": [{"account_id": bob}],
        },
    )
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert body["edited_after_payment"] is True
    assert body["bill_date"] == bill_date.isoformat()
    assert any(h.get("delta") == 10.0 for h in body.get("edit_history") or [])

    splits = client.get("/api/quick-splits", headers=ha).json()
    adjustments = [
        s for s in splits
        if s.get("adjustment_of_split_id") == split_id
        and str(s.get("bill_date", "")).startswith(date.today().strftime("%Y-%m"))
    ]
    assert len(adjustments) == 1
    assert float(adjustments[0]["total_amount"]) == 10.0


def test_friends_js_renders_edit_history():
    js = open(
        os.path.join(os.path.dirname(__file__), "app", "static", "friends.js"),
        encoding="utf-8",
    ).read()
    assert "edit_history" in js
    assert "Edit history" in js
