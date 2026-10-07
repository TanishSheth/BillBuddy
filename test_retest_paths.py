"""Automated coverage for previously untested QA paths."""
import os
from datetime import date

os.environ.setdefault("BILLBUDDY_ENV", "development")

from fastapi.testclient import TestClient

from app import models
from app.auth import hash_password
from app.database import Base, SessionLocal, engine
from app.main import app

client = TestClient(app)


def _pair():
    pid = os.getpid()
    email_a = f"retest_a_{pid}@example.com"
    email_b = f"retest_b_{pid}@gmail.com"
    Base.metadata.create_all(bind=engine)
    ra = client.post("/api/auth/register", json={"email": email_a, "password": "Password12!", "display_name": "Ada"})
    rb = client.post("/api/auth/register", json={"email": email_b, "password": "Password12!", "display_name": "Bea"})
    if ra.status_code != 200:
        # Already registered in a prior run — log in instead.
        ra = client.post("/api/auth/login", json={"email": email_a, "password": "Password12!"})
        rb = client.post("/api/auth/login", json={"email": email_b, "password": "Password12!"})
    assert ra.status_code == 200 and rb.status_code == 200, (ra.text, rb.text)
    ha = {"Authorization": f"Bearer {ra.json()['access_token']}"}
    hb = {"Authorization": f"Bearer {rb.json()['access_token']}"}
    alice = client.get("/api/auth/me", headers=ha).json()["account"]["id"]
    bob = client.get("/api/auth/me", headers=hb).json()["account"]["id"]
    client.post("/api/friends/invites", json={"email": email_b}, headers=ha)
    return alice, bob, ha, hb, email_a


def test_payment_applies_immediately_and_confirm_is_idempotent():
    alice, bob, ha, hb, _email = _pair()
    created = client.post(
        "/api/quick-splits",
        json={
            "title": "Coffee",
            "total_amount": 20.0,
            "currency": "USD",
            "bill_date": date.today().isoformat(),
            "category": "Food & Dining",
            "split_type": "EQUAL",
            "payer_account_id": alice,
            "participants": [{"account_id": bob}],
            "silent_invite": True,
        },
        headers=ha,
    )
    assert created.status_code == 200, created.text
    split_id = created.json()["id"]

    paid = client.post(
        f"/api/friends/{alice}/consolidated-payments",
        json={"amount": 10.0, "currency": "USD"},
        headers=hb,
    )
    assert paid.status_code == 200, paid.text
    payment = paid.json()
    assert payment["status"] == "CONFIRMED"

    # Legacy confirm endpoint remains safe for already-applied rows.
    confirm = client.post(
        f"/api/friends/{alice}/consolidated-payments/{payment['id']}/confirm",
        headers=hb,
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["status"] == "CONFIRMED"

    detail = client.get(f"/api/quick-splits/{split_id}", headers=ha)
    assert detail.status_code == 200
    assert detail.json()["participants"][0]["payment_status"] == "CONFIRMED"


def test_locked_bill_rejects_edit_after_payment():
    alice, bob, ha, hb, _email = _pair()
    # Use a unique title/amount so this run is independent of prior payment balance.
    created = client.post(
        "/api/quick-splits",
        json={
            "title": "Locked Bill",
            "total_amount": 40.0,
            "currency": "USD",
            "bill_date": date.today().isoformat(),
            "category": "Food & Dining",
            "split_type": "EQUAL",
            "payer_account_id": alice,
            "participants": [{"account_id": bob}],
            "silent_invite": True,
        },
        headers=ha,
    )
    assert created.status_code == 200, created.text
    split = created.json()
    bal = client.get(f"/api/friends/{alice}/consolidated-balance?currency=USD", headers=hb).json()
    amount = abs(float(bal.get("settleable_balance") or bal.get("balance") or 20.0))
    paid = client.post(
        f"/api/friends/{alice}/consolidated-payments",
        json={"amount": min(amount, 20.0), "currency": "USD"},
        headers=hb,
    )
    assert paid.status_code == 200, paid.text

    # Mark month settled so the bill is payment-locked for edits.
    db = SessionLocal()
    try:
        one, two = sorted((alice, bob))
        month_key = date.today().strftime("%Y-%m")
        existing = (
            db.query(models.FriendMonthlySettlement)
            .filter_by(account_one_id=one, account_two_id=two, month_key=month_key)
            .first()
        )
        if not existing:
            db.add(
                models.FriendMonthlySettlement(
                    account_one_id=one,
                    account_two_id=two,
                    month_key=month_key,
                    payer_account_id=bob,
                    recipient_account_id=alice,
                    amount=20,
                    currency="USD",
                    status="SETTLED",
                )
            )
        else:
            existing.status = "SETTLED"
        db.commit()
    finally:
        db.close()

    edited = client.put(
        f"/api/quick-splits/{split['id']}",
        json={
            "title": "Should Fail",
            "total_amount": 41.0,
            "currency": "USD",
            "bill_date": date.today().isoformat(),
            "category": "Food & Dining",
            "split_type": "EQUAL",
            "payer_account_id": alice,
            "participants": [{"account_id": bob}],
        },
        headers=ha,
    )
    assert edited.status_code == 409, edited.text
    assert "completed payment" in edited.json()["detail"].lower()


def test_forgot_password_success_when_smtp_works(monkeypatch):
    Base.metadata.create_all(bind=engine)
    email = "retest-forgot-ok@example.com"
    db = SessionLocal()
    try:
        if not db.query(models.Account).filter_by(email=email).first():
            db.add(
                models.Account(
                    email=email,
                    password_hash=hash_password("Password12!"),
                    display_name="Forgot OK",
                )
            )
            db.commit()
    finally:
        db.close()

    sent = {"n": 0}

    def ok_send(**_kwargs):
        sent["n"] += 1
        return True

    monkeypatch.setattr("app.main.is_email_configured", lambda: True)
    monkeypatch.setattr("app.main.send_password_reset_email", ok_send)
    res = client.post("/api/auth/forgot-password", json={"email": email})
    assert res.status_code == 200
    body = res.json()
    assert "password reset" in body["message"].lower()
    assert sent["n"] == 1
    # Dev responses may include a reset_url for local testing.
    if body.get("reset_url"):
        assert "reset-password.html?token=" in body["reset_url"]


def test_camera_capture_hooks_present():
    """Camera OCR uses the same upload path as gallery; UI wiring must exist."""
    static = os.path.join(os.path.dirname(__file__), "app", "static")
    group_js = open(os.path.join(static, "group.js"), encoding="utf-8").read()
    personal_js = open(os.path.join(static, "personal.js"), encoding="utf-8").read()
    api_js = open(os.path.join(static, "api.js"), encoding="utf-8").read()
    group_html = open(os.path.join(static, "group.html"), encoding="utf-8").read()
    assert "bindReceiptCamera" in group_js and "onCapture: handleReceiptUpload" in group_js
    assert "bindReceiptCamera" in personal_js and "onCapture: handleReceiptUpload" in personal_js
    assert "canvas.toBlob" in api_js and 'image/jpeg' in api_js
    assert 'id="liveCameraModal"' in group_html
    assert 'id="pickCameraBtn"' in group_html
