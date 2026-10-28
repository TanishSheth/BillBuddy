"""Preview or repair legacy private-bill statuses from explicit payment evidence."""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import models
from app.database import SessionLocal

CENT = Decimal("0.01")


def money(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT)


def reconcile(apply: bool) -> tuple[int, int]:
    db = SessionLocal()
    coverage = defaultdict(lambda: Decimal("0.00"))
    for payment in db.query(models.PairPayment).filter_by(status="CONFIRMED").all():
        try:
            allocations = json.loads(payment.allocations_json or "[]")
        except (TypeError, ValueError):
            continue
        for allocation in allocations:
            if not isinstance(allocation, dict) or allocation.get("kind") != "DIRECT":
                continue
            if allocation.get("quick_split_id") and payment.payer_account_id:
                coverage[(int(allocation["quick_split_id"]), int(payment.payer_account_id))] += money(allocation.get("amount"))
    inspected = changed = 0
    try:
        for split in db.query(models.QuickSplit).all():
            split_changed = False
            for participant in split.participants:
                if not participant.account_id:
                    continue
                inspected += 1
                recorded = money(participant.paid_amount)
                # Preserve status-only historic confirmations: their original
                # cash record predates bill-linked allocations.
                if recorded <= 0:
                    continue
                explicit = min(money(participant.share_amount), coverage[(split.id, participant.account_id)])
                if recorded == explicit:
                    continue
                print(f"split={split.id} participant={participant.id} title={split.title!r} recorded={recorded} explicit={explicit}")
                changed += 1
                if apply:
                    participant.paid_amount = explicit
                    participant.payment_status = "CONFIRMED" if explicit >= money(participant.share_amount) else "PARTIAL" if explicit > 0 else "PENDING"
                    participant.payment_marked_at = participant.payment_marked_at if explicit > 0 else None
                    split_changed = True
            if split_changed:
                debtors = [p for p in split.participants if p.account_id and p.account_id != split.payer_account_id]
                split.status = "SETTLED" if debtors and all(money(p.paid_amount) >= money(p.share_amount) for p in debtors) else "OPEN"
        if apply:
            db.commit()
        else:
            db.rollback()
        return inspected, changed
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    inspected, changed = reconcile(args.apply)
    print(f"Inspected {inspected}; {'applied' if args.apply else 'would change'} {changed}.")
