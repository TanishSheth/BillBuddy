"""Regression coverage for the 7 must-fixes in REGRESSION-REPORT-20261007ocrScaleComplete."""
import os
from datetime import date
from decimal import Decimal

os.environ.setdefault("BILLBUDDY_ENV", "development")

from fastapi.testclient import TestClient

from app import models
from app.auth import hash_password
from app.database import Base, SessionLocal, engine
from app.fx_service import get_conversion_rate
from app.main import app, get_group_dashboard
from app.ocr_extractor import tesseract_status

client = TestClient(app)


def test_health_reports_tesseract():
    health = client.get("/api/health").json()
    assert "tesseract" in health
    assert "ocr_ready" in health
    assert "available" in health["tesseract"]
    status = tesseract_status()
    assert status["available"] is True or status.get("error")


def test_login_html_rejects_marketing_next():
    html = open(
        os.path.join(os.path.dirname(__file__), "app", "static", "login.html"),
        encoding="utf-8",
    ).read()
    assert "safeAppRedirect" in html
    assert 'next === "/"' in html
    assert "location.replace" in html


def test_group_js_honors_query_group():
    js = open(
        os.path.join(os.path.dirname(__file__), "app", "static", "group.js"),
        encoding="utf-8",
    ).read()
    assert 'get("group")' in js
    assert "setStoredGroupId(urlGroup)" in js


def test_dashboard_does_not_probe_demo_sandbox():
    js = open(
        os.path.join(os.path.dirname(__file__), "app", "static", "dashboard.js"),
        encoding="utf-8",
    ).read()
    assert 'api("/demo/sandbox")' not in js
    assert 'sharesquad_demo_sandbox") === "1"' in js


def test_smtp_timeout_default_is_short():
    src = open(
        os.path.join(os.path.dirname(__file__), "app", "emailer.py"),
        encoding="utf-8",
    ).read()
    assert "SMTP_TIMEOUT_SECONDS" in src
    assert 'timeout=smtp_timeout' in src


def test_create_expense_sets_fx_rate_to_group():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        owner = db.query(models.Account).filter_by(email="fx-owner@example.com").first()
        if not owner:
            owner = models.Account(
                email="fx-owner@example.com",
                password_hash=hash_password("Password12!"),
                display_name="FX Owner",
            )
            db.add(owner)
            db.commit()
            db.refresh(owner)
        friend = db.query(models.Account).filter_by(email="fx-friend@gmail.com").first()
        if not friend:
            friend = models.Account(
                email="fx-friend@gmail.com",
                password_hash=hash_password("Password12!"),
                display_name="FX Friend",
            )
            db.add(friend)
            db.commit()
            db.refresh(friend)
        group = models.Group(name="FX Reg", currency="USD", created_by_id=owner.id)
        db.add(group)
        db.commit()
        db.refresh(group)
        m1 = models.GroupMember(group_id=group.id, account_id=owner.id, display_name="O", role="owner", is_active=True)
        m2 = models.GroupMember(group_id=group.id, account_id=friend.id, display_name="F", role="member", is_active=True)
        db.add_all([m1, m2])
        db.commit()
        db.refresh(m1)
        db.refresh(m2)
        group_id, m1_id, m2_id, owner_id = group.id, m1.id, m2.id, owner.id
    finally:
        db.close()

    token = client.post(
        "/api/auth/login", json={"email": "fx-owner@example.com", "password": "Password12!"}
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    res = client.post(
        f"/api/groups/{group_id}/expenses",
        headers=headers,
        json={
            "store_name": "Delhi Cafe",
            "bill_date": date.today().isoformat(),
            "total_amount": 1000,
            "currency": "INR",
            "category": "Food & Dining",
            "split_type": "EQUAL",
            "payer_member_id": m1_id,
            "split_with": [m1_id, m2_id],
        },
    )
    assert res.status_code == 200, res.text
    expense_id = res.json()["id"]
    db = SessionLocal()
    try:
        expense = db.query(models.Expense).get(expense_id)
        assert expense is not None
        rate = float(expense.fx_rate_to_group or 0)
        assert rate > 0
        assert abs(rate - 1.0) > 0.01  # must not leave INR at 1:1 into USD
        expected = get_conversion_rate("INR", "USD")
        assert abs(rate - expected) < 0.01
        owner = db.query(models.Account).get(owner_id)
        dash = get_group_dashboard(group_id, date.today().strftime("%Y-%m"), owner, db)
        assert dash.total_spent < 50  # 1000 INR ≈ $12, not $1000
    finally:
        db.close()


def test_paid_bill_edit_notifies_and_marks_edited():
    pid = os.getpid()
    email_a = f"reg7_a_{pid}@example.com"
    email_b = f"reg7_b_{pid}@gmail.com"
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

    created = client.post(
        "/api/quick-splits",
        headers=ha,
        json={
            "title": "Dinner",
            "total_amount": 100,
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
    split = created.json()
    paid = client.post(
        f"/api/friends/{alice}/consolidated-payments",
        headers=hb,
        json={"amount": 50, "currency": "USD"},
    )
    assert paid.status_code == 200, paid.text

    edited = client.put(
        f"/api/quick-splits/{split['id']}",
        headers=ha,
        json={
            "title": "Dinner",
            "total_amount": 120,
            "currency": "USD",
            "bill_date": date.today().isoformat(),
            "category": "Food & Dining",
            "split_type": "EQUAL",
            "payer_account_id": alice,
            "participants": [{"account_id": bob}],
        },
    )
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert body["edited_after_payment"] is True
    assert body["last_adjustment_amount"] == 10.0
    assert body["total_amount"] == 120.0

    notes = client.get("/api/notifications", headers=hb).json()
    assert any("edited" in (n.get("message") or "").lower() and "Dinner" in (n.get("message") or "") for n in notes)
