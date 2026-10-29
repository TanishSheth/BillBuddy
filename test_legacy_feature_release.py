"""Contract tests for features on the existing separate bill tables."""
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from app import models
from app.database import Base, engine, SessionLocal
from app.auth import create_access_token_for_account
from app.main import app, _business_today, _consolidated_friend_balance, _viewer_direct_effect
from app.redis_support import clear_memory_rate_limits


@pytest.fixture()
def pair():
    clear_memory_rate_limits()
    db = SessionLocal()
    accounts = [models.Account(email=f'{uuid.uuid4().hex}@test.invalid', display_name=name, password_hash='unused') for name in ('Tanish', 'KP')]
    db.add_all(accounts)
    db.flush()
    db.add(models.FriendConnection(account_one_id=accounts[0].id, account_two_id=accounts[1].id, status='ACTIVE'))
    headers = [{'Authorization': 'Bearer ' + create_access_token_for_account(a)} for a in accounts]
    ids = [a.id for a in accounts]
    db.commit()
    yield TestClient(app), db, ids, headers
    db.close()


def add_bill(db, payer, debtor, title, amount, status='PENDING', month=10):
    bill = models.QuickSplit(title=title, total_amount=amount, payer_share_amount=0, payer_account_id=payer,
        bill_date=date(2026, month, 1), currency='USD', status='SETTLED' if status == 'CONFIRMED' else 'OPEN',
        participants=[models.QuickSplitParticipant(email=f'{debtor}@test.invalid', account_id=debtor,
            share_amount=amount, payment_status=status, paid_amount=0)])
    db.add(bill)
    db.flush()
    return bill


def test_no_unified_tables_are_created():
    tables = set(inspect(engine).get_table_names())
    assert {'expenses', 'quick_splits', 'personal_expenses', 'pair_payments'} <= tables
    assert not tables & {'bills', 'bill_participants', 'bill_payers', 'payment_allocations', 'bill_id_map'}


def test_kp_open_bills_and_old_payments_total_701(pair):
    client, db, (me, kp), headers = pair
    for title, signed in [('Fajita', '-9.27'), ('Burlington', '10.68'), ('Desk', '-21.29'), ('API', '-4.08'), ('Splitwise', '30.97')]:
        amount = Decimal(signed)
        add_bill(db, me if amount > 0 else kp, kp if amount > 0 else me, title, abs(amount))
    old_ids = []
    for title, amount, payer, debtor, month in [('Lyft', '18.37', kp, me, 9), ('Uber', '8.17', kp, me, 9), ('Ride', '12.69', kp, me, 9), ('Cookies', '12.00', me, kp, 10)]:
        old_ids.append(add_bill(db, payer, debtor, title, Decimal(amount), 'CONFIRMED', month).id)
    db.add(models.FriendMonthlySettlement(account_one_id=me, account_two_id=kp, month_key='2026-09',
        payer_account_id=me, recipient_account_id=kp, amount=Decimal('9'), currency='USD', status='SETTLED'))
    for payer, recipient, amount, allocations in [
        (kp, me, '4.5', [{'kind': 'DIRECT', 'month_key': '2026-09', 'amount': 4.5}]),
        (me, kp, '10', [{'kind': 'DIRECT', 'month_key': '2026-09', 'amount': 10, 'quick_split_id': old_ids[0], 'private_applied': True}]),
        (kp, me, '10', [{'kind': 'DIRECT', 'month_key': '2026-10', 'amount': 10, 'quick_split_id': old_ids[3], 'private_applied': True}]),
    ]:
        db.add(models.PairPayment(account_one_id=me, account_two_id=kp, payer_account_id=payer, recipient_account_id=recipient,
            amount=Decimal(amount), currency='USD', status='CONFIRMED', reported_by_account_id=recipient, allocations_json=json.dumps(allocations)))
    db.commit()
    balance, sources = _consolidated_friend_balance(db, me, kp, 'USD')
    assert balance == Decimal('7.01'), sources
    assert _consolidated_friend_balance(db, kp, me, 'USD')[0] == Decimal('-7.01')
    for _ in range(2):
        response = client.get(f'/api/friends/{kp}/consolidated-balances', headers=headers[0])
        assert response.status_code == 200, response.text
        assert response.json()['balances'][0]['balance'] == 7.01
    assert db.query(models.QuickSplitParticipant).filter_by(account_id=kp, payment_status='PENDING').count() >= 2


