"""Temporary 5-person sample group with mixed expenses for local testing.

Usage:
  python seed_sample_group.py          # create (or refresh) sample data
  python seed_sample_group.py --purge  # delete sample accounts/group only
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from decimal import Decimal

from app.auth import hash_password
from app.database import SessionLocal, Base, engine
from app import models
from app.split_logic import calculate_splits, compute_balances, graph_constrained_settlements

SAMPLE_TAG = "[TEMP] Sample House"
SAMPLE_PASSWORD = "SampleDemo1!"
PEOPLE = [
    ("sample.tanish@sharesquad.demo", "Tanish"),
    ("sample.sanju@sharesquad.demo", "Sanju"),
    ("sample.kuku@sharesquad.demo", "Kuku"),
    ("sample.kp@sharesquad.demo", "KP"),
    ("sample.bk@sharesquad.demo", "Bk"),
]


def _ensure_account(db, email: str, name: str) -> models.Account:
    row = db.query(models.Account).filter_by(email=email).first()
    if row:
        row.display_name = name
        row.password_hash = hash_password(SAMPLE_PASSWORD)
        row.deletion_requested_at = None
        row.deletion_purge_after = None
        row.deletion_completed_at = None
        return row
    row = models.Account(email=email, display_name=name, password_hash=hash_password(SAMPLE_PASSWORD))
    db.add(row)
    db.flush()
    db.add(models.AccountSettings(account_id=row.id, preferred_currency="USD"))
    return row


def _friend(db, a: models.Account, b: models.Account) -> None:
    one, two = sorted((a.id, b.id))
    row = db.query(models.FriendConnection).filter_by(account_one_id=one, account_two_id=two).first()
    if row is None:
        db.add(models.FriendConnection(account_one_id=one, account_two_id=two, status="ACTIVE"))
    else:
        row.status = "ACTIVE"


def _add_expense(db, period, payer: models.GroupMember, participants: list[models.GroupMember], *, store: str, total: float, bill_date: date, category: str = "General") -> None:
    member_ids = [m.id for m in participants]
    shares = calculate_splits("EQUAL", total, member_ids)
    expense = models.Expense(
        period_id=period.id,
        store_name=store,
        bill_date=bill_date,
        total_amount=Decimal(str(total)),
        currency="USD",
        category=category,
        split_type="EQUAL",
        payer_member_id=payer.id,
    )
    db.add(expense)
    db.flush()
    for mid, amount in shares.items():
        db.add(models.ExpenseSplit(expense_id=expense.id, member_id=mid, share_amount=amount, split_value=1))


def _add_quick_split(db, payer: models.Account, other: models.Account, *, title: str, total: float, bill_date: date) -> None:
    half = (Decimal(str(total)) / 2).quantize(Decimal("0.01"))
    other_share = Decimal(str(total)) - half
    split = models.QuickSplit(
        title=title,
        total_amount=Decimal(str(total)),
        currency="USD",
        bill_date=bill_date,
        category="General",
        split_type="EQUAL",
        payer_share_amount=half,
        created_by_account_id=payer.id,
        payer_account_id=payer.id,
        status="OPEN",
    )
    db.add(split)
    db.flush()
    db.add(models.QuickSplitParticipant(
        quick_split_id=split.id,
        email=other.email,
        account_id=other.id,
        display_name=other.display_name,
        share_amount=other_share,
        split_value=1,
    ))


def purge(db) -> None:
    emails = [email for email, _ in PEOPLE]
    accounts = db.query(models.Account).filter(models.Account.email.in_(emails)).all()
    account_ids = [a.id for a in accounts]
    groups = db.query(models.Group).filter(models.Group.name == SAMPLE_TAG).all()
    for group in groups:
        db.delete(group)
    db.flush()
    if account_ids:
        db.query(models.QuickSplitParticipant).filter(models.QuickSplitParticipant.account_id.in_(account_ids)).delete(synchronize_session=False)
        db.query(models.QuickSplit).filter(
            (models.QuickSplit.payer_account_id.in_(account_ids))
            | (models.QuickSplit.created_by_account_id.in_(account_ids))
        ).delete(synchronize_session=False)
        db.query(models.FriendConnection).filter(
            (models.FriendConnection.account_one_id.in_(account_ids))
            | (models.FriendConnection.account_two_id.in_(account_ids))
        ).delete(synchronize_session=False)
        db.query(models.AccountSettings).filter(models.AccountSettings.account_id.in_(account_ids)).delete(synchronize_session=False)
        db.query(models.AccountVendor).filter(models.AccountVendor.account_id.in_(account_ids)).delete(synchronize_session=False)
        db.query(models.PersonalExpense).filter(models.PersonalExpense.account_id.in_(account_ids)).delete(synchronize_session=False)
        for account in accounts:
            db.delete(account)
    db.commit()
    print("Purged temporary sample accounts and group.")


def seed(db) -> None:
    Base.metadata.create_all(bind=engine)
    # Refresh cleanly each run so amounts stay predictable.
    purge(db)

    accounts = [_ensure_account(db, email, name) for email, name in PEOPLE]
    db.flush()
    for i, a in enumerate(accounts):
        for b in accounts[i + 1:]:
            _friend(db, a, b)

    owner = accounts[0]
    group = models.Group(name=SAMPLE_TAG, currency="USD", created_by_id=owner.id)
    db.add(group)
    db.flush()
    members = []
    for i, account in enumerate(accounts):
        member = models.GroupMember(
            group_id=group.id,
            account_id=account.id,
            display_name=account.display_name,
            role="owner" if i == 0 else "member",
            is_active=True,
        )
        db.add(member)
        members.append(member)
    db.flush()

    period_key = date.today().strftime("%Y-%m")
    period = models.MonthlyPeriod(group_id=group.id, period_key=period_key, status="ACTIVE")
    db.add(period)
    db.flush()

    tanish, sanju, kuku, kp, bk = members
    today = date.today()

    # Mixed payers / participant sets — similar shape to a real house month.
    _add_expense(db, period, bk, members, store="Costco run", total=418.70, bill_date=today, category="Groceries")
    _add_expense(db, period, tanish, [tanish, sanju, kuku], store="Uber to airport", total=54.00, bill_date=today, category="Travel")
    _add_expense(db, period, kp, [tanish, kp, kuku], store="Dinner at Olive Garden", total=96.30, bill_date=today, category="Food")
    _add_expense(db, period, sanju, [sanju, bk, tanish, kuku], store="Internet bill", total=80.00, bill_date=today, category="Utilities")
    _add_expense(db, period, kuku, [kuku, kp], store="Movie tickets", total=32.00, bill_date=today, category="Fun")
    _add_expense(db, period, bk, [tanish, sanju, kuku, kp, bk], store="AirBNB cleaning fee", total=125.00, bill_date=today, category="Travel")

    # Direct friend bills (only Tanish↔KP), so Friends adds group + direct.
    _add_quick_split(db, accounts[0], accounts[3], title="Coffee + pastry", total=18.40, bill_date=today)
    _add_quick_split(db, accounts[3], accounts[0], title="Parking downtown", total=12.00, bill_date=today)

    db.commit()

    # Print expected Who pays whom for the seeded period.
    member_dicts = [{"id": m.id, "name": m.display_name} for m in members]
    expenses = db.query(models.Expense).filter_by(period_id=period.id).all()
    expense_dicts = []
    for expense in expenses:
        expense_dicts.append({
            "payer_id": expense.payer_member_id,
            "total_amount": float(expense.total_amount),
            "payments": [{"payer_id": expense.payer_member_id, "amount": float(expense.total_amount)}],
            "splits": [{"user_id": s.member_id, "share_amount": float(s.share_amount)} for s in expense.splits],
        })
    balances = compute_balances(member_dicts, expense_dicts)
    settlements = graph_constrained_settlements(member_dicts, expense_dicts, balances)

    print()
    print(f"Created {SAMPLE_TAG} (group id={group.id}, period={period_key})")
    print(f"Open: http://localhost:8001/group.html?group={group.id}&tab=balances")
    print(f"Friends: http://localhost:8001/index.html?view=friends")
    print()
    print("Sign in as any sample user (same password for all):")
    print(f"  password: {SAMPLE_PASSWORD}")
    for email, name in PEOPLE:
        print(f"  {name:8}  {email}")
    print()
    print("Expected Who pays whom (expense-aware):")
    if not settlements:
        print("  (none)")
    for row in settlements:
        print(f"  {row['from_name']} -> pays {row['to_name']}  ${row['amount']:.2f}")
    print()
    print("Net balances:")
    for mid, bal in sorted(balances.items(), key=lambda item: item[1]["name"]):
        print(f"  {bal['name']:8}  net {bal['net_balance']:+.2f}")
    print()
    print("Purge later with:  python seed_sample_group.py --purge")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--purge", action="store_true", help="Delete temporary sample data only")
    args = parser.parse_args()
    db = SessionLocal()
    try:
        if args.purge:
            purge(db)
        else:
            seed(db)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
