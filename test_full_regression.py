"""
ShareSquad full-feature regression suite.

Covers auth, groups, expenses (month-saved ledger), balances/settlements math,
currency conversion, friend consolidated payments + payment history, digests
with late-added bills, reports, notifications, OCR dates, and static UI/UX
guards for contrast, sorting, and motion.

Run:
  python -m pytest test_full_regression.py -v
  python test_full_regression.py
"""
from __future__ import annotations

import os
import re
import sys
import uuid
import warnings
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.main import (
    app,
    _build_account_monthly_digest,
    _business_today,
    _period_key_for,
    _unlock_group_period,
)
from app.database import SessionLocal
from app import models
from app.redis_support import clear_memory_rate_limits
from app.split_logic import compute_balances, equal_split, graph_constrained_settlements, simplify_debts
from app.ocr_extractor import _find_date
from app.fx_service import convert_amount

client = TestClient(app)
STATIC = Path(__file__).resolve().parent / "app" / "static"
TEMPLATES = Path(__file__).resolve().parent / "app" / "templates"


def _uid(prefix: str = "user") -> str:
    # Use gmail.com so group invite domain validation accepts test accounts.
    return f"{prefix}_{uuid.uuid4().hex[:10]}@gmail.com"


def _auth_pair(name_a: str = "Alex", name_b: str = "Blake"):
    clear_memory_rate_limits()
    email_a, email_b = _uid("a"), _uid("b")
    ra = client.post("/api/auth/register", json={"email": email_a, "password": "Password12!", "display_name": name_a})
    assert ra.status_code == 200, ra.text
    rb = client.post("/api/auth/register", json={"email": email_b, "password": "Password12!", "display_name": name_b})
    assert rb.status_code == 200, rb.text
    ha = {"Authorization": f"Bearer {ra.json()['access_token']}"}
    hb = {"Authorization": f"Bearer {rb.json()['access_token']}"}
    return email_a, email_b, ha, hb


def _ready_group():
    email_a, email_b, ha, hb = _auth_pair()
    r = client.post("/api/groups", json={"name": f"G-{uuid.uuid4().hex[:6]}", "currency": "USD"}, headers=ha)
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
        "group_id": group_id,
        "me": by_email[email_a],
        "friend": by_email[email_b],
    }


# ---------------------------------------------------------------------------
# Auth & health
# ---------------------------------------------------------------------------
class TestAuthAndHealth:
    def test_health_ok(self):
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json() is not None

    def test_register_login_me_and_reject_weak_password_on_register(self):
        clear_memory_rate_limits()
        email = _uid("auth")
        weak = client.post("/api/auth/register", json={"email": email, "password": "short", "display_name": "Weak"})
        assert weak.status_code in (400, 422)
        strong = client.post("/api/auth/register", json={"email": email, "password": "Password12!", "display_name": "Strong"})
        assert strong.status_code == 200
        token = strong.json()["access_token"]
        me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        assert me.json()["account"]["email"] == email

    def test_login_allows_existing_account(self):
        clear_memory_rate_limits()
        email = _uid("login")
        client.post("/api/auth/register", json={"email": email, "password": "Password12!", "display_name": "L"})
        r = client.post("/api/auth/login", json={"email": email, "password": "Password12!"})
        assert r.status_code == 200
        assert "access_token" in r.json()

    def test_unauthenticated_groups_blocked(self):
        assert client.get("/api/groups").status_code in (401, 403)


