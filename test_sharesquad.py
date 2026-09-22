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

    # A debtor who owes two original payers must not overpay either creditor
    # and create a follow-up transfer between the creditors.
    three_way = [
        {"id": 1, "name": "Tanish"},
        {"id": 2, "name": "Kuku"},
        {"id": 3, "name": "KP"},
    ]
    three_way_expenses = [
        {"payer_id": 1, "total_amount": 17.00, "splits": [{"user_id": 1, "share_amount": 5.85}, {"user_id": 2, "share_amount": 11.15}]},
        {"payer_id": 3, "total_amount": 17.53, "splits": [{"user_id": 2, "share_amount": 11.69}, {"user_id": 3, "share_amount": 5.84}]},
    ]
    three_way_balances = compute_balances(three_way, three_way_expenses)
    three_way_settlements = graph_constrained_settlements(three_way, three_way_expenses, three_way_balances)
    assert {
        (row["from_member_id"], row["to_member_id"]): row["amount"]
        for row in three_way_settlements
    } == {(2, 1): 11.15, (2, 3): 11.69}

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
    assert any(s["to_member_id"] == 1 for s in kartavya_pays)
    assert not any(
        s["from_member_id"] == 1 and s["to_member_id"] == 2
        for s in who_pays
    ), "A creditor should never need to forward an overpayment"

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


