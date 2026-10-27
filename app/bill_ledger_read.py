"""Phase 4 dual-read / compare: rebuild comparable snapshots from unified bills.

Modes (BILL_LEDGER_READ):
  legacy  — serve legacy only (default)
  compare — serve legacy; log diffs vs unified bill snapshot
  unified — prefer unified snapshot rebuilt into API-shaped dicts where possible
            (falls back to legacy if unmapped)
"""
from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Optional

from sqlalchemy.orm import Session, selectinload

from . import models
from .config import BILL_LEDGER_READ

logger = logging.getLogger("sharesquad.bill_ledger_read")
TWOPLACES = Decimal("0.01")


def read_mode() -> str:
    mode = (BILL_LEDGER_READ or "legacy").strip().lower()
    if mode not in {"legacy", "compare", "unified"}:
        return "legacy"
    return mode


def _q(value: Any) -> float:
    return float(Decimal(str(value or 0)).quantize(TWOPLACES, rounding=ROUND_HALF_EVEN))


def _map_bill(db: Session, kind: str, legacy_id: int) -> Optional[models.Bill]:
    row = (
        db.query(models.BillIdMap)
        .filter_by(legacy_kind=kind, legacy_id=int(legacy_id))
        .first()
    )
    if not row:
        return None
    return (
        db.query(models.Bill)
        .options(
            selectinload(models.Bill.participants),
            selectinload(models.Bill.payers),
        )
        .filter_by(id=row.bill_id)
        .first()
    )


def _diff(left: dict, right: dict) -> dict:
    keys = sorted(set(left) | set(right))
    out = {}
    for key in keys:
        if left.get(key) != right.get(key):
            out[key] = {"legacy": left.get(key), "unified": right.get(key)}
    return out


def log_compare(kind: str, legacy_id: int, legacy_snap: dict, unified_snap: Optional[dict]) -> None:
    if unified_snap is None:
        logger.warning("bill_ledger_compare_unmapped kind=%s legacy_id=%s", kind, legacy_id)
        return
    delta = _diff(legacy_snap, unified_snap)
    if delta:
        logger.warning(
            "bill_ledger_compare_diff kind=%s legacy_id=%s diff=%s",
            kind,
            legacy_id,
            delta,
        )
    else:
        logger.debug("bill_ledger_compare_ok kind=%s legacy_id=%s", kind, legacy_id)


# --- GROUP expense ---

def legacy_expense_snapshot(db: Session, expense: models.Expense) -> dict:
    payer_member = (
        db.get(models.GroupMember, expense.payer_member_id)
        if expense.payer_member_id
        else None
    )
    payers = []
    if expense.payers:
        for row in expense.payers:
            member = db.get(models.GroupMember, row.member_id) if row.member_id else None
            payers.append(
                {
                    "account_id": member.account_id if member else None,
                    "amount_paid": _q(row.amount_paid),
                }
            )
    else:
        payers.append(
            {
                "account_id": payer_member.account_id if payer_member else None,
                "amount_paid": _q(expense.total_amount),
            }
        )
    shares = []
    for split in expense.splits or []:
        member = db.get(models.GroupMember, split.member_id) if split.member_id else None
        shares.append(
            {
                "account_id": member.account_id if member else None,
                "share_amount": _q(split.share_amount),
            }
        )
    payers.sort(key=lambda r: (r["account_id"] is None, r["account_id"] or 0))
    shares.sort(key=lambda r: (r["account_id"] is None, r["account_id"] or 0))
    return {
        "scope": "GROUP",
        "title": expense.store_name,
        "bill_date": str(expense.bill_date),
        "total_amount": _q(expense.total_amount),
        "currency": (expense.currency or "USD").upper()[:3],
        "category": expense.category or "General",
        "split_type": expense.split_type or "EQUAL",
        "period_id": expense.period_id,
        "payers": payers,
        "shares": shares,
    }