# ---------------------------------------------------------------------------
# Calculation correctness
# ---------------------------------------------------------------------------
class TestCalculationAccuracy:
    def test_equal_split_pennies_sum_exact(self):
        shares = equal_split(100.0, [1, 2, 3])
        assert sum(shares.values()) == Decimal("100.00")

    def test_balances_and_who_pays_whom_match_ledger(self):
        members = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
        expenses = [{
            "payer_id": 1,
            "total_amount": 80.0,
            "splits": [{"user_id": 1, "share_amount": 40.0}, {"user_id": 2, "share_amount": 40.0}],
        }]
        balances = compute_balances(members, expenses)
        assert balances[1]["net_balance"] == 40.0
        assert balances[2]["net_balance"] == -40.0
        settlements = graph_constrained_settlements(members, expenses, balances)
        assert len(settlements) == 1
        assert settlements[0]["from_member_id"] == 2
        assert settlements[0]["to_member_id"] == 1
        assert settlements[0]["amount"] == 40.0

    def test_fx_conversion_is_deterministic_with_custom_rate(self):
        amount, rate = convert_amount(10.0, "USD", "INR", custom_rate=83.5)
        assert rate == Decimal("83.50") or rate == Decimal("83.5")
        assert amount == Decimal("835.00")

    def test_simplify_debts_netting(self):
        members = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}, {"id": 3, "name": "C"}]
        expenses = [{
            "payer_id": 1,
            "total_amount": 90.0,
            "splits": [
                {"user_id": 1, "share_amount": 30.0},
                {"user_id": 2, "share_amount": 30.0},
                {"user_id": 3, "share_amount": 30.0},
            ],
        }]
        balances = compute_balances(members, expenses)
        settled = simplify_debts(balances)
        assert abs(sum(s["amount"] for s in settled) - 60.0) < 0.01


# ---------------------------------------------------------------------------
# Expenses: month-saved ledger, late bills, sorting
# ---------------------------------------------------------------------------
class TestExpenseLedgerBehavior:
    def test_expense_lands_in_month_saved_not_bill_date_month(self):
        ctx = _ready_group()
        today_key = _period_key_for(_business_today())
        # Older bill date, explicit current period omitted → server uses today.
        payload = {
            "store_name": "Late Uber",
            "bill_date": "2026-09-12",
            "total_amount": 30.0,
            "currency": "USD",
            "category": "Transit",
            "split_type": "EQUAL",
            "payer_member_id": ctx["me"]["id"],
            "split_with": [ctx["me"]["id"], ctx["friend"]["id"]],
            "period_key": today_key,
        }
        r = client.post(f"/api/groups/{ctx['group_id']}/expenses", json=payload, headers=ctx["ha"])
        assert r.status_code == 200, r.text
        # Balances for September should not include this (unless today is Sept).
        oct_or_today = client.get(f"/api/groups/{ctx['group_id']}/periods/{today_key}/balances", headers=ctx["ha"])
        assert oct_or_today.status_code == 200
        today_balances = {b["member_id"]: b for b in oct_or_today.json()}
        assert abs(today_balances[ctx["me"]["id"]]["net_balance"] - 15.0) < 0.02
        assert abs(today_balances[ctx["friend"]["id"]]["net_balance"] + 15.0) < 0.02
        if today_key != "2026-09":
            sept = client.get(f"/api/groups/{ctx['group_id']}/periods/2026-09/balances", headers=ctx["ha"])
            if sept.status_code == 200 and sept.json():
                sept_nets = {b["member_id"]: b["net_balance"] for b in sept.json()}
                assert abs(sept_nets.get(ctx["me"]["id"], 0.0)) < 0.02
                assert abs(sept_nets.get(ctx["friend"]["id"], 0.0)) < 0.02

    def test_expenses_list_sorted_newest_save_first_shows_bill_date(self):
        ctx = _ready_group()
        gid = ctx["group_id"]
        base = {
            "currency": "USD",
            "category": "General",
            "split_type": "EQUAL",
            "payer_member_id": ctx["me"]["id"],
            "split_with": [ctx["me"]["id"], ctx["friend"]["id"]],
            "period_key": _period_key_for(_business_today()),
        }
        first = client.post(f"/api/groups/{gid}/expenses", json={**base, "store_name": "First", "bill_date": "2026-09-01", "total_amount": 10}, headers=ctx["ha"])
        second = client.post(f"/api/groups/{gid}/expenses", json={**base, "store_name": "Second", "bill_date": "2026-08-01", "total_amount": 20}, headers=ctx["ha"])
        assert first.status_code == 200 and second.status_code == 200
        listed = client.get(f"/api/groups/{gid}/expenses", headers=ctx["ha"]).json()
        assert listed[0]["store_name"] == "Second"
        assert listed[0]["bill_date"] == "2026-08-01"
        assert listed[1]["store_name"] == "First"
        assert listed[1]["bill_date"] == "2026-09-01"
        created = [row["created_at"] for row in listed[:2]]
        assert created[0] >= created[1]

    def test_legacy_settled_period_accepts_new_expense(self):
        ctx = _ready_group()
        gid = ctx["group_id"]
        period_key = "2026-09"
        db = SessionLocal()
        try:
            period = db.query(models.MonthlyPeriod).filter_by(group_id=gid, period_key=period_key).first()
            if not period:
                period = models.MonthlyPeriod(group_id=gid, period_key=period_key, status="SETTLED")
                db.add(period)
            else:
                period.status = "SETTLED"
            db.commit()
        finally:
            db.close()
        r = client.post(
            f"/api/groups/{gid}/expenses",
            json={
                "store_name": "Unlock me",
                "bill_date": "2026-09-20",
                "total_amount": 12.0,
                "currency": "USD",
                "category": "General",
                "split_type": "EQUAL",
                "payer_member_id": ctx["me"]["id"],
                "split_with": [ctx["me"]["id"], ctx["friend"]["id"]],
                "period_key": period_key,
            },
            headers=ctx["ha"],
        )
        assert r.status_code == 200, r.text
        assert "already settled" not in (r.text or "").lower()


