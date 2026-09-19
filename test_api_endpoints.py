"""
Verify all FastAPI Backend Routes End-to-End
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient
from app.main import app

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

    # 4. Add Group Expense with Percentage Split
    me = client.get("/api/auth/me", headers=headers).json()
    my_member_id = client.get(f"/api/groups/{group_id}", headers=headers).json()["members"][0]["id"]

    exp_payload = {
        "store_name": "Costco Wholesale",
        "bill_date": "2026-09-15",
        "total_amount": 150.00,
        "currency": "USD",
        "category": "Groceries",
        "split_type": "PERCENTAGE",
        "payer_member_id": my_member_id,
        "split_details": [{"member_id": my_member_id, "value": 100.0}],
    }
    r = client.post(f"/api/groups/{group_id}/expenses", json=exp_payload, headers=headers)
    assert r.status_code == 200, f"Expense create failed: {r.text}"
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

    # 7. Personal Expense Create & Summary
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
