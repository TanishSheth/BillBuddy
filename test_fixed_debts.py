"""Financial invariants: no debt rerouting; exact cash and append-only changes."""
from datetime import datetime, timezone
from decimal import Decimal
import json
import random

from app import models, fixed_debts
from app.main import _business_today, _consolidated_friend_balance, _period_report_inputs
from test_legacy_feature_release import pair


def group_with_three(pair):
    client, db, (one, two), headers = pair
    third = models.Account(email=f'funding-{one}@test.invalid', display_name='Bhagu', password_hash='unused')
    db.add(third); db.commit()
    gid = client.post('/api/groups', headers=headers[0], json={'name':'Fixed bills', 'currency':'USD'}).json()['id']
    for aid in (two, third.id):
        response = client.post(f'/api/groups/{gid}/members', headers=headers[0], json={'account_id':aid})
        assert response.status_code == 200, response.text
    members = client.get(f'/api/groups/{gid}', headers=headers[0]).json()['members']
    mids = {m['account_id']:m['id'] for m in members}
    return client, db, (one, two, third.id), headers, gid, mids


def bill(client, header, gid, payer, debtor, amount, payers=None):
    payload = {'store_name':'Posted bill', 'total_amount':amount, 'currency':'USD', 'bill_date':str(_business_today()),
        'payer_member_id':payer, 'split_type':'EQUAL', 'split_with':[debtor]}
    if payers:
        payload['payers'] = payers
    response = client.post(f'/api/groups/{gid}/expenses', headers=header, json=payload)
    assert response.status_code == 200, response.text
    return response.json(), payload


def test_new_bill_never_reroutes_old_recipients(pair):
    client, db, (one, two, three), headers, gid, mids = group_with_three(pair)
    original, _ = bill(client, headers[0], gid, mids[one], mids[two], 100,
        [{'member_id':mids[one], 'amount':61.03}, {'member_id':mids[three], 'amount':38.97}])
    db.expire_all()
    period = db.query(models.Expense).get(original['id']).period
    group = db.query(models.Group).get(gid)
    before = fixed_debts.period_sources(db, group, period)
    original_terms = [s for s in before if s['expense_id'] == original['id']]
    assert sorted(s['amount'] for s in original_terms) == [Decimal('38.97'), Decimal('61.03')]
    bill(client, headers[0], gid, mids[three], mids[one], 55)
    db.expire_all()
    after = fixed_debts.period_sources(db, group, period)
    assert [s for s in after if s['expense_id'] == original['id']] == original_terms
    assert _consolidated_friend_balance(db, one, two, 'USD')[0] == Decimal('61.03')
    rows = client.get(f'/api/groups/{gid}/settlements', headers=headers[0]).json()
    assert any(r['from_member_id'] == mids[two] and r['to_member_id'] == mids[three] and r['amount'] == 38.97 for r in rows)


def test_paid_bill_edit_delete_keeps_cash_and_original_terms(pair):
    client, db, (one, two, _), headers, gid, mids = group_with_three(pair)
    expense, payload = bill(client, headers[0], gid, mids[one], mids[two], 100)
    response = client.post(f'/api/friends/{two}/consolidated-payments', headers=headers[0], json={'amount':40,'currency':'USD'})
    assert response.status_code == 200, response.text
    allocations = response.json()['allocations']
    assert allocations[0]['expense_id'] == expense['id']
    assert allocations[0]['group_payment_id']
    assert _consolidated_friend_balance(db, one, two, 'USD')[0] == Decimal('60')
    response = client.put(f"/api/groups/{gid}/expenses/{expense['id']}?owner_override=true", headers=headers[0], json={**payload, 'total_amount':25})
    assert response.status_code == 200, response.text
    db.expire_all()
    assert _consolidated_friend_balance(db, one, two, 'USD')[0] == Decimal('-15')
    history = client.get(f"/api/groups/{gid}/expenses/{expense['id']}/audit", headers=headers[0]).json()
    update = next(row for row in history if row['event_type'] == 'UPDATED')
    assert update['before']['debt_terms'][0]['amount'] == '100'
    assert update['after']['debt_terms'][0]['amount'] == '25'
    response = client.delete(f"/api/groups/{gid}/expenses/{expense['id']}?owner_override=true", headers=headers[0])
    assert response.status_code == 200, response.text
    db.expire_all()
    assert _consolidated_friend_balance(db, one, two, 'USD')[0] == Decimal('-40')
    assert client.get(f'/api/groups/{gid}/audit/integrity', headers=headers[0]).json()['valid']