# ---------------------------------------------------------------------------
# Payments, settlements, past payment viewer
# ---------------------------------------------------------------------------
class TestPaymentsAndSettlements:
    def test_who_pays_whom_and_friend_payment_updates_balance(self):
        ctx = _ready_group()
        gid = ctx["group_id"]
        period_key = _period_key_for(_business_today())
        invite = client.post("/api/friends/invites", json={"email": ctx["email_b"]}, headers=ctx["ha"])
        assert invite.status_code == 200, invite.text
        friend_id = client.get("/api/auth/me", headers=ctx["hb"]).json()["account"]["id"]
        owner_account_id = client.get("/api/auth/me", headers=ctx["ha"]).json()["account"]["id"]

        r = client.post(
            f"/api/groups/{gid}/expenses",
            json={
                "store_name": "Dinner",
                "bill_date": date.today().isoformat(),
                "total_amount": 100.0,
                "currency": "USD",
                "category": "Food & Dining",
                "split_type": "EQUAL",
                "payer_member_id": ctx["me"]["id"],
                "split_with": [ctx["me"]["id"], ctx["friend"]["id"]],
                "period_key": period_key,
            },
            headers=ctx["ha"],
        )
        assert r.status_code == 200, r.text

        settlements = client.get(f"/api/groups/{gid}/periods/{period_key}/settlements?mode=smart", headers=ctx["ha"])
        assert settlements.status_code == 200
        rows = settlements.json()
        assert any(abs(s["amount"] - 50.0) < 0.02 for s in rows)

        bal = client.get(f"/api/friends/{friend_id}/consolidated-balance?currency=USD", headers=ctx["ha"])
        assert bal.status_code == 200, bal.text
        assert abs(bal.json()["balance"]) >= 49.0
        # Friend owes owner → friend records a partial payment
        pay = client.post(
            f"/api/friends/{owner_account_id}/consolidated-payments",
            json={"amount": 20.0, "currency": "USD"},
            headers=ctx["hb"],
        )
        assert pay.status_code == 200, pay.text
        assert pay.json()['status'] == 'PENDING'
        pending_balance = client.get(f"/api/friends/{friend_id}/consolidated-balance?currency=USD", headers=ctx['ha'])
        assert pending_balance.json()['balance'] == 50.0
        confirmed = client.post(
            f"/api/friends/{friend_id}/consolidated-payments/{pay.json()['id']}/confirm", headers=ctx['ha'],
        )
        assert confirmed.status_code == 200, confirmed.text
        after = client.get(f"/api/friends/{friend_id}/consolidated-balance?currency=USD", headers=ctx["ha"])
        assert after.status_code == 200
        assert abs(abs(after.json()["balance"]) - 30.0) < 0.51

        payments = client.get(f"/api/groups/{gid}/periods/{period_key}/payments", headers=ctx["ha"])
        assert payments.status_code == 200
        assert any(abs(float(p["amount"]) - 20.0) < 0.01 for p in payments.json())

    def test_settlements_empty_when_even(self):
        ctx = _ready_group()
        period_key = _period_key_for(_business_today())
        # Each pays their own equal share via two expenses
        for store, payer in (("A", ctx["me"]["id"]), ("B", ctx["friend"]["id"])):
            r = client.post(
                f"/api/groups/{ctx['group_id']}/expenses",
                json={
                    "store_name": store,
                    "bill_date": date.today().isoformat(),
                    "total_amount": 40.0,
                    "currency": "USD",
                    "category": "General",
                    "split_type": "EQUAL",
                    "payer_member_id": payer,
                    "split_with": [ctx["me"]["id"], ctx["friend"]["id"]],
                    "period_key": period_key,
                },
                headers=ctx["ha"] if payer == ctx["me"]["id"] else ctx["hb"],
            )
            assert r.status_code == 200, r.text
        settlements = client.get(
            f"/api/groups/{ctx['group_id']}/periods/{period_key}/settlements?mode=smart",
            headers=ctx["ha"],
        ).json()
        assert settlements == [] or all(abs(s["amount"]) < 0.02 for s in settlements)