def unified_expense_snapshot(db: Session, expense_id: int) -> Optional[dict]:
    bill = _map_bill(db, "EXPENSE", expense_id)
    if not bill or bill.scope != "GROUP":
        return None
    payers = sorted(
        (
            {"account_id": p.account_id, "amount_paid": _q(p.amount_paid)}
            for p in (bill.payers or [])
        ),
        key=lambda r: (r["account_id"] is None, r["account_id"] or 0),
    )
    shares = sorted(
        (
            {"account_id": p.account_id, "share_amount": _q(p.share_amount)}
            for p in (bill.participants or [])
            if p.account_id is not None
        ),
        key=lambda r: (r["account_id"] is None, r["account_id"] or 0),
    )
    return {
        "scope": "GROUP",
        "title": bill.title,
        "bill_date": str(bill.bill_date),
        "total_amount": _q(bill.total_amount),
        "currency": (bill.currency or "USD").upper()[:3],
        "category": bill.category or "General",
        "split_type": bill.split_type or "EQUAL",
        "period_id": bill.period_id,
        "payers": payers,
        "shares": shares,
    }


def compare_expense(db: Session, expense: models.Expense) -> None:
    if read_mode() != "compare":
        return
    log_compare(
        "EXPENSE",
        expense.id,
        legacy_expense_snapshot(db, expense),
        unified_expense_snapshot(db, expense.id),
    )


def compare_expenses(db: Session, expenses: list[models.Expense]) -> None:
    if read_mode() != "compare":
        return
    for expense in expenses:
        compare_expense(db, expense)


# --- PAIR quick split ---

def legacy_quick_split_snapshot(split: models.QuickSplit) -> dict:
    participants = sorted(
        (
            {
                "account_id": p.account_id,
                "email": (p.email or "").lower() if p.email and not p.email.endswith("@invite.local") else None,
                "share_amount": _q(p.share_amount),
                "paid_amount": _q(p.paid_amount),
            }
            for p in (split.participants or [])
        ),
        key=lambda r: (
            r["account_id"] is None,
            r["account_id"] or 0,
            r["email"] or "",
        ),
    )
    return {
        "scope": "PAIR",
        "title": split.title,
        "bill_date": str(split.bill_date),
        "total_amount": _q(split.total_amount),
        "currency": (split.currency or "USD").upper()[:3],
        "category": split.category or "General",
        "split_type": split.split_type or "EQUAL",
        "payer_account_id": split.payer_account_id,
        "payer_share_amount": _q(split.payer_share_amount),
        "payer_paid_amount": _q(split.payer_paid_amount) if split.payer_paid_amount is not None else None,
        "participants": participants,
    }


def unified_quick_split_snapshot(db: Session, split_id: int) -> Optional[dict]:
    bill = _map_bill(db, "QUICK_SPLIT", split_id)
    if not bill or bill.scope != "PAIR":
        return None
    # Primary payer = largest amount_paid (or sole payer).
    payers = list(bill.payers or [])
    payer_account_id = None
    payer_paid = None
    if payers:
        top = max(payers, key=lambda p: Decimal(str(p.amount_paid or 0)))
        payer_account_id = top.account_id
        payer_paid = _q(top.amount_paid)
        # Legacy stores None when the payer covered the full bill.
        if abs(payer_paid - _q(bill.total_amount)) < 0.005:
            payer_paid = None
    payer_share = None
    participants = []
    for p in bill.participants or []:
        if payer_account_id and p.account_id == payer_account_id:
            payer_share = _q(p.share_amount)
            continue
        participants.append(
            {
                "account_id": p.account_id,
                "email": (p.email or "").lower() if p.email and not str(p.email).endswith("@invite.local") else None,
                "share_amount": _q(p.share_amount),
                "paid_amount": _q(p.paid_amount),
            }
        )
    participants.sort(
        key=lambda r: (r["account_id"] is None, r["account_id"] or 0, r["email"] or "")
    )
    return {
        "scope": "PAIR",
        "title": bill.title,
        "bill_date": str(bill.bill_date),
        "total_amount": _q(bill.total_amount),
        "currency": (bill.currency or "USD").upper()[:3],
        "category": bill.category or "General",
        "split_type": bill.split_type or "EQUAL",
        "payer_account_id": payer_account_id,
        "payer_share_amount": payer_share if payer_share is not None else 0.0,
        "payer_paid_amount": payer_paid,
        "participants": participants,
    }


