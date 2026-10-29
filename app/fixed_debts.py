"""Deterministic per-bill debts on the existing Expense/GroupPayment tables.

Bill audit snapshots preserve published terms. GroupPayment is the cash source
of truth; PairPayment allocations identify the exact bills that cash covered.
Weekly snapshots and group-wide optimization do not participate in accounting.
"""
from collections import defaultdict
from decimal import Decimal, ROUND_FLOOR
import json

from . import models

CENT = Decimal('0.01')
ZERO = Decimal('0.00')


def money(value):
    return Decimal(str(value or 0)).quantize(CENT)


def terms(payers, shares):
    """Match each bill's net debtors to its net funders in integer cents.

    Largest remainders and member-id tie breaks make the result independent of
    database row order, other bills, date, or future group membership changes.
    """
    paid = defaultdict(lambda: ZERO)
    owed = defaultdict(lambda: ZERO)
    for row in payers:
        paid[int(row['member_id'])] += money(row['amount'])
    for row in shares:
        owed[int(row['member_id'])] += money(row['share_amount'])
    if sum(paid.values(), ZERO) != sum(owed.values(), ZERO):
        raise ValueError('Bill funding and shares must have the same total')
    nets = {mid: paid[mid] - owed[mid] for mid in paid.keys() | owed.keys()}
    creditors = {mid: int(net * 100) for mid, net in nets.items() if net > 0}
    debtors = {mid: int(-net * 100) for mid, net in nets.items() if net < 0}
    remaining = dict(creditors)
    result = []
    for debtor, amount in sorted(debtors.items()):
        total = sum(remaining.values())
        if not total:
            break
        quotas = {mid: Decimal(amount) * due / total for mid, due in remaining.items() if due}
        portions = {mid: int(value.to_integral_value(rounding=ROUND_FLOOR)) for mid, value in quotas.items()}
        leftover = amount - sum(portions.values())
        ranking = sorted(quotas, key=lambda mid: (-(quotas[mid] - portions[mid]), mid))
        for mid in ranking[:leftover]:
            portions[mid] += 1
        for creditor, cents in sorted(portions.items()):
            if cents:
                result.append({'payer_member_id': debtor, 'recipient_member_id': creditor, 'amount': str(Decimal(cents) / 100)})
                remaining[creditor] -= cents
    return result


def snapshot_terms(snapshot):
    return terms(snapshot['payers'], snapshot['splits'])


def expense_terms(expense):
    payers = [{'member_id': p.member_id, 'amount': p.amount_paid} for p in expense.payers]
    if not payers:
        payers = [{'member_id': expense.payer_member_id, 'amount': expense.total_amount}]
    return terms(payers, [{'member_id': s.member_id, 'share_amount': s.share_amount} for s in expense.splits])