def test_shared_funding_math_uses_cash_contributions(pair):
    _, db, (me, kp), _ = pair
    bill = add_bill(db, me, kp, 'Both paid', Decimal('10'))
    bill.payer_paid_amount = Decimal('6')
    bill.payer_share_amount = Decimal('5')
    bill.participants[0].share_amount = Decimal('5')
    assert _viewer_direct_effect(bill, bill.participants[0], me) == Decimal('1')
    assert _viewer_direct_effect(bill, bill.participants[0], kp) == Decimal('-1')


def test_partial_full_and_overpay_persist_allocations(pair):
    client, db, (me, kp), headers = pair
    bill = add_bill(db, me, kp, 'Debt', Decimal('100'))
    db.commit()
    response = client.post(f'/api/friends/{kp}/consolidated-payments', headers=headers[0], json={'amount': 30, 'currency': 'USD'})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'CONFIRMED'
    db.expire_all()
    assert bill.participants[0].paid_amount == Decimal('30')
    assert bill.participants[0].payment_status == 'PARTIAL'
    assert _consolidated_friend_balance(db, me, kp, 'USD')[0] == Decimal('70')
    response = client.post(f'/api/friends/{kp}/consolidated-payments', headers=headers[0], json={'amount': 90, 'currency': 'USD'})
    assert response.status_code == 200, response.text
    assert any(a['kind'] == 'CREDIT' and a['amount'] == 20 for a in response.json()['allocations'])
    db.expire_all()
    assert bill.participants[0].payment_status == 'CONFIRMED'
    assert _consolidated_friend_balance(db, me, kp, 'USD')[0] == Decimal('-20')


def test_new_expense_and_audit_use_legacy_tables(pair):
    client, db, (me, kp), headers = pair
    group = client.post('/api/groups', headers=headers[0], json={'name': 'Legacy release', 'currency': 'USD'}).json()
    gid = group['id']
    response = client.post(f'/api/groups/{gid}/members', headers=headers[0], json={'account_id': kp})
    assert response.status_code == 200, response.text
    members = client.get(f'/api/groups/{gid}', headers=headers[0]).json()['members']
    mine = next(m['id'] for m in members if m['account_id'] == me)
    theirs = next(m['id'] for m in members if m['account_id'] == kp)
    payload = {'store_name': 'Dinner', 'total_amount': 100, 'bill_date': str(_business_today()), 'currency': 'USD', 'split_type': 'EQUAL', 'payer_member_id': mine, 'split_with': [mine, theirs]}
    response = client.post(f'/api/groups/{gid}/expenses', headers=headers[0], json=payload)
    assert response.status_code == 200, response.text
    eid = response.json()['id']
    assert db.query(models.Expense).get(eid) is not None
    response = client.put(f'/api/groups/{gid}/expenses/{eid}', headers=headers[0], json={**payload, 'total_amount': 120})
    assert response.status_code == 200, response.text
    response = client.get(f'/api/groups/{gid}/expenses/{eid}/audit', headers=headers[0])
    assert response.status_code == 200, response.text
    assert len(response.json()) >= 2
    integrity = client.get(f'/api/groups/{gid}/audit/integrity', headers=headers[0])
    assert integrity.status_code == 200, integrity.text
    assert integrity.json()['valid'] is True


def test_demo_create_reset_uses_legacy_records(pair):
    client, db, _, _ = pair
    response = client.post('/api/demo/sandbox')
    assert response.status_code == 200, response.text
    sandbox = response.json()
    auth = {'Authorization': 'Bearer ' + sandbox['access_token']}
    splits = client.get('/api/quick-splits', headers=auth)
    assert splits.status_code == 200, splits.text
    assert len(splits.json()) >= 10
    assert any(p['payment_status'] == 'PARTIAL' for b in splits.json() for p in b['participants'])
    response = client.post('/api/demo/sandbox/reset', headers=auth)
    assert response.status_code == 200, response.text
    assert response.json()['sandbox_id'] != sandbox['sandbox_id']


