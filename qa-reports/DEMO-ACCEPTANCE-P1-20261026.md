# Demo acceptance — P1 + session/bills/payments (27 Oct 2026)

Run against local ShareSquad Web (`http://localhost:8001`) using **Try the full demo**.
Record `APP_VERSION` from `/api/health` before starting.

## Demo session

- [ ] Anonymous **Try the full demo** → lands on `/app?view=groups` (not inside the sample group).
- [ ] Demo banner shows **Reset demo** and **Exit demo** on group / Friends / dashboard.
- [ ] **Exit demo** → marketing landing (`/`), not login.
- [ ] Signed-in → Try demo → Exit demo → still signed in; **Open app** opens own dashboard.
- [ ] Demo expiry / 401 while demoing restores stashed account (does not wipe real session into login).
- [ ] **Reset demo** refreshes sandbox; pre-demo stash unchanged.

## Bill CRUD (demo group)

- [ ] **Add** equal-split bill → appears on Expenses; Who pays whom matches shares; Friends balance for roommate moves.
- [ ] **Edit** amount/store → list + Who pays whom + Friends update; no double-count.
- [ ] **Delete** bill → row gone; settlements and Friends reverse cleanly.
- [ ] Other currencies untouched when editing a USD bill.

## Payments (Friends)

- [ ] Debtor **Mark partial** → Pending; balances unchanged until lender confirms.
- [ ] Lender **Confirm** → debt drops once; other currencies untouched.
- [ ] Lender **Record received** (full remaining) → auto-confirmed; that currency ~0.
- [ ] **Overpay** → Extra pay / credit visible; no phantom group debt.

## Add / Edit bill UI (light + dark)

- [ ] Labels, split pills, chips, errors readable in light and dark.
- [ ] Save is primary; Cancel edit secondary and visible while editing.
- [ ] Split amount inputs readable on dark surfaces (no hard-coded white field).
- [ ] Success toast after save; expenses list updates without a blank flash.
- [ ] Mobile width: form usable; receipt preview does not overflow.

## Legacy P1 checklist

- [ ] **Smoke** — Try full demo → open roommate group; currency-separated sample balances visible.
- [ ] **F01** — Add a valid equal $100 bill; shares/balances conserved.
- [ ] **F01** — (API) duplicate `split_with` returns 400.
- [ ] **F10** — Old receipt date lands in current save month.
- [ ] **F07** — Failed save keeps pending upload reusable.
- [ ] **F06** — Two tabs; edit refreshes via `/sync` or SSE.
- [ ] **F02** — Password change revokes old JWT (non-demo / pytest).
- [ ] **Reset demo** — fresh template; no cross-sandbox bleed.

## Pass criteria

All checked items pass on the build under test. Automated gate:

```powershell
cd "Expense Splitter Web"
python -m pytest test_demo_bills_payments.py test_p1_audit_fixes.py test_regression7.py::test_demo_preserves_real_session_on_exit -v --tb=short
```

**Automated status (2026-10-27):** `test_demo_bills_payments.py` — 4 passed (CRUD reflect, payments, session source, demo sandbox CRUD).

## Notes

| Area | Intentional product rule |
|------|--------------------------|
| F03 / payments | Lender records received → auto-confirm. Debtor marks → pending until lender confirms. |
| Friends group debt | Live Who-pays-whom for shared group periods (unified GROUP bills). |
| Exit demo | Always returns to `/` (landing). |
| Cache bust | `api.js` / `styles.css` / `group.js` use `?v=20261027demoSession`. |