def test_midmonth_group_payment_has_same_bill_history_and_reports(pair):
    client, db, (one, two, _), headers, gid, mids = group_with_three(pair)
    expense, _ = bill(client, headers[0], gid, mids[one], mids[two], 100)
    period = db.query(models.Expense).get(expense['id']).period
    response = client.post(f'/api/groups/{gid}/periods/{period.period_key}/payments/received', headers=headers[0],
        json={'payer_member_id':mids[two], 'recipient_member_id':mids[one], 'amount':30, 'currency':'USD'})
    assert response.status_code == 200, response.text
    db.expire_all()
    assert _consolidated_friend_balance(db, one, two, 'USD')[0] == Decimal('70')
    history = client.get(f'/api/friends/{two}/consolidated-balances', headers=headers[0]).json()['balances'][0]['payments']
    assert history[0]['allocations'][0]['expense_id'] == expense['id']
    group = db.query(models.Group).get(gid)
    _, settlements, _ = _period_report_inputs(db, group, period)
    assert settlements[0]['amount'] == 70


def test_member_leaving_does_not_erase_or_reassign_debt(pair):
    client, db, (one, two, _), headers, gid, mids = group_with_three(pair)
    expense, _ = bill(client, headers[0], gid, mids[one], mids[two], 100)
    response = client.delete(f'/api/groups/{gid}/members/{mids[two]}', headers=headers[0])
    assert response.status_code == 200, response.text
    db.expire_all()
    assert db.query(models.Expense).get(expense['id']) is not None
    assert _consolidated_friend_balance(db, one, two, 'USD')[0] == Decimal('100')


def test_deleted_bill_payment_cannot_attach_to_a_reused_bill_id(pair):
    client, db, (one, two, _), headers, gid, mids = group_with_three(pair)
    original, _ = bill(client, headers[0], gid, mids[one], mids[two], 100)
    response = client.post(f'/api/friends/{two}/consolidated-payments', headers=headers[0], json={'amount':40,'currency':'USD'})
    assert response.status_code == 200, response.text
    old_identity = response.json()['allocations'][0]['obligation_key']
    assert old_identity
    assert client.delete(f"/api/groups/{gid}/expenses/{original['id']}?owner_override=true", headers=headers[0]).status_code == 200
    created, _ = bill(client, headers[0], gid, mids[one], mids[two], 100)
    db.expire_all()
    period = db.query(models.Expense).get(created['id']).period
    sources = fixed_debts.period_sources(db, db.query(models.Group).get(gid), period)
    new_debt = next(s for s in sources if s['expense_id'] == created['id'])
    assert new_debt['amount'] == Decimal('100')
    assert new_debt['obligation_key'] != old_identity
    assert _consolidated_friend_balance(db, one, two, 'USD')[0] == Decimal('60')


def test_integer_cent_allocation_conserves_every_member_net():
    rng = random.Random(901)
    for _ in range(250):
        paid = [rng.randint(0,10000) for _ in range(5)]
        total = sum(paid)
        cuts = sorted([0,total] + [rng.randint(0,total) for _ in range(4)])
        shares = [cuts[i+1]-cuts[i] for i in range(5)]
        payers = [{'member_id':i+1,'amount':Decimal(v)/100} for i,v in enumerate(paid)]
        split = [{'member_id':i+1,'share_amount':Decimal(v)/100} for i,v in enumerate(shares)]
        first = fixed_debts.terms(payers,split)
        assert first == fixed_debts.terms(list(reversed(payers)),list(reversed(split)))
        actual = {i+1:Decimal(0) for i in range(5)}
        for edge in first:
            actual[edge['payer_member_id']] -= Decimal(edge['amount'])
            actual[edge['recipient_member_id']] += Decimal(edge['amount'])
        assert actual == {i+1:Decimal(paid[i]-shares[i])/100 for i in range(5)}


def test_saved_exchange_rate_cannot_reprice_existing_debt(pair, monkeypatch):
    import app.main as main
    client, db, (one, two, _), headers, gid, mids = group_with_three(pair)
    monkeypatch.setattr(main, '_fx_rate_between', lambda *_: Decimal('1.20'))
    response = client.post(f'/api/groups/{gid}/expenses', headers=headers[0], json={
        'store_name':'Foreign receipt', 'total_amount':100, 'currency':'EUR',
        'bill_date':str(_business_today()), 'payer_member_id':mids[one],
        'split_type':'EQUAL', 'split_with':[mids[two]],
    })
    assert response.status_code == 200, response.text
    assert _consolidated_friend_balance(db, one, two, 'USD')[0] == Decimal('120')
    monkeypatch.setattr(main, '_fx_rate_between', lambda *_: Decimal('2.00'))
    monkeypatch.setattr(main, '_rate_to_dest', lambda *_: (_ for _ in ()).throw(AssertionError('Live FX must not reprice a saved bill')))
    db.expire_all()
    assert _consolidated_friend_balance(db, one, two, 'USD')[0] == Decimal('120')
    expense = db.query(models.Expense).get(response.json()['id'])
    group = db.query(models.Group).get(gid)
    balances, settlements, _ = _period_report_inputs(db, group, expense.period)
    assert settlements[0]['amount'] == 120
    assert next(row for row in balances if row['member_id'] == mids[one])['net_balance'] == 120
