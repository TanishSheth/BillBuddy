"""Friends display aggregation (N months) + all-time group balances."""
from __future__ import annotations

import os
import sys
import uuid

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.main import app

client = TestClient(app)


def _uid(prefix: str = "user") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}@gmail.com"


def _ready_pair_and_group():
    email_a, email_b = _uid("ca"), _uid("cb")
    ra = client.post("/api/auth/register", json={"email": email_a, "password": "Password12!", "display_name": "Alex"})
    rb = client.post("/api/auth/register", json={"email": email_b, "password": "Password12!", "display_name": "Blake"})
    assert ra.status_code == 200 and rb.status_code == 200, (ra.text, rb.text)
    ha = {"Authorization": f"Bearer {ra.json()['access_token']}"}
    hb = {"Authorization": f"Bearer {rb.json()['access_token']}"}
    group = client.post("/api/groups", json={"name": f"Combo-{uuid.uuid4().hex[:6]}", "currency": "USD"}, headers=ha)
    assert group.status_code == 200, group.text
    group_id = group.json()["id"]
    friend_id = client.get("/api/auth/me", headers=hb).json()["account"]["id"]
    me_id = client.get("/api/auth/me", headers=ha).json()["account"]["id"]
    added = client.post(f"/api/groups/{group_id}/members", json={"account_id": friend_id}, headers=ha)
    assert added.status_code == 200, added.text
    detail = client.get(f"/api/groups/{group_id}", headers=ha).json()
    by_email = {m["email"]: m for m in detail["members"]}
    return {
        "email_a": email_a,
        "email_b": email_b,
        "ha": ha,
        "hb": hb,
        "group_id": group_id,
        "me_account_id": me_id,
        "friend_account_id": friend_id,
        "me": by_email[email_a],
        "friend": by_email[email_b],
    }


def _add_group_expense(ctx, *, period_key: str, bill_date: str, total: float, store: str):
    payload = {
        "store_name": store,
        "bill_date": bill_date,
        "total_amount": total,
        "currency": "USD",
        "category": "Food",
        "split_type": "EQUAL",
        "payer_member_id": ctx["me"]["id"],
        "split_with": [ctx["me"]["id"], ctx["friend"]["id"]],
        "period_key": period_key,
    }
    r = client.post(f"/api/groups/{ctx['group_id']}/expenses", json=payload, headers=ctx["ha"])
    assert r.status_code == 200, r.text
    return r.json()


