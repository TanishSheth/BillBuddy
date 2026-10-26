"""Cloud audit: Friends weekly vs live smart Who-pays-whom for every active pair."""
from collections import defaultdict
from decimal import Decimal

from app.database import SessionLocal
from app import models
from app.main import (
    _aggregate_friend_display_sources,
    _consolidated_friend_sources,
    _friend_balance_currencies,
    _normalize_currency,
    _pair_settlement_source_for_period,
    _weekly_pair_settlement_sources,
)


def main() -> None:
    db = SessionLocal()
    try:
        accounts = {a.id: a for a in db.query(models.Account).all()}
        connections = db.query(models.FriendConnection).filter_by(status="ACTIVE").all()
        print("active_friend_links=%s" % len(connections))
        mismatches = 0
        flips = 0
        credits = 0
        seen_pairs = set()
        for conn in connections:
            for viewer_id, friend_id in ((conn.account_one_id, conn.account_two_id), (conn.account_two_id, conn.account_one_id)):
                pair_key = (min(viewer_id, friend_id), max(viewer_id, friend_id), viewer_id)
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                viewer = accounts.get(viewer_id)
                friend = accounts.get(friend_id)
                if not viewer or not friend:
                    continue
                for currency in _friend_balance_currencies(db, viewer_id, friend_id):
                    atomic = _consolidated_friend_sources(db, viewer_id, friend_id, currency)
                    display = _aggregate_friend_display_sources(atomic)
                    bal = sum((Decimal(str(s["amount"])) for s in atomic), Decimal("0")).quantize(Decimal("0.01"))
                    credit_rows = [s for s in display if s.get("kind") == "CREDIT"]
                    if credit_rows:
                        credits += 1
                        print(
                            "CREDIT viewer=%r friend=%r currency=%s balance=%s credits=%s groups=%s"
                            % (
                                viewer.display_name,
                                friend.display_name,
                                currency,
                                float(bal),
                                [(c.get("label"), c.get("amount")) for c in credit_rows],
                                [(g.get("label"), g.get("amount")) for g in display if g.get("kind") == "GROUP"],
                            )
                        )
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
                        if not group:
                            continue
                        if _normalize_currency(group.currency, "USD") != _normalize_currency(currency, "USD"):
                            continue
                        mine, theirs = mmap[viewer_id].id, mmap[friend_id].id
                        for period in db.query(models.MonthlyPeriod).filter_by(group_id=gid).all():
                            live = _pair_settlement_source_for_period(
                                db, group, period, mine, theirs, currency, settlement_ready=True,
                            )
                            weekly = _weekly_pair_settlement_sources(db, group, period, mine, theirs, currency)
                            weekly_sum = sum((Decimal(str(w["amount"])) for w in weekly), Decimal("0"))
                            live_amt = Decimal(str(live["amount"])) if live else Decimal("0")
                            if abs(weekly_sum - live_amt) < Decimal("0.02"):
                                continue
                            mismatches += 1
                            print(
                                "MISMATCH viewer=%r friend=%r group=%r period=%s weekly=%s live=%s kinds=%s"
                                % (
                                    viewer.display_name,
                                    friend.display_name,
                                    group.name,
                                    period.period_key,
                                    float(weekly_sum),
                                    float(live_amt),
                                    [(w.get("kind"), w.get("amount")) for w in weekly],
                                )
                            )
                            if weekly_sum * live_amt < 0 and abs(weekly_sum) >= Decimal("0.01") and abs(live_amt) >= Decimal("0.01"):
                                flips += 1
                                print("  SIGN_FLIP")
                    if abs(bal) >= Decimal("0.005"):
                        print(
                            "BALANCE viewer=%r friend=%r currency=%s balance=%s display=%s"
                            % (
                                viewer.display_name,
                                friend.display_name,
                                currency,
                                float(bal),
                                [(s.get("kind"), s.get("label"), s.get("amount")) for s in display],
                            )
                        )
        print("done mismatches=%s sign_flips=%s credit_rows=%s" % (mismatches, flips, credits))
    finally:
        db.close()


if __name__ == "__main__":
    main()
