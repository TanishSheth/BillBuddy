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
                {"email": f"quick_friend_{suffix}@quicksharemail.com", "value": 80},
            ],
        },
    )
    assert custom.status_code == 200, custom.text
    assert custom.json()["payer_share_amount"] == 20
    assert [p["share_amount"] for p in custom.json()["participants"]] == [80]

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
                {"email": f"quick_friend_{suffix}@quicksharemail.com", "value": 90},
            ],
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["payer_share_amount"] == 10
    assert [p["share_amount"] for p in updated.json()["participants"]] == [90]

    multiple_people = client.post(
        "/api/quick-splits",
        headers=owner_headers,
        json={
            "title": "Not a one-to-one split",
            "total_amount": 90,
            "currency": "USD",
            "bill_date": "2026-09-18",
            "participants": [
                {"label": "Person 1"},
                {"label": "Person 2"},
            ],
        },
    )
    assert multiple_people.status_code == 422

    link_only = client.post(
        "/api/quick-splits",
        headers=owner_headers,
        json={
            "title": "Tickets",
            "total_amount": 90,
            "currency": "USD",
            "bill_date": "2026-09-18",
            "category": "Entertainment",
            "participants": [{"label": "Person 1"}],
        },
    )
    assert link_only.status_code == 200, link_only.text
    link_split = link_only.json()
    assert [p["email"] for p in link_split["participants"]] == [None]
    updated_link_split = client.put(
        f"/api/quick-splits/{link_split['id']}",
        headers=owner_headers,
        json={
            "title": "Updated tickets",
            "total_amount": 90,
            "currency": "USD",
            "bill_date": "2026-09-18",
            "category": "Entertainment",
            "split_type": "EXACT",
            "payer_value": 65,
            "participants": [
                {"label": "Person 1", "value": 25},
            ],
        },
    )
    assert updated_link_split.status_code == 200, updated_link_split.text
    assert [p["share_amount"] for p in updated_link_split.json()["participants"]] == [25]
    link_split = updated_link_split.json()
    claimed = client.post(
        "/api/quick-splits/join",
        headers=friend_headers,
        json={"invite_code": link_split["invite_code"], "participant_token": link_split["participants"][0]["invite_token"]},
    )
    assert claimed.status_code == 200, claimed.text
    assert any(p["is_me"] for p in claimed.json()["participants"])

    friend_account_id = next(p["account_id"] for p in claimed.json()["participants"] if p["is_me"])
    future_bill = client.post(
        "/api/quick-splits",
        headers=owner_headers,
        json={
            "title": "Coffee",
            "total_amount": 12,
            "currency": "USD",
            "bill_date": "2026-09-18",
            "silent_invite": True,
            "participants": [{"account_id": friend_account_id}],
        },
    )
    assert future_bill.status_code == 200, future_bill.text
    friend_splits = client.get("/api/quick-splits", headers=friend_headers)
    assert any(row["id"] == future_bill.json()["id"] for row in friend_splits.json())

    owner_account_id = client.get("/api/auth/me", headers=owner_headers).json()["account"]["id"]
    friend_paid = client.post(
        "/api/quick-splits",
        headers=owner_headers,
        json={
            "title": "Friend paid for lunch",
            "total_amount": 20,
            "currency": "USD",
            "bill_date": "2026-09-19",
            "payer_account_id": friend_account_id,
            "participants": [{"account_id": owner_account_id}],
        },
    )
    assert friend_paid.status_code == 200, friend_paid.text
    assert friend_paid.json()["is_payer"] is False
    assert friend_paid.json()["is_owner"] is True
    assert friend_paid.json()["participants"][0]["is_me"] is True

    forbidden_delete = client.delete(f"/api/quick-splits/{link_split['id']}", headers=friend_headers)
    assert forbidden_delete.status_code == 403
    deleted = client.delete(f"/api/quick-splits/{link_split['id']}", headers=owner_headers)
    assert deleted.status_code == 200, deleted.text
    missing = client.get(f"/api/quick-splits/{link_split['id']}", headers=owner_headers)
    assert missing.status_code == 404
