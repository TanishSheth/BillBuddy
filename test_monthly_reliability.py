from datetime import datetime, date, timezone
import json
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.database import Base
from app.main import (
    _apply_private_participant_payment, _expense_snapshot, _lock_weekly_settlement_batch,
    _membership_belongs_in_period, _period_dates, _private_participant_remaining,
    _reconcile_latest_weekly_lock, _record_expense_audit, _weekly_pair_settlement_sources,
    redirect_friends,
)
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


def test_weekly_lock_edit_reconciles_same_locked_week_for_friends():
    """Bill edits after a lock rewrite that same week on Friends — not the next week."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        one = models.Account(email="one@audit.test", password_hash="x", display_name="One")
        two = models.Account(email="two@audit.test", password_hash="x", display_name="Two")
        session.add_all([one, two]); session.flush()
        group = models.Group(name="Audit group", currency="USD", created_by_id=one.id)
        session.add(group); session.flush()
        member_one = models.GroupMember(group_id=group.id, account_id=one.id, display_name="One", role="owner")
        member_two = models.GroupMember(group_id=group.id, account_id=two.id, display_name="Two")
        session.add_all([member_one, member_two]); session.flush()
        period = models.MonthlyPeriod(group_id=group.id, period_key="2026-10")
        session.add(period); session.flush()
        expense = models.Expense(period_id=period.id, store_name="Rent", bill_date=date(2026, 10, 1), total_amount=Decimal("20.00"), currency="USD", payer_member_id=member_one.id)
        session.add(expense); session.flush()
        session.add(models.ExpenseSplit(expense_id=expense.id, member_id=member_two.id, share_amount=Decimal("20.00")))
        session.flush()
        _record_expense_audit(session, group_id=group.id, expense_id=expense.id, actor=one, event_type="CREATED", before=None, after=_expense_snapshot(session, expense))
        first = _lock_weekly_settlement_batch(session, group, period, window_ends_at=datetime(2026, 10, 8, tzinfo=timezone.utc), created_by=one)
        assert session.query(models.WeeklySettlementLeg).filter_by(batch_id=first.id).one().amount == Decimal("20.00")
        session.add(models.PairPayment(
            account_one_id=min(one.id, two.id), account_two_id=max(one.id, two.id),
            payer_account_id=two.id, recipient_account_id=one.id, amount=Decimal("5.00"), currency="USD",
                status="CONFIRMED", reported_by_account_id=two.id, allocations_json=json.dumps([{
                "kind": "GROUP", "batch_id": first.id, "amount": 5,
                "payer_member_id": member_two.id, "recipient_member_id": member_one.id,
            }]),
        ))
        session.flush()
        remaining_sources = _weekly_pair_settlement_sources(session, group, period, member_one.id, member_two.id, "USD")
        assert remaining_sources[0]["amount"] == 15.0

        before = _expense_snapshot(session, expense)
        expense.total_amount = Decimal("10.00")
        session.query(models.ExpenseSplit).filter_by(expense_id=expense.id).one().share_amount = Decimal("10.00")
        session.flush()
        _record_expense_audit(session, group_id=group.id, expense_id=expense.id, actor=one, event_type="UPDATED", before=before, after=_expense_snapshot(session, expense))
        _reconcile_latest_weekly_lock(session, group, period, actor=one)
        session.flush()

        # Same locked batch is rewritten; no second-week spillover batch needed.
        assert session.query(models.WeeklySettlementBatch).filter_by(period_id=period.id).count() == 1
        first_leg = session.query(models.WeeklySettlementLeg).filter_by(batch_id=first.id).one()
        assert first_leg.amount == Decimal("10.00")
        assert first_leg.payer_member_id == member_two.id
        assert first_leg.recipient_member_id == member_one.id
        remaining_after = _weekly_pair_settlement_sources(session, group, period, member_one.id, member_two.id, "USD")
        assert remaining_after[0]["amount"] == 5.0  # 10 owed − 5 already paid
        assert remaining_after[0]["batch_id"] == first.id
        assert session.query(models.ExpenseAuditEvent).filter_by(expense_id=expense.id).count() == 2
    finally:
        session.close()
