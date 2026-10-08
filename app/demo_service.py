"""Private template and expiring sandbox lifecycle for the public product demo.

Sandbox data mirrors the live product rules: expenses belong to the month they
were saved, older receipt dates show as late bills, Friends is where payments
are recorded, and months stay editable (digests only — no auto-SETTLED lock).

The fixture spans multiple calendar months so Overall group balances and the
combined Friends breakdown (one Direct + one line per group) are easy to try.
"""
from __future__ import annotations

import calendar
import json
import os
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from . import models
from .auth import create_access_token, hash_password
from .config import APP_VERSION
from .split_logic import calculate_splits

# Every released application version gets a fresh private template. New demos
# and "Reset demo" therefore always show the current product fixture, while an
# in-progress visitor sandbox is never silently overwritten.
# Keep this under the demo_templates.version column width (80).
TEMPLATE_VERSION = f"ss-demo-{APP_VERSION}"
SANDBOX_TTL_HOURS = 2
_PEOPLE = ("Alex", "Jordan", "Casey", "Morgan", "Riley")


def _month_key(value: date) -> str:
    return value.strftime("%Y-%m")


def _shift_month(value: date, delta: int) -> date:
    """Return the first day of the month `delta` months away from `value`."""
    year = value.year + ((value.month - 1 + delta) // 12)
    month = (value.month - 1 + delta) % 12 + 1
    return date(year, month, 1)


def _clamp_day(year: int, month: int, day: int) -> date:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last))


def _new_account(db: Session, *, email: str, display_name: str) -> models.Account:
    account = models.Account(
        email=email,
        display_name=display_name,
        password_hash=hash_password(secrets.token_urlsafe(32)),
    )
    db.add(account)
    db.flush()
    db.add(models.AccountSettings(account_id=account.id, preferred_currency="USD"))
    return account


def _add_expense(
    db: Session,
    period: models.MonthlyPeriod,
    members: list[models.GroupMember],
    *,
    store: str,
    total: float,
    category: str,
    payer_index: int,
    participant_indexes: tuple[int, ...],
    bill_date: date,
    currency: str = "USD",
    fx_rate_to_group: float = 1.0,
    created_at: datetime | None = None,
) -> models.Expense:
    participants = [members[index] for index in participant_indexes]
    shares = calculate_splits("EQUAL", total, [member.id for member in participants])
    expense = models.Expense(
        period_id=period.id,
        store_name=store,
        bill_date=bill_date,
        total_amount=Decimal(str(total)),
        currency=currency,
        category=category,
        split_type="EQUAL",
        fx_rate_to_group=Decimal(str(fx_rate_to_group)),
        payer_member_id=members[payer_index].id,
        created_at=created_at or datetime.utcnow(),
    )
    db.add(expense)
    db.flush()
    for member_id, amount in shares.items():
        db.add(models.ExpenseSplit(expense_id=expense.id, member_id=member_id, share_amount=amount, split_value=1))
    return expense


def _ensure_period(db: Session, group_id: int, period_key: str) -> models.MonthlyPeriod:
    period = db.query(models.MonthlyPeriod).filter_by(group_id=group_id, period_key=period_key).first()
    if period:
        return period
    period = models.MonthlyPeriod(group_id=group_id, period_key=period_key, status="ACTIVE")
    db.add(period)
    db.flush()
    return period


