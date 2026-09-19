let me; let friends = []; let activeFriend = null; let activeLedger = "current";
const money = (value, currency) => ShareSquad.formatMoney(Math.abs(value), currency);
const cents = (value) => Math.round((Number(value) || 0) * 100) / 100;
const todayISO = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };

function splitRow(split) {
  const person = split.participants[0]; if (!person) return null;
  let friend; let balance;
  if (split.is_payer) {
    friend = { id: person.account_id || null, name: person.display_name || person.email || "Friend" };
    // A Friends balance stays open until the month is confirmed by both people.
    balance = person.share_amount;
  } else if (person.is_me) {
    friend = { id: split.payer_account_id, name: split.payer_name || "Friend" };
    balance = -person.share_amount;
  }
  return friend && friend.id !== me.account.id ? { split, person, friend, balance } : null;
}

function groupByFriend(splits) {
  const map = new Map();
  splits.map(splitRow).filter(Boolean).forEach((row) => {
    const key = row.friend.id ? `account-${row.friend.id}` : `pending-${row.friend.name.toLowerCase()}`;
    const entry = map.get(key) || { key, friend: row.friend, balance: 0, currency: row.split.currency, rows: [] };
    entry.balance += row.balance;
    entry.rows.push(row);
    map.set(key, entry);
  });
  return [...map.values()].sort((a, b) => Math.abs(b.balance) - Math.abs(a.balance));
}

function statusFor(friend) {
  const balance = cents(friend.balance);
  if (balance > 0) return { tone: "text-green-800 bg-green-600/10 border-green-700/20", text: `${ShareSquad.escapeHtml(friend.friend.name)} owes you ${money(balance, friend.currency)}`, hint: "They owe you" };
  if (balance < 0) return { tone: "text-red-800 bg-red-600/10 border-red-700/20", text: `You owe ${ShareSquad.escapeHtml(friend.friend.name)} ${money(balance, friend.currency)}`, hint: "You owe them" };
  return { tone: "text-ink/65 bg-ink/5 border-ink/10", text: "No one owes anything", hint: "Settled up" };
}

function statusForBill(row) {
  const balance = cents(row.balance);
  if (balance > 0) return { tone: "text-green-700", text: `+${money(balance, row.split.currency)}`, hint: "They owe you" };
  if (balance < 0) return { tone: "text-red-700", text: `−${money(balance, row.split.currency)}`, hint: "You owe them" };
  return { tone: "text-ink/60", text: "Settled", hint: "Settled up" };
}

function renderList() {
  const grid = $("#friendsGrid");
  if (!friends.length) { grid.innerHTML = '<div class="bb-card sm:col-span-2 lg:col-span-3 text-center py-10"><p class="font-mono font-bold">No friend splits yet</p><p class="text-sm text-ink/60 mt-2">Start a one-to-one split and share its private link. It appears here after the other person joins.</p></div>'; return; }
  grid.innerHTML = friends.map((friend) => { const state = statusFor(friend); return `<button class="bb-card text-left border ${state.tone} hover:shadow-md transition-shadow" data-friend-key="${friend.key}"><p class="font-mono font-bold text-base">${ShareSquad.escapeHtml(friend.friend.name)}</p><p class="text-sm font-semibold mt-3">${state.text}</p><p class="text-[10px] font-mono mt-2 opacity-70">${friend.rows.length} bill${friend.rows.length === 1 ? "" : "s"} in history · View history →</p></button>`; }).join("");
  grid.querySelectorAll("[data-friend-key]").forEach((button) => button.onclick = () => openFriend(button.dataset.friendKey));
}

function renderValues() {
  const friendPaid = $("#friendPaidBy").value === "friend";
  $("#friendValues").classList.toggle("hidden", $("#friendExpenseType").value === "EQUAL");
  const name = activeFriend?.friend?.name || "Friend";
  $("#friendPayerValueLabel").textContent = friendPaid ? `${name}'s value` : "Your value";
  $("#friendOtherValueLabel").textContent = friendPaid ? "Your value" : `${name}'s value`;
}

