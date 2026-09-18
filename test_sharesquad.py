"""
Automated Test Suite for ShareSquad
Tests:
1. Split Math: Equal, Percentage, Exact, Shares
2. FX Exchange Rate & Currency Conversion
3. Greedy Debt Simplification
4. Database Tables & CRUD Integrity
"""
import sys
import os
from decimal import Decimal
from datetime import date

# Add directory to sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.split_logic import (
    equal_split,
    percentage_split,
    exact_split,
    shares_split,
    calculate_splits,
    compute_balances,
    simplify_debts,
    graph_constrained_settlements,
)
from app.fx_service import get_exchange_rates, get_conversion_rate, convert_amount
from app.database import Base, engine, SessionLocal
from app import models


def test_split_logic():
    print("Testing Split Logic...")

    # 1. Equal Split ($100 among 3 people -> 33.34, 33.33, 33.33)
    eq = equal_split(100.0, [1, 2, 3])
    assert sum(eq.values()) == Decimal("100.00"), f"Equal split sum mismatch: {sum(eq.values())}"
    assert eq[1] == Decimal("33.34") and eq[2] == Decimal("33.33") and eq[3] == Decimal("33.33")

    eq_rot = equal_split(100.0, [1, 2, 3], remainder_offset=1)
    assert sum(eq_rot.values()) == Decimal("100.00")
    assert eq_rot[1] == Decimal("33.33") and eq_rot[2] == Decimal("33.34") and eq_rot[3] == Decimal("33.33")
    eq_rot2 = equal_split(100.0, [1, 2, 3], remainder_offset=2)
    assert eq_rot2[3] == Decimal("33.34")

    # 2. Percentage Split ($200 with 15%, 40%, 45% -> $30.00, $80.00, $90.00)
    pct = percentage_split(200.0, {1: 15.0, 2: 40.0, 3: 45.0})
    assert sum(pct.values()) == Decimal("200.00"), f"Pct split sum mismatch: {sum(pct.values())}"
    assert pct[1] == Decimal("30.00") and pct[2] == Decimal("80.00") and pct[3] == Decimal("90.00")

    # 3. Exact Split ($150 with $50.50, $99.50)
    ex = exact_split(150.0, {1: 50.50, 2: 99.50})
    assert sum(ex.values()) == Decimal("150.00"), f"Exact split sum mismatch: {sum(ex.values())}"
    assert ex[1] == Decimal("50.50") and ex[2] == Decimal("99.50")

    # 4. Shares Split ($90 with 1 share, 2 shares -> $30.00, $60.00)
    sh = shares_split(90.0, {1: 1, 2: 2})
    assert sum(sh.values()) == Decimal("90.00"), f"Shares split sum mismatch: {sum(sh.values())}"
    assert sh[1] == Decimal("30.00") and sh[2] == Decimal("60.00")

    # 5. Dispatcher
    d1 = calculate_splits("PERCENTAGE", 100.0, [{"member_id": 1, "value": 50}, {"member_id": 2, "value": 50}])
    assert d1[1] == Decimal("50.00") and d1[2] == Decimal("50.00")

    print("  [PASS] Split logic tests passed!")


def test_currency_conversion():
    print("Testing Currency Service...")
    rates = get_exchange_rates("USD")
    assert "INR" in rates, "INR not in exchange rates"
    assert "EUR" in rates, "EUR not in exchange rates"

    converted, rate = convert_amount(100.0, "USD", "INR", custom_rate=83.50)
    assert converted == Decimal("8350.00"), f"Expected 8350.00, got {converted}"
    assert rate == Decimal("83.50")

    print(f"  [PASS] FX Service tests passed! (1 USD = {rates.get('INR')} INR)")


def test_debt_simplification():
    print("Testing Debt Simplification...")
    members = [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}, {"id": 3, "name": "Charlie"}]
    # Alice paid $90 for Alice, Bob, Charlie ($30 each)
    expenses = [
        {
            "payer_id": 1,
            "total_amount": 90.0,
            "splits": [
                {"user_id": 1, "share_amount": 30.0},
                {"user_id": 2, "share_amount": 30.0},
                {"user_id": 3, "share_amount": 30.0},
            ],
        }
    ]
    balances = compute_balances(members, expenses)
    assert balances[1]["net_balance"] == 60.0
    assert balances[2]["net_balance"] == -30.0
    assert balances[3]["net_balance"] == -30.0

    settlements = simplify_debts(balances)
    assert len(settlements) == 2
    # Bob pays Alice 30, Charlie pays Alice 30
    payer_ids = {s["from_member_id"] for s in settlements}
    assert 2 in payer_ids and 3 in payer_ids
    assert all(s["to_member_id"] == 1 for s in settlements)
    assert all(s["amount"] == 30.0 for s in settlements)

    constrained = graph_constrained_settlements(members, expenses, balances)
    assert all(s["to_member_id"] == 1 for s in constrained)
    assert {s["from_member_id"] for s in constrained} == {2, 3}

    # Kartavya only sits on Bhagu's receipt. Sanjivani's bill does not include him.
    trio = [
        {"id": 1, "name": "Bhagu"},
        {"id": 2, "name": "Sanjivani"},
        {"id": 3, "name": "Kartavya"},
        {"id": 4, "name": "Tanish"},
    ]
    mixed = [
        {
            "payer_id": 2,
            "total_amount": 12.0,
            "splits": [
                {"user_id": 1, "share_amount": 4.0},
                {"user_id": 2, "share_amount": 4.0},
                {"user_id": 4, "share_amount": 4.0},
            ],
        },
        {
            "payer_id": 1,
            "total_amount": 10.0,
            "splits": [{"user_id": 3, "share_amount": 10.0}],
        },
    ]
    mixed_balances = compute_balances(trio, mixed)
    who_pays = graph_constrained_settlements(trio, mixed, mixed_balances)
    kartavya_pays = [s for s in who_pays if s["from_member_id"] == 3]
    assert kartavya_pays, "Kartavya should still have a payment"
    assert all(s["to_member_id"] == 1 for s in kartavya_pays)

    print("  [PASS] Debt simplification tests passed!")


def test_db_schema():
    print("Testing Database Schema & Models...")
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        # Check Account & PersonalExpense models
        account = db.query(models.Account).first()
        if not account:
            account = models.Account(
                email="test_sharesquad@example.com",
                password_hash="testhash",
                display_name="Test User",
            )
            db.add(account)
            db.commit()
            db.refresh(account)

        # Add a personal expense with new categories
        p_exp = models.PersonalExpense(
            account_id=account.id,
            store_name="Metro Transit",
            bill_date=date.today(),
            total_amount=Decimal("14.50"),
            currency="USD",
            category="Transit",
            notes="Morning commute",
        )
        db.add(p_exp)
        db.commit()

        fetched = db.query(models.PersonalExpense).filter_by(id=p_exp.id).first()
        assert fetched is not None
        assert fetched.category == "Transit"
        assert fetched.total_amount == Decimal("14.50")

        db.delete(fetched)
        db.commit()
        print("  [PASS] Database schema & personal expenses tests passed!")
    finally:
        db.close()


if __name__ == "__main__":
    test_split_logic()
    test_currency_conversion()
    test_debt_simplification()
    test_db_schema()
    print("\nALL SHARESQUAD TESTS PASSED SUCCESSFULLY!")