def _seed_apartment_template(db: Session, accounts: list[models.Account], members: list[models.GroupMember], group: models.Group) -> None:
    """Main roommate group with expenses saved across three calendar months."""
    today = date.today()
    current = _shift_month(today, 0)
    prev = _shift_month(today, -1)
    older = _shift_month(today, -2)
    now = datetime.utcnow()

    period_current = _ensure_period(db, group.id, _month_key(current))
    period_prev = _ensure_period(db, group.id, _month_key(prev))
    period_older = _ensure_period(db, group.id, _month_key(older))

    current_expenses = (
        ("Costco run", 418.70, "Groceries", 4, (0, 1, 2, 3, 4), _clamp_day(current.year, current.month, min(today.day, 28)), "USD", 1.0),
        ("Uber to airport", 54.00, "Travel", 0, (0, 1, 2), _clamp_day(prev.year, prev.month, 12), "USD", 1.0),  # late bill
        ("Dinner at Olive Garden", 96.30, "Food", 3, (0, 2, 3), _clamp_day(current.year, current.month, max(1, today.day - 2)), "USD", 1.0),
        ("Internet bill", 80.00, "Utilities", 1, (0, 1, 2, 4), _clamp_day(current.year, current.month, max(1, today.day - 5)), "USD", 1.0),
        ("Movie tickets", 32.00, "Fun", 2, (2, 3), _clamp_day(current.year, current.month, max(1, today.day - 1)), "USD", 1.0),
        ("Delhi street food", 2400.00, "Food", 0, (0, 1), _clamp_day(older.year, older.month, 18), "INR", 0.012),
    )
    prev_expenses = (
        ("Rent share top-up", 240.00, "Home", 0, (0, 1, 2, 3, 4), _clamp_day(prev.year, prev.month, 3), "USD", 1.0),
        ("HEB groceries", 186.40, "Groceries", 1, (0, 1, 2), _clamp_day(prev.year, prev.month, 14), "USD", 1.0),
        ("Electric bill", 112.00, "Utilities", 2, (0, 1, 2, 3, 4), _clamp_day(prev.year, prev.month, 20), "USD", 1.0),
        ("Thai takeout", 68.50, "Food", 0, (0, 1), _clamp_day(prev.year, prev.month, 22), "USD", 1.0),
    )
    older_expenses = (
        ("IKEA shelf", 149.00, "Home", 0, (0, 1), _clamp_day(older.year, older.month, 8), "USD", 1.0),
        ("Gas fill-up", 52.00, "Travel", 1, (0, 1, 2), _clamp_day(older.year, older.month, 16), "USD", 1.0),
        ("Streaming bundle", 45.00, "Fun", 3, (0, 1, 2, 3, 4), _clamp_day(older.year, older.month, 25), "USD", 1.0),
    )

    for index, (store, total, category, payer_index, participant_indexes, bill_date, currency, fx) in enumerate(current_expenses):
        _add_expense(
            db, period_current, members, store=store, total=total, category=category,
            payer_index=payer_index, participant_indexes=participant_indexes, bill_date=bill_date,
            currency=currency, fx_rate_to_group=fx,
            created_at=now - timedelta(hours=len(current_expenses) - index),
        )
    for index, row in enumerate(prev_expenses):
        store, total, category, payer_index, participant_indexes, bill_date, currency, fx = row
        _add_expense(
            db, period_prev, members, store=store, total=total, category=category,
            payer_index=payer_index, participant_indexes=participant_indexes, bill_date=bill_date,
            currency=currency, fx_rate_to_group=fx,
            created_at=now - timedelta(days=20, hours=len(prev_expenses) - index),
        )
    for index, row in enumerate(older_expenses):
        store, total, category, payer_index, participant_indexes, bill_date, currency, fx = row
        _add_expense(
            db, period_older, members, store=store, total=total, category=category,
            payer_index=payer_index, participant_indexes=participant_indexes, bill_date=bill_date,
            currency=currency, fx_rate_to_group=fx,
            created_at=now - timedelta(days=50, hours=len(older_expenses) - index),
        )
    db.add(models.GroupActivity(
        group_id=group.id, actor_account_id=accounts[0].id,
        event_type="expense_added", summary="Alex added Costco run",
        entity_type="expense",
    ))


def ensure_template(db: Session) -> models.DemoTemplate:
    template = db.query(models.DemoTemplate).filter_by(version=TEMPLATE_VERSION).first()
    if template:
        return template

    token = secrets.token_hex(8)
    accounts = [
        _new_account(
            db,
            email=f"template-{token}-{index}@internal.invalid",
            display_name=name,
        )
        for index, name in enumerate(_PEOPLE)
    ]
    group = models.Group(name="Austin Apartment · Internal Template", currency="USD", created_by_id=accounts[0].id)
    db.add(group)
    db.flush()
    members = []
    for index, account in enumerate(accounts):
        member = models.GroupMember(
            group_id=group.id,
            account_id=account.id,
            display_name=account.display_name,
            role="owner" if index == 0 else "member",
            is_active=True,
        )
        db.add(member)
        members.append(member)
    db.flush()
    _seed_apartment_template(db, accounts, members, group)
    template = models.DemoTemplate(version=TEMPLATE_VERSION, group_id=group.id)
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