# ---------------------------------------------------------------------------
# Digests: late-added section
# ---------------------------------------------------------------------------
class TestMonthlyDigestLateAdds:
    def test_digest_splits_late_bills_into_added_late(self):
        ctx = _ready_group()
        period_key = _period_key_for(_business_today())
        start, _ = __import__("app.main", fromlist=["_period_dates"])._period_dates(period_key)
        late_day = (start.replace(day=1) - __import__("datetime").timedelta(days=5)).isoformat()
        r = client.post(
            f"/api/groups/{ctx['group_id']}/expenses",
            json={
                "store_name": "Old receipt",
                "bill_date": late_day,
                "total_amount": 22.0,
                "currency": "USD",
                "category": "General",
                "split_type": "EQUAL",
                "payer_member_id": ctx["me"]["id"],
                "split_with": [ctx["me"]["id"], ctx["friend"]["id"]],
                "period_key": period_key,
            },
            headers=ctx["ha"],
        )
        assert r.status_code == 200, r.text
        r2 = client.post(
            f"/api/groups/{ctx['group_id']}/expenses",
            json={
                "store_name": "This month coffee",
                "bill_date": date.today().isoformat(),
                "total_amount": 8.0,
                "currency": "USD",
                "category": "Food & Dining",
                "split_type": "EQUAL",
                "payer_member_id": ctx["me"]["id"],
                "split_with": [ctx["me"]["id"], ctx["friend"]["id"]],
                "period_key": period_key,
            },
            headers=ctx["ha"],
        )
        assert r2.status_code == 200, r2.text

        from app.main import _period_dates, build_balances_and_settlements
        from app.pdf_gen import pdf_reports_available

        db = SessionLocal()
        try:
            start_d, _end = _period_dates(period_key)
            period = db.query(models.MonthlyPeriod).filter_by(group_id=ctx["group_id"], period_key=period_key).one()
            _b, _s, expenses = build_balances_and_settlements(db, ctx["group_id"], period)
            late = [e for e in expenses if e.bill_date < start_d]
            current = [e for e in expenses if e.bill_date >= start_d]
            assert any(e.store_name == "Old receipt" for e in late)
            assert any(e.store_name == "This month coffee" for e in current)
            if pdf_reports_available():
                account_id = client.get("/api/auth/me", headers=ctx["ha"]).json()["account"]["id"]
                account = db.get(models.Account, account_id)
                digest = _build_account_monthly_digest(db, account, period_key)
                db.commit()
                assert digest.pdf_path and os.path.exists(digest.pdf_path)
            else:
                warnings.warn("WeasyPrint unavailable — digest PDF generation skipped; late/current split still verified.", UserWarning)
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Currency, reports, activity, notifications, settings
# ---------------------------------------------------------------------------
class TestCurrencyReportsActivity:
    def test_convert_currency_and_generate_pdf_without_locking(self):
        ctx = _ready_group()
        period_key = _period_key_for(_business_today())
        client.post(
            f"/api/groups/{ctx['group_id']}/expenses",
            json={
                "store_name": "Market",
                "bill_date": date.today().isoformat(),
                "total_amount": 50.0,
                "currency": "USD",
                "category": "Groceries",
                "split_type": "EQUAL",
                "payer_member_id": ctx["me"]["id"],
                "split_with": [ctx["me"]["id"], ctx["friend"]["id"]],
                "period_key": period_key,
            },
            headers=ctx["ha"],
        )
        conv = client.post(
            f"/api/groups/{ctx['group_id']}/periods/{period_key}/convert-currency",
            json={"target_currency": "CAD", "custom_exchange_rate": 1.35},
            headers=ctx["ha"],
        )
        assert conv.status_code == 200, conv.text
        balances = client.get(f"/api/groups/{ctx['group_id']}/periods/{period_key}/balances", headers=ctx["ha"]).json()
        assert any(b.get("converted_currency") == "CAD" for b in balances)
        from app.pdf_gen import pdf_reports_available
        report = client.post(f"/api/groups/{ctx['group_id']}/periods/{period_key}/generate-report", headers=ctx["ha"])
        if pdf_reports_available():
            assert report.status_code == 200, report.text
        else:
            assert report.status_code in (200, 503), report.text
            warnings.warn("WeasyPrint unavailable — PDF generation returned non-200; lock check still applies.", UserWarning)
        periods = client.get(f"/api/groups/{ctx['group_id']}/periods", headers=ctx["ha"]).json()
        row = next(p for p in periods if p["period_key"] == period_key)
        # PDF generation must not lock the month
        assert row["status"] == "ACTIVE"

    def test_activity_notifications_dashboard_settings(self):
        ctx = _ready_group()
        period_key = _period_key_for(_business_today())
        client.post(
            f"/api/groups/{ctx['group_id']}/expenses",
            json={
                "store_name": "NoteMe",
                "bill_date": date.today().isoformat(),
                "total_amount": 9.0,
                "currency": "USD",
                "category": "General",
                "split_type": "EQUAL",
                "payer_member_id": ctx["me"]["id"],
                "split_with": [ctx["me"]["id"], ctx["friend"]["id"]],
                "period_key": period_key,
            },
            headers=ctx["ha"],
        )
        assert client.get(f"/api/groups/{ctx['group_id']}/activity", headers=ctx["ha"]).status_code == 200
        assert client.get("/api/notifications", headers=ctx["hb"]).status_code == 200
        dash = client.get(f"/api/groups/{ctx['group_id']}/dashboard?period_key={period_key}", headers=ctx["ha"])
        assert dash.status_code == 200
        assert dash.json()["total_spent"] >= 9.0
        settings = client.get("/api/settings/account", headers=ctx["ha"])
        assert settings.status_code == 200


