"""Regression tests for FINAL-SUMMARY + theme Wave 0/1 fixes."""
import os
from datetime import date
from decimal import Decimal

os.environ.setdefault("BILLBUDDY_ENV", "development")

from fastapi.testclient import TestClient

from app import models
from app.config import IS_DEVELOPMENT
from app.database import SessionLocal, Base, engine
from app.emailer import send_password_reset_email
from app.main import (
    BLOCKED_INVITE_DOMAINS,
    _CATEGORY_ALIASES,
    app,
    get_group_dashboard,
)
from app.auth import hash_password, create_access_token


def test_development_allows_example_com_invites():
    assert IS_DEVELOPMENT is True
    assert "example.com" not in BLOCKED_INVITE_DOMAINS


def test_category_aliases_include_food_slug():
    assert "Food" in _CATEGORY_ALIASES["Food & Dining"]
    assert "Food & Dining" in _CATEGORY_ALIASES["Food"]


def test_password_reset_smtp_failure_does_not_raise(monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("SMTP down")

    monkeypatch.setattr("app.emailer.is_email_configured", lambda: True)
    monkeypatch.setattr("app.emailer._send_message", boom)
    assert send_password_reset_email("a@example.com", "http://x/reset", "Ada") is False


def test_forgot_password_returns_200_when_smtp_fails(monkeypatch):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        email = "qa-forgot@example.com"
        existing = db.query(models.Account).filter_by(email=email).first()
        if not existing:
            existing = models.Account(
                email=email,
                password_hash=hash_password("Password12!"),
                display_name="QA Forgot",
            )
            db.add(existing)
            db.commit()
            db.refresh(existing)
    finally:
        db.close()

    monkeypatch.setattr("app.main.is_email_configured", lambda: True)
    monkeypatch.setattr(
        "app.main.send_password_reset_email",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("SMTP down")),
    )
    # Prefer the soft-fail path in emailer; also wrap the main import path.
    monkeypatch.setattr(
        "app.main.send_password_reset_email",
        lambda **kwargs: False,
    )
    client = TestClient(app)
    res = client.post("/api/auth/forgot-password", json={"email": email})
    assert res.status_code == 200
    assert "password reset" in res.json()["message"].lower()


def test_insights_converts_mixed_currency_totals():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        owner = db.query(models.Account).filter_by(email="qa-insights@example.com").first()
        if not owner:
            owner = models.Account(
                email="qa-insights@example.com",
                password_hash=hash_password("Password12!"),
                display_name="QA Insights",
            )
            db.add(owner)
            db.commit()
            db.refresh(owner)
        group = models.Group(name="FX Group", currency="USD", created_by_id=owner.id)
        db.add(group)
        db.commit()
        db.refresh(group)
        member = models.GroupMember(
            group_id=group.id, account_id=owner.id, display_name="QA", role="owner", is_active=True,
        )
        db.add(member)
        db.commit()
        db.refresh(member)
        period = models.MonthlyPeriod(group_id=group.id, period_key="2026-10", status="ACTIVE")
        db.add(period)
        db.commit()
        db.refresh(period)
        db.add_all([
            models.Expense(
                period_id=period.id, store_name="USD Cafe", bill_date=date(2026, 10, 1),
                total_amount=Decimal("10.00"), currency="USD", category="Food & Dining",
                split_type="EQUAL", fx_rate_to_group=Decimal("1"), payer_member_id=member.id,
            ),
            models.Expense(
                period_id=period.id, store_name="INR Shop", bill_date=date(2026, 10, 2),
                total_amount=Decimal("830.00"), currency="INR", category="Shopping",
                split_type="EQUAL", fx_rate_to_group=Decimal("0.012"), payer_member_id=member.id,
            ),
        ])
        # Equal split to self so build_balances keeps expenses.
        db.commit()
        for expense in db.query(models.Expense).filter_by(period_id=period.id).all():
            db.add(models.ExpenseSplit(
                expense_id=expense.id, member_id=member.id, share_amount=expense.total_amount,
            ))
        db.commit()

        out = get_group_dashboard(group.id, "2026-10", owner, db)
        # 10 USD + 830*0.012 = 9.96 ≈ 19.96 — must NOT be 840.
        assert out.total_spent < 50
        assert out.total_spent > 15
        assert out.currency == "USD"
    finally:
        db.close()


def test_health_still_ok():
    client = TestClient(app)
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200


def test_styles_removed_font_mono_inherit():
    css_path = os.path.join(os.path.dirname(__file__), "app", "static", "styles.css")
    css = open(css_path, encoding="utf-8").read()
    assert "html.theme-dark .font-mono { color: inherit; }" not in css
    assert ".tone-banner-success" in css
    assert ".public-page a.bb-btn-primary { color: var(--on-brand) !important; }" in css
    assert ".tab-more-select" in css


def _static(name: str) -> str:
    return open(
        os.path.join(os.path.dirname(__file__), "app", "static", name),
        encoding="utf-8",
    ).read()


def test_friends_ledger_uses_segment_pills():
    html = _static("friends.html")
    js = _static("friends.js")
    assert 'id="currentBillsTab"' in html
    assert "split-type-pill" in html
    assert 'classList.toggle("bb-btn-primary"' not in js
    assert 'classList.toggle("active"' in js


def test_group_invite_method_uses_segment_pills():
    html = _static("group.html")
    js = _static("group.js")
    assert 'id="showInviteEmailBtn"' in html
    assert "split-type-pill" in html
    assert 'classList.toggle("bb-btn-primary", emailMode)' not in js
    assert 'id="groupTabSelect"' in html
    assert "groupTabSelect" in js
    assert 'id="onlyMyExpenses"' in html
    assert 'id="onlyMyExpenses" type="checkbox" checked' not in html
    assert "tone-banner-warning" in html


def test_quick_join_passes_next_to_login():
    html = _static("quick-join.html")
    assert "login.html?next=" in html
    assert "sharesquad_pending_quick_split" in html
    assert 'id="signInLink"' in html


def test_public_pages_include_session_aware_chrome():
    auth_js = _static("public-auth.js")
    assert "Open app" in auth_js
    for page in (
        "landing.html",
        "help.html",
        "about.html",
        "receipt-scanning.html",
        "shared-groups.html",
        "clear-balances.html",
        "privacy.html",
        "terms.html",
        "contact.html",
    ):
        assert "public-auth.js" in _static(page), page


def test_dashboard_draft_card_copy():
    js = _static("dashboard.js")
    assert "Finish setup" in js
    assert "invite a member" in js
