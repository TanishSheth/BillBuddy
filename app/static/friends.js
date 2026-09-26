document.querySelectorAll('a[href="/"]').forEach((link) => { link.href = "/index.html"; });
try { sessionStorage.setItem("sharesquad_expense_view", "friends"); } catch (_) {}
let me; let friends = []; let activeFriend = null; let activeLedger = "current"; let editingSplitId = null;
// Opened from Expenses → Friends cards (?friend=). Back returns there, not Manage friends.
let openedFromBalances = Number.isFinite(Number(new URLSearchParams(location.search).get("friend"))) && Number(new URLSearchParams(location.search).get("friend")) > 0;
const money = (value, currency) => ShareSquad.formatMoney(Math.abs(value), currency);
const cents = (value) => Math.round((Number(value) || 0) * 100) / 100;
const todayISO = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
function friendAvatar(friend) { const name = ShareSquad.escapeHtml(friend.name); return `<span class="avatar-initials">${ShareSquad.initials(friend.name)}</span>${friend.hasAvatar && friend.id ? `<img class="person-avatar hidden" alt="${name}" data-friend-avatar="${friend.id}">` : ""}`; }
function loadFriendAvatars(root = document) { root.querySelectorAll("[data-friend-avatar]").forEach((img) => ShareSquad.setAuthImage(img, `/friends/${img.dataset.friendAvatar}/avatar`).then(() => { if (!img.classList.contains("hidden")) img.previousElementSibling?.classList.add("hidden"); })); }