def _add_direct(ctx, *, bill_date: str, total: float, title: str):
    r = client.post(
        "/api/quick-splits",
        json={
            "title": title,
            "total_amount": total,
            "currency": "USD",
            "bill_date": bill_date,
            "category": "Food",
            "split_type": "EQUAL",
            "payer_account_id": ctx["me_account_id"],
            "participants": [{"account_id": ctx["friend_account_id"]}],
            "silent_invite": True,
        },
        headers=ctx["ha"],
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_friend_display_sources_combine_three_months():
    ctx = _ready_pair_and_group()
    months = [
        ("2026-07", "2026-07-10", 20.0),
        ("2026-08", "2026-08-10", 30.0),
        ("2026-09", "2026-09-10", 40.0),
    ]
    for period_key, bill_date, total in months:
        _add_group_expense(ctx, period_key=period_key, bill_date=bill_date, total=total, store=f"Shop {period_key}")
        _add_direct(ctx, bill_date=bill_date, total=total, title=f"Direct {period_key}")

    bal = client.get(f"/api/friends/{ctx['friend_account_id']}/consolidated-balances", headers=ctx["ha"])
    assert bal.status_code == 200, bal.text
    rows = bal.json()["balances"]
    assert len(rows) == 1, rows
    total = rows[0]
    sources = total["sources"]
    atomic = total["atomic_sources"]

    direct_display = [s for s in sources if s["kind"] == "DIRECT"]
    group_display = [s for s in sources if s["kind"] == "GROUP"]
    assert len(direct_display) == 1, sources
    assert direct_display[0]["label"] == "Direct splits"
    assert len(group_display) == 1, sources
    assert group_display[0]["group_id"] == ctx["group_id"]

    direct_atomic = [s for s in atomic if s["kind"] == "DIRECT"]
    group_atomic = [s for s in atomic if s["kind"] == "GROUP"]
    assert len(direct_atomic) >= 3, atomic
    assert len(group_atomic) >= 3, atomic

    expected = sum(float(s["amount"]) for s in atomic)
    assert abs(float(total["balance"]) - expected) < 0.02
    display_sum = sum(float(s["amount"]) for s in sources)
    assert abs(display_sum - expected) < 0.02

    # Each equal-split month: friend owes half of group bill + half of direct.
    # 20+30+40 group → 45; 20+30+40 direct → 45; friend owes 90 to alex.
    assert abs(float(total["balance"]) - 90.0) < 0.05, total


def test_partial_payment_clears_oldest_atomic_first():
    ctx = _ready_pair_and_group()
    for period_key, bill_date, total in [
        ("2026-07", "2026-07-05", 20.0),
        ("2026-08", "2026-08-05", 20.0),
        ("2026-09", "2026-09-05", 20.0),
    ]:
        _add_direct(ctx, bill_date=bill_date, total=total, title=f"D {period_key}")

    before = client.get(f"/api/friends/{ctx['friend_account_id']}/consolidated-balances", headers=ctx["ha"]).json()["balances"][0]
    assert abs(float(before["balance"]) - 30.0) < 0.05

    paid = client.post(
        f"/api/friends/{ctx['me_account_id']}/consolidated-payments",
        json={"amount": 10.0, "currency": "USD"},
        headers=ctx["hb"],
    )
    assert paid.status_code == 200, paid.text
    allocs = paid.json().get("allocations") or []
    direct_allocs = [a for a in allocs if a.get("kind") == "DIRECT"]
    assert direct_allocs, allocs
    assert direct_allocs[0].get("month_key") == "2026-07", allocs

    after = client.get(f"/api/friends/{ctx['friend_account_id']}/consolidated-balances", headers=ctx["ha"]).json()["balances"][0]
    assert abs(float(after["balance"]) - 20.0) < 0.05, after
    direct_atomic = [s for s in after["atomic_sources"] if s["kind"] == "DIRECT"]
    by_month = {s["period_key"]: float(s["amount"]) for s in direct_atomic}
    assert abs(by_month.get("2026-07", 0.0)) < 0.02, by_month
    assert abs(by_month.get("2026-08", 0.0) - 10.0) < 0.02, by_month
    assert abs(by_month.get("2026-09", 0.0) - 10.0) < 0.02, by_month
    assert len([s for s in after["sources"] if s["kind"] == "DIRECT"]) == 1


def test_group_all_time_balances_sum_three_periods():
    ctx = _ready_pair_and_group()
    months = [
        ("2026-07", "2026-07-12", 20.0),
        ("2026-08", "2026-08-12", 30.0),
        ("2026-09", "2026-09-12", 40.0),
    ]
    for period_key, bill_date, total in months:
        _add_group_expense(ctx, period_key=period_key, bill_date=bill_date, total=total, store=f"All {period_key}")

    period_nets = {ctx["me"]["id"]: 0.0, ctx["friend"]["id"]: 0.0}
    for period_key, _, _ in months:
        rows = client.get(
            f"/api/groups/{ctx['group_id']}/periods/{period_key}/balances",
            headers=ctx["ha"],
        )
        assert rows.status_code == 200, rows.text
        for row in rows.json():
            period_nets[row["member_id"]] = period_nets.get(row["member_id"], 0.0) + float(row["net_balance"])

    overall = client.get(f"/api/groups/{ctx['group_id']}/balances", headers=ctx["ha"])
    assert overall.status_code == 200, overall.text
    overall_nets = {row["member_id"]: float(row["net_balance"]) for row in overall.json()}
    assert abs(overall_nets[ctx["me"]["id"]] - period_nets[ctx["me"]["id"]]) < 0.05
    assert abs(overall_nets[ctx["friend"]["id"]] - period_nets[ctx["friend"]["id"]]) < 0.05
    # Alex paid 90 total, half share → +45; Blake −45.
    assert abs(overall_nets[ctx["me"]["id"]] - 45.0) < 0.05
    assert abs(overall_nets[ctx["friend"]["id"]] + 45.0) < 0.05

    settlements = client.get(f"/api/groups/{ctx['group_id']}/settlements?mode=smart", headers=ctx["ha"])
    assert settlements.status_code == 200, settlements.text
    edges = settlements.json()
    assert len(edges) == 1, edges
    assert edges[0]["from_member_id"] == ctx["friend"]["id"]
    assert edges[0]["to_member_id"] == ctx["me"]["id"]
    assert abs(float(edges[0]["amount"]) - 45.0) < 0.05


if __name__ == "__main__":
    test_friend_display_sources_combine_three_months()
    print("  [PASS] friend display combines 3 months")
    test_partial_payment_clears_oldest_atomic_first()
    print("  [PASS] partial payment clears oldest month first")
    test_group_all_time_balances_sum_three_periods()
    print("  [PASS] group all-time balances sum 3 periods")
    print("OK")