def period_sources(db, group, period, currency=None):
    """Atomic remaining bill obligations, with confirmed cash applied once.

    New cash retains exact expense IDs. Historical cash without IDs consumes
    only bills that existed when it was recorded, in oldest-first order. Reads
    do not write or relabel any payment/bill.
    """
    currency = (currency or group.currency or 'USD').upper()
    base = (group.currency or 'USD').upper()
    target = (period.target_currency or '').upper()
    if currency not in {base, target}:
        return []
    rate = Decimal(str(period.settlement_fx_rate or 1)) if currency != base else Decimal(1)
    expenses = db.query(models.Expense).filter_by(period_id=period.id).order_by(models.Expense.id).all()
    events = db.query(models.ExpenseAuditEvent).filter_by(group_id=group.id).order_by(models.ExpenseAuditEvent.id).all()
    latest = {}
    payment_links = {}
    for event in events:
        after = json.loads(event.after_json) if event.after_json else None
        if event.expense_id and after and 'payers' in after:
            latest[event.expense_id] = after
        if after and after.get('group_payment_id') and after.get('expense_id'):
            payment_links[int(after['group_payment_id'])] = [(int(after['expense_id']), money(after.get('amount')), after.get('obligation_key'))]
    # Payment allocations are persisted alongside the cash, including a link
    # to the mirrored GroupPayment. Never count the PairPayment a second time.
    linked_from_pairs = defaultdict(list)
    for payment in db.query(models.PairPayment).filter_by(status='CONFIRMED').all():
        for allocation in json.loads(payment.allocations_json or '[]'):
            if allocation.get('kind') == 'GROUP' and allocation.get('period_id') == period.id:
                if allocation.get('group_payment_id') and allocation.get('expense_id'):
                    linked_from_pairs[int(allocation['group_payment_id'])].append((int(allocation['expense_id']), money(allocation.get('amount')), allocation.get('obligation_key')))
    payment_links.update(linked_from_pairs)
    amounts = defaultdict(lambda: ZERO)
    metadata = {}
    for expense in expenses:
        snapshot = latest.get(expense.id) or {}
        edges = snapshot.get('debt_terms') or expense_terms(expense)
        fx = Decimal(str(snapshot.get('fx_rate_to_group', expense.fx_rate_to_group or 1)))
        bill_rate = Decimal(1) if (expense.currency or base).upper() == base else fx
        metadata[expense.id] = expense
        for edge in edges:
            key = (expense.id, int(edge['payer_member_id']), int(edge['recipient_member_id']))
            amounts[key] += money(Decimal(str(edge['amount'])) * bill_rate * rate)
    for payment in db.query(models.GroupPayment).filter(
        models.GroupPayment.period_id == period.id,
        models.GroupPayment.status.in_(('CONFIRMED', 'PAID')),
    ).order_by(models.GroupPayment.id).all():
        paid_currency = (payment.currency or base).upper()
        if paid_currency == currency:
            amount = money(payment.amount)
        elif paid_currency == base:
            amount = money(Decimal(str(payment.amount)) * rate)
        elif paid_currency == target and period.settlement_fx_rate:
            amount = money(Decimal(str(payment.amount)) / Decimal(str(period.settlement_fx_rate)) * rate)
        else:
            raise ValueError('Payment currency has no recorded conversion rate')
        payer, recipient = int(payment.payer_member_id), int(payment.recipient_member_id)
        links = payment_links.get(payment.id, [])
        linked_total = ZERO
        for expense_id, cash, obligation_key in links:
            if obligation_key and latest.get(expense_id, {}).get('bill_identity') != obligation_key:
                expense_id = 0
            converted = money(cash * amount / money(payment.amount)) if payment.amount else ZERO
            amounts[(expense_id, payer, recipient)] -= converted
            linked_total += converted
        if links:
            # Keep rounding residue or deliberate overpayment separate from
            # the original bill allocation instead of applying it to new bills.
            if amount != linked_total:
                amounts[(0, payer, recipient)] -= amount - linked_total
            continue
        remaining = amount
        for key in sorted(amounts):
            eid, debtor, lender = key
            bill = metadata.get(eid)
            if (debtor, lender) != (payer, recipient) or amounts[key] <= 0 or not bill:
                continue
            if payment.expense_cutoff_id is not None and eid > payment.expense_cutoff_id:
                continue
            if payment.created_at and bill.created_at:
                if bill.created_at.replace(tzinfo=None) > payment.created_at.replace(tzinfo=None):
                    continue
            applied = min(remaining, amounts[key])
            amounts[key] -= applied
            remaining -= applied
            if remaining <= 0:
                break
        if remaining:
            # Preserve actual cash even when its bill has been deleted, edited
            # downward, or was already settled. It becomes a visible credit.
            amounts[(0, payer, recipient)] -= remaining
    sources = []
    for (eid, payer, recipient), value in sorted(amounts.items()):
        value = money(value)
        if not value:
            continue
        if value < 0:
            payer, recipient = recipient, payer
        expense = metadata.get(eid)
        sources.append({
            'kind': 'GROUP', 'group_id': group.id, 'period_id': period.id, 'period_key': period.period_key,
            'expense_id': eid or None, 'label': group.name,
            'obligation_key': latest.get(eid, {}).get('bill_identity'),
            'bill_title': expense.store_name if expense else 'Payment credit / corrected bill',
            'payer_member_id': payer, 'recipient_member_id': recipient,
            'amount': abs(value), 'currency': currency, 'settlement_ready': True,
        })
    return sources


def settlements(db, group, period, currency=None):
    totals = defaultdict(lambda: ZERO)
    names = {m.id: m.display_name for m in db.query(models.GroupMember).filter_by(group_id=group.id)}
    for source in period_sources(db, group, period, currency):
        one, two = sorted((source['payer_member_id'], source['recipient_member_id']))
        totals[(one, two)] += source['amount'] if source['payer_member_id'] == one else -source['amount']
    result = []
    for (one, two), amount in sorted(totals.items()):
        if not amount:
            continue
        debtor, lender = (one, two) if amount > 0 else (two, one)
        result.append({'from_member_id': debtor, 'to_member_id': lender,
            'from_name': names.get(debtor, 'Former member'), 'to_name': names.get(lender, 'Former member'),
            'amount': float(abs(amount))})
    return result
