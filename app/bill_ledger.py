"""Unified bills ledger write path (Phases 3 + 6).

Phase 3 dual-write: legacy first, then unified mirror (fail-closed).
Phase 6 cutover: WRITE=unified marks bills as primary; LEGACY_MIRROR keeps the
deprecated legacy write cushion until Phase 7. FREEZE blocks money mutations.

Lock order (dual / mirrored unified): parent FOR SHARE (sorted ids) → legacy →
unified. SQLite skips FOR SHARE; Postgres uses ascending id locks.
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import models
from .bill_backfill import (
    backfill_expense,
    backfill_pair_payment_allocations,
    backfill_personal,
    backfill_quick_split,
)
from . import config as _config

logger = logging.getLogger("sharesquad.bill_ledger")

_WRITE_MODES = frozenset({"legacy", "dual", "unified"})


def write_mode() -> str:
    mode = (_config.BILL_LEDGER_WRITE or "legacy").strip().lower()
    return mode if mode in _WRITE_MODES else "legacy"


def dual_write_enabled() -> bool:
    """Phase 7: legacy dual-write helpers are no-ops (bill_ops is SoT)."""
    return False


def unified_primary() -> bool:
    return True


def ledger_frozen() -> bool:
    return bool(_config.BILL_LEDGER_FREEZE)


def legacy_mirror_enabled() -> bool:
    """Whether legacy expense/quick_split/personal tables still accept writes."""
    mode = write_mode()
    if mode == "legacy":
        return True
    if mode == "dual":
        return True
    # unified primary: optional deprecated mirror (soak cushion)
    return bool(_config.BILL_LEDGER_LEGACY_MIRROR)


def cutover_status() -> dict:
    return {
        "write_mode": write_mode(),
        "unified_primary": unified_primary(),
        "dual_write": dual_write_enabled(),
        "legacy_mirror": legacy_mirror_enabled(),
        "frozen": ledger_frozen(),
    }


def _require_dual() -> bool:
    return dual_write_enabled()


def lock_parents(
    db: Session,
    *,
    account_ids: Sequence[int] = (),
    group_ids: Sequence[int] = (),
    period_ids: Sequence[int] = (),
) -> None:
    """Acquire parent row locks in ascending id order before child unified DML."""
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    accounts = sorted({int(i) for i in account_ids if i})
    groups = sorted({int(i) for i in group_ids if i})
    periods = sorted({int(i) for i in period_ids if i})
    if accounts:
        db.execute(
            text("SELECT id FROM accounts WHERE id = ANY(:ids) ORDER BY id FOR SHARE"),
            {"ids": accounts},
        )
    if groups:
        db.execute(
            text("SELECT id FROM groups WHERE id = ANY(:ids) ORDER BY id FOR SHARE"),
            {"ids": groups},
        )
    if periods:
        db.execute(
            text("SELECT id FROM monthly_periods WHERE id = ANY(:ids) ORDER BY id FOR SHARE"),
            {"ids": periods},
        )


def _map_row(db: Session, kind: str, legacy_id: int) -> Optional[models.BillIdMap]:
    return (
        db.query(models.BillIdMap)
        .filter_by(legacy_kind=kind, legacy_id=int(legacy_id))
        .first()
    )


def _delete_unified_bill(db: Session, bill_id: int) -> None:
    # Explicit map delete (SQLite may not enforce ON DELETE CASCADE without PRAGMA).
    db.query(models.BillIdMap).filter_by(bill_id=bill_id).delete(synchronize_session=False)
    bill = db.get(models.Bill, bill_id)
    if bill:
        db.delete(bill)


def _replace_mapped_bill(db: Session, kind: str, legacy_id: int) -> None:
    mapped = _map_row(db, kind, legacy_id)
    if mapped:
        _delete_unified_bill(db, mapped.bill_id)
        db.flush()


def _account_ids_for_expense(db: Session, expense: models.Expense) -> list[int]:
    ids: set[int] = set()
    payer = db.query(models.GroupMember).get(expense.payer_member_id)
    if payer:
        ids.add(payer.account_id)
    for row in expense.payers or []:
        member = db.query(models.GroupMember).get(row.member_id)
        if member:
            ids.add(member.account_id)
    for split in expense.splits or []:
        member = db.query(models.GroupMember).get(split.member_id)
        if member:
            ids.add(member.account_id)
    return sorted(ids)


def sync_expense(db: Session, expense: models.Expense) -> None:
    """Upsert unified GROUP bill from a flushed legacy expense (fail-closed)."""
    if not _require_dual():
        return
    period = expense.period or db.query(models.MonthlyPeriod).get(expense.period_id)
    if not period:
        raise RuntimeError(f"dual_write expense {expense.id}: missing period")
    lock_parents(
        db,
        account_ids=_account_ids_for_expense(db, expense),
        group_ids=[period.group_id],
        period_ids=[period.id],
    )
    _replace_mapped_bill(db, "EXPENSE", expense.id)
    result = backfill_expense(db, expense)
    if result is None:
        raise RuntimeError(f"dual_write expense {expense.id}: backfill rejected (quarantine)")
    db.flush()


def delete_expense_mirror(db: Session, expense_id: int) -> None:
    if not _require_dual():
        return
    mapped = _map_row(db, "EXPENSE", expense_id)
    if not mapped:
        return
    bill = db.query(models.Bill).get(mapped.bill_id)
    account_ids: list[int] = []
    group_ids: list[int] = []
    period_ids: list[int] = []
    if bill:
        account_ids = [p.account_id for p in (bill.payers or []) if p.account_id]
        account_ids += [p.account_id for p in (bill.participants or []) if p.account_id]
        if bill.group_id:
            group_ids = [bill.group_id]
        if bill.period_id:
            period_ids = [bill.period_id]
    lock_parents(db, account_ids=account_ids, group_ids=group_ids, period_ids=period_ids)
    _delete_unified_bill(db, mapped.bill_id)
    db.flush()


def sync_quick_split(db: Session, split: models.QuickSplit) -> None:
    if not _require_dual():
        return
    account_ids = sorted(
        {
            split.payer_account_id,
            *(p.account_id for p in (split.participants or []) if p.account_id),
            *(
                [split.created_by_account_id]
                if split.created_by_account_id
                else []
            ),
        }
    )
    # Decision 14: resolve invite emails that already have accounts before mirror.
    for participant in split.participants or []:
        if participant.account_id or not participant.email:
            continue
        existing = (
            db.query(models.Account)
            .filter(models.Account.email == participant.email.lower())
            .first()
        )
        if existing:
            # Refuse duplicate if account already on bill as another row.
            clash = next(
                (
                    p
                    for p in split.participants
                    if p.id != participant.id and p.account_id == existing.id
                ),
                None,
            )
            if clash:
                raise RuntimeError(
                    f"dual_write quick_split {split.id}: email {participant.email} "
                    "already attached as account on this bill"
                )
            participant.account_id = existing.id
    lock_parents(db, account_ids=account_ids)
    _replace_mapped_bill(db, "QUICK_SPLIT", split.id)
    result = backfill_quick_split(db, split)
    if result is None:
        raise RuntimeError(f"dual_write quick_split {split.id}: backfill rejected")
    db.flush()


def delete_quick_split_mirror(db: Session, split_id: int) -> None:
    if not _require_dual():
        return
    mapped = _map_row(db, "QUICK_SPLIT", split_id)
    if not mapped:
        return
    bill = db.query(models.Bill).get(mapped.bill_id)
    account_ids: list[int] = []
    if bill:
        account_ids = [p.account_id for p in (bill.payers or []) if p.account_id]
        account_ids += [p.account_id for p in (bill.participants or []) if p.account_id]
    lock_parents(db, account_ids=account_ids)
    _delete_unified_bill(db, mapped.bill_id)
    db.flush()


def sync_personal(db: Session, expense: models.PersonalExpense) -> None:
    if not _require_dual():
        return
    lock_parents(db, account_ids=[expense.account_id])
    _replace_mapped_bill(db, "PERSONAL", expense.id)
    result = backfill_personal(db, expense)
    if result is None:
        raise RuntimeError(f"dual_write personal {expense.id}: backfill rejected")
    db.flush()


def delete_personal_mirror(db: Session, expense_id: int) -> None:
    if not _require_dual():
        return
    mapped = _map_row(db, "PERSONAL", expense_id)
    if not mapped:
        return
    bill = db.query(models.Bill).get(mapped.bill_id)
    account_ids = [bill.created_by_account_id] if bill else []
    lock_parents(db, account_ids=account_ids)
    _delete_unified_bill(db, mapped.bill_id)
    db.flush()


def sync_pair_payment(db: Session, payment: models.PairPayment) -> None:
    """Replace payment_allocations from allocations_json (legacy JSON still written first)."""
    if not _require_dual():
        return
    lock_parents(
        db,
        account_ids=[
            payment.account_one_id,
            payment.account_two_id,
            payment.payer_account_id,
            payment.recipient_account_id,
        ],
    )
    db.query(models.PaymentAllocation).filter_by(pair_payment_id=payment.id).delete(
        synchronize_session=False
    )
    db.flush()
    backfill_pair_payment_allocations(db, payment)
    db.flush()


def ensure_expense_mapped(db: Session, expense: models.Expense) -> None:
    """Lazy on-demand backfill for UPDATE/DELETE when map missing (P2→P3 gap)."""
    if not _require_dual():
        return
    if _map_row(db, "EXPENSE", expense.id):
        return
    sync_expense(db, expense)


def ensure_quick_split_mapped(db: Session, split: models.QuickSplit) -> None:
    if not _require_dual():
        return
    if _map_row(db, "QUICK_SPLIT", split.id):
        return
    sync_quick_split(db, split)


def ensure_personal_mapped(db: Session, expense: models.PersonalExpense) -> None:
    if not _require_dual():
        return
    if _map_row(db, "PERSONAL", expense.id):
        return
    sync_personal(db, expense)