function splitRow(split) {
  const person = split.participants[0]; if (!person) return null;
  let friend; let balance;
  const total = Number(split.total_amount);
  const payerPaid = split.payer_paid_amount == null ? total : Number(split.payer_paid_amount);
  if (split.is_payer) {
    friend = { id: person.account_id || null, name: person.display_name || ShareSquad.maskEmail(person.email) || "Friend", hasAvatar: !!person.has_avatar };
    balance = cents(payerPaid - Number(split.payer_share_amount));
  } else if (person.is_me) {
    friend = { id: split.payer_account_id, name: split.payer_name || "Friend", hasAvatar: !!split.payer_has_avatar };
    balance = cents((total - payerPaid) - Number(person.share_amount));
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

function mergeFriends(splits, contacts) {
  const map = new Map(groupByFriend(splits).map((entry) => [entry.key, entry]));
  contacts.forEach((contact) => {
    const key = `account-${contact.id}`;
    const entry = map.get(key) || { key, friend: { id: contact.id, name: contact.display_name, hasAvatar: contact.has_avatar }, balance: 0, currency: "USD", rows: [] };
    entry.friend = { id: contact.id, name: contact.display_name, email: contact.email, hasAvatar: contact.has_avatar };
    map.set(key, entry);
  });
  return [...map.values()].sort((a, b) => a.friend.name.localeCompare(b.friend.name));
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
  if (!friends.length) { grid.innerHTML = '<div class="bb-card sm:col-span-2 lg:col-span-3 text-center py-10"><p class="font-mono font-bold">No friends yet</p><p class="text-sm text-ink/60 mt-2">Add an existing ShareSquad user by email. Group members will also appear here automatically.</p></div>'; return; }
  grid.innerHTML = friends.map((friend) => `<article class="bb-card border border-ink/10"><div class="flex items-center justify-between gap-3"><button class="text-left grow flex items-center gap-3" data-friend-key="${friend.key}">${friendAvatar(friend.friend)}<p class="font-mono font-bold text-base">${ShareSquad.escapeHtml(friend.friend.name)}</p></button><button type="button" class="remove-friend bb-btn-secondary !py-1.5 !px-2.5 text-[10px] text-stamp border-stamp/30" data-remove-friend="${friend.friend.id}">Remove</button></div><button type="button" class="invite-friend-group bb-btn-secondary w-full !py-1.5 mt-3 text-[10px]" data-invite-friend="${friend.friend.id}">Invite to group</button></article>`).join("");
  loadFriendAvatars(grid);
  grid.querySelectorAll("[data-friend-key]").forEach((button) => button.onclick = () => {
    openedFromBalances = false;
    openFriend(button.dataset.friendKey);
  });
  grid.querySelectorAll("[data-remove-friend]").forEach((button) => button.onclick = () => removeFriend(Number(button.dataset.removeFriend)));
  grid.querySelectorAll("[data-invite-friend]").forEach((button) => button.onclick = () => inviteFriendToGroup(Number(button.dataset.inviteFriend)));
}

async function inviteFriendToGroup(friendId) {
  const friend = friends.find((entry) => entry.friend.id === friendId)?.friend;
  if (!friend) return;
  try {
    const groups = await ShareSquad.api("/groups");
    if (!groups.length) return ShareSquad.showToast("Create a group before inviting a friend.", "error");
    const groupDetails = await Promise.all(groups.map((group) => ShareSquad.api(`/groups/${group.id}`).catch(() => null)));
    const availableGroups = groupDetails.filter((group) => group && !group.members.some((member) => Number(member.account_id) === Number(friend.id) && member.is_active !== false));
    if (!availableGroups.length) return ShareSquad.showToast(`${friend.name} is already in all of your groups.`, "info");
    const picker = document.createElement("div");
    picker.className = "fixed inset-0 z-50 bg-ink/50 flex items-center justify-center p-4";
    picker.innerHTML = `<div class="bb-card w-full max-w-sm"><h2 class="font-mono font-bold text-brand">Invite ${ShareSquad.escapeHtml(friend.name)} to a group</h2><select class="bb-input w-full mt-4">${availableGroups.map((group) => `<option value="${group.id}">${ShareSquad.escapeHtml(group.name)}</option>`).join("")}</select><div class="flex gap-2 mt-4"><button class="bb-btn-primary flex-1 send">Send invite</button><button class="bb-btn-secondary cancel">Cancel</button></div></div>`;
    document.body.appendChild(picker);
    picker.querySelector(".cancel").onclick = () => picker.remove();
    picker.querySelector(".send").onclick = async () => { const group = availableGroups.find((item) => item.id === Number(picker.querySelector("select").value)); if (!group) return; try { await ShareSquad.api(`/groups/${group.id}/invites`, { method: "POST", body: JSON.stringify({ email: friend.email }) }); picker.remove(); ShareSquad.showToast(`${friend.name} was added to ${group.name}.`, "success"); } catch (error) { ShareSquad.showToast(error.message || "Could not add this friend", "error"); } };
  } catch (error) { ShareSquad.showToast(error.message || "Could not invite this friend", "error"); }
}

function renderValues() {
  const paidBy = $("#friendPaidBy").value;
  const friendPaid = paidBy === "friend";
  $("#friendValues").classList.toggle("hidden", $("#friendExpenseType").value === "EQUAL");
  $("#friendPaySplit").classList.toggle("hidden", paidBy !== "both");
  const name = activeFriend?.friend?.name || "Friend";
  $("#friendPayerValueLabel").textContent = friendPaid ? `${name}'s value` : "Your value";
  $("#friendOtherValueLabel").textContent = friendPaid ? "Your value" : `${name}'s value`;
  const total = Number($("#friendExpenseAmount").value) || 0;
  const mine = Number($("#friendMyPaid").value) || 0;
  const theirs = Number($("#friendTheirPaid").value) || 0;
  const notice = $("#friendPayNotice");
  if (notice) notice.textContent = total ? `Paid ${cents(mine + theirs).toFixed(2)} of ${total.toFixed(2)}.` : "Enter the cash each of you paid. The two amounts must add up to the bill.";
}

function monthName(month) {
  return new Date(`${month}-01T00:00:00`).toLocaleDateString(undefined, { month: "long", year: "numeric" });
}

function canEditBill(split) {
  return !!split?.is_owner;
}

function setExpenseFormMode(editing) {
  editingSplitId = editing ? Number(editing) : null;
  $("#friendExpenseEditId").value = editingSplitId || "";
  $("#friendExpenseHeading").textContent = editingSplitId ? "Edit bill" : "Add another bill";
  $("#friendExpenseHelp").textContent = editingSplitId
    ? "Save changes to update this private bill for both of you."
    : "Both people can add, edit, or delete a private bill from here.";
  $("#friendExpenseSubmit").textContent = editingSplitId ? "Save changes" : "Add private bill";
  $("#cancelFriendExpenseEdit")?.classList.toggle("hidden", !editingSplitId);
  $("#friendMsg").textContent = "";
}

function resetExpenseForm() {
  $("#friendExpenseForm").reset();
  $("#friendExpenseDate").value = todayISO();
  if (activeFriend) ShareSquad.fillCurrencySelect($("#friendExpenseCurrency"), activeFriend.currency);
  setExpenseFormMode(null);
  renderValues();
}

function startEditBill(row) {
  const split = row.split;
  if (!canEditBill(split)) {
    return ShareSquad.showToast("This bill can no longer be edited after a payment was marked.", "info");
  }
  const panel = $("#friendExpensePanel");
  if (panel.classList.contains("hidden")) return;
  setExpenseFormMode(split.id);
  $("#friendExpenseTitle").value = split.title || "";
  $("#friendExpenseAmount").value = split.total_amount;
  ShareSquad.fillCurrencySelect($("#friendExpenseCurrency"), split.currency);
  $("#friendExpenseDate").value = String(split.bill_date).slice(0, 10);
  $("#friendExpenseDate")._flatpickr?.setDate($("#friendExpenseDate").value, true);
  let paidBy = "me";
  if (split.payer_paid_amount != null) paidBy = "both";
  else if (!split.is_payer) paidBy = "friend";
  $("#friendPaidBy").value = paidBy;
  if (paidBy === "both") {
    const mine = split.is_payer ? Number(split.payer_paid_amount) : cents(Number(split.total_amount) - Number(split.payer_paid_amount));
    const theirs = cents(Number(split.total_amount) - mine);
    $("#friendMyPaid").value = mine;
    $("#friendTheirPaid").value = theirs;
  }
  $("#friendExpenseType").value = split.split_type || "EQUAL";
  const meParticipant = split.participants.find((item) => item.is_me);
  const friendParticipant = split.participants.find((item) => !item.is_me);
  if ((split.split_type || "EQUAL") !== "EQUAL") {
    if (paidBy === "friend") {
      const myValue = meParticipant?.split_value;
      $("#friendPayerValue").value = split.split_type === "PERCENTAGE" && myValue != null
        ? cents(100 - Number(myValue))
        : split.payer_share_amount;
      $("#friendOtherValue").value = myValue ?? meParticipant?.share_amount ?? "";
    } else {
      const theirValue = friendParticipant?.split_value;
      $("#friendPayerValue").value = split.split_type === "PERCENTAGE" && theirValue != null
        ? cents(100 - Number(theirValue))
        : split.payer_share_amount;
      $("#friendOtherValue").value = theirValue ?? friendParticipant?.share_amount ?? "";
    }
  } else {
    $("#friendPayerValue").value = "";
    $("#friendOtherValue").value = "";
  }
  renderValues();
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
  setTimeout(() => $("#friendExpenseTitle").focus(), 350);
}

async function deleteBill(row) {
  const split = row.split;
  if (!split?.is_owner) return ShareSquad.showToast("You cannot delete this bill.", "error");
  if (!await ShareSquad.confirmDialog({
    title: `Delete “${split.title}”?`,
    message: "This private bill will be removed from both of your histories. You can undo for a few seconds.",
    confirmLabel: "Delete bill",
  })) return;
  const friendKey = activeFriend?.key;
  const snapshotRows = activeFriend ? activeFriend.rows.slice() : [];
  ShareSquad.runWithUndo({
    message: `Bill “${split.title}” deleted`,
    onHide: () => {
      if (editingSplitId === split.id) resetExpenseForm();
      if (activeFriend) {
        activeFriend.rows = activeFriend.rows.filter((item) => item.split.id !== split.id);
        renderLedger(activeFriend);
      }
    },
    onUndo: async () => {
      if (activeFriend) activeFriend.rows = snapshotRows;
      await refresh();
      if (friendKey) openFriend(friendKey);
    },
    onCommit: async () => {
      await ShareSquad.api(`/quick-splits/${split.id}`, { method: "DELETE" });
      await refresh();
      if (friendKey) openFriend(friendKey);
    },
  });
}

function renderLedger(friend) {
  const currentMonth = new Date().toISOString().slice(0, 7);
  const selectedMonth = activeLedger === "current" ? currentMonth : $("#historyMonth").value;
  const rows = friend.rows.filter((row) => String(row.split.bill_date).slice(0, 7) === selectedMonth);
  // Shared group bills are view-only history; they never change the Friend balance.
  const groupRows = (friend.groupContributions || []).filter((row) => String(row.bill_date).slice(0, 7) === selectedMonth);
  const entries = [
    ...rows.map((row) => ({ kind: "direct", date: row.split.bill_date, row })),
    ...groupRows.map((row) => ({ kind: "group", date: row.bill_date, row })),
  ].sort((a, b) => String(b.date).localeCompare(String(a.date)));
  $("#ledgerTitle").textContent = activeLedger === "current" ? `This month · ${monthName(currentMonth)}` : `History · ${monthName(selectedMonth)}`;
  $("#historyMonth").classList.toggle("hidden", activeLedger !== "history");
  [["#currentBillsTab", activeLedger === "current"], ["#historyBillsTab", activeLedger === "history"]].forEach(([selector, isActive]) => {
    const tab = $(selector);
    tab.classList.toggle("bb-btn-primary", isActive);
    tab.classList.toggle("bb-btn-secondary", !isActive);
  });
  $("#friendLedger").innerHTML = entries.length ? entries.map((entry) => {
    if (entry.kind === "direct") {
      const bill = statusForBill(entry.row);
      const split = entry.row.split;
      const editable = canEditBill(split);
      const actions = split.is_owner
        ? `<div class="flex justify-end gap-1.5 mt-2"><button type="button" class="edit-friend-bill bb-btn-secondary !py-1 !px-2 text-[10px]" data-split-id="${split.id}" ${editable ? "" : "disabled title=\"Payment already marked\""}>Edit</button><button type="button" class="delete-friend-bill bb-btn-secondary !py-1 !px-2 text-[10px] text-stamp border-stamp/30" data-split-id="${split.id}">Delete</button></div>`
        : "";
      return `<div class="member-row" data-direct-split="${split.id}"><div class="min-w-0"><p class="font-semibold text-sm">${ShareSquad.escapeHtml(split.title)}</p><p class="text-xs text-ink/55">${ShareSquad.formatDate(split.bill_date)} · ${split.payer_paid_amount == null ? (split.is_payer ? "You paid" : `${ShareSquad.escapeHtml(friend.friend.name)} paid`) : "You both paid"} ${money(split.total_amount, split.currency)}</p>${actions}</div><div class="text-right shrink-0"><p class="text-xs font-mono font-bold ${bill.tone}">${bill.text}</p><p class="text-[10px] text-ink/50">${bill.hint}</p></div></div>`;
    }
    const contribution = entry.row;
    return `<button type="button" class="member-row group-ledger-row w-full text-left" data-group-id="${contribution.group_id}" data-expense-id="${contribution.expense_id}"><div><p class="font-semibold text-sm">${ShareSquad.escapeHtml(contribution.title)}</p><p class="text-xs text-ink/55"><span class="group-source-label">From ${ShareSquad.escapeHtml(contribution.group_name)}</span> · ${ShareSquad.formatDate(contribution.bill_date)} · view only</p></div><div class="text-right"><p class="text-[10px] font-mono text-brand">View in group</p></div></button>`;
  }).join("") : '<p class="text-sm text-ink/55 py-3">No bills in this month.</p>';
  $("#friendLedger").querySelectorAll(".group-ledger-row").forEach((button) => button.addEventListener("click", () => {
    ShareSquad.setStoredGroupId(button.dataset.groupId);
    window.location.href = `/group.html?group=${encodeURIComponent(button.dataset.groupId)}&tab=expenses&focus=expense-${encodeURIComponent(button.dataset.expenseId)}`;
  }));
  $("#friendLedger").querySelectorAll(".edit-friend-bill").forEach((button) => button.addEventListener("click", (event) => {
    event.stopPropagation();
    const row = friend.rows.find((item) => String(item.split.id) === String(button.dataset.splitId));
    if (row) startEditBill(row);
  }));
  $("#friendLedger").querySelectorAll(".delete-friend-bill").forEach((button) => button.addEventListener("click", (event) => {
    event.stopPropagation();
    const row = friend.rows.find((item) => String(item.split.id) === String(button.dataset.splitId));
    if (row) deleteBill(row);
  }));
  $("#monthlySettlement").innerHTML = `<p class="text-[10px] text-ink/55">Shared group bills above are view-only (they are already rolled into Who pays whom in the total). Direct bills count on their own. Open a group bill to edit it there.</p>`;
}

function renderConsolidated(friend) {
  const total = friend.consolidated;
  const breakdown = $("#consolidatedBreakdown"); const payments = $("#consolidatedPayments");
  if (!total) { breakdown.innerHTML = ""; payments.innerHTML = ""; return; }
  const sources = total.sources || [];
  breakdown.innerHTML = `<p class="text-[10px] font-mono uppercase tracking-widest text-ink/50 mb-2">Balance breakdown</p><p class="text-[10px] text-ink/55 mb-3">Total = group Who pays whom (for this pair) + private direct bills. Individual shared group expenses in the ledger are view-only and are not added again.</p><div id="consolidatedBreakdownRows" class="space-y-2">${sources.length ? sources.map((source) => {
    const ready = source.settlement_ready !== false;
    const hint = source.kind === "GROUP" ? (ready ? " · counts · payable" : " · counts · payable after group settles") : " · counts";
    return `<div class="flex justify-between gap-3 text-xs"><span>${ShareSquad.escapeHtml(source.label)}<span class="text-ink/40">${hint}</span></span><span class="font-mono ${source.amount > 0 ? "text-green-700" : "text-red-700"}">${source.amount > 0 ? "+" : "−"}${ShareSquad.formatMoney(Math.abs(source.amount), source.currency)}</span></div>`;
  }).join("") : '<p class="text-xs text-ink/55">No open Who-pays-whom or direct balances in this currency.</p>'}</div>`;
  const pending = (total.payments || []).filter((payment) => payment.status === "PENDING");
  const balance = Number(total.balance || 0);
  const settleableBalance = Number(total.settleable_balance || 0);
  const iOwe = settleableBalance < 0;
  const fullLabel = iOwe ? "Mark full payment" : "Mark full payment received";
  const partialLabel = iOwe ? "Mark partial payment" : "Record partial payment received";
  const waitMsg = balance && !settleableBalance
    ? '<p class="text-xs text-ink/55">Part of this total is from an open group period. It becomes payable from Friends after that period is settled.</p>'
    : (!balance ? '<p class="text-xs text-ink/55">No balance to settle with this person.</p>' : "");
  payments.innerHTML = `${settleableBalance ? `<div class="flex flex-wrap gap-2"><button type="button" id="markFullConsolidatedPayment" class="bb-btn-primary !py-1.5 !px-3 text-[10px]">${fullLabel}</button><button type="button" id="showPartialConsolidatedPayment" class="bb-btn-secondary !py-1.5 !px-3 text-[10px]">${partialLabel}</button></div><div id="partialConsolidatedPaymentForm" class="hidden mt-3 flex flex-wrap items-end gap-2"><label class="text-[10px] font-mono text-ink/55">Payment amount<input id="consolidatedPaymentAmount" class="bb-input block !w-32 !py-1.5 mt-1 text-xs" type="number" min="0.01" max="${Math.abs(settleableBalance)}" step="0.01" placeholder="0.00"></label><button type="button" id="recordConsolidatedPayment" class="bb-btn-primary !py-1.5 !px-3 text-[10px]">Save partial payment</button></div>` : waitMsg}${pending.map((payment) => `<div class="mt-3 p-2 rounded bg-amber-500/10 text-xs flex items-center justify-between gap-2"><span>${ShareSquad.formatMoney(payment.amount, payment.currency)} older pending payment</span><button class="confirm-consolidated-payment bb-btn-secondary !py-1 !px-2 text-[10px]" data-id="${payment.id}">Apply now</button></div>`).join("")}`;
  const savePayment = async (amount) => {
    if (!Number.isFinite(amount) || amount <= 0 || amount > Math.abs(settleableBalance)) return ShareSquad.showToast("Enter an amount up to the payment-ready balance.", "error");
    try { await ShareSquad.api(`/friends/${friend.friend.id}/consolidated-payments`, { method: "POST", body: JSON.stringify({ amount, currency: total.currency }) }); ShareSquad.showToast("Payment recorded and applied to the balance.", "success"); await refresh(); openFriend(friend.key); }
    catch (err) { ShareSquad.showToast(err.message || "Could not record payment", "error"); }
  };
  $("#markFullConsolidatedPayment")?.addEventListener("click", () => savePayment(Math.abs(settleableBalance)));
  $("#showPartialConsolidatedPayment")?.addEventListener("click", () => { $("#partialConsolidatedPaymentForm")?.classList.remove("hidden"); $("#consolidatedPaymentAmount")?.focus(); });
  $("#recordConsolidatedPayment")?.addEventListener("click", () => savePayment(Number($("#consolidatedPaymentAmount")?.value)));
  payments.querySelectorAll(".confirm-consolidated-payment").forEach((button) => button.addEventListener("click", async () => {
    try { await ShareSquad.api(`/friends/${friend.friend.id}/consolidated-payments/${button.dataset.id}/confirm`, { method: "POST" }); ShareSquad.showToast("Payment applied to the open balances.", "success"); await refresh(); openFriend(friend.key); }
    catch (err) { ShareSquad.showToast(err.message || "Could not apply payment", "error"); }
  }));
}

function setupHistory(friend) {
  const months = [...new Set([
    ...friend.rows.map((row) => String(row.split.bill_date).slice(0, 7)),
    ...(friend.groupContributions || []).map((row) => String(row.bill_date).slice(0, 7)),
  ])].sort().reverse();
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
  $("#friendsListPane").classList.add("hidden");
  const pane = $("#friendDetailPane");
  pane.classList.remove("hidden");
  pane.classList.add("sliding-in");
  pane.addEventListener("animationend", () => pane.classList.remove("sliding-in"), { once: true });
  const state = statusFor(friend); const balance = cents(friend.balance); $("#friendName").textContent = friend.friend.name; $("#friendBalance").innerHTML = state.text; $("#friendBalance").className = `font-mono font-bold text-xl mt-3 ${balance > 0 ? "text-green-700" : balance < 0 ? "text-red-700" : "text-ink/60"}`; $("#friendBalanceHelp").textContent = balance > 0 ? "Green: they owe you (Who pays whom + private bills)." : balance < 0 ? "Red: you owe them (Who pays whom + private bills)." : "Settled on Who pays whom + private bills. Shared group bills below are view-only."; renderConsolidated(friend);
  setupHistory(friend);
  $("#friendExpensePanel").classList.toggle("hidden", !friend.friend.id); $("#friendExpenseForm").dataset.friendId = friend.friend.id || ""; $("#friendPaidBy").options[1].textContent = `${friend.friend.name} paid`; ShareSquad.fillCurrencySelect($("#friendExpenseCurrency"), friend.currency); resetExpenseForm();
}

async function refresh() {
  if (!me) me = await ShareSquad.api("/auth/me");
  const [splits, contacts] = await Promise.all([ShareSquad.api("/quick-splits"), ShareSquad.api("/friends")]);
  friends = mergeFriends(splits, contacts);
  // Show the split immediately. A settlement lookup is helpful metadata, but
  // must never prevent a newly-created friend card from appearing.
  renderList();
  await Promise.all(friends.filter((friend) => friend.friend.id).map(async (friend) => {
    const settlements = await ShareSquad.api(`/friends/${friend.friend.id}/settlements`).catch(() => []);
    friend.consolidated = await ShareSquad.api(`/friends/${friend.friend.id}/consolidated-balance?currency=USD`).catch(() => null);
    friend.groupContributions = await ShareSquad.api(`/friends/${friend.friend.id}/group-contributions`).catch(() => []);
    friend.settlements = new Map(settlements.map((settlement) => [settlement.month_key, settlement]));
    friend.balance = friend.consolidated ? Number(friend.consolidated.balance) : friend.rows.reduce((sum, row) => {
      const settlement = friend.settlements.get(String(row.split.bill_date).slice(0, 7));
      if (settlement?.status === "SETTLED") return sum;
      if (settlement?.settled_at && new Date(row.split.created_at) <= new Date(settlement.settled_at)) return sum;
      return sum + row.balance;
    }, 0);
  }));
  renderList();
}

async function removeFriend(friendId) {
  const entry = friends.find((item) => item.friend.id === friendId);
  const friend = entry?.friend;
  if (!friend || !await ShareSquad.confirmDialog({
    title: `Remove ${friend.name}?`,
    message: "This only removes them from your Friends list. You can undo for a few seconds.",
    confirmLabel: "Remove friend",
  })) return;
  const snapshot = friends.slice();
  const wasActive = activeFriend?.friend?.id === friendId;
  ShareSquad.runWithUndo({
    message: `${friend.name} was removed from Friends`,
    onHide: () => {
      friends = friends.filter((item) => item.friend.id !== friendId);
      renderList();
      if (wasActive) {
        activeFriend = null;
        if (openedFromBalances) {
          location.href = "/index.html?view=friends";
          return;
        }
        $("#friendDetailPane")?.classList.add("hidden");
        $("#friendsListPane")?.classList.remove("hidden");
      }
    },
    onUndo: async () => {
      friends = snapshot;
      renderList();
      await refresh();
    },
    onCommit: () => ShareSquad.api(`/friends/${friendId}`, { method: "DELETE" }),
  });
}

function openFriendInvite() { $("#friendInviteModal").classList.remove("hidden"); $("#friendInviteMsg").textContent = ""; $("#friendInviteEmail").focus(); }
function closeFriendInvite() { $("#friendInviteModal").classList.add("hidden"); }
async function createFriendInvite(email = "") {
  try {
    const result = await ShareSquad.api("/friends/invites", { method: "POST", body: JSON.stringify(email ? { email } : {}) });
    $("#friendInviteMsg").textContent = result.message;
    $("#friendInviteMsg").className = "text-xs font-mono mt-3 balance-positive";
    if (result.status === "ACTIVE") { closeFriendInvite(); await refresh(); }
  } catch (error) { $("#friendInviteMsg").textContent = error.message || "Could not add friend"; $("#friendInviteMsg").className = "text-xs font-mono mt-3 balance-negative"; }
}

async function confirmMonthSettlement(friend, month, action) {
  try {
    await ShareSquad.api(`/friends/${friend.friend.id}/settlements/confirm`, { method: "POST", body: JSON.stringify({ month_key: month, action }) });
    ShareSquad.showToast(action === "paid" ? "Payment recorded — month settled." : "Receipt recorded — month settled.", "success");
    await refresh();
    openFriend(friend.key);
  } catch (err) {
    ShareSquad.showToast(err.message || "Could not confirm settlement", "error");
  }
}
$("#backFriends").onclick = () => {
  resetExpenseForm();
  if (openedFromBalances) {
    location.href = "/index.html?view=friends";
    return;
  }
  $("#friendDetailPane").classList.add("hidden");
  $("#friendsListPane").classList.remove("hidden");
  history.replaceState({}, "", "/friends.html");
};
$("#currentBillsTab").onclick = () => { activeLedger = "current"; if (activeFriend) renderLedger(activeFriend); };
$("#historyBillsTab").onclick = () => { activeLedger = "history"; if (activeFriend) renderLedger(activeFriend); };
$("#historyMonth").onchange = () => { if (activeFriend) renderLedger(activeFriend); };
$("#openAddBill").onclick = () => { const panel = $("#friendExpensePanel"); if (panel.classList.contains("hidden")) return; resetExpenseForm(); panel.scrollIntoView({ behavior: "smooth", block: "start" }); setTimeout(() => $("#friendExpenseTitle").focus(), 350); };
$("#cancelFriendExpenseEdit")?.addEventListener("click", () => resetExpenseForm());
$("#friendExpenseType").onchange = renderValues;
$("#friendPaidBy").onchange = renderValues;
$("#friendExpenseAmount").oninput = renderValues;
$("#friendMyPaid").oninput = renderValues;
$("#friendTheirPaid").oninput = renderValues;
$("#settingsBtn").onclick = () => { location.href = "/settings.html"; };
$("#logoutBtn")?.addEventListener("click", () => { ShareSquad.removeToken(); ShareSquad.removeStoredGroupId(); location.href = "/login.html"; });
$("#addFriendBtn").onclick = openFriendInvite;
$("#addFriendHeaderBtn")?.addEventListener("click", openFriendInvite);
const legacySplitButton = document.querySelector('a[href="/quick-split.html"]');
if (legacySplitButton) {
  legacySplitButton.querySelector(".btn-label").textContent = "Add friend";
  legacySplitButton.querySelector("svg")?.setAttribute("data-lucide", "user-plus");
  legacySplitButton.addEventListener("click", (event) => { event.preventDefault(); openFriendInvite(); });
}
$("#closeFriendInviteBtn").onclick = closeFriendInvite;
$("#friendInviteModal").onclick = (event) => { if (event.target.id === "friendInviteModal") closeFriendInvite(); };
$("#sendFriendInviteBtn").onclick = () => { const email = $("#friendInviteEmail").value.trim(); if (!email) { $("#friendInviteMsg").textContent = "Enter an existing user's email address."; return; } createFriendInvite(email); };
$("#friendExpenseForm").onsubmit = async (event) => {
  event.preventDefault();
  const friendId = Number(event.currentTarget.dataset.friendId);
  const type = $("#friendExpenseType").value;
  const paidBy = $("#friendPaidBy").value;
  const total = Number($("#friendExpenseAmount").value);
  const editId = Number($("#friendExpenseEditId").value) || editingSplitId;
  let payerAccountId = paidBy === "friend" ? friendId : null;
  let payerPaidAmount = null;
  let participantId = paidBy === "friend" ? me.account.id : friendId;
  if (paidBy === "both") {
    const mine = cents(Number($("#friendMyPaid").value));
    const theirs = cents(Number($("#friendTheirPaid").value));
    if (mine <= 0 || theirs <= 0 || cents(mine + theirs) !== cents(total)) {
      $("#friendMsg").textContent = "The two payments must both be greater than zero and add up to the bill.";
      $("#friendMsg").className = "text-xs font-mono balance-negative";
      return;
    }
    payerAccountId = null;
    participantId = friendId;
    payerPaidAmount = mine;
  }
  const body = {
    title: $("#friendExpenseTitle").value.trim(),
    total_amount: total,
    currency: $("#friendExpenseCurrency").value,
    bill_date: $("#friendExpenseDate").value,
    category: "General",
    split_type: type,
    payer_value: type === "EQUAL" ? null : Number($("#friendPayerValue").value),
    payer_account_id: payerAccountId,
    payer_paid_amount: payerPaidAmount,
    silent_invite: true,
    participants: [{ account_id: participantId, value: type === "EQUAL" ? null : Number($("#friendOtherValue").value) }],
  };
  try {
    if (editId) {
      await ShareSquad.api(`/quick-splits/${editId}`, { method: "PUT", body: JSON.stringify(body) });
      ShareSquad.showToast("Bill updated", "success");
    } else {
      await ShareSquad.api("/quick-splits", { method: "POST", body: JSON.stringify(body) });
      ShareSquad.showToast("Added to this friend’s history", "success");
    }
    resetExpenseForm();
    await refresh();
    openFriend(`account-${friendId}`);
  } catch (err) {
    $("#friendMsg").textContent = err.message;
    $("#friendMsg").className = "text-xs font-mono balance-negative";
  }
};
(async () => { if (!ShareSquad.getToken()) { location.href = "/login.html"; return; } ShareSquad.fillAllCurrencySelects(); $("#friendExpenseDate").max = todayISO(); $("#friendExpenseDate").value = todayISO(); renderValues(); try { me = await ShareSquad.api("/auth/me"); $("#headerInitials").textContent = ShareSquad.initials(me.account.display_name); if (me.account.has_avatar) { ShareSquad.setAuthImage($("#headerAvatar"), "/auth/me/avatar"); $("#headerInitials").classList.add("hidden"); } await refresh(); const selectedFriendId = Number(new URLSearchParams(location.search).get("friend")); if (selectedFriendId) { openedFromBalances = true; openFriend(`account-${selectedFriendId}`); } } catch (err) { ShareSquad.showToast(err.message || "Could not load friend splits", "error"); } if (window.lucide) lucide.createIcons(); })();
