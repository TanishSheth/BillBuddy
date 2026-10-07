"""D7: Insights FX must convert any supported expense currency into any group currency."""
import os
from datetime import date
from decimal import Decimal

import pytest

os.environ.setdefault("BILLBUDDY_ENV", "development")

from fastapi.testclient import TestClient

from app import models
from app.auth import hash_password
from app.database import Base, SessionLocal, engine
from app.fx_service import SUPPORTED_CURRENCIES, get_conversion_rate
from app.main import _backfill_expense_fx_rates, _fx_rate_between, app, get_group_dashboard

client = TestClient(app)

# Representative pairs across major bases — not only INR→USD.
PAIRS = [
    ("USD", "EUR", 100),
    ("EUR", "GBP", 100),
    ("GBP", "CAD", 100),
    ("CAD", "AUD", 100),
    ("AUD", "JPY", 100),
    ("JPY", "USD", 10000),
    ("INR", "AED", 5000),
    ("SGD", "CHF", 100),
    ("MXN", "BRL", 1000),
    ("THB", "NZD", 2000),
    ("CNY", "USD", 500),
]


def _seed_accounts(owner_email: str, friend_email: str):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        ids = []
        for email, name in ((owner_email, "FX Any"), (friend_email, "FX Friend")):
            row = db.query(models.Account).filter_by(email=email).first()
            if not row:
                row = models.Account(
                    email=email,
                    password_hash=hash_password("Password12!"),
                    display_name=name,
                )
                db.add(row)
                db.commit()
                db.refresh(row)
            ids.append(row.id)
        return ids[0], ids[1]
    finally:
        db.close()


@pytest.mark.parametrize("group_ccy,expense_ccy,amount", PAIRS)
def test_create_expense_fx_any_pair(group_ccy, expense_ccy, amount):
    assert group_ccy in SUPPORTED_CURRENCIES and expense_ccy in SUPPORTED_CURRENCIES
    pid = os.getpid()
    email = f"d7any_{group_ccy}_{expense_ccy}_{pid}@example.com".lower()
    friend_email = f"d7anyf_{group_ccy}_{expense_ccy}_{pid}@gmail.com".lower()
    owner_id, friend_id = _seed_accounts(email, friend_email)

    db = SessionLocal()
    try:
        group = models.Group(name=f"{group_ccy} base", currency=group_ccy, created_by_id=owner_id)
        db.add(group)
        db.commit()
        db.refresh(group)
        m1 = models.GroupMember(
            group_id=group.id, account_id=owner_id, display_name="O", role="owner", is_active=True,
        )
        m2 = models.GroupMember(
            group_id=group.id, account_id=friend_id, display_name="F", role="member", is_active=True,
        )
        db.add_all([m1, m2])
        db.commit()
        db.refresh(m1)
        db.refresh(m2)
        group_id, member_id, friend_member_id = group.id, m1.id, m2.id
    finally:
        db.close()

    token = client.post("/api/auth/login", json={"email": email, "password": "Password12!"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    res = client.post(
        f"/api/groups/{group_id}/expenses",
        headers=headers,
        json={
            "store_name": f"{expense_ccy} shop",
            "bill_date": date.today().isoformat(),
            "total_amount": amount,
            "currency": expense_ccy,
            "category": "Food & Dining",
            "split_type": "EQUAL",
            "payer_member_id": member_id,
            "split_with": [member_id, friend_member_id],
        },
    )
    assert res.status_code == 200, res.text
    expense_id = res.json()["id"]

    expected = get_conversion_rate(expense_ccy, group_ccy)
    db = SessionLocal()
    try:
        expense = db.query(models.Expense).get(expense_id)
        rate = float(expense.fx_rate_to_group or 0)
        assert rate > 0
        assert abs(rate - expected) < max(0.01, abs(expected) * 0.05)
        # Must not treat foreign amounts as already in group currency.
        if abs(expected - 1.0) > 0.05:
            assert abs(rate - 1.0) > 0.01
        owner = db.query(models.Account).get(owner_id)
        dash = get_group_dashboard(group_id, date.today().strftime("%Y-%m"), owner, db)
        assert dash.currency == group_ccy
        expected_total = round(amount * expected, 2)
        assert abs(dash.total_spent - expected_total) < max(0.5, abs(expected_total) * 0.05)
        # Raw foreign total must not appear as group spend when rates differ.
        if abs(expected - 1.0) > 0.05:
            assert abs(dash.total_spent - amount) > 1.0
    finally:
        db.close()


@pytest.mark.parametrize("group_ccy,expense_ccy,amount", PAIRS[:6])
def test_backfill_and_dashboard_live_resolve_any_pair(group_ccy, expense_ccy, amount):
    pid = os.getpid()
    email = f"d7bf_{group_ccy}_{expense_ccy}_{pid}@example.com".lower()
    friend_email = f"d7bff_{group_ccy}_{expense_ccy}_{pid}@gmail.com".lower()
    owner_id, _friend_id = _seed_accounts(email, friend_email)
    db = SessionLocal()
    try:
        group = models.Group(name=f"BF {group_ccy}", currency=group_ccy, created_by_id=owner_id)
        db.add(group)
        db.commit()
        db.refresh(group)
        member = models.GroupMember(
            group_id=group.id, account_id=owner_id, display_name="O", role="owner", is_active=True,
        )
        db.add(member)
        db.commit()
        db.refresh(member)
        period = models.MonthlyPeriod(
            group_id=group.id, period_key=date.today().strftime("%Y-%m"), status="ACTIVE",
        )
        db.add(period)
        db.commit()
        db.refresh(period)
        expense = models.Expense(
            period_id=period.id,
            store_name="Stuck rate",
            bill_date=date.today(),
            total_amount=Decimal(str(amount)),
            currency=expense_ccy,
            category="General",
            split_type="EQUAL",
            fx_rate_to_group=Decimal("1.0"),
            payer_member_id=member.id,
        )
        db.add(expense)
        db.flush()
        db.add(models.ExpenseSplit(
            expense_id=expense.id, member_id=member.id, share_amount=Decimal(str(amount)),
        ))
        db.commit()
        expense_id, group_id = expense.id, group.id
    finally:
        db.close()

    # Dashboard must convert even before backfill (live fallback).
    db = SessionLocal()
    try:
        owner = db.query(models.Account).get(owner_id)
        dash = get_group_dashboard(group_id, date.today().strftime("%Y-%m"), owner, db)
        expected = get_conversion_rate(expense_ccy, group_ccy)
        expected_total = round(amount * expected, 2)
        assert abs(dash.total_spent - expected_total) < max(0.5, abs(expected_total) * 0.05)
    finally:
        db.close()

    _backfill_expense_fx_rates()
    db = SessionLocal()
    try:
        expense = db.query(models.Expense).get(expense_id)
        rate = float(expense.fx_rate_to_group or 0)
        expected = get_conversion_rate(expense_ccy, group_ccy)
        assert abs(rate - expected) < max(0.01, abs(expected) * 0.05)
    finally:
        db.close()


def test_fx_rate_between_covers_supported_matrix_sample():
    samples = [
        ("USD", "INR"), ("INR", "USD"), ("EUR", "JPY"), ("AUD", "CAD"),
        ("GBP", "CHF"), ("AED", "SGD"), ("BRL", "MXN"), ("THB", "CNY"),
    ]
    for src, dest in samples:
        rate = float(_fx_rate_between(src, dest))
        assert rate > 0
        assert abs(rate - get_conversion_rate(src, dest)) < 0.01
        assert float(_fx_rate_between(src, src)) == 1.0
