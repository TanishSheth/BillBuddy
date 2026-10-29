const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/static/friends.js', 'utf8');
const context = vm.createContext({ me: {account: {id: 1}}, cents: n => Math.round(Number(n) * 100) / 100 });
for (const name of ['friendPaymentsById', 'indexPaidDirectBills', 'isDirectBillSettled']) {
  const start = source.indexOf(`function ${name}(`);
  const end = source.indexOf('\nfunction ', start + 1);
  vm.runInContext(source.slice(start, end < 0 ? undefined : end), context);
}
function row(id, share, paid = 0, status = 'PENDING') {
  return {balance: share - paid, split: {id, total_amount: share, payer_share_amount: 0, currency: 'USD', is_payer: true}, person: {paid_amount: paid, payment_status: status}};
}
function friend(rows, allocations) {
  return {friend: {id: 2}, rows, settlements: new Map([['2026-10', {status:'SETTLED'}]]),
    consolidatedBalances: {balances: [{currency:'USD', balance:0, payments:[{id:1, currency:'USD', status:'CONFIRMED', amount:60, allocations}]}]}};
}
const partial = row(1,100,60,'PARTIAL');
let value = friend([partial], [{kind:'DIRECT', amount:60, quick_split_id:1}]);
context.indexPaidDirectBills(value);
assert.equal(context.isDirectBillSettled(partial,value), false, 'Partial payment must not be compared against reduced remaining debt');
const pending = row(2,10);
value = friend([pending], [{kind:'DIRECT', amount:10, month_key:'2026-10'}]);
context.indexPaidDirectBills(value);
assert.equal(context.isDirectBillSettled(pending,value), false, 'Settled month and unlinked payment must not imply bill paid');
const paid = row(3,10,10,'CONFIRMED');
value = friend([paid], [{kind:'DIRECT', amount:10, quick_split_id:3}]);
context.indexPaidDirectBills(value);
assert.equal(context.isDirectBillSettled(paid,value), true);
console.log('3 Friends payment-status regressions passed');
