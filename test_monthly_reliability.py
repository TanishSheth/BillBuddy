from datetime import datetime
from decimal import Decimal

from app import models
from app.main import _apply_private_participant_payment, _membership_belongs_in_period, _period_dates, _private_participant_remaining, redirect_friends
from starlette.requests import Request


def test_period_dates_use_the_full_calendar_month():
    assert _period_dates("2026-02")[0].isoformat() == "2026-02-01"
    assert _period_dates("2026-02")[1].isoformat() == "2026-02-28"


def test_member_added_after_month_end_is_excluded():
    membership = models.GroupMember(
        group_id=1, account_id=1, display_name="New member",
        joined_at=datetime(2026, 10, 1, 0, 1),
    )
    assert not _membership_belongs_in_period(membership, _period_dates("2026-09")[1])


def test_member_who_left_before_month_end_is_excluded():
    membership = models.GroupMember(
        group_id=1, account_id=1, display_name="Former member",
        joined_at=datetime(2026, 9, 1), left_at=datetime(2026, 9, 20),
    )
    assert not _membership_belongs_in_period(membership, _period_dates("2026-09")[1])


def test_member_present_for_the_full_month_is_eligible():
    membership = models.GroupMember(
        group_id=1, account_id=1, display_name="Member",
        joined_at=datetime(2026, 8, 20), left_at=datetime(2026, 10, 2),
    )
    assert _membership_belongs_in_period(membership, _period_dates("2026-09")[1])


def test_partial_private_payment_keeps_the_bill_open_until_the_full_share_is_paid():
    participant = models.QuickSplitParticipant(account_id=2, email="friend@example.com", share_amount=Decimal("20.00"))
    split = models.QuickSplit(total_amount=Decimal("20.00"), payer_share_amount=Decimal("0.00"), payer_account_id=1, participants=[participant])
    assert _apply_private_participant_payment(split, participant, Decimal("5.00")) == Decimal("5.00")
    assert participant.payment_status == "PARTIAL"
    assert _private_participant_remaining(split, participant) == Decimal("15.00")
    assert _apply_private_participant_payment(split, participant, Decimal("15.00")) == Decimal("15.00")
    assert participant.payment_status == "CONFIRMED"
    assert _private_participant_remaining(split, participant) == Decimal("0.00")


def test_legacy_html_friend_url_redirects_to_the_clean_route_with_its_query():
    request = Request({"type": "http", "method": "GET", "path": "/friends.html", "query_string": b"friend=7", "headers": []})
    response = redirect_friends(request)
    assert response.status_code == 308
    assert response.headers["location"] == "/friends?friend=7"