def test_weekly_snapshots_are_immutable_and_do_not_gate_bill_balances(pair):
    from datetime import timedelta
    from app.main import _lock_weekly_settlement_batch, _reconcile_latest_weekly_lock, _repair_all_weekly_locks_to_smart
    client, db, (me, kp), headers = pair
    gid = client.post('/api/groups', headers=headers[0], json={'name': 'Weekly', 'currency': 'USD'}).json()['id']
    client.post(f'/api/groups/{gid}/members', headers=headers[0], json={'account_id': kp})
    members = client.get(f'/api/groups/{gid}', headers=headers[0]).json()['members']
    mine = next(m['id'] for m in members if m['account_id'] == me)
    theirs = next(m['id'] for m in members if m['account_id'] == kp)
    payload = {'store_name': 'Week one', 'total_amount': 100, 'bill_date': str(_business_today()), 'currency': 'USD', 'split_type': 'EQUAL', 'payer_member_id': mine, 'split_with': [mine, theirs]}
    response = client.post(f'/api/groups/{gid}/expenses', headers=headers[0], json=payload)
    assert response.status_code == 200, response.text
    eid = response.json()['id']
    db.expire_all()
    expense = db.query(models.Expense).get(eid)
    period = expense.period
    group = period.group
    now = datetime.now(timezone.utc)
    first = _lock_weekly_settlement_batch(db, group, period, window_ends_at=now)
    db.commit()
    original_hash = first.batch_hash
    assert db.query(models.WeeklySettlementLeg).filter_by(batch_id=first.id).one().amount == Decimal('50')
    response = client.put(f'/api/groups/{gid}/expenses/{eid}', headers=headers[0], json={**payload, 'total_amount': 140})
    assert response.status_code == 200, response.text
    db.expire_all()
    assert _repair_all_weekly_locks_to_smart(db) == 0
    _lock_weekly_settlement_batch(db, group, period, window_ends_at=now)
    assert first.batch_hash == original_hash
    assert db.query(models.WeeklySettlementLeg).filter_by(batch_id=first.id).one().amount == Decimal('50')
    assert _consolidated_friend_balance(db, me, kp, 'USD')[0] == Decimal('70')
    second = _lock_weekly_settlement_batch(db, group, period, window_ends_at=now + timedelta(seconds=1))
    db.commit()
    assert db.query(models.WeeklySettlementLeg).filter_by(batch_id=second.id).one().amount == Decimal('20')
    assert _consolidated_friend_balance(db, me, kp, 'USD')[0] == Decimal('70')
    response = client.post(f'/api/friends/{kp}/consolidated-payments', headers=headers[0], json={'amount': 30, 'currency': 'USD'})
    assert response.status_code == 200, response.text
    db.expire_all()
    assert _consolidated_friend_balance(db, me, kp, 'USD')[0] == Decimal('40')
    third = _lock_weekly_settlement_batch(db, group, period, window_ends_at=now + timedelta(seconds=2))
    db.commit()
    assert db.query(models.WeeklySettlementLeg).filter_by(batch_id=third.id).count() == 0
    assert _consolidated_friend_balance(db, me, kp, 'USD')[0] == Decimal('40')


def test_private_bill_create_edit_delete_and_personal_crud(pair):
    client, db, (me, kp), headers = pair
    payload = {'title': 'Taxi', 'total_amount': 20, 'currency': 'USD', 'bill_date': str(_business_today()),
        'split_type': 'EQUAL', 'payer_account_id': me, 'participants': [{'account_id': kp}], 'silent_invite': True}
    response = client.post('/api/quick-splits', headers=headers[0], json=payload)
    assert response.status_code == 200, response.text
    split_id = response.json()['id']
    assert db.query(models.QuickSplit).get(split_id) is not None
    response = client.put(f'/api/quick-splits/{split_id}', headers=headers[0], json={**payload, 'total_amount': 40})
    assert response.status_code == 200, response.text
    assert response.json()['participants'][0]['share_amount'] == 20
    assert client.delete(f'/api/quick-splits/{split_id}', headers=headers[0]).status_code == 200
    personal = {'store_name': 'Coffee', 'total_amount': 5, 'currency': 'USD', 'bill_date': str(_business_today()), 'category': 'Food'}
    response = client.post('/api/personal/expenses', headers=headers[0], json=personal)
    assert response.status_code == 200, response.text
    pid = response.json()['id']
    assert db.query(models.PersonalExpense).get(pid) is not None
    response = client.put(f'/api/personal/expenses/{pid}', headers=headers[0], json={**personal, 'total_amount': 6})
    assert response.status_code == 200, response.text
    assert client.delete(f'/api/personal/expenses/{pid}', headers=headers[0]).status_code == 200