def compare_quick_split(db: Session, split: models.QuickSplit) -> None:
    if read_mode() != "compare":
        return
    log_compare(
        "QUICK_SPLIT",
        split.id,
        legacy_quick_split_snapshot(split),
        unified_quick_split_snapshot(db, split.id),
    )


def compare_quick_splits(db: Session, splits: list[models.QuickSplit]) -> None:
    if read_mode() != "compare":
        return
    for split in splits:
        compare_quick_split(db, split)


# --- PERSONAL ---

def legacy_personal_snapshot(expense: models.PersonalExpense) -> dict:
    return {
        "scope": "PERSONAL",
        "title": expense.store_name,
        "bill_date": str(expense.bill_date),
        "total_amount": _q(expense.total_amount),
        "currency": (expense.currency or "USD").upper()[:3],
        "category": expense.category or "General",
        "account_id": expense.account_id,
        "notes": expense.notes or None,
    }


def unified_personal_snapshot(db: Session, expense_id: int) -> Optional[dict]:
    bill = _map_bill(db, "PERSONAL", expense_id)
    if not bill or bill.scope != "PERSONAL":
        return None
    return {
        "scope": "PERSONAL",
        "title": bill.title,
        "bill_date": str(bill.bill_date),
        "total_amount": _q(bill.total_amount),
        "currency": (bill.currency or "USD").upper()[:3],
        "category": bill.category or "General",
        "account_id": bill.created_by_account_id,
        "notes": bill.notes or None,
    }


def compare_personal(db: Session, expense: models.PersonalExpense) -> None:
    if read_mode() != "compare":
        return
    log_compare(
        "PERSONAL",
        expense.id,
        legacy_personal_snapshot(expense),
        unified_personal_snapshot(db, expense.id),
    )


def compare_personals(db: Session, expenses: list[models.PersonalExpense]) -> None:
    if read_mode() != "compare":
        return
    for expense in expenses:
        compare_personal(db, expense)


def compare_stats(db: Session, *, limit: int = 200) -> dict:
    """Diagnostics for /api internal or scripts — sample recent legacy rows."""
    mode = read_mode()
    stats = {
        "mode": mode,
        "expense_checked": 0,
        "expense_unmapped": 0,
        "expense_diff": 0,
        "quick_checked": 0,
        "quick_unmapped": 0,
        "quick_diff": 0,
        "personal_checked": 0,
        "personal_unmapped": 0,
        "personal_diff": 0,
    }
    expenses = db.query(models.Expense).order_by(models.Expense.id.desc()).limit(limit).all()
    for expense in expenses:
        stats["expense_checked"] += 1
        left = legacy_expense_snapshot(db, expense)
        right = unified_expense_snapshot(db, expense.id)
        if right is None:
            stats["expense_unmapped"] += 1
        elif _diff(left, right):
            stats["expense_diff"] += 1
    splits = db.query(models.QuickSplit).order_by(models.QuickSplit.id.desc()).limit(limit).all()
    for split in splits:
        stats["quick_checked"] += 1
        left = legacy_quick_split_snapshot(split)
        right = unified_quick_split_snapshot(db, split.id)
        if right is None:
            stats["quick_unmapped"] += 1
        elif _diff(left, right):
            stats["quick_diff"] += 1
    personals = (
        db.query(models.PersonalExpense)
        .order_by(models.PersonalExpense.id.desc())
        .limit(limit)
        .all()
    )
    for expense in personals:
        stats["personal_checked"] += 1
        left = legacy_personal_snapshot(expense)
        right = unified_personal_snapshot(db, expense.id)
        if right is None:
            stats["personal_unmapped"] += 1
        elif _diff(left, right):
            stats["personal_diff"] += 1
    return stats