# ---------------------------------------------------------------------------
# OCR date robustness
# ---------------------------------------------------------------------------
class TestOcrDateGrab:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Store\n09/13/26 06:49PM\nTOTAL 22.25", "2026-09-13"),
            ("Date: 09/13/26\nTOTAL 10.00", "2026-09-13"),
            ("DATE 09 / 13 / 2026\nTOTAL 10.00", "2026-09-13"),
            ("Purchase Date Sep 13th, 2026\nTOTAL 10.00", "2026-09-13"),
            ("TXN 13SEP2026 TOTAL 10.00", "2026-09-13"),
            ("DT 09|13|26\nTOTAL 10.00", "2026-09-13"),
            ("GSTIN 29ABCDE1234F1Z5\n13.09.26 TOTAL 22.25", "2026-09-13"),
        ],
    )
    def test_date_patterns(self, text, expected):
        parsed, ok = _find_date(text)
        assert ok and parsed == expected, (text, parsed)


# ---------------------------------------------------------------------------
# Static UI/UX / contrast / motion / product rules
# ---------------------------------------------------------------------------
class TestStaticUiUxGuards:
    def test_group_html_has_no_global_month_picker(self):
        html = (STATIC / "group.html").read_text(encoding="utf-8")
        assert 'id="periodSelect"' not in html
        assert "View past months" not in html
        assert 'id="expenseMonthFilter"' in html
        assert 'id="insightMonthFilter"' in html
        assert 'id="openAddExpenseBtn"' in html
        assert "Change settlement currency" in html
        # Currency change only on reports tab, not balances card
        assert html.index("convertCurrencyBtn") > html.index('id="tab-reports"')

    def test_expense_filters_have_theme_safe_option_classes(self):
        html = (STATIC / "group.html").read_text(encoding="utf-8")
        js = (STATIC / "group.js").read_text(encoding="utf-8")
        css = (STATIC / "styles.css").read_text(encoding="utf-8")
        assert "ledger-filter-option" in html or "ledger-filter-option" in js
        assert "#expenseMonthFilter" in css
        assert "#insightMonthFilter" in css
        assert "html.theme-dark #expenseMonthFilter option.ledger-filter-option" in css
        assert "html.theme-dark #insightMonthFilter option.ledger-filter-option" in css
        assert "html.theme-light #expenseMonthFilter option.ledger-filter-option" in css
        assert "html.theme-light #insightMonthFilter option.ledger-filter-option" in css
        assert "color: var(--ink)" in css.split("#expenseMonthFilter")[1][:400]
        js = (STATIC / "group.js").read_text(encoding="utf-8")
        assert "fillInsightMonthFilter" in js
        assert "state.insightPeriod" in js

    def test_expenses_sort_and_period_on_save_in_frontend(self):
        js = (STATIC / "group.js").read_text(encoding="utf-8")
        assert "created_at" in js
        assert "calendarPeriodKey()" in js
        assert "period_key: calendarPeriodKey()" in js
        assert "bill_date" in js

    def test_no_forbidden_pastel_ink_pairs_on_balances_bar(self):
        html = (STATIC / "group.html").read_text(encoding="utf-8")
        # Conversion banner should use token/brand wash, not emerald-50 + emerald-900
        assert "bg-emerald-50" not in html or "conversionBanner" not in html
        banner_slice = html[html.find("conversionBanner"): html.find("conversionBanner") + 350]
        assert "text-emerald-900" not in banner_slice

    def test_motion_primitives_exist(self):
        css = (STATIC / "styles.css").read_text(encoding="utf-8")
        assert "@keyframes" in css
        assert "transition" in css
        assert "scan-spin" in css or "skeleton-shimmer" in css or "slide-in-right" in css

    def test_digest_template_has_added_late_section(self):
        tpl = (TEMPLATES / "monthly_digest.html").read_text(encoding="utf-8")
        assert "Added late" in tpl
        assert "expenses_added_late" in tpl
        assert "expenses_this_month" in tpl

    def test_public_pages_serve(self):
        for path in ("/login", "/friends", "/group", "/personal", "/settings", "/app"):
            r = client.get(path)
            assert r.status_code in (200, 307, 308), path


