"""Paid bill edit/delete must reverse into balances as credit (overpayment)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app import models

client = TestClient(app)


def _auth(email: str, name: str):
    r = client.post("/api/auth/register", json={"email": email, "password": "Password12!", "display_name": name})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    me = client.get("/api/auth/me", headers=headers).json()
    return headers, me["account"]["id"]


def test_group_delete_after_payment_creates_credit():
    email = f"overpay_a_{os.getpid()}@example.com"
    friend_email = f"overpay_b_{os.getpid()}@gmail.com"
    headers, _ = _auth(email, "Alice")
    friend_headers, _ = _auth(friend_email, "Bob")

    r = client.post("/api/groups", json={"name": "Overpay Group", "currency": "USD"}, headers=headers)
    assert r.status_code == 200, r.text
    group_id = r.json()["id"]
    assert client.post(f"/api/groups/{group_id}/invites", json={"email": friend_email}, headers=headers).status_code == 200

    members = client.get(f"/api/groups/{group_id}", headers=headers).json()["members"]
    my_member_id = next(m["id"] for m in members if m["email"] == email)
    friend_member_id = next(m["id"] for m in members if m["email"] == friend_email)

    r = client.post(
        f"/api/groups/{group_id}/expenses",
        json={
            "store_name": "Dinner",
            "bill_date": "2026-09-20",
            "total_amount": 100.00,
            "currency": "USD",
            "category": "Food",
            "split_type": "EQUAL",
            "payer_member_id": my_member_id,
            "split_with": [my_member_id, friend_member_id],
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text
    expense_id = r.json()["id"]

    db = SessionLocal()
    try:
        db.query(models.MonthlyPeriod).filter_by(group_id=group_id, period_key="2026-09").update({"status": "SETTLED"})
        db.commit()
    finally:
        db.close()

    r = client.post(
        f"/api/groups/{group_id}/periods/2026-09/payments",
        json={"payer_member_id": friend_member_id, "recipient_member_id": my_member_id, "amount": 50, "currency": "USD"},
        headers=friend_headers,
    )
    assert r.status_code == 200, r.text
    payment_id = r.json()["id"]
    r = client.post(f"/api/groups/{group_id}/payments/{payment_id}/confirm", headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "CONFIRMED", r.text

    balances = {b["member_id"]: b for b in client.get(f"/api/groups/{group_id}/periods/2026-09/balances", headers=headers).json()}
    assert abs(float(balances[friend_member_id]["net_balance"])) < 0.01, balances[friend_member_id]

    # Owner corrects a settled/paid period by deleting the bill — overpay becomes credit.
    r = client.delete(f"/api/groups/{group_id}/expenses/{expense_id}?owner_override=true", headers=headers)
    assert r.status_code == 200, r.text

    balances = {b["member_id"]: b for b in client.get(f"/api/groups/{group_id}/periods/2026-09/balances", headers=headers).json()}
    # Friend paid 50; bill gone → friend is owed 50 (positive net).
    assert float(balances[friend_member_id]["net_balance"]) == 50.0, balances[friend_member_id]
    assert float(balances[my_member_id]["net_balance"]) == -50.0, balances[my_member_id]
    print("  [PASS] Group expense delete after payment leaves overpay credit")


def test_group_edit_down_after_payment_creates_credit():
    email = f"editpay_a_{os.getpid()}@example.com"
    friend_email = f"editpay_b_{os.getpid()}@gmail.com"
    headers, _ = _auth(email, "Alice")
    friend_headers, _ = _auth(friend_email, "Bob")

    r = client.post("/api/groups", json={"name": "Edit Overpay", "currency": "USD"}, headers=headers)
    group_id = r.json()["id"]
    client.post(f"/api/groups/{group_id}/invites", json={"email": friend_email}, headers=headers)
    members = client.get(f"/api/groups/{group_id}", headers=headers).json()["members"]
    my_member_id = next(m["id"] for m in members if m["email"] == email)
    friend_member_id = next(m["id"] for m in members if m["email"] == friend_email)

    payload = {
        "store_name": "Lunch",
        "bill_date": "2026-09-21",
        "total_amount": 100.00,
        "currency": "USD",
        "category": "Food",
        "split_type": "EQUAL",
        "payer_member_id": my_member_id,
        "split_with": [my_member_id, friend_member_id],
    }
    r = client.post(f"/api/groups/{group_id}/expenses", json=payload, headers=headers)
    expense_id = r.json()["id"]

    db = SessionLocal()
    try:
        db.query(models.MonthlyPeriod).filter_by(group_id=group_id, period_key="2026-09").update({"status": "SETTLED"})
        db.commit()
    finally:
        db.close()

    r = client.post(
        f"/api/groups/{group_id}/periods/2026-09/payments",
        json={"payer_member_id": friend_member_id, "recipient_member_id": my_member_id, "amount": 50, "currency": "USD"},
        headers=friend_headers,
    )
    payment_id = r.json()["id"]
    client.post(f"/api/groups/{group_id}/payments/{payment_id}/confirm", headers=headers)

    r = client.put(
        f"/api/groups/{group_id}/expenses/{expense_id}?owner_override=true",
        json={**payload, "total_amount": 40.00},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    balances = {b["member_id"]: b for b in client.get(f"/api/groups/{group_id}/periods/2026-09/balances", headers=headers).json()}
    # Friend paid 50; new share is 20 → credit 30.
    assert float(balances[friend_member_id]["net_balance"]) == 30.0, balances[friend_member_id]
    assert float(balances[my_member_id]["net_balance"]) == -30.0, balances[my_member_id]
    print("  [PASS] Group expense edit-down after payment leaves overpay credit")


def test_friend_delete_after_confirm_creates_credit():
    email = f"fpay_a_{os.getpid()}@example.com"
    friend_email = f"fpay_b_{os.getpid()}@gmail.com"
    headers, alice_id = _auth(email, "Alice")
    friend_headers, bob_id = _auth(friend_email, "Bob")

    r = client.post("/api/friends/invites", json={"email": friend_email}, headers=headers)
    assert r.status_code == 200, r.text
    # Accept invite — find pending request on Bob's side
    invites = client.get("/api/friends", headers=friend_headers).json()
    # Some installs activate on invite create; ensure friendship via quick-split if needed.
    r = client.post(
        "/api/quick-splits",
        json={
            "title": "Coffee",
            "total_amount": 100,
            "currency": "USD",
            "bill_date": "2026-09-20",
            "category": "Food",
            "split_type": "EQUAL",
            "payer_account_id": alice_id,
            "participants": [{"account_id": bob_id, "value": 1}],
        },
        headers=headers,
    )
    if r.status_code != 200:
        # Alternate payload shape
        r = client.post(
            "/api/quick-splits",
            json={
                "title": "Coffee",
                "total_amount": 100,
                "currency": "USD",
                "bill_date": "2026-09-20",
                "category": "Food",
                "split_type": "EQUAL",
                "participants": [{"account_id": bob_id}],
            },
            headers=headers,
        )
    assert r.status_code == 200, r.text
    split = r.json()
    split_id = split["id"]
    bob_participant = next(p for p in split["participants"] if p.get("account_id") == bob_id or not p.get("is_me"))

    r = client.post(
        f"/api/quick-splits/{split_id}/participants/{bob_participant['id']}/mark-paid",
        headers=friend_headers,
    )
    assert r.status_code == 200, r.text
    r = client.post(
        f"/api/quick-splits/{split_id}/participants/{bob_participant['id']}/confirm",
        headers=headers,
    )
    assert r.status_code == 200, r.text

    before = client.get(f"/api/friends/{bob_id}/consolidated-balance?currency=USD", headers=headers).json()
    assert abs(float(before["balance"])) < 0.02, before

    r = client.delete(f"/api/quick-splits/{split_id}", headers=headers)
    assert r.status_code == 200, r.text

    after = client.get(f"/api/friends/{bob_id}/consolidated-balance?currency=USD", headers=headers).json()
    # Alice's view vs Bob: Bob paid 50 then bill deleted → Alice owes Bob 50 → balance negative for Alice.
    assert float(after["balance"]) == -50.0, after
    print("  [PASS] Friend quick-split delete after confirm leaves overpay credit")


if __name__ == "__main__":
    test_group_delete_after_payment_creates_credit()
    test_group_edit_down_after_payment_creates_credit()
    test_friend_delete_after_confirm_creates_credit()
    print("\nOverpay credit tests done.")
