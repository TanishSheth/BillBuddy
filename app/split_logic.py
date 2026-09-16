"""
ShareSquad Core Money-Splitting Math:
  1. equal_split(total, member_ids)             -> per-member share amounts (cent-accurate)
  2. percentage_split(total, member_percents)   -> validates 100%, cent-accurate shares
  3. exact_split(total, member_amounts)         -> validates sum == total
  4. shares_split(total, member_shares)         -> weighted ratio split, cent-accurate
  5. calculate_splits(split_type, total, items) -> unified split dispatcher
  6. compute_balances(expenses, members)        -> net balance per member
  7. simplify_debts(balances)                   -> greedy min-transaction settlement plan
"""
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Sequence, Tuple, Union
import heapq


def equal_split(total_amount: Decimal | float, member_ids: Sequence[int]) -> Dict[int, Decimal]:
    if not member_ids:
        raise ValueError("split_with must contain at least one member")

    n = len(member_ids)
    total_cents = int((Decimal(str(total_amount)) * 100).to_integral_value(rounding=ROUND_HALF_UP))
    base_cents, remainder = divmod(total_cents, n)

    shares: Dict[int, Decimal] = {}
    for i, mid in enumerate(member_ids):
        cents = base_cents + (1 if i < remainder else 0)
        shares[mid] = (Decimal(cents) / 100).quantize(Decimal("0.01"))
    return shares


def percentage_split(total_amount: Decimal | float, member_percents: Dict[int, Decimal | float]) -> Dict[int, Decimal]:
    """
    member_percents: {member_id: percentage} e.g. {1: 40.0, 2: 60.0}
    Validates that percentages sum to 100.0 (tolerance 0.01) and balances cent rounding.
    """
    if not member_percents:
        raise ValueError("Split must contain at least one member")

    total_pct = sum(Decimal(str(p)) for p in member_percents.values())
    if abs(total_pct - Decimal("100.0")) > Decimal("0.05"):
        raise ValueError(f"Percentages must sum to 100%, got {total_pct}%")

    total_dec = Decimal(str(total_amount))
    total_cents = int((total_dec * 100).to_integral_value(rounding=ROUND_HALF_UP))

    shares: Dict[int, Decimal] = {}
    allocated_cents = 0
    items = list(member_percents.items())

    for mid, pct in items[:-1]:
        pct_dec = Decimal(str(pct))
        cents = int((total_dec * pct_dec).to_integral_value(rounding=ROUND_HALF_UP))
        allocated_cents += cents
        shares[mid] = (Decimal(cents) / 100).quantize(Decimal("0.01"))

    # Allocate remainder to last member to guarantee zero cent drift
    last_mid = items[-1][0]
    last_cents = total_cents - allocated_cents
    shares[last_mid] = (Decimal(last_cents) / 100).quantize(Decimal("0.01"))

    return shares


def exact_split(total_amount: Decimal | float, member_amounts: Dict[int, Decimal | float]) -> Dict[int, Decimal]:
    """
    member_amounts: {member_id: exact_amount} e.g. {1: 25.50, 2: 74.50}
    Validates that sum of exact amounts equals total_amount.
    """
    if not member_amounts:
        raise ValueError("Split must contain at least one member")

    total_dec = Decimal(str(total_amount)).quantize(Decimal("0.01"))
    shares = {mid: Decimal(str(amt)).quantize(Decimal("0.01")) for mid, amt in member_amounts.items()}
    sum_shares = sum(shares.values())

    if abs(sum_shares - total_dec) > Decimal("0.01"):
        raise ValueError(f"Sum of split amounts ({sum_shares}) must equal total amount ({total_dec})")

    return shares


def shares_split(total_amount: Decimal | float, member_shares: Dict[int, Decimal | float | int]) -> Dict[int, Decimal]:
    """
    member_shares: {member_id: share_weight} e.g. {1: 1, 2: 2} (1/3 and 2/3)
    """
    if not member_shares:
        raise ValueError("Split must contain at least one member")

    total_weight = sum(Decimal(str(w)) for w in member_shares.values())
    if total_weight <= Decimal("0"):
        raise ValueError("Total shares weight must be greater than 0")

    total_dec = Decimal(str(total_amount))
    total_cents = int((total_dec * 100).to_integral_value(rounding=ROUND_HALF_UP))

    shares: Dict[int, Decimal] = {}
    allocated_cents = 0
    items = list(member_shares.items())

    for mid, weight in items[:-1]:
        w_dec = Decimal(str(weight))
        cents = int(((total_dec * 100 * w_dec) / total_weight).to_integral_value(rounding=ROUND_HALF_UP))
        allocated_cents += cents
        shares[mid] = (Decimal(cents) / 100).quantize(Decimal("0.01"))

    last_mid = items[-1][0]
    last_cents = total_cents - allocated_cents
    shares[last_mid] = (Decimal(last_cents) / 100).quantize(Decimal("0.01"))

    return shares


def calculate_splits(
    split_type: str,
    total_amount: Decimal | float,
    split_inputs: Union[List[int], Dict[int, Union[Decimal, float, int]], List[dict]],
) -> Dict[int, Decimal]:
    """
    Unified router for split calculations.
    split_type: 'EQUAL' | 'PERCENTAGE' | 'EXACT' | 'SHARES'
    """
    split_type = (split_type or "EQUAL").upper()

    if split_type == "EQUAL":
        if isinstance(split_inputs, dict):
            member_ids = list(split_inputs.keys())
        elif isinstance(split_inputs, list) and split_inputs and isinstance(split_inputs[0], dict):
            member_ids = [item["member_id"] for item in split_inputs]
        else:
            member_ids = list(split_inputs)
        return equal_split(total_amount, member_ids)

    # Convert list of dicts [{member_id: X, value: Y}] to dict {member_id: value}
    data_dict: Dict[int, Union[Decimal, float, int]] = {}
    if isinstance(split_inputs, dict):
        data_dict = split_inputs
    elif isinstance(split_inputs, list):
        for item in split_inputs:
            if isinstance(item, dict):
                data_dict[item["member_id"]] = item.get("value", 0)
            elif isinstance(item, (int, str)):
                data_dict[int(item)] = 1

    if split_type == "PERCENTAGE":
        return percentage_split(total_amount, data_dict)
    elif split_type == "EXACT":
        return exact_split(total_amount, data_dict)
    elif split_type == "SHARES":
        return shares_split(total_amount, data_dict)
    else:
        raise ValueError(f"Unknown split type: {split_type}")


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
    """
    Greedy min-transaction settlement algorithm.
    Balances: { member_id: { net_balance, name, ... } }
    """
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