# ---------------------------------------------------------------------------
# Demo sandbox matches current product (not a legacy fixture)
# ---------------------------------------------------------------------------
class TestDemoSandboxCurrentProduct:
    def test_sandbox_creates_current_version_workspace(self):
        from app.config import APP_VERSION
        from app.demo_service import TEMPLATE_VERSION

        assert APP_VERSION in TEMPLATE_VERSION
        r = client.post("/api/demo/sandbox")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("access_token") and data.get("group_id")
        headers = {"Authorization": f"Bearer {data['access_token']}"}

        expenses = client.get(f"/api/groups/{data['group_id']}/expenses", headers=headers)
        assert expenses.status_code == 200
        rows = expenses.json()
        assert len(rows) >= 6
        # Late bill dates exist while period is the current save month
        current_key = _business_today().strftime("%Y-%m")
        older = [e for e in rows if str(e.get("bill_date", ""))[:7] < current_key]
        assert older, "sandbox should include older receipt dates (late bills)"
        # Ledger ordering is newest save first
        created = [e.get("created_at") for e in rows if e.get("created_at")]
        assert created == sorted(created, reverse=True)
        assert any(e.get("currency") == "INR" for e in rows), "sandbox should include mixed-currency sample"

        periods = client.get(f"/api/groups/{data['group_id']}/periods", headers=headers)
        assert periods.status_code == 200
        assert all(p.get("status") == "ACTIVE" for p in periods.json())

        friends = client.get("/api/friends", headers=headers)
        assert friends.status_code == 200
        friend_list = friends.json()
        assert friend_list, "sandbox should include a Friends connection"
        payment_rows = []
        for friend in friend_list:
            balances = client.get(f"/api/friends/{friend['id']}/consolidated-balances", headers=headers)
            assert balances.status_code == 200
            for block in balances.json().get("balances") or []:
                payment_rows.extend(block.get("payments") or [])
        assert any(p.get("status") == "CONFIRMED" for p in payment_rows), (
            "sandbox should seed past Friends payment history"
        )

    def test_landing_and_banner_describe_current_demo(self):
        landing = (STATIC / "landing.html").read_text(encoding="utf-8")
        api_js = (STATIC / "api.js").read_text(encoding="utf-8")
        assert "Try the full demo" in landing
        assert "same as a real account" in landing.lower() or "current ShareSquad" in landing
        assert "some details may differ" not in api_js
        assert "Same current ShareSquad" in api_js
        assert "Added late" in landing or "month you" in landing.lower()
        assert "Record payments on Friends" in (STATIC / "clear-balances.html").read_text(encoding="utf-8") or "Friends" in (STATIC / "clear-balances.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Unlock helper unit
# ---------------------------------------------------------------------------
class TestUnlockHelper:
    def test_unlock_group_period_clears_settled(self):
        period = models.MonthlyPeriod(
            group_id=1, period_key="2026-09", status="SETTLED",
            settled_at=datetime.now(__import__("datetime").timezone.utc),
        )
        assert _unlock_group_period(period) is True
        assert period.status == "ACTIVE"
        assert period.settled_at is None
        assert _unlock_group_period(period) is False


def _run_as_script():
    # Collect warnings during pytest-less direct run of critical paths
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        failures = []
        for name, obj in list(globals().items()):
            if not (isinstance(obj, type) and name.startswith("Test")):
                continue
            suite = obj()
            for attr in dir(suite):
                if not attr.startswith("test_"):
                    continue
                fn = getattr(suite, attr)
                try:
                    # Skip parametrized OCR when running as script without pytest.mark support
                    if attr == "test_date_patterns":
                        for text, expected in [
                            ("Store\n09/13/26 06:49PM\nTOTAL 22.25", "2026-09-13"),
                            ("DATE 09 / 13 / 2026\nTOTAL 10.00", "2026-09-13"),
                        ]:
                            parsed, ok = _find_date(text)
                            assert ok and parsed == expected
                        print(f"  [PASS] {name}.{attr} (sample)")
                        continue
                    fn()
                    print(f"  [PASS] {name}.{attr}")
                except Exception as exc:
                    failures.append(f"{name}.{attr}: {exc}")
                    print(f"  [FAIL] {name}.{attr}: {exc}")
        warn_msgs = [str(w.message) for w in caught if issubclass(w.category, Warning)]
        if warn_msgs:
            print("\nWarnings:")
            for msg in warn_msgs[:30]:
                print(f"  - {msg}")
        if failures:
            print(f"\n{len(failures)} failure(s)")
            raise SystemExit(1)
        print("\nALL FULL REGRESSION CHECKS PASSED")


if __name__ == "__main__":
    _run_as_script()
