# Fixed bill debts — legacy tables

Local source release: `20261029fixedDebts1`.

Uses the existing `expenses`, `expense_payers`, `expense_splits`,
`quick_splits`, `quick_split_participants`, `personal_expenses`,
`group_payments`, `pair_payments`, and audit tables. No unified bill tables,
Alembic migrations, or bill-backfill modules are included or required.

Each group bill records deterministic debtor/creditor terms in its audit
snapshot. Integer-cent allocation conserves each participant's net amount.
Updates retain previous versions and publish corrected terms immediately.
Confirmed payments reference the original bill identity and persist their cash
history. Editing/deleting a paid bill preserves the cash as a visible credit.

Friends, group settlement cards, member nets, and report inputs use the same
fixed debt calculation. New group bills cannot reroute a previous bill's debt.
Weekly snapshots are historical reporting records, not the source of debts.
Historical snapshots are never rewritten at startup. The optional simplified
settlement API remains a proposal and is not applied to Friends accounting.

Borrower-reported Friends payments require recipient confirmation. Group-page
payment recording retains its existing permissions and also writes the same
Friends allocation history. Payments can be recorded during an open month.

The release contains the expanded disposable demo fixtures, session restoration,
bill/share-link UI, payment-history UI, live split previews, receipt API-key
settings, theme rules, and notification focus behavior. A notification for a
deleted bill opens its audit history when the expense row no longer exists.

Validation uses disposable local SQLite databases with email and background
jobs disabled. This folder is independent of the unfinished unified-ledger
development checkout. It has not been pushed or deployed to Oracle; production
rollout and database migration remain outside this task.

Run from this folder:

```text
python -m pytest test_fixed_debts.py test_legacy_feature_release.py test_p1_audit_fixes.py test_demo_bills_payments.py test_full_regression.py -q
node test_friend_status.cjs
node test_theme_contrast.cjs
```
