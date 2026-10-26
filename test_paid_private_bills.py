"""Private bills covered by Friend payments must show as paid."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app import models
from app.redis_support import clear_memory_rate_limits
import json

client = TestClient(app)


def test_consolidated_payment_marks_private_bill_paid():
    clear_memory_rate_limits()
    pid = os.getpid()
    email_a = f"lyft_a_{pid}@example.com"
    email_b = f"lyft_b_{pid}@gmail.com"
    ra = client.post("/api/auth/register", json={"email": email_a, "password": "Password12!", "display_name": "Alice"})
    rb = client.post("/api/auth/register", json={"email": email_b, "password": "Password12!", "display_name": "Bob"})
    assert ra.status_code == 200 and rb.status_code == 200, (ra.text, rb.text)
    ha = {"Authorization": f"Bearer {ra.json()['access_token']}"}
    hb = {"Authorization": f"Bearer {rb.json()['access_token']}"}
    alice = client.get("/api/auth/me", headers=ha).json()["account"]["id"]
    bob = client.get("/api/auth/me", headers=hb).json()["account"]["id"]
    client.post("/api/friends/invites", json={"email": email_b}, headers=ha)

    created = client.post(
        "/api/quick-splits",
        json={
            "title": "Lyft",
            "total_amount": 10.34,
            "currency": "USD",
            "bill_date": "2026-10-01",
            "category": "Transport",
            "split_type": "EQUAL",
            "payer_account_id": alice,
            "participants": [{"account_id": bob}],
            "silent_invite": True,
        },
        headers=ha,
    )
    assert created.status_code == 200, created.text
    split = created.json()
    assert abs(float(split["participants"][0]["share_amount"]) - 5.17) < 0.001

    paid = client.post(
        f"/api/friends/{alice}/consolidated-payments",
        json={"amount": 5.17, "currency": "USD"},
        headers=hb,
    )
    assert paid.status_code == 200, paid.text
    payment = paid.json()
    # Debtor-marked payments stay PENDING until the lender confirms.
    assert payment["status"] == "PENDING", payment

    confirmed = client.post(
        f"/api/friends/{bob}/consolidated-payments/{payment['id']}/confirm",
        headers=ha,
    )
    assert confirmed.status_code == 200, confirmed.text
    confirmed_body = confirmed.json()
    assert confirmed_body["status"] == "CONFIRMED"
    assert any(
        alloc.get("quick_split_id") == split["id"] for alloc in confirmed_body.get("allocations") or []
    ), confirmed_body

    detail = client.get(f"/api/quick-splits/{split['id']}", headers=ha)
    assert detail.status_code == 200, detail.text
    participant = detail.json()["participants"][0]
    assert participant["payment_status"] == "CONFIRMED", participant
    print("  [PASS] debtor payment pending then lender confirm marks Lyft private bill CONFIRMED")


def test_repair_marks_legacy_month_allocation():
    clear_memory_rate_limits()
    pid = os.getpid() + 17
    email_a = f"legacy_a_{pid}@example.com"
    email_b = f"legacy_b_{pid}@gmail.com"
    ra = client.post("/api/auth/register", json={"email": email_a, "password": "Password12!", "display_name": "Alice"})
    rb = client.post("/api/auth/register", json={"email": email_b, "password": "Password12!", "display_name": "Bob"})
    ha = {"Authorization": f"Bearer {ra.json()['access_token']}"}
    hb = {"Authorization": f"Bearer {rb.json()['access_token']}"}
    alice = client.get("/api/auth/me", headers=ha).json()["account"]["id"]
    bob = client.get("/api/auth/me", headers=hb).json()["account"]["id"]
    client.post("/api/friends/invites", json={"email": email_b}, headers=ha)
    created = client.post(
        "/api/quick-splits",
        json={
            "title": "Lyft Legacy",
            "total_amount": 10.34,
            "currency": "USD",
            "bill_date": "2026-09-15",
            "category": "Transport",
            "split_type": "EQUAL",
            "payer_account_id": alice,
            "participants": [{"account_id": bob}],
            "silent_invite": True,
        },
        headers=ha,
    )
    assert created.status_code == 200, created.text
    split_id = created.json()["id"]

    db = SessionLocal()
    try:
        one, two = sorted((alice, bob))
        payment = models.PairPayment(
            account_one_id=one,
            account_two_id=two,
            payer_account_id=bob,
            recipient_account_id=alice,
            amount=5.17,
            currency="USD",
            status="CONFIRMED",
            reported_by_account_id=bob,
            allocations_json=json.dumps([{
                "kind": "DIRECT",
                "month_key": "2026-09",
                "amount": 5.17,
            }]),
            confirmed_at=datetime_now(),
        )
        db.add(payment)
        db.commit()
    finally:
        db.close()

    # Opening consolidated balances runs the repair for existing rows.
    bal = client.get(f"/api/friends/{bob}/consolidated-balances", headers=ha)
    assert bal.status_code == 200, bal.text
    detail = client.get(f"/api/quick-splits/{split_id}", headers=ha)
    assert detail.status_code == 200, detail.text
    assert detail.json()["participants"][0]["payment_status"] == "CONFIRMED", detail.json()
    print("  [PASS] legacy month allocation repair marks private bill CONFIRMED")


def datetime_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


if __name__ == "__main__":
    test_consolidated_payment_marks_private_bill_paid()
    test_repair_marks_legacy_month_allocation()
    print("\nPaid private bill tests done.")