function monthName(month) {
  return new Date(`${month}-01T00:00:00`).toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

function renderLedger(friend) {
  const currentMonth = new Date().toISOString().slice(0, 7);
  const selectedMonth = activeLedger === "current" ? currentMonth : $("#historyMonth").value;
  const rows = friend.rows.filter((row) => String(row.split.bill_date).slice(0, 7) === selectedMonth).sort((a, b) => String(b.split.bill_date).localeCompare(String(a.split.bill_date)));
  const monthBalance = cents(rows.reduce((sum, row) => sum + row.balance, 0));
  $("#ledgerTitle").textContent = activeLedger === "current" ? `This month · ${monthName(currentMonth)}` : `History · ${monthName(selectedMonth)}`;
  $("#historyMonth").classList.toggle("hidden", activeLedger !== "history");
  [["#currentBillsTab", activeLedger === "current"], ["#historyBillsTab", activeLedger === "history"]].forEach(([selector, isActive]) => {
    const tab = $(selector);
    tab.classList.toggle("bb-btn-primary", isActive);
    tab.classList.toggle("bb-btn-secondary", !isActive);
  });
  $("#friendLedger").innerHTML = rows.length ? rows.map((row) => { const bill = statusForBill(row); return `<div class="member-row"><div><p class="font-semibold text-sm">${ShareSquad.escapeHtml(row.split.title)}</p><p class="text-xs text-ink/55">${ShareSquad.formatDate(row.split.bill_date)} · ${row.split.is_payer ? "You paid" : `${ShareSquad.escapeHtml(friend.friend.name)} paid`} ${money(row.split.total_amount, row.split.currency)}</p></div><div class="text-right"><p class="text-xs font-mono font-bold ${bill.tone}">${bill.text}</p><p class="text-[10px] text-ink/50">${bill.hint}</p></div></div>`; }).join("") : '<p class="text-sm text-ink/55 py-3">No bills in this month.</p>';
  const settlement = friend.settlements?.get(selectedMonth);
  const panel = $("#monthlySettlement");
  if (!rows.length || monthBalance === 0) {
    panel.innerHTML = '<p class="text-xs text-ink/55">No settlement is needed for this month.</p>';
  } else if (settlement?.status === "SETTLED") {
    panel.innerHTML = '<p class="text-xs font-mono text-ink/55">✓ This month is settled by both people.</p>';
  } else {
    const payerId = settlement?.payer_account_id || (monthBalance < 0 ? me.account.id : friend.friend.id);
    const iOwe = payerId === me.account.id;
    const confirmed = iOwe ? settlement?.payer_confirmed : settlement?.recipient_confirmed;
    const otherConfirmed = iOwe ? settlement?.recipient_confirmed : settlement?.payer_confirmed;
    const action = iOwe ? "paid" : "received";
    const label = iOwe ? "Confirm I paid" : "Settle this month (payment received)";
    const note = confirmed ? (otherConfirmed ? "Settlement is being finalized…" : "Your payment confirmation is saved. Waiting for the receiver to settle the month.") : iOwe ? "Your confirmation waits for the receiver to confirm payment." : "As the receiver, you can settle this month once payment is received.";
    panel.innerHTML = `<div class="flex flex-wrap items-center justify-between gap-3"><p class="text-xs text-ink/60 max-w-sm">${note}</p>${confirmed ? '<span class="text-[10px] font-mono text-brand">Confirmed by you</span>' : `<button type="button" class="bb-btn-primary !py-1.5 !px-3 text-[10px] confirm-month-settlement" data-month="${selectedMonth}" data-action="${action}">${label}</button>`}</div>`;
    panel.querySelector(".confirm-month-settlement")?.addEventListener("click", () => confirmMonthSettlement(friend, selectedMonth, action));
  }
}

function setupHistory(friend) {
  const months = [...new Set(friend.rows.map((row) => String(row.split.bill_date).slice(0, 7)))].sort().reverse();
  const currentMonth = new Date().toISOString().slice(0, 7);
  const previousMonth = months.find((month) => month !== currentMonth) || months[0] || currentMonth;
  $("#historyMonth").innerHTML = months.map((month) => `<option value="${month}">${monthName(month)}</option>`).join("");
  $("#historyMonth").value = previousMonth;
  activeLedger = "current";
  renderLedger(friend);
}

function openFriend(key) {
  const friend = friends.find((item) => item.key === key); if (!friend) return;
  activeFriend = friend;
  $("#friendsListPane").classList.add("hidden"); $("#friendDetailPane").classList.remove("hidden");
  const state = statusFor(friend); const balance = cents(friend.balance); $("#friendName").textContent = friend.friend.name; $("#friendBalance").innerHTML = state.text; $("#friendBalance").className = `font-mono font-bold text-xl mt-3 ${balance > 0 ? "text-green-700" : balance < 0 ? "text-red-700" : "text-ink/60"}`; $("#friendBalanceHelp").textContent = balance > 0 ? "Green means they should pay you overall." : balance < 0 ? "Red means you should pay them overall." : "Grey means you are settled overall.";
  setupHistory(friend);
  $("#friendExpensePanel").classList.toggle("hidden", !friend.friend.id); $("#friendExpenseForm").dataset.friendId = friend.friend.id || ""; $("#friendPaidBy").options[1].textContent = `${friend.friend.name} paid`; ShareSquad.fillCurrencySelect($("#friendExpenseCurrency"), friend.currency); renderValues();
}

async function refresh() {
  me = await ShareSquad.api("/auth/me");
  friends = groupByFriend(await ShareSquad.api("/quick-splits"));
  await Promise.all(friends.filter((friend) => friend.friend.id).map(async (friend) => {
    const settlements = await ShareSquad.api(`/friends/${friend.friend.id}/settlements`);
    friend.settlements = new Map(settlements.map((settlement) => [settlement.month_key, settlement]));
    friend.balance = friend.rows.filter((row) => friend.settlements.get(String(row.split.bill_date).slice(0, 7))?.status !== "SETTLED").reduce((sum, row) => sum + row.balance, 0);
  }));
  renderList();
}

async function confirmMonthSettlement(friend, month, action) {
  try {
    await ShareSquad.api(`/friends/${friend.friend.id}/settlements/confirm`, { method: "POST", body: JSON.stringify({ month_key: month, action }) });
    ShareSquad.showToast(action === "paid" ? "Your payment confirmation is saved" : "Your receipt confirmation is saved", "success");
    await refresh();
    openFriend(friend.key);
  } catch (err) {
    ShareSquad.showToast(err.message || "Could not confirm settlement", "error");
  }
}
$("#backFriends").onclick = () => { $("#friendDetailPane").classList.add("hidden"); $("#friendsListPane").classList.remove("hidden"); };
$("#currentBillsTab").onclick = () => { activeLedger = "current"; if (activeFriend) renderLedger(activeFriend); };
$("#historyBillsTab").onclick = () => { activeLedger = "history"; if (activeFriend) renderLedger(activeFriend); };
$("#historyMonth").onchange = () => { if (activeFriend) renderLedger(activeFriend); };
$("#openAddBill").onclick = () => { const panel = $("#friendExpensePanel"); if (panel.classList.contains("hidden")) return; panel.scrollIntoView({ behavior: "smooth", block: "start" }); setTimeout(() => $("#friendExpenseTitle").focus(), 350); };
$("#friendExpenseType").onchange = renderValues;
$("#friendPaidBy").onchange = renderValues;
$("#friendExpenseForm").onsubmit = async (event) => { event.preventDefault(); const friendId = Number(event.currentTarget.dataset.friendId); const type = $("#friendExpenseType").value; const friendPaid = $("#friendPaidBy").value === "friend"; try { await ShareSquad.api("/quick-splits", { method: "POST", body: JSON.stringify({ title: $("#friendExpenseTitle").value.trim(), total_amount: Number($("#friendExpenseAmount").value), currency: $("#friendExpenseCurrency").value, bill_date: $("#friendExpenseDate").value, category: "General", split_type: type, payer_value: type === "EQUAL" ? null : Number($("#friendPayerValue").value), payer_account_id: friendPaid ? friendId : null, silent_invite: true, participants: [{ account_id: friendPaid ? me.account.id : friendId, value: type === "EQUAL" ? null : Number($("#friendOtherValue").value) }] }) }); ShareSquad.showToast("Added to this friend’s history", "success"); $("#friendExpenseForm").reset(); $("#friendExpenseDate").value = todayISO(); await refresh(); openFriend(`account-${friendId}`); } catch (err) { $("#friendMsg").textContent = err.message; $("#friendMsg").className = "text-xs font-mono balance-negative"; } };
(async () => { if (!ShareSquad.getToken()) { location.href = "/login.html"; return; } ShareSquad.fillAllCurrencySelects(); $("#friendExpenseDate").max = todayISO(); $("#friendExpenseDate").value = todayISO(); renderValues(); await refresh(); if (window.lucide) lucide.createIcons(); })();