def _add_direct_split(
    db: Session,
    *,
    owner: models.Account,
    friend: models.Account,
    title: str,
    total: Decimal,
    bill_date: date,
    category: str = "Food",
    paid_amount: Decimal = Decimal("0.00"),
    payment_status: str = "PENDING",
    created_at: datetime | None = None,
) -> models.QuickSplit:
    half = (total / Decimal("2")).quantize(Decimal("0.01"))
    split = models.QuickSplit(
        title=title,
        total_amount=total,
        currency="USD",
        bill_date=bill_date,
        category=category,
        split_type="EQUAL",
        payer_share_amount=half,
        created_by_account_id=owner.id,
        payer_account_id=owner.id,
        status="OPEN",
        created_at=created_at or datetime.utcnow(),
    )
    db.add(split)
    db.flush()
    db.add(models.QuickSplitParticipant(
        quick_split_id=split.id,
        email=friend.email,
        account_id=friend.id,
        display_name=friend.display_name,
        share_amount=half,
        split_value=1,
        paid_amount=paid_amount,
        payment_status=payment_status,
        payment_marked_at=datetime.utcnow() - timedelta(days=1) if paid_amount > 0 else None,
    ))
    return split


def _seed_sandbox_extras(
    db: Session,
    *,
    owner: models.Account,
    cloned_accounts: list[models.Account],
    primary_group: models.Group,
    member_map: dict[int, models.GroupMember],
    period_map: dict[int, models.MonthlyPeriod],
    source_group: models.Group,
) -> None:
    """Direct splits, second group, and partial payments for the demo visitor."""
    today = date.today()
    prev = _shift_month(today, -1)
    older = _shift_month(today, -2)
    jordan = cloned_accounts[1]
    casey = cloned_accounts[2] if len(cloned_accounts) > 2 else None

    # Friend everyone with the demo owner so the Friends list is useful.
    for other in cloned_accounts[1:]:
        one, two = sorted((owner.id, other.id))
        existing = db.query(models.FriendConnection).filter_by(account_one_id=one, account_two_id=two).first()
        if not existing:
            db.add(models.FriendConnection(account_one_id=one, account_two_id=two, status="ACTIVE"))

    # --- Direct private bills across months (Jordan) ---
    coffee = _add_direct_split(
        db, owner=owner, friend=jordan, title="Coffee and pastries",
        total=Decimal("18.40"), bill_date=today, category="Food",
        paid_amount=Decimal("5.00"), payment_status="PARTIAL",
        created_at=datetime.utcnow() - timedelta(days=1),
    )
    walmart = _add_direct_split(
        db, owner=owner, friend=jordan, title="Walmart run",
        total=Decimal("75.02"),
        bill_date=_clamp_day(prev.year, prev.month, 26),
        category="Groceries",
        created_at=datetime.utcnow() - timedelta(days=25),
    )
    lyft = _add_direct_split(
        db, owner=owner, friend=jordan, title="Lyft from airport",
        total=Decimal("42.00"),
        bill_date=_clamp_day(older.year, older.month, 9),
        category="Travel",
        paid_amount=Decimal("10.00"), payment_status="PARTIAL",
        created_at=datetime.utcnow() - timedelta(days=55),
    )
    if casey:
        _add_direct_split(
            db, owner=owner, friend=casey, title="Concert tickets",
            total=Decimal("96.00"),
            bill_date=_clamp_day(prev.year, prev.month, 11),
            category="Fun",
            created_at=datetime.utcnow() - timedelta(days=28),
        )

    one, two = sorted((owner.id, jordan.id))
    # Partial direct payments — leave remaining Direct balance across months.
    db.add(models.PairPayment(
        account_one_id=one, account_two_id=two,
        payer_account_id=jordan.id, recipient_account_id=owner.id,
        amount=Decimal("5.00"), currency="USD", status="CONFIRMED",
        reported_by_account_id=owner.id,
        allocations_json=json.dumps([{
            "kind": "DIRECT", "amount": 5.0, "month_key": _month_key(today),
            "payment_kind": "PARTIAL", "quick_split_id": coffee.id,
        }]),
        confirmed_at=datetime.utcnow() - timedelta(days=1),
        created_at=datetime.utcnow() - timedelta(days=1),
    ))
    db.add(models.PairPayment(
        account_one_id=one, account_two_id=two,
        payer_account_id=jordan.id, recipient_account_id=owner.id,
        amount=Decimal("10.00"), currency="USD", status="CONFIRMED",
        reported_by_account_id=owner.id,
        allocations_json=json.dumps([{
            "kind": "DIRECT", "amount": 10.0, "month_key": _month_key(older),
            "payment_kind": "PARTIAL", "quick_split_id": lyft.id,
        }]),
        confirmed_at=datetime.utcnow() - timedelta(days=40),
        created_at=datetime.utcnow() - timedelta(days=40),
    ))
    # Unpaid Walmart remains fully open in the previous month.
    _ = walmart

    # --- Second shared group (weekend trip) with two months of bills ---
    trip = models.Group(name="Weekend Trip · Demo", currency="USD", created_by_id=owner.id)
    db.add(trip)
    db.flush()
    trip_people = [owner, jordan] + ([casey] if casey else [])
    trip_members: list[models.GroupMember] = []
    for index, account in enumerate(trip_people):
        member = models.GroupMember(
            group_id=trip.id,
            account_id=account.id,
            display_name=account.display_name.replace(" · Demo", ""),
            role="owner" if index == 0 else "member",
            is_active=True,
        )
        db.add(member)
        trip_members.append(member)
    db.flush()
    trip_current = _ensure_period(db, trip.id, _month_key(today))
    trip_prev = _ensure_period(db, trip.id, _month_key(prev))
    _add_expense(
        db, trip_current, trip_members,
        store="Airbnb deposit", total=210.00, category="Travel",
        payer_index=0, participant_indexes=tuple(range(len(trip_members))),
        bill_date=_clamp_day(today.year, today.month, max(1, today.day - 3)),
        created_at=datetime.utcnow() - timedelta(days=2),
    )
    _add_expense(
        db, trip_current, trip_members,
        store="Trail snacks", total=36.00, category="Food",
        payer_index=1, participant_indexes=(0, 1),
        bill_date=_clamp_day(today.year, today.month, max(1, today.day - 1)),
        created_at=datetime.utcnow() - timedelta(days=1),
    )
    _add_expense(
        db, trip_prev, trip_members,
        store="Gas for road trip", total=64.00, category="Travel",
        payer_index=0, participant_indexes=tuple(range(len(trip_members))),
        bill_date=_clamp_day(prev.year, prev.month, 18),
        created_at=datetime.utcnow() - timedelta(days=22),
    )
    db.add(models.GroupActivity(
        group_id=trip.id, actor_account_id=owner.id,
        event_type="expense_added", summary="Alex added Airbnb deposit",
        entity_type="expense",
    ))

    # --- Partial group payment on the oldest apartment period (Jordan → Alex) ---
    source_older = next(
        (period for period in source_group.periods if period.period_key == _month_key(older)),
        None,
    )
    if source_older and source_older.id in period_map:
        sandbox_older = period_map[source_older.id]
        # Alex is index 0, Jordan index 1 in the apartment clone order.
        source_members = sorted(source_group.members, key=lambda member: member.id)
        if len(source_members) >= 2:
            alex_member = member_map.get(source_members[0].id)
            jordan_member = member_map.get(source_members[1].id)
            if alex_member and jordan_member:
                cutoff = (
                    db.query(models.Expense)
                    .filter_by(period_id=sandbox_older.id)
                    .order_by(models.Expense.id.desc())
                    .first()
                )
                db.add(models.GroupPayment(
                    period_id=sandbox_older.id,
                    payer_member_id=jordan_member.id,
                    recipient_member_id=alex_member.id,
                    amount=Decimal("20.00"),
                    currency="USD",
                    status="CONFIRMED",
                    expense_cutoff_id=cutoff.id if cutoff else None,
                    cleared_amount=Decimal("20.00"),
                    marked_paid_at=datetime.utcnow() - timedelta(days=35),
                    confirmed_at=datetime.utcnow() - timedelta(days=35),
                ))
                db.add(models.PairPayment(
                    account_one_id=one, account_two_id=two,
                    payer_account_id=jordan.id, recipient_account_id=owner.id,
                    amount=Decimal("20.00"), currency="USD", status="CONFIRMED",
                    reported_by_account_id=owner.id,
                    allocations_json=json.dumps([{
                        "kind": "GROUP",
                        "amount": 20.0,
                        "month_key": sandbox_older.period_key,
                        "payment_kind": "PARTIAL",
                        "period_id": sandbox_older.id,
                        "group_id": primary_group.id,
                        "payer_member_id": jordan_member.id,
                        "recipient_member_id": alex_member.id,
                    }]),
                    confirmed_at=datetime.utcnow() - timedelta(days=35),
                    created_at=datetime.utcnow() - timedelta(days=35),
                ))

    db.add(models.Notification(
        account_id=owner.id, group_id=primary_group.id,
        message="Demo tip: open Friends → Jordan to see one combined Direct total + one line per group across months. Group Balances shows Overall.",
        link="/friends.html",
    ))