def test_ocr_receipt_parsing():
    print("Testing OCR receipt parsing...")
    from app.ocr_extractor import (
        _find_date,
        _find_store_name,
        _find_total,
        _group_tokens_into_lines,
        _is_register_line,
        _remove_wrinkle_shadows,
        _variant_score,
    )

    date, ok = _find_date("Store\n09/13/26 06:49PM\nTOTAL 22.25")
    assert ok and date == "2026-09-13", date

    assert _find_date("09/13/268 extra") == (None, False)

    assert _is_register_line("*1935 FS67/003/006 09/13/26")
    assert not _is_register_line("TOTAL ........ $ 22.25")
    assert not _is_register_line("09/13/26")

    lines = [
        "MILK 3.50",
        "BREAD 4.00",
        "SUBTOTAL 20.00",
        "TAX 2.25",
        "TOTAL 22.25",
        "*1935 FS67/003/006 09/13/26",
        "19428.08",
        "CASH 40.00",
        "CHANGE 17.75",
    ]
    total, confident = _find_total(lines)
    assert total == 22.25, total
    assert confident is True

    footer_only = [
        "MILK 3.50",
        "*1935 FS67/003/006 09/13/26",
        "19428.08",
    ]
    fallback_total, fallback_ok = _find_total(footer_only)
    assert fallback_total is None
    assert fallback_ok is False

    data = {
        "text": ["SUBTOTAL", "........", "20.00", "TOTAL", "........", "22.25"],
        "conf": ["80", "80", "80", "80", "80", "80"],
        "left": [10, 80, 200, 10, 80, 200],
        "top": [10, 12, 11, 40, 41, 42],
        "width": [60, 40, 40, 50, 40, 40],
        "height": [12, 10, 12, 12, 10, 12],
    }
    grouped, ntok, mean, lefts, heights, line_boxes = _group_tokens_into_lines(data)
    assert ntok == 6
    assert mean == 80.0
    assert len(grouped) == 2
    assert grouped[0].startswith("SUBTOTAL") and "20.00" in grouped[0]
    assert grouped[1].startswith("TOTAL") and "22.25" in grouped[1]
    assert len(lefts) == 2
    assert heights == [12, 12]
    assert line_boxes[1]["tokens"][-1]["text"] == "22.25"

    geometry_lines = ["SUBTOTAL 80.00", "CASH TENDERED 100.00", "BALANCE DUE 90.00"]
    geometry = [
        {"left": 10, "top": 100, "right": 290, "bottom": 120, "tokens": [
            {"text": "SUBTOTAL", "left": 10, "top": 100, "w": 90, "h": 20},
            {"text": "80.00", "left": 240, "top": 100, "w": 50, "h": 20},
        ]},
        {"left": 10, "top": 160, "right": 290, "bottom": 180, "tokens": [
            {"text": "CASH", "left": 10, "top": 160, "w": 45, "h": 20},
            {"text": "TENDERED", "left": 60, "top": 160, "w": 90, "h": 20},
            {"text": "100.00", "left": 230, "top": 160, "w": 60, "h": 20},
        ]},
        {"left": 10, "top": 420, "right": 290, "bottom": 445, "tokens": [
            {"text": "BALANCE", "left": 10, "top": 420, "w": 75, "h": 25},
            {"text": "DUE", "left": 90, "top": 420, "w": 35, "h": 25},
            {"text": "90.00", "left": 240, "top": 420, "w": 50, "h": 25},
        ]},
    ]
    assert _find_total(geometry_lines, geometry) == (90.00, True)

    # A wrinkle can make the right-aligned price look like a separate row.
    # Geometry matching should still attach it to the total label.
    split_total_lines = ["GRAND TOTAL", "14.20"]
    split_total_geometry = [
        {"left": 10, "top": 300, "right": 125, "bottom": 320, "tokens": [
            {"text": "GRAND", "left": 10, "top": 300, "w": 70, "h": 20},
            {"text": "TOTAL", "left": 85, "top": 300, "w": 40, "h": 20},
        ]},
        {"left": 610, "top": 339, "right": 665, "bottom": 359, "tokens": [
            {"text": "14.20", "left": 610, "top": 339, "w": 55, "h": 20},
        ]},
    ]
    assert _find_total(split_total_lines, split_total_geometry) == (14.20, True)

    import numpy as np
    shadowed = np.tile(np.linspace(55, 210, 80, dtype=np.uint8), (50, 1))
    flattened = _remove_wrinkle_shadows(shadowed)
    assert flattened.mean() > shadowed.mean(), "shadow normalization should brighten a dark crease gradient"

    pub, pub_ok = _find_store_name(
        ["Palmetto Park Square"],
        "Palmetto Park Square\n1339 W Palmetto Park Rd\n(561) 362-5305\nstore 0553",
    )
    assert pub_ok and pub == "Publix", pub

    store_num, store_ok = _find_store_name(
        ["o u b l i x"], "Publix Milk Whole Red Ga\nfor shopping at store 1708",
    )
    assert store_ok and store_num == "Publix", store_num

    milk_only, milk_ok = _find_store_name(
        ["Cafe Luna"], "Cafe Luna\nPublix Milk Whole Red Ga\nTOTAL 10.00",
    )
    assert milk_only == "Cafe Luna" and milk_ok is False, milk_only

    aldi_false, aldi_false_ok = _find_store_name(
        ["Cafe Luna"],
        "Cafe Luna\nvalid thru 12/26\nQUALITY GUARANTEED\nthank you\nTOTAL 10.00",
    )
    assert aldi_false == "Cafe Luna" and aldi_false_ok is False, aldi_false

    cvs_false, cvs_false_ok = _find_store_name(
        ["Joe's Deli"],
        "Joe's Deli\nvalid thru 12/26\nTOTAL 4.00",
    )
    assert cvs_false == "Joe's Deli" and cvs_false_ok is False

    aldi_header, aldi_ok = _find_store_name(["ALDI"], "ALDI\nThank you\nTOTAL 10.00")
    assert aldi_header == "Aldi" and aldi_ok is False

    logo, logo_ok = _find_store_name(
        ["small print", "Premium Wine"],
        "small print\nPremium Wine\nTOTAL 10.00",
        heights=[8, 28],
    )
    assert logo == "Premium Wine" and logo_ok is False, logo

    local_shop, local_ok = _find_store_name(
        ["Premium Wine and Liquor"],
        "Premium Wine and Liquor\n(561) 555-1212\nTOTAL 40.00",
        learned_vendors=[{
            "name": "Premium Wine and Liquor",
            "phones": {"5615551212"},
            "store_ids": set(),
            "header_key": "",
        }],
    )
    assert local_ok and local_shop == "Premium Wine and Liquor"

    gazetteer = [{
        "name": "Premium Wine and Liquor",
        "phones": set(),
        "store_ids": set(),
        "header_key": "",
        "category": "Shopping",
    }]
    cached, cached_ok = _find_store_name(
        ["Premium Wine"],
        "Premium Wine\nThank you\nTOTAL 40.00",
        learned_vendors=gazetteer,
    )
    assert cached_ok and cached == "Premium Wine and Liquor", cached

    typo, typo_ok = _find_store_name(
        ["Premlum Wine and Liquor"],
        "Premlum Wine and Liquor\nTOTAL 40.00",
        learned_vendors=gazetteer,
    )
    assert typo_ok and typo == "Premium Wine and Liquor", typo

    aldi_cache = [{"name": "Aldi", "phones": set(), "store_ids": set(), "header_key": ""}]
    not_cached, not_cached_ok = _find_store_name(
        ["Cafe Luna"],
        "Cafe Luna\nvalid thru 12/26\nTOTAL 10.00",
        learned_vendors=aldi_cache,
    )
    assert not_cached == "Cafe Luna" and not_cached_ok is False

    trip, trip_ok = _find_store_name(
        ["Your trip"],
        "Your trip\nfare breakdown\nTotal 12.40",
    )
    assert trip == "Uber" and trip_ok is False

    ups_false, _ = _find_store_name(
        ["Shipping Label"],
        "Shipping Label\ntracking number 1Z999\nTOTAL 8.00",
    )
    assert ups_false != "UPS"

    sav_lines = [
        "SUBTOTAL 11.77",
        "TOTAL TAX 0.00",
        "TOTAL 11.77",
        "SAVINGS: $2.84",
        "Visa: *7212 $11.77 Purchase",
    ]
    sav_total, sav_ok = _find_total(sav_lines)
    assert sav_total == 11.77, sav_total
    assert sav_ok is True

    portal_total, portal_ok = _find_total(["Account Balance $2,603.61"])
    assert portal_total == 2603.61, portal_total
    assert portal_ok is True

    ignored = _find_total([
        "SUBTOTAL 99.00",
        "TAX 7.00",
        "SAVINGS 5.00",
        "CASH 50.00",
        "TOTAL 22.25",
    ])
    assert ignored == (22.25, True)

    from app.ocr_extractor import _parse_amounts, _repair_ocr_digits

    assert _repair_ocr_digits("TOTAL 12.O9") == "TOTAL 12.09"
    assert _repair_ocr_digits("DATE 09/l3/26") == "DATE 09/13/26"
    assert _parse_amounts("Total 1.234,56") == [1234.56]

    slogan, slogan_ok = _find_store_name(["TAX INVOICE", "Some Cafe"])
    assert slogan_ok is False
    assert slogan == "Some Cafe"

    eu_date, eu_ok = _find_date("GSTIN 29ABCDE1234F1Z5\n13.09.26 TOTAL 22.25")
    assert eu_ok and eu_date == "2026-09-13", eu_date

    sparse = _variant_score("HI", 95.0, 1)
    rich = _variant_score("SUBTOTAL 20.00 TAX 2.25 TOTAL $ 22.25 09/13/26", 60.0, 12)
    assert rich > sparse, (rich, sparse)

    print("  [PASS] OCR receipt parsing tests passed!")


if __name__ == "__main__":
    test_split_logic()
    test_currency_conversion()
    test_debt_simplification()
    test_db_schema()
    test_ocr_receipt_parsing()
    print("\nALL SHARESQUAD TESTS PASSED SUCCESSFULLY!")
