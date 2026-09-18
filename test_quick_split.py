"""End-to-end checks for the one-off Quick Split flow."""
import os

from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def _register(email, name):
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123", "display_name": name},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_quick_split_create_join_and_confirm_payment():
    suffix = str(os.getpid())
    owner_headers = _register(f"quick_owner_{suffix}@quicksharemail.com", "Owner")
    friend_headers = _register(f"quick_friend_{suffix}@quicksharemail.com", "Friend")

    created = client.post(
        "/api/quick-splits",
        headers=owner_headers,
        json={
            "title": "Dinner",
            "total_amount": 42.00,
            "currency": "USD",
            "bill_date": "2026-09-18",
            "category": "Food & Dining",
            "participants": [{"email": f"quick_friend_{suffix}@quicksharemail.com"}],
        },
    )
    assert created.status_code == 200, created.text
    split = created.json()
    assert split["participants"][0]["share_amount"] == 21.0

    joined = client.post(
        "/api/quick-splits/join",
        headers=friend_headers,
        json={"invite_code": split["invite_code"]},
    )
    assert joined.status_code == 200, joined.text
    participant_id = joined.json()["participants"][0]["id"]

    marked = client.post(
        f"/api/quick-splits/{split['id']}/participants/{participant_id}/mark-paid",
        headers=friend_headers,
    )
    assert marked.status_code == 200, marked.text
    assert marked.json()["participants"][0]["payment_status"] == "PAID"

    confirmed = client.post(
        f"/api/quick-splits/{split['id']}/participants/{participant_id}/confirm",
        headers=owner_headers,
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "SETTLED"

    third_email = f"quick_third_{suffix}@quicksharemail.com"
    _register(third_email, "Third")
    custom = client.post(
        "/api/quick-splits",
        headers=owner_headers,
        json={
            "title": "Cab",
            "total_amount": 100,
            "currency": "USD",
            "bill_date": "2026-09-18",
            "category": "Transit",
            "split_type": "PERCENTAGE",
            "payer_value": 20,
            "participants": [
                {"email": f"quick_friend_{suffix}@quicksharemail.com", "value": 30},
                {"email": third_email, "value": 50},
            ],
        },
    )
    assert custom.status_code == 200, custom.text
    assert custom.json()["payer_share_amount"] == 20
    assert sorted(p["share_amount"] for p in custom.json()["participants"]) == [30, 50]

    updated = client.put(
        f"/api/quick-splits/{custom.json()['id']}",
        headers=owner_headers,
        json={
            "title": "Cab updated",
            "total_amount": 100,
            "currency": "USD",
            "bill_date": "2026-09-18",
            "category": "Transit",
            "split_type": "EXACT",
            "payer_value": 10,
            "participants": [
                {"email": f"quick_friend_{suffix}@quicksharemail.com", "value": 40},
                {"email": third_email, "value": 50},
            ],
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["payer_share_amount"] == 10
    assert sorted(p["share_amount"] for p in updated.json()["participants"]) == [40, 50]