def _copy_template_group(db: Session, template: models.DemoTemplate, sandbox_id: str) -> tuple[models.Account, models.Group, list[models.Account]]:
    source_group = db.query(models.Group).get(template.group_id)
    if not source_group:
        raise RuntimeError("Demo template group is unavailable")
    source_members = sorted(source_group.members, key=lambda member: member.id)
    cloned_accounts: list[models.Account] = []
    account_map: dict[int, models.Account] = {}
    for index, source_member in enumerate(source_members):
        account = _new_account(
            db,
            email=f"sandbox-{sandbox_id[:12]}-{index}@demo.invalid",
            display_name=f"{source_member.display_name}{' · Demo' if index == 0 else ''}",
        )
        cloned_accounts.append(account)
        account_map[source_member.account_id] = account
    owner = cloned_accounts[0]
    group = models.Group(name="Austin Apartment · Your Sandbox", currency=source_group.currency, created_by_id=owner.id)
    db.add(group)
    db.flush()
    member_map: dict[int, models.GroupMember] = {}
    for index, source_member in enumerate(source_members):
        member = models.GroupMember(
            group_id=group.id,
            account_id=account_map[source_member.account_id].id,
            display_name=cloned_accounts[index].display_name.replace(" · Demo", ""),
            role=source_member.role,
            is_active=True,
        )
        db.add(member)
        member_map[source_member.id] = member
    db.flush()
    period_map: dict[int, models.MonthlyPeriod] = {}
    for source_period in sorted(source_group.periods, key=lambda row: row.period_key):
        # Always ACTIVE — demo matches live product (no auto month-end SETTLED).
        period = models.MonthlyPeriod(group_id=group.id, period_key=source_period.period_key, status="ACTIVE")
        db.add(period)
        db.flush()
        period_map[source_period.id] = period
    for source_period in sorted(source_group.periods, key=lambda row: row.period_key):
        for source_expense in sorted(source_period.expenses, key=lambda row: row.created_at or datetime.min):
            expense = models.Expense(
                period_id=period_map[source_period.id].id,
                store_name=source_expense.store_name,
                bill_date=source_expense.bill_date,
                total_amount=source_expense.total_amount,
                currency=source_expense.currency,
                category=source_expense.category,
                split_type=source_expense.split_type,
                fx_rate_to_group=source_expense.fx_rate_to_group,
                payer_member_id=member_map[source_expense.payer_member_id].id,
                created_at=source_expense.created_at or datetime.utcnow(),
            )
            db.add(expense)
            db.flush()
            for source_split in source_expense.splits:
                db.add(models.ExpenseSplit(
                    expense_id=expense.id,
                    member_id=member_map[source_split.member_id].id,
                    share_amount=source_split.share_amount,
                    split_value=source_split.split_value,
                ))
    db.add(models.GroupActivity(
        group_id=group.id, actor_account_id=owner.id,
        event_type="expense_added", summary="Alex added Costco run",
        entity_type="expense",
    ))
    db.add_all([
        models.PersonalExpense(account_id=owner.id, store_name="Whole Foods", bill_date=date.today(), total_amount=Decimal("64.20"), currency="USD", category="Groceries", notes="Demo personal expense"),
        models.PersonalExpense(account_id=owner.id, store_name="Metro pass", bill_date=date.today() - timedelta(days=12), total_amount=Decimal("28.00"), currency="USD", category="Travel", notes="Saved this month; older receipt date"),
    ])
    db.add(models.Notification(
        account_id=owner.id, group_id=group.id,
        message="Welcome to your ShareSquad demo — same features as a real account, with sample data across several months.",
        link=f"/group.html?group={group.id}&tab=balances",
    ))
    _seed_sandbox_extras(
        db,
        owner=owner,
        cloned_accounts=cloned_accounts,
        primary_group=group,
        member_map=member_map,
        period_map=period_map,
        source_group=source_group,
    )
    return owner, group, cloned_accounts


