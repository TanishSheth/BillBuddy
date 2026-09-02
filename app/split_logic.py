"""
Core money-splitting math:
  1. equal_split(total, member_ids)     -> per-member share amounts (cent-accurate)
  2. compute_balances(expenses, members) -> net balance per member
  3. simplify_debts(balances)           -> greedy min-transaction settlement plan
"""
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Sequence
import heapq


def equal_split(total_amount: Decimal, member_ids: Sequence[int]) -> Dict[int, Decimal]:
    if not member_ids:
        raise ValueError("split_with must contain at least one member")

    n = len(member_ids)
    total_cents = int((Decimal(total_amount) * 100).to_integral_value(rounding=ROUND_HALF_UP))
    base_cents, remainder = divmod(total_cents, n)

    shares: Dict[int, Decimal] = {}
    for i, mid in enumerate(member_ids):
        cents = base_cents + (1 if i < remainder else 0)
        shares[mid] = (Decimal(cents) / 100).quantize(Decimal("0.01"))
    return shares


def compute_balances(
    members: List[dict],
    expenses: List[dict],
) -> Dict[int, dict]:
    """
    members: [{id, name}]
    expenses: [{payer_id, total_amount, splits: [{user_id, share_amount}]}]
    """
    balances = {
        m["id"]: {
            "member_id": m["id"],
            "name": m["name"],
            "total_paid": Decimal("0.00"),
            "total_share_owed": Decimal("0.00"),
        }
        for m in members
    }

    for exp in expenses:
        payer_id = exp["payer_id"]
        if payer_id in balances:
            balances[payer_id]["total_paid"] += Decimal(str(exp["total_amount"]))
        for split in exp["splits"]:
            mid = split["user_id"]
            if mid in balances:
                balances[mid]["total_share_owed"] += Decimal(str(split["share_amount"]))

    result = {}
    for mid, b in balances.items():
        net = b["total_paid"] - b["total_share_owed"]
        result[mid] = {
            **b,
            "total_paid": float(b["total_paid"]),
            "total_share_owed": float(b["total_share_owed"]),
            "net_balance": float(net.quantize(Decimal("0.01"))),
        }
    return result


def simplify_debts(balances: Dict[int, dict]) -> List[dict]:
    creditors = []
    debtors = []

    for mid, b in balances.items():
        cents = int(round(b["net_balance"] * 100))
        if cents > 0:
            heapq.heappush(creditors, (-cents, mid, b["name"]))
        elif cents < 0:
            heapq.heappush(debtors, (cents, mid, b["name"]))

    settlements = []
    while creditors and debtors:
        neg_credit, cred_id, cred_name = heapq.heappop(creditors)
        debt_cents, deb_id, deb_name = heapq.heappop(debtors)

        credit = -neg_credit
        debt = -debt_cents

        amount = min(credit, debt)
        settlements.append({
            "from_member_id": deb_id,
            "from_name": deb_name,
            "to_member_id": cred_id,
            "to_name": cred_name,
            "amount": round(amount / 100, 2),
        })

        remaining_credit = credit - amount
        remaining_debt = debt - amount

        if remaining_credit > 0:
            heapq.heappush(creditors, (-remaining_credit, cred_id, cred_name))
        if remaining_debt > 0:
            heapq.heappush(debtors, (-remaining_debt, deb_id, deb_name))

    return settlements
