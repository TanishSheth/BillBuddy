"""Full production audit: expenses, payments, weekly locks, Friends vs live WPW."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from app.database import SessionLocal
from app import models
from app.config import APP_VERSION
from app.main import (
    _aggregate_friend_display_sources,
    _consolidated_friend_sources,
    _friend_balance_currencies,
    _normalize_currency,
    _pair_settlement_source_for_period,
    _repair_all_weekly_locks_to_smart,
    _weekly_pair_settlement_sources,
    build_balances_and_settlements,
)


def main() -> None:
    db = SessionLocal()
    try:
        print("APP_VERSION", APP_VERSION)
        n_accounts = db.query(models.Account).count()
        n_groups = db.query(models.Group).count()
        n_expenses = db.query(models.Expense).count()
        n_gp = db.query(models.GroupPayment).count()
        n_pp = db.query(models.PairPayment).count()
        n_weeks = db.query(models.WeeklySettlementBatch).filter_by(status="LOCKED").count()
        n_legs = db.query(models.WeeklySettlementLeg).count()
        print(
            "counts accounts=%s groups=%s expenses=%s group_payments=%s pair_payments=%s locked_weeks=%s legs=%s"
            % (n_accounts, n_groups, n_expenses, n_gp, n_pp, n_weeks, n_legs)
        )

        print("--- repairing weekly locks to smart Who-pays-whom ---")
        repaired = _repair_all_weekly_locks_to_smart(db)
        print("repaired_locks", repaired)

        print("--- group period nets vs who-pays-whom ---")
        period_issues = 0
        for group in db.query(models.Group).all():
            for period in db.query(models.MonthlyPeriod).filter_by(group_id=group.id).all():
                balances, settlements, expenses = build_balances_and_settlements(
                    db, group.id, period, settlement_mode="smart",
                )
                if not expenses and not any(abs(float(b["net_balance"])) >= 0.005 for b in balances):
                    continue
                net_sum = sum(Decimal(str(b["net_balance"])) for b in balances)
                settle_sum = sum(Decimal(str(s["amount"])) for s in settlements)
                debtor_sum = sum(
                    -Decimal(str(b["net_balance"])) for b in balances if Decimal(str(b["net_balance"])) < 0
                )
                if abs(net_sum) >= Decimal("0.02"):
                    period_issues += 1
                    print(
                        "NET_IMBALANCE group=%r period=%s net_sum=%s members=%s"
                        % (group.name, period.period_key, float(net_sum),
                           [(b["name"], b["net_balance"]) for b in balances if abs(float(b["net_balance"])) >= 0.005])
                    )
                if abs(debtor_sum - settle_sum) >= Decimal("0.05") and debtor_sum >= Decimal("0.01"):
                    # smart graph may leave unmatched residual when no payer edge
                    unmatched = float(debtor_sum - settle_sum)
                    if abs(unmatched) >= 0.05:
                        print(
                            "UNMATCHED_FLOW group=%r period=%s debtor=%s settled=%s gap=%s settlements=%s"
                            % (
                                group.name, period.period_key, float(debtor_sum), float(settle_sum), unmatched,
                                [(s["from_name"], s["to_name"], s["amount"]) for s in settlements],
                            )
                        )

        print("--- Friends vs live smart edges (every active pair) ---")
        accounts = {a.id: a for a in db.query(models.Account).all()}
        connections = db.query(models.FriendConnection).filter_by(status="ACTIVE").all()
        mismatches = flips = credits = balances_n = 0
        for conn in connections:
            for viewer_id, friend_id in ((conn.account_one_id, conn.account_two_id),):
                viewer, friend = accounts.get(viewer_id), accounts.get(friend_id)
                if not viewer or not friend:
                    continue
                for currency in _friend_balance_currencies(db, viewer_id, friend_id) or ["USD"]:
                    atomic = _consolidated_friend_sources(db, viewer_id, friend_id, currency)
                    display = _aggregate_friend_display_sources(atomic)
                    bal = sum((Decimal(str(s["amount"])) for s in atomic), Decimal("0")).quantize(Decimal("0.01"))
                    if abs(bal) >= Decimal("0.005") or display:
                        balances_n += 1
                        print(
                            "BALANCE %r <-> %r %s total=%s lines=%s"
                            % (
                                viewer.display_name, friend.display_name, currency, float(bal),
                                [(s.get("kind"), s.get("label"), s.get("amount")) for s in display],
                            )
                        )
                    if any(s.get("kind") == "CREDIT" for s in display):
                        credits += 1
                    members = db.query(models.GroupMember).filter(
                        models.GroupMember.account_id.in_((viewer_id, friend_id))
                    ).all()
                    by_group = defaultdict(dict)
                    for m in members:
                        by_group[m.group_id][m.account_id] = m
                    for gid, mmap in by_group.items():
                        if viewer_id not in mmap or friend_id not in mmap:
                            continue
                        group = db.query(models.Group).get(gid)
                        if not group or _normalize_currency(group.currency, "USD") != _normalize_currency(currency, "USD"):
                            continue
                        mine, theirs = mmap[viewer_id].id, mmap[friend_id].id
                        for period in db.query(models.MonthlyPeriod).filter_by(group_id=gid).all():
                            live = _pair_settlement_source_for_period(
                                db, group, period, mine, theirs, currency, settlement_ready=True,
                            )
                            # Friends GROUP now mirrors live smart Who-pays-whom.
                            live_amt = Decimal(str(live["amount"])) if live else Decimal("0")
                            friend_group = sum(
                                (
                                    Decimal(str(s["amount"]))
                                    for s in atomic
                                    if s.get("kind") == "GROUP"
                                    and s.get("group_id") == gid
                                    and s.get("period_key") == period.period_key
                                ),
                                Decimal("0"),
                            )
                            if abs(friend_group) < Decimal("0.02") and abs(live_amt) < Decimal("0.02"):
                                continue
                            if friend_group * live_amt < 0 and abs(friend_group) >= Decimal("0.01") and abs(live_amt) >= Decimal("0.01"):
                                flips += 1
                                mismatches += 1
                                print(
                                    "SIGN_FLIP %r->%r group=%r period=%s friends=%s live=%s"
                                    % (
                                        viewer.display_name, friend.display_name, group.name, period.period_key,
                                        float(friend_group), float(live_amt),
                                    )
                                )
                            elif abs(friend_group - live_amt) >= Decimal("0.02"):
                                mismatches += 1
                                print(
                                    "GAP %r->%r group=%r period=%s friends=%s live=%s delta=%s"
                                    % (
                                        viewer.display_name, friend.display_name, group.name, period.period_key,
                                        float(friend_group), float(live_amt), float(friend_group - live_amt),
                                    )
                                )

        print("--- pair payment allocation sanity ---")
        bad_pay = 0
        for pay in db.query(models.PairPayment).filter_by(status="CONFIRMED").all():
            try:
                allocs = __import__("json").loads(pay.allocations_json or "[]")
            except Exception:
                bad_pay += 1
                print("BAD_JSON payment_id=%s" % pay.id)
                continue
            total = sum(Decimal(str(a.get("amount") or 0)) for a in allocs)
            if abs(total - Decimal(str(pay.amount))) > Decimal("0.02"):
                bad_pay += 1
                print(
                    "ALLOC_MISMATCH payment_id=%s amount=%s alloc_sum=%s kinds=%s"
                    % (pay.id, pay.amount, float(total), [a.get("kind") for a in allocs])
                )

        print(
            "SUMMARY period_net_issues=%s friend_balances=%s credits=%s mismatches=%s sign_flips=%s bad_payments=%s"
            % (period_issues, balances_n, credits, mismatches, flips, bad_pay)
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