def create_sandbox(db: Session) -> dict:
    template = ensure_template(db)
    sandbox_id = secrets.token_urlsafe(24)
    owner, group, accounts = _copy_template_group(db, template, sandbox_id)
    sandbox = models.DemoSandbox(
        id=sandbox_id,
        template_id=template.id,
        account_id=owner.id,
        group_id=group.id,
        expires_at=datetime.utcnow() + timedelta(hours=SANDBOX_TTL_HOURS),
    )
    db.add(sandbox)
    db.flush()
    for account in accounts:
        db.add(models.DemoSandboxAccount(sandbox_id=sandbox.id, account_id=account.id))
    db.commit()
    return {
        "sandbox_id": sandbox.id,
        "access_token": create_access_token(owner.id),
        "group_id": group.id,
        "expires_at": sandbox.expires_at,
    }


def sandbox_for_account(db: Session, account_id: int) -> models.DemoSandbox | None:
    return db.query(models.DemoSandbox).filter_by(account_id=account_id).first()


def _unlink(path: str | None) -> None:
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def purge_sandbox(db: Session, sandbox: models.DemoSandbox) -> None:
    """Remove only records mapped to this sandbox; never touch the template."""
    account_ids = [row.account_id for row in db.query(models.DemoSandboxAccount).filter_by(sandbox_id=sandbox.id)]
    # A visitor can create additional groups after the initial clone. They are
    # owned by a sandbox account and must be removed with the sandbox too.
    group_ids = {sandbox.group_id}
    if account_ids:
        group_ids.update(
            row.id for row in db.query(models.Group.id).filter(models.Group.created_by_id.in_(account_ids)).all()
        )
    period_ids = [row.id for row in db.query(models.MonthlyPeriod).filter(models.MonthlyPeriod.group_id.in_(group_ids))]
    expense_ids = [row.id for row in db.query(models.Expense).filter(models.Expense.period_id.in_(period_ids)).all()] if period_ids else []
    for path, in db.query(models.Expense.image_path).filter(models.Expense.id.in_(expense_ids)).all() if expense_ids else []:
        _unlink(path)
    for path, in db.query(models.PersonalExpense.image_path).filter(models.PersonalExpense.account_id.in_(account_ids)).all() if account_ids else []:
        _unlink(path)
    for path, in db.query(models.PendingUpload.file_path).filter(models.PendingUpload.account_id.in_(account_ids)).all() if account_ids else []:
        _unlink(path)
    for path, in db.query(models.MonthlyPeriod.pdf_path).filter(models.MonthlyPeriod.id.in_(period_ids)).all() if period_ids else []:
        _unlink(path)

    if expense_ids:
        db.query(models.ExpenseComment).filter(models.ExpenseComment.expense_id.in_(expense_ids)).delete(synchronize_session=False)
        db.query(models.ExpensePayer).filter(models.ExpensePayer.expense_id.in_(expense_ids)).delete(synchronize_session=False)
        db.query(models.ExpenseSplit).filter(models.ExpenseSplit.expense_id.in_(expense_ids)).delete(synchronize_session=False)
        db.query(models.Expense).filter(models.Expense.id.in_(expense_ids)).delete(synchronize_session=False)
    if period_ids:
        db.query(models.GroupPayment).filter(models.GroupPayment.period_id.in_(period_ids)).delete(synchronize_session=False)
        db.query(models.MonthlyPeriod).filter(models.MonthlyPeriod.id.in_(period_ids)).delete(synchronize_session=False)
    db.query(models.GroupActivity).filter(models.GroupActivity.group_id.in_(group_ids)).delete(synchronize_session=False)
    db.query(models.GroupMemberSettings).filter(models.GroupMemberSettings.group_id.in_(group_ids)).delete(synchronize_session=False)
    db.query(models.GroupInvite).filter(models.GroupInvite.group_id.in_(group_ids)).delete(synchronize_session=False)
    db.query(models.PendingUpload).filter(models.PendingUpload.group_id.in_(group_ids)).delete(synchronize_session=False)
    db.query(models.GroupMember).filter(models.GroupMember.group_id.in_(group_ids)).delete(synchronize_session=False)
    db.query(models.Group).filter(models.Group.id.in_(group_ids)).delete(synchronize_session=False)
    if account_ids:
        quick_ids = [row.id for row in db.query(models.QuickSplit).filter(models.QuickSplit.created_by_account_id.in_(account_ids)).all()]
        if quick_ids:
            db.query(models.QuickSplitParticipant).filter(models.QuickSplitParticipant.quick_split_id.in_(quick_ids)).delete(synchronize_session=False)
            db.query(models.QuickSplit).filter(models.QuickSplit.id.in_(quick_ids)).delete(synchronize_session=False)
        db.query(models.PairPayment).filter(
            (models.PairPayment.account_one_id.in_(account_ids)) | (models.PairPayment.account_two_id.in_(account_ids))
        ).delete(synchronize_session=False)
        db.query(models.FriendMonthlySettlement).filter(
            (models.FriendMonthlySettlement.account_one_id.in_(account_ids))
            | (models.FriendMonthlySettlement.account_two_id.in_(account_ids))
        ).delete(synchronize_session=False)
        db.query(models.FriendInvite).filter(models.FriendInvite.inviter_account_id.in_(account_ids)).delete(synchronize_session=False)
        db.query(models.Notification).filter(models.Notification.account_id.in_(account_ids)).delete(synchronize_session=False)
        db.query(models.FriendConnection).filter(
            (models.FriendConnection.account_one_id.in_(account_ids)) | (models.FriendConnection.account_two_id.in_(account_ids))
        ).delete(synchronize_session=False)
        db.query(models.PersonalExpense).filter(models.PersonalExpense.account_id.in_(account_ids)).delete(synchronize_session=False)
        db.query(models.AccountVendor).filter(models.AccountVendor.account_id.in_(account_ids)).delete(synchronize_session=False)
        db.query(models.AccountSettings).filter(models.AccountSettings.account_id.in_(account_ids)).delete(synchronize_session=False)
        db.query(models.AccountMonthlyDigest).filter(models.AccountMonthlyDigest.account_id.in_(account_ids)).delete(synchronize_session=False)
    db.query(models.DemoSandboxAccount).filter_by(sandbox_id=sandbox.id).delete(synchronize_session=False)
    db.delete(sandbox)
    if account_ids:
        db.query(models.Account).filter(models.Account.id.in_(account_ids)).delete(synchronize_session=False)


def reset_sandbox(db: Session, account_id: int) -> dict:
    sandbox = sandbox_for_account(db, account_id)
    if not sandbox:
        raise ValueError("This account is not a demo sandbox")
    purge_sandbox(db, sandbox)
    db.commit()
    # SQLite may reuse primary keys after deletion. Clear stale ORM identities
    # before building the replacement workspace in the same request session.
    db.expunge_all()
    return create_sandbox(db)


def purge_expired_sandboxes(db: Session) -> int:
    expired = db.query(models.DemoSandbox).filter(models.DemoSandbox.expires_at <= datetime.utcnow()).all()
    for sandbox in expired:
        purge_sandbox(db, sandbox)
    if expired:
        db.commit()
    return len(expired)
