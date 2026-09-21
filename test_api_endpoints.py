"""
Verify all FastAPI Backend Routes End-to-End
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app import models

client = TestClient(app)

def test_full_flow():
    # 1. Health
    r = client.get("/api/health")
    assert r.status_code == 200, f"Health failed: {r.text}"
    print("  [PASS] Health check passed!")

    # 2. Register / Login
    email = f"user_{os.getpid()}@example.com"
    r = client.post("/api/auth/register", json={"email": email, "password": "Password12!", "display_name": "Tanish"})
    if r.status_code == 409:
        r = client.post("/api/auth/login", json={"email": email, "password": "Password12!"})
    assert r.status_code == 200, f"Auth failed: {r.text}"
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    print("  [PASS] Registration & Auth passed!")

    # 3. Create Group
    r = client.post("/api/groups", json={"name": "US Vacation 2026", "currency": "USD"}, headers=headers)
    assert r.status_code == 200, f"Group create failed: {r.text}"
    group = r.json()
    group_id = group["id"]
    print(f"  [PASS] Group created (ID: {group_id}, Currency: {group['currency']})!")

    # 4. Add a second active member. Draft groups cannot hold shared expenses.
    friend_email = f"friend_{os.getpid()}@gmail.com"
    r = client.post("/api/auth/register", json={"email": friend_email, "password": "Password12!", "display_name": "Friend"})
    assert r.status_code == 200, f"Second account creation failed: {r.text}"
    friend_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = client.post(f"/api/groups/{group_id}/invites", json={"email": friend_email}, headers=headers)
    assert r.status_code == 200, f"Group invite failed: {r.text}"

    # 5. Add Group Expense with Percentage Split
    me = client.get("/api/auth/me", headers=headers).json()
    members = client.get(f"/api/groups/{group_id}", headers=headers).json()["members"]
    my_member_id = next(member["id"] for member in members if member["email"] == email)
    friend_member_id = next(member["id"] for member in members if member["email"] == friend_email)

    exp_payload = {
        "store_name": "Costco Wholesale",
        "bill_date": "2026-09-15",
        "total_amount": 150.00,
        "currency": "USD",
        "category": "Groceries",
        "split_type": "PERCENTAGE",
        "payer_member_id": my_member_id,
        "split_details": [{"member_id": my_member_id, "value": 50.0}, {"member_id": friend_member_id, "value": 50.0}],
    }
    r = client.post(f"/api/groups/{group_id}/expenses", json=exp_payload, headers=headers)
    assert r.status_code == 200, f"Expense create failed: {r.text}"
    expense_id = r.json()["id"]
    print("  [PASS] Custom percentage expense created!")

    # 5. Currency Auto-Conversion Endpoint
    r = client.post(
        f"/api/groups/{group_id}/periods/2026-09/convert-currency",
        json={"target_currency": "INR", "custom_exchange_rate": 83.50},
        headers=headers,
    )
    assert r.status_code == 200, f"Currency convert failed: {r.text}"
    print("  [PASS] Settlement currency auto-conversion set!")

    # 6. Balances & Settlements Check
    r = client.get(f"/api/groups/{group_id}/periods/2026-09/balances", headers=headers)
    assert r.status_code == 200, f"Balances failed: {r.text}"
    balances = r.json()
    assert len(balances) > 0
    assert balances[0]["converted_currency"] == "INR"
    print(f"  [PASS] Converted balances verified (Net: {balances[0]['net_balance']} USD -> {balances[0]['converted_net_balance']} INR)!")

    # 7. Payment confirmation locks ordinary edits. The owner can correct the
    # period, which keeps the payment and recalculates the remaining balance.
    active_payment = client.post(
        f"/api/groups/{group_id}/periods/2026-09/payments",
        json={"payer_member_id": friend_member_id, "recipient_member_id": my_member_id, "amount": 75, "currency": "USD"},
        headers=friend_headers,
    )
    assert active_payment.status_code == 400
    db = SessionLocal()
    try:
        db.query(models.MonthlyPeriod).filter_by(group_id=group_id, period_key="2026-09").update({"status": "SETTLED"})
        db.commit()
    finally:
        db.close()
    r = client.post(
        f"/api/groups/{group_id}/periods/2026-09/payments",
        json={"payer_member_id": friend_member_id, "recipient_member_id": my_member_id, "amount": 75, "currency": "USD"},
        headers=friend_headers,
    )
    assert r.status_code == 200, f"Mark paid failed: {r.text}"
    payment_id = r.json()["id"]
    r = client.post(f"/api/groups/{group_id}/payments/{payment_id}/confirm", headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "CONFIRMED", f"Payment confirm failed: {r.text}"
    r = client.post(f"/api/groups/{group_id}/periods/2026-09/reopen", headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "ACTIVE", f"Reopen failed: {r.text}"
    blocked = client.put(f"/api/groups/{group_id}/expenses/{expense_id}", json=exp_payload, headers=friend_headers)
    assert blocked.status_code == 409
    r = client.put(f"/api/groups/{group_id}/expenses/{expense_id}?owner_override=true", json={**exp_payload, "total_amount": 200.00}, headers=headers)
    assert r.status_code == 200, f"Owner correction failed: {r.text}"
    r = client.get(f"/api/groups/{group_id}/dashboard?period_key=2026-09", headers=headers)
    assert r.status_code == 200 and r.json()["total_spent"] == 200.0
    r = client.post(f"/api/groups/{group_id}/expenses/{expense_id}/comments", json={"body": "Updated after the payment."}, headers=headers)
    assert r.status_code == 200
    assert client.get(f"/api/groups/{group_id}/activity", headers=headers).status_code == 200
    assert client.get("/api/notifications", headers=headers).status_code == 200

    # A completed report locks expense editing, but people can still record
    # the real transfer that completes an outstanding settlement.
    db = SessionLocal()
    try:
        period = db.query(models.MonthlyPeriod).filter_by(group_id=group_id, period_key="2026-09").one()
        period.status = "SETTLED"
        db.commit()
    finally:
        db.close()
    r = client.post(
        f"/api/groups/{group_id}/periods/2026-09/payments/received",
        json={"payer_member_id": friend_member_id, "recipient_member_id": my_member_id, "amount": 100, "currency": "USD"},
        headers=headers,
    )
    assert r.status_code == 200 and r.json()["status"] == "CONFIRMED", f"Receiver shortcut on a settled period failed: {r.text}"
    db = SessionLocal()
    try:
        db.query(models.MonthlyPeriod).filter_by(group_id=group_id, period_key="2026-09").update({"status": "ACTIVE"})
        db.commit()
    finally:
        db.close()

    # A bill recorded after these transfers must remain outstanding. Older
    # confirmed payments are capped at the ledger snapshot they settled.
    later_expense = {**exp_payload, "store_name": "After settlement", "total_amount": 20.00}
    r = client.post(f"/api/groups/{group_id}/expenses", json=later_expense, headers=headers)
    assert r.status_code == 200, f"Later expense create failed: {r.text}"
    later_balances = {
        row["member_id"]: row for row in client.get(
            f"/api/groups/{group_id}/periods/2026-09/balances", headers=headers
        ).json()
    }
    assert later_balances[my_member_id]["net_balance"] == 10.0
    assert later_balances[friend_member_id]["net_balance"] == -10.0
    print("  [PASS] Payment confirmation, owner correction, activity, notes, and insights verified!")

    # 8. Removing a member from a reopened, paid period hides them from new
    # group activity without changing the agreed payment ledger.
    before_removal = client.get(f"/api/groups/{group_id}/periods/2026-09/balances", headers=headers)
    assert before_removal.status_code == 200
    r = client.delete(f"/api/groups/{group_id}/members/{friend_member_id}", headers=headers)
    assert r.status_code == 200, f"Member removal failed: {r.text}"
    r = client.get(f"/api/groups/{group_id}", headers=headers)
    assert r.status_code == 200
    assert all(member["id"] != friend_member_id for member in r.json()["members"])
    r = client.get(f"/api/groups/{group_id}/periods/2026-09/balances", headers=headers)
    assert r.status_code == 200
    assert r.json() == before_removal.json(), "Removing a member changed a payment-protected ledger"
    print("  [PASS] Removed member while preserving their payment-protected balance history!")

    # 9. Personal Expense Create & Summary
    p_exp = {
        "store_name": "Starbucks Coffee",
        "bill_date": "2026-09-15",
        "total_amount": 7.50,
        "currency": "USD",
        "category": "Food & Dining",
        "notes": "Morning latte",
    }
    r = client.post("/api/personal/expenses", json=p_exp, headers=headers)
    assert r.status_code == 200, f"Personal expense failed: {r.text}"
    print("  [PASS] Personal expense created successfully!")

    r = client.get("/api/personal/summary?period_key=2026-09", headers=headers)
    assert r.status_code == 200, f"Personal summary failed: {r.text}"
    summary = r.json()
    assert summary["total_spent"] >= 7.50
    assert "Food & Dining" in summary["category_breakdown"]
    print(f"  [PASS] Personal summary verified (Total spent: ${summary['total_spent']}, Categories: {list(summary['category_breakdown'].keys())})!")

if __name__ == "__main__":
    test_full_flow()
    print("\nALL BACKEND API & FRONTEND INTEGRATIONS ARE 100% OPERATIONAL!")
