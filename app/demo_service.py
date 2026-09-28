"""Private template and expiring sandbox lifecycle for the public product demo."""
from __future__ import annotations

import secrets
import os
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from . import models
from .auth import create_access_token, hash_password
from .split_logic import calculate_splits

TEMPLATE_VERSION = "austin-apartment-v1"
SANDBOX_TTL_HOURS = 2
_PEOPLE = ("Alex", "Jordan", "Casey", "Morgan", "Riley")
_EXPENSES = (
    ("Costco run", 418.70, "Groceries", 4, (0, 1, 2, 3, 4)),
    ("Uber to airport", 54.00, "Travel", 0, (0, 1, 2)),
    ("Dinner at Olive Garden", 96.30, "Food", 3, (0, 2, 3)),
    ("Internet bill", 80.00, "Utilities", 1, (0, 1, 2, 4)),
    ("Movie tickets", 32.00, "Fun", 2, (2, 3)),
    ("Airbnb cleaning fee", 125.00, "Travel", 4, (0, 1, 2, 3, 4)),
)


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
) -> None:
    participants = [members[index] for index in participant_indexes]
    shares = calculate_splits("EQUAL", total, [member.id for member in participants])
    expense = models.Expense(
        period_id=period.id,
        store_name=store,
        bill_date=date.today(),
        total_amount=Decimal(str(total)),
        currency="USD",
        category=category,
        split_type="EQUAL",
        payer_member_id=members[payer_index].id,
    )
    db.add(expense)
    db.flush()
    for member_id, amount in shares.items():
        db.add(models.ExpenseSplit(expense_id=expense.id, member_id=member_id, share_amount=amount, split_value=1))


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
    period = models.MonthlyPeriod(group_id=group.id, period_key=date.today().strftime("%Y-%m"), status="ACTIVE")
    db.add(period)
    db.flush()
    for store, total, category, payer_index, participant_indexes in _EXPENSES:
        _add_expense(
            db, period, members, store=store, total=total, category=category,
            payer_index=payer_index, participant_indexes=participant_indexes,
        )
    template = models.DemoTemplate(version=TEMPLATE_VERSION, group_id=group.id)
    db.add(template)
    db.commit()
    db.refresh(template)
    return template


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
    for source_period in source_group.periods:
        period = models.MonthlyPeriod(group_id=group.id, period_key=source_period.period_key, status="ACTIVE")
        db.add(period)
        db.flush()
        period_map[source_period.id] = period
    for source_period in source_group.periods:
        for source_expense in source_period.expenses:
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
    db.add_all([
        models.PersonalExpense(account_id=owner.id, store_name="Whole Foods", bill_date=date.today(), total_amount=Decimal("64.20"), currency="USD", category="Groceries", notes="Demo personal expense"),
        models.PersonalExpense(account_id=owner.id, store_name="Metro pass", bill_date=date.today(), total_amount=Decimal("28.00"), currency="USD", category="Travel", notes="Demo personal expense"),
    ])
    if len(cloned_accounts) > 1:
        friend = cloned_accounts[1]
        one, two = sorted((owner.id, friend.id))
        db.add(models.FriendConnection(account_one_id=one, account_two_id=two, status="ACTIVE"))
        split = models.QuickSplit(
            title="Coffee and pastries", total_amount=Decimal("18.40"), currency="USD", bill_date=date.today(),
            category="Food", split_type="EQUAL", payer_share_amount=Decimal("9.20"),
            created_by_account_id=owner.id, payer_account_id=owner.id, status="OPEN",
        )
        db.add(split)
        db.flush()
        db.add(models.QuickSplitParticipant(
            quick_split_id=split.id, email=friend.email, account_id=friend.id,
            display_name=friend.display_name, share_amount=Decimal("9.20"), split_value=1,
        ))
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
        db.query(models.Notification).filter(models.Notification.account_id.in_(account_ids)).delete(synchronize_session=False)
        db.query(models.FriendConnection).filter(
            (models.FriendConnection.account_one_id.in_(account_ids)) | (models.FriendConnection.account_two_id.in_(account_ids))
        ).delete(synchronize_session=False)
        db.query(models.PersonalExpense).filter(models.PersonalExpense.account_id.in_(account_ids)).delete(synchronize_session=False)
        db.query(models.AccountVendor).filter(models.AccountVendor.account_id.in_(account_ids)).delete(synchronize_session=False)
        db.query(models.AccountSettings).filter(models.AccountSettings.account_id.in_(account_ids)).delete(synchronize_session=False)
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
