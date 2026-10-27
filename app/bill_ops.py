"""Phase 7 primary ledger ops: bills are SoT (no legacy expense/quick_split/personal writes).

API ids are bill.id. Keep HTTP Out shapes via to_*_out helpers.
"""
from __future__ import annotations

import secrets
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Optional, Sequence

from sqlalchemy.orm import Session, selectinload

from . import models
from .bill_ledger import lock_parents

TWOPLACES = Decimal("0.01")


def _q(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(TWOPLACES, rounding=ROUND_HALF_EVEN)


def _currency(raw: Optional[str], default: str = "USD") -> str:
    text = (raw or "").strip().upper()
    return (text or default)[:3]


def _invite_code() -> str:
    return secrets.token_urlsafe(12)


# --- PERSONAL -----------------------------------------------------------------

def create_personal(
    db: Session,
    *,
    account_id: int,
    store_name: str,
    bill_date: date,
    total_amount: Decimal,
    currency: str = "USD",
    category: str = "General",
    notes: Optional[str] = None,
    image_path: Optional[str] = None,
) -> models.Bill:
    lock_parents(db, account_ids=[account_id])
    total = _q(total_amount)
    bill = models.Bill(
        scope="PERSONAL",
        created_by_account_id=account_id,
        title=store_name.strip(),
        bill_date=bill_date,
        total_amount=total,
        currency=_currency(currency),
        category=category or "General",
        split_type="EQUAL",
        notes=notes,
        image_path=image_path,
    )
    db.add(bill)
    db.flush()
    db.add(models.BillPayer(bill_id=bill.id, account_id=account_id, amount_paid=total))
    db.add(
        models.BillParticipant(
            bill_id=bill.id,
            account_id=account_id,
            share_amount=total,
            paid_amount=Decimal("0.00"),
        )
    )
    db.flush()
    return bill


def get_personal(db: Session, bill_id: int, account_id: int) -> Optional[models.Bill]:
    return (
        db.query(models.Bill)
        .options(selectinload(models.Bill.payers), selectinload(models.Bill.participants))
        .filter_by(id=bill_id, scope="PERSONAL", created_by_account_id=account_id)
        .first()
    )


def list_personal(
    db: Session, account_id: int, *, start: date, end: date
) -> list[models.Bill]:
    return (
        db.query(models.Bill)
        .options(selectinload(models.Bill.payers), selectinload(models.Bill.participants))
        .filter(
            models.Bill.scope == "PERSONAL",
            models.Bill.created_by_account_id == account_id,
            models.Bill.bill_date >= start,
            models.Bill.bill_date <= end,
        )
        .order_by(models.Bill.bill_date.desc(), models.Bill.id.desc())
        .all()
    )


def update_personal(
    db: Session,
    bill: models.Bill,
    *,
    store_name: str,
    bill_date: date,
    total_amount: Decimal,
    currency: str,
    category: str,
    notes: Optional[str],
) -> models.Bill:
    lock_parents(db, account_ids=[bill.created_by_account_id])
    total = _q(total_amount)
    bill.title = store_name.strip()
    bill.bill_date = bill_date
    bill.total_amount = total
    bill.currency = _currency(currency, bill.currency or "USD")
    bill.category = category or bill.category or "General"
    bill.notes = notes
    db.query(models.BillPayer).filter_by(bill_id=bill.id).delete(synchronize_session=False)
    db.query(models.BillParticipant).filter_by(bill_id=bill.id).delete(synchronize_session=False)
    db.flush()
    db.add(models.BillPayer(bill_id=bill.id, account_id=bill.created_by_account_id, amount_paid=total))
    db.add(
        models.BillParticipant(
            bill_id=bill.id,
            account_id=bill.created_by_account_id,
            share_amount=total,
            paid_amount=Decimal("0.00"),
        )
    )
    db.flush()
    return bill


def delete_personal(db: Session, bill: models.Bill) -> None:
    lock_parents(db, account_ids=[bill.created_by_account_id])
    db.delete(bill)
    db.flush()


def personal_to_out(bill: models.Bill) -> dict:
    return {
        "id": bill.id,
        "account_id": bill.created_by_account_id,
        "store_name": bill.title,
        "bill_date": bill.bill_date,
        "total_amount": float(_q(bill.total_amount)),
        "currency": bill.currency or "USD",
        "category": bill.category or "General",
        "notes": bill.notes,
        "image_path": bill.image_path,
        "created_at": bill.created_at or datetime.utcnow(),
    }


# --- PAIR ---------------------------------------------------------------------

def create_pair(
    db: Session,
    *,
    created_by_account_id: int,
    payer_account_id: int,
    title: str,
    total_amount: Decimal,
    currency: str,
    bill_date: date,
    category: str,
    split_type: str,
    payer_share_amount: Decimal,
    payer_paid_amount: Optional[Decimal],
    participants: Sequence[dict],
    status: str = "OPEN",
) -> models.Bill:
    account_ids = {created_by_account_id, payer_account_id}
    for p in participants:
        if p.get("account_id"):
            account_ids.add(int(p["account_id"]))
    lock_parents(db, account_ids=sorted(account_ids))
    total = _q(total_amount)
    paid = _q(payer_paid_amount) if payer_paid_amount is not None else total
    bill = models.Bill(
        scope="PAIR",
        created_by_account_id=created_by_account_id,
        title=title.strip(),
        bill_date=bill_date,
        total_amount=total,
        currency=_currency(currency),
        category=category or "General",
        split_type=(split_type or "EQUAL").upper(),
        invite_code=_invite_code(),
        status=status,
    )
    db.add(bill)
    db.flush()
    db.add(models.BillPayer(bill_id=bill.id, account_id=payer_account_id, amount_paid=paid))
    db.add(
        models.BillParticipant(
            bill_id=bill.id,
            account_id=payer_account_id,
            share_amount=_q(payer_share_amount),
            paid_amount=Decimal("0.00"),
        )
    )
    for p in participants:
        email = (p.get("email") or "").strip().lower() or None
        account_id = p.get("account_id")
        if account_id is None and email:
            existing = db.query(models.Account).filter(models.Account.email == email).first()
            if existing:
                already = any(
                    x.get("account_id") == existing.id for x in participants
                ) or existing.id == payer_account_id
                if already and existing.id != account_id:
                    # Decision 14: attach rather than duplicate invite row
                    pass
                account_id = existing.id
                email = existing.email
        db.add(
            models.BillParticipant(
                bill_id=bill.id,
                account_id=account_id,
                share_amount=_q(p.get("share_amount")),
                paid_amount=_q(p.get("paid_amount")),
                split_value=p.get("split_value"),
                email=email,
                display_name=p.get("display_name"),
                invite_token=p.get("invite_token") or _invite_code(),
                payment_status=p.get("payment_status") or "PENDING",
            )
        )
    db.flush()
    return bill


def get_pair(db: Session, bill_id: int) -> Optional[models.Bill]:
    return (
        db.query(models.Bill)
        .options(selectinload(models.Bill.payers), selectinload(models.Bill.participants))
        .filter_by(id=bill_id, scope="PAIR")
        .first()
    )


def delete_pair(db: Session, bill: models.Bill) -> None:
    ids = {bill.created_by_account_id}
    ids |= {p.account_id for p in (bill.payers or []) if p.account_id}
    ids |= {p.account_id for p in (bill.participants or []) if p.account_id}
    lock_parents(db, account_ids=sorted(ids))
    db.delete(bill)
    db.flush()


def pair_payer_account_id(bill: models.Bill) -> int:
    payers = list(bill.payers or [])
    if not payers:
        return bill.created_by_account_id
    return max(payers, key=lambda p: _q(p.amount_paid)).account_id


def pair_payer_share(bill: models.Bill) -> Decimal:
    payer_id = pair_payer_account_id(bill)
    for p in bill.participants or []:
        if p.account_id == payer_id:
            return _q(p.share_amount)
    return Decimal("0.00")


def pair_payer_paid(bill: models.Bill) -> Optional[Decimal]:
    payer_id = pair_payer_account_id(bill)
    for p in bill.payers or []:
        if p.account_id == payer_id:
            return _q(p.amount_paid)
    return None


# --- GROUP --------------------------------------------------------------------

def create_group_bill(
    db: Session,
    *,
    group_id: int,
    period_id: int,
    created_by_account_id: int,
    title: str,
    bill_date: date,
    total_amount: Decimal,
    currency: str,
    category: str,
    split_type: str,
    payers: Sequence[tuple[int, Decimal]],  # account_id, amount_paid
    shares: Sequence[tuple[int, Decimal, Optional[Decimal]]],  # account_id, share, split_value
    fx_rate_to_group: Optional[Decimal] = None,
    image_path: Optional[str] = None,
    recurring_template_id: Optional[int] = None,
) -> models.Bill:
    account_ids = sorted({created_by_account_id, *(a for a, _ in payers), *(a for a, _, _ in shares)})
    lock_parents(db, account_ids=account_ids, group_ids=[group_id], period_ids=[period_id])
    total = _q(total_amount)
    bill = models.Bill(
        scope="GROUP",
        group_id=group_id,
        period_id=period_id,
        created_by_account_id=created_by_account_id,
        title=title.strip(),
        bill_date=bill_date,
        total_amount=total,
        currency=_currency(currency),
        category=category or "General",
        split_type=(split_type or "EQUAL").upper(),
        fx_rate_to_group=fx_rate_to_group,
        image_path=image_path,
        recurring_template_id=recurring_template_id,
    )
    db.add(bill)
    db.flush()
    for account_id, amount_paid in payers:
        db.add(models.BillPayer(bill_id=bill.id, account_id=account_id, amount_paid=_q(amount_paid)))
    for account_id, share_amount, split_value in shares:
        db.add(
            models.BillParticipant(
                bill_id=bill.id,
                account_id=account_id,
                share_amount=_q(share_amount),
                paid_amount=Decimal("0.00"),
                split_value=split_value,
            )
        )
    db.flush()
    return bill


def get_group_bill(db: Session, bill_id: int, group_id: int) -> Optional[models.Bill]:
    return (
        db.query(models.Bill)
        .options(selectinload(models.Bill.payers), selectinload(models.Bill.participants))
        .filter_by(id=bill_id, scope="GROUP", group_id=group_id)
        .first()
    )


def list_group_bills(db: Session, period_id: int) -> list[models.Bill]:
    return (
        db.query(models.Bill)
        .options(selectinload(models.Bill.payers), selectinload(models.Bill.participants))
        .filter_by(scope="GROUP", period_id=period_id)
        .order_by(models.Bill.bill_date.desc(), models.Bill.id.desc())
        .all()
    )


def delete_group_bill(db: Session, bill: models.Bill) -> None:
    account_ids = {bill.created_by_account_id}
    account_ids |= {p.account_id for p in (bill.payers or []) if p.account_id}
    account_ids |= {p.account_id for p in (bill.participants or []) if p.account_id}
    lock_parents(
        db,
        account_ids=sorted(account_ids),
        group_ids=[bill.group_id] if bill.group_id else [],
        period_ids=[bill.period_id] if bill.period_id else [],
    )
    db.delete(bill)
    db.flush()


def replace_group_bill_money(
    db: Session,
    bill: models.Bill,
    *,
    title: str,
    bill_date: date,
    total_amount: Decimal,
    currency: str,
    category: str,
    split_type: str,
    payers: Sequence[tuple[int, Decimal]],
    shares: Sequence[tuple[int, Decimal, Optional[Decimal]]],
    fx_rate_to_group: Optional[Decimal] = None,
) -> models.Bill:
    """Update GROUP bill header + replace payers/participants (Phase 7 edit)."""
    account_ids = sorted(
        {
            bill.created_by_account_id,
            *(a for a, _ in payers),
            *(a for a, _, _ in shares),
            *(p.account_id for p in (bill.payers or []) if p.account_id),
            *(p.account_id for p in (bill.participants or []) if p.account_id),
        }
    )
    lock_parents(
        db,
        account_ids=account_ids,
        group_ids=[bill.group_id] if bill.group_id else [],
        period_ids=[bill.period_id] if bill.period_id else [],
    )
    bill.title = title.strip()
    bill.bill_date = bill_date
    bill.total_amount = _q(total_amount)
    bill.currency = _currency(currency)
    bill.category = category or "General"
    bill.split_type = (split_type or "EQUAL").upper()
    if fx_rate_to_group is not None:
        bill.fx_rate_to_group = fx_rate_to_group
    for row in list(bill.payers or []):
        db.delete(row)
    for row in list(bill.participants or []):
        db.delete(row)
    db.flush()
    for account_id, amount_paid in payers:
        db.add(models.BillPayer(bill_id=bill.id, account_id=account_id, amount_paid=_q(amount_paid)))
    for account_id, share_amount, split_value in shares:
        db.add(
            models.BillParticipant(
                bill_id=bill.id,
                account_id=account_id,
                share_amount=_q(share_amount),
                paid_amount=Decimal("0.00"),
                split_value=split_value,
            )
        )
    db.flush()
    return bill


def member_id_for_account(db: Session, group_id: int, account_id: int) -> Optional[int]:
    row = (
        db.query(models.GroupMember)
        .filter_by(group_id=group_id, account_id=account_id)
        .first()
    )
    return row.id if row else None


def account_id_for_member(db: Session, member_id: int) -> Optional[int]:
    row = db.get(models.GroupMember, member_id)
    return row.account_id if row else None


# --- Duck-type views (Phase 7: keep helper signatures without legacy tables) ---

class _SimpleNS:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def pair_view_from_bill(db: Session, bill: models.Bill) -> _SimpleNS:
    """Shape a PAIR bill like legacy QuickSplit for Friends/helpers."""
    payer_id = pair_payer_account_id(bill)
    payer = db.get(models.Account, payer_id)
    non_payer_parts = [
        p for p in (bill.participants or []) if p.account_id != payer_id or p.account_id is None
    ]
    # Prefer non-payer participants for the "other" side; include all non-payer rows.
    participants = []
    for p in bill.participants or []:
        if p.account_id == payer_id:
            continue
        acct = db.get(models.Account, p.account_id) if p.account_id else None
        participants.append(
            _SimpleNS(
                id=p.id,
                account_id=p.account_id,
                email=p.email or (acct.email if acct else ""),
                display_name=p.display_name or (acct.display_name if acct else None),
                share_amount=p.share_amount,
                paid_amount=p.paid_amount or 0,
                split_value=p.split_value,
                payment_status=p.payment_status or "PENDING",
                payment_marked_at=p.payment_marked_at,
                invite_token=p.invite_token,
                account=acct,
            )
        )
    paid = pair_payer_paid(bill)
    total = _q(bill.total_amount)
    return _SimpleNS(
        id=bill.id,
        title=bill.title,
        total_amount=bill.total_amount,
        currency=bill.currency,
        bill_date=bill.bill_date,
        category=bill.category,
        split_type=bill.split_type,
        payer_share_amount=pair_payer_share(bill),
        payer_paid_amount=None if paid == total else paid,
        payer_account_id=payer_id,
        created_by_account_id=bill.created_by_account_id,
        invite_code=bill.invite_code or "",
        status=bill.status or "OPEN",
        created_at=bill.created_at,
        edited_after_payment=bool(bill.edited_after_payment),
        edit_history_json=bill.edit_history_json,
        adjustment_of_split_id=bill.adjustment_of_bill_id,
        payer=payer,
        participants=participants,
        _bill=bill,
    )


def list_pair_bills_for_accounts(
    db: Session, account_id: int, friend_id: int, currency: str
) -> list[tuple[_SimpleNS, _SimpleNS]]:
    """Return (pair_view, other_participant) rows for Friend DIRECT."""
    currency = _currency(currency)
    bills = (
        db.query(models.Bill)
        .options(selectinload(models.Bill.participants), selectinload(models.Bill.payers))
        .filter_by(scope="PAIR", currency=currency)
        .all()
    )
    out = []
    for bill in bills:
        view = pair_view_from_bill(db, bill)
        ids = {view.payer_account_id, *(p.account_id for p in view.participants if p.account_id)}
        if account_id not in ids or friend_id not in ids:
            continue
        other = next(
            (
                p
                for p in view.participants
                if p.account_id
                and p.account_id != account_id
                and (
                    (view.payer_account_id == account_id and p.account_id == friend_id)
                    or (view.payer_account_id == friend_id and p.account_id == account_id)
                )
            ),
            None,
        )
        if other is None and view.payer_account_id in (account_id, friend_id):
            # Friend is the payer and viewer is a participant
            other = next((p for p in view.participants if p.account_id == account_id), None)
            if other and view.payer_account_id == friend_id:
                out.append((view, other))
                continue
        if other:
            out.append((view, other))
    return out


def group_bills_as_expense_rows(db: Session, period_id: int) -> list[_SimpleNS]:
    """Project GROUP bills into expense-like rows (member_id resolved)."""
    bills = list_group_bills(db, period_id)
    rows = []
    for bill in bills:
        if not bill.group_id:
            continue
        payer_account = None
        if bill.payers:
            payer_account = max(bill.payers, key=lambda p: _q(p.amount_paid)).account_id
        payer_member_id = (
            member_id_for_account(db, bill.group_id, payer_account) if payer_account else None
        )
        payers = []
        for p in bill.payers or []:
            mid = member_id_for_account(db, bill.group_id, p.account_id)
            if mid:
                payers.append(_SimpleNS(member_id=mid, amount_paid=p.amount_paid, expense_id=bill.id))
        splits = []
        for p in bill.participants or []:
            if not p.account_id:
                continue
            mid = member_id_for_account(db, bill.group_id, p.account_id)
            if mid:
                splits.append(
                    _SimpleNS(
                        member_id=mid,
                        share_amount=p.share_amount,
                        split_value=p.split_value,
                        expense_id=bill.id,
                    )
                )
        rows.append(
            _SimpleNS(
                id=bill.id,
                period_id=bill.period_id,
                store_name=bill.title,
                bill_date=bill.bill_date,
                total_amount=bill.total_amount,
                currency=bill.currency,
                category=bill.category,
                split_type=bill.split_type,
                fx_rate_to_group=bill.fx_rate_to_group or Decimal("1.0"),
                payer_member_id=payer_member_id,
                image_path=bill.image_path,
                recurring_template_id=bill.recurring_template_id,
                created_at=bill.created_at,
                payers=payers,
                splits=splits,
                period=db.get(models.MonthlyPeriod, bill.period_id),
                _bill=bill,
            )
        )
    return rows
