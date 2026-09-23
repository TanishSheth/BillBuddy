document.querySelectorAll('a[href="/"]').forEach((link) => { link.href = "/index.html"; });
document.querySelectorAll(".site-nav .nav-tab").forEach((link) => {
  if (link.getAttribute("href") === "/index.html") { link.querySelector("span").textContent = "Expenses"; link.querySelector("svg")?.setAttribute("data-lucide", "layout-dashboard"); }
  if (link.getAttribute("href") === "/personal.html") link.querySelector("span").textContent = "Personal";
});
let me; let friends = []; let activeFriend = null; let activeLedger = "current";
const money = (value, currency) => ShareSquad.formatMoney(Math.abs(value), currency);
const cents = (value) => Math.round((Number(value) || 0) * 100) / 100;
const todayISO = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
function friendAvatar(friend) { const name = ShareSquad.escapeHtml(friend.name); return `<span class="avatar-initials">${ShareSquad.initials(friend.name)}</span>${friend.hasAvatar && friend.id ? `<img class="person-avatar hidden" alt="${name}" data-friend-avatar="${friend.id}">` : ""}`; }
function loadFriendAvatars(root = document) { root.querySelectorAll("[data-friend-avatar]").forEach((img) => ShareSquad.setAuthImage(img, `/friends/${img.dataset.friendAvatar}/avatar`).then(() => { if (!img.classList.contains("hidden")) img.previousElementSibling?.classList.add("hidden"); })); }

function splitRow(split) {
  const person = split.participants[0]; if (!person) return null;
  let friend; let balance;
  if (split.is_payer) {
    friend = { id: person.account_id || null, name: person.display_name || person.email || "Friend", hasAvatar: !!person.has_avatar };
    // A Friends balance stays open until the month is confirmed by both people.
    balance = person.share_amount;
  } else if (person.is_me) {
    friend = { id: split.payer_account_id, name: split.payer_name || "Friend", hasAvatar: !!split.payer_has_avatar };
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
  if (!friends.length) { grid.innerHTML = '<div class="bb-card sm:col-span-2 lg:col-span-3 text-center py-10"><p class="font-mono font-bold">No friends yet</p><p class="text-sm text-ink/60 mt-2">Add a friend by email or private link. Group members will also appear here automatically.</p></div>'; return; }
  grid.innerHTML = friends.map((friend) => `<article class="bb-card border border-ink/10"><div class="flex items-center justify-between gap-3"><button class="text-left grow flex items-center gap-3" data-friend-key="${friend.key}">${friendAvatar(friend.friend)}<p class="font-mono font-bold text-base">${ShareSquad.escapeHtml(friend.friend.name)}</p></button><button type="button" class="remove-friend bb-btn-secondary !py-1.5 !px-2.5 text-[10px] text-stamp border-stamp/30" data-remove-friend="${friend.friend.id}">Remove</button></div><button type="button" class="invite-friend-group bb-btn-secondary w-full !py-1.5 mt-3 text-[10px]" data-invite-friend="${friend.friend.id}">Invite to group</button></article>`).join("");
  loadFriendAvatars(grid);
  grid.querySelectorAll("[data-friend-key]").forEach((button) => button.onclick = () => openFriend(button.dataset.friendKey));
  grid.querySelectorAll("[data-remove-friend]").forEach((button) => button.onclick = () => removeFriend(Number(button.dataset.removeFriend)));
  grid.querySelectorAll("[data-invite-friend]").forEach((button) => button.onclick = () => inviteFriendToGroup(Number(button.dataset.inviteFriend)));
}

async function inviteFriendToGroup(friendId) {
  const friend = friends.find((entry) => entry.friend.id === friendId)?.friend;
  if (!friend) return;
  try {
    const groups = await ShareSquad.api("/groups");
    if (!groups.length) return ShareSquad.showToast("Create a group before inviting a friend.", "error");
    const picker = document.createElement("div");
    picker.className = "fixed inset-0 z-50 bg-ink/50 flex items-center justify-center p-4";
    picker.innerHTML = `<div class="bb-card w-full max-w-sm"><h2 class="font-mono font-bold text-brand">Invite ${ShareSquad.escapeHtml(friend.name)} to a group</h2><select class="bb-input w-full mt-4">${groups.map((group) => `<option value="${group.id}">${ShareSquad.escapeHtml(group.name)}</option>`).join("")}</select><div class="flex gap-2 mt-4"><button class="bb-btn-primary flex-1 send">Send invite</button><button class="bb-btn-secondary cancel">Cancel</button></div></div>`;
    document.body.appendChild(picker);
    picker.querySelector(".cancel").onclick = () => picker.remove();
    picker.querySelector(".send").onclick = async () => { const group = groups.find((item) => item.id === Number(picker.querySelector("select").value)); if (!group) return; try { await ShareSquad.api(`/groups/${group.id}/invites`, { method: "POST", body: JSON.stringify({ email: friend.email }) }); picker.remove(); ShareSquad.showToast(`${friend.name} was invited to ${group.name}.`, "success"); } catch (error) { ShareSquad.showToast(error.message || "Could not invite this friend", "error"); } };
  } catch (error) { ShareSquad.showToast(error.message || "Could not invite this friend", "error"); }
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
  const settlement = friend.settlements?.get(selectedMonth);
  const cutoff = settlement?.status !== "SETTLED" && settlement?.settled_at ? new Date(settlement.settled_at) : null;
  const openRows = cutoff ? rows.filter((row) => new Date(row.split.created_at) > cutoff) : rows;
  const monthBalance = cents(openRows.reduce((sum, row) => sum + row.balance, 0));
  $("#ledgerTitle").textContent = activeLedger === "current" ? `This month · ${monthName(currentMonth)}` : `History · ${monthName(selectedMonth)}`;
  $("#historyMonth").classList.toggle("hidden", activeLedger !== "history");
  [["#currentBillsTab", activeLedger === "current"], ["#historyBillsTab", activeLedger === "history"]].forEach(([selector, isActive]) => {
    const tab = $(selector);
    tab.classList.toggle("bb-btn-primary", isActive);
    tab.classList.toggle("bb-btn-secondary", !isActive);
  });
  $("#friendLedger").innerHTML = rows.length ? rows.map((row) => { const bill = statusForBill(row); return `<div class="member-row"><div><p class="font-semibold text-sm">${ShareSquad.escapeHtml(row.split.title)}</p><p class="text-xs text-ink/55">${ShareSquad.formatDate(row.split.bill_date)} · ${row.split.is_payer ? "You paid" : `${ShareSquad.escapeHtml(friend.friend.name)} paid`} ${money(row.split.total_amount, row.split.currency)}</p></div><div class="text-right"><p class="text-xs font-mono font-bold ${bill.tone}">${bill.text}</p><p class="text-[10px] text-ink/50">${bill.hint}</p></div></div>`; }).join("") : '<p class="text-sm text-ink/55 py-3">No bills in this month.</p>';
  $("#monthlySettlement").innerHTML = `<p class="text-xs text-ink/55">Direct bills here are included in the total above. Record one combined payment there for both group and direct balances.</p>`;
}

function renderConsolidated(friend) {
  const total = friend.consolidated;
  const breakdown = $("#consolidatedBreakdown"); const payments = $("#consolidatedPayments");
  if (!total) { breakdown.innerHTML = ""; payments.innerHTML = ""; return; }
  const sources = total.sources || [];
  breakdown.innerHTML = `<p class="text-[10px] font-mono uppercase tracking-widest text-ink/45">Included in this total</p>${sources.length ? sources.map((source) => `<div class="flex justify-between gap-3 text-xs"><span>${ShareSquad.escapeHtml(source.label)}</span><span class="font-mono ${source.amount > 0 ? "text-green-700" : "text-red-700"}">${source.amount > 0 ? "+" : "−"}${ShareSquad.formatMoney(Math.abs(source.amount), source.currency)}</span></div>`).join("") : '<p class="text-xs text-ink/55">No open balances in this currency.</p>'}`;
  const pending = (total.payments || []).filter((payment) => payment.status === "PENDING");
  const balance = Number(total.balance || 0);
  const iOwe = balance < 0;
  const fullLabel = iOwe ? "Mark full payment" : "Mark full payment received";
  const partialLabel = iOwe ? "Mark partial payment" : "Record partial payment received";
  payments.innerHTML = `${balance ? `<div class="flex flex-wrap gap-2"><button type="button" id="markFullConsolidatedPayment" class="bb-btn-primary !py-1.5 !px-3 text-[10px]">${fullLabel}</button><button type="button" id="showPartialConsolidatedPayment" class="bb-btn-secondary !py-1.5 !px-3 text-[10px]">${partialLabel}</button></div><div id="partialConsolidatedPaymentForm" class="hidden mt-3 flex flex-wrap items-end gap-2"><label class="text-[10px] font-mono text-ink/55">Payment amount<input id="consolidatedPaymentAmount" class="bb-input block !w-32 !py-1.5 mt-1 text-xs" type="number" min="0.01" max="${Math.abs(balance)}" step="0.01" placeholder="0.00"></label><button type="button" id="recordConsolidatedPayment" class="bb-btn-primary !py-1.5 !px-3 text-[10px]">Save partial payment</button></div>` : ""}${pending.map((payment) => `<div class="mt-3 p-2 rounded bg-amber-500/10 text-xs flex items-center justify-between gap-2"><span>${ShareSquad.formatMoney(payment.amount, payment.currency)} awaiting lender confirmation</span>${Number(payment.reported_by_account_id) !== Number(me.account.id) ? `<button class="confirm-consolidated-payment bb-btn-secondary !py-1 !px-2 text-[10px]" data-id="${payment.id}">Confirm received</button>` : '<span class="text-[10px] font-mono text-amber-700">Reported by you</span>'}</div>`).join("")}`;
  const savePayment = async (amount) => {
    if (!Number.isFinite(amount) || amount <= 0 || amount > Math.abs(balance)) return ShareSquad.showToast("Enter an amount up to the open balance.", "error");
    try { await ShareSquad.api(`/friends/${friend.friend.id}/consolidated-payments`, { method: "POST", body: JSON.stringify({ amount, currency: total.currency }) }); ShareSquad.showToast(iOwe ? "Payment request sent to the lender for confirmation." : "Payment received and applied to the balance.", "success"); await refresh(); openFriend(friend.key); }
    catch (err) { ShareSquad.showToast(err.message || "Could not record payment", "error"); }
  };
  $("#markFullConsolidatedPayment")?.addEventListener("click", () => savePayment(Math.abs(balance)));
  $("#showPartialConsolidatedPayment")?.addEventListener("click", () => { $("#partialConsolidatedPaymentForm")?.classList.remove("hidden"); $("#consolidatedPaymentAmount")?.focus(); });
  $("#recordConsolidatedPayment")?.addEventListener("click", () => savePayment(Number($("#consolidatedPaymentAmount")?.value)));
  payments.querySelectorAll(".confirm-consolidated-payment").forEach((button) => button.addEventListener("click", async () => {
    try { await ShareSquad.api(`/friends/${friend.friend.id}/consolidated-payments/${button.dataset.id}/confirm`, { method: "POST" }); ShareSquad.showToast("Payment confirmed and applied to the open balances.", "success"); await refresh(); openFriend(friend.key); }
    catch (err) { ShareSquad.showToast(err.message || "Could not confirm payment", "error"); }
  }));
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
  const state = statusFor(friend); const balance = cents(friend.balance); $("#friendName").textContent = friend.friend.name; $("#friendBalance").innerHTML = state.text; $("#friendBalance").className = `font-mono font-bold text-xl mt-3 ${balance > 0 ? "text-green-700" : balance < 0 ? "text-red-700" : "text-ink/60"}`; $("#friendBalanceHelp").textContent = balance > 0 ? "Green means they should pay you overall." : balance < 0 ? "Red means you should pay them overall." : "Grey means you are settled overall."; renderConsolidated(friend);
  setupHistory(friend);
  $("#friendExpensePanel").classList.toggle("hidden", !friend.friend.id); $("#friendExpenseForm").dataset.friendId = friend.friend.id || ""; $("#friendPaidBy").options[1].textContent = `${friend.friend.name} paid`; ShareSquad.fillCurrencySelect($("#friendExpenseCurrency"), friend.currency); renderValues();
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
  const friend = friends.find((entry) => entry.friend.id === friendId)?.friend;
  if (!friend || !confirm(`Remove ${friend.name} from your Friends list? This will not remove them from any group or delete past expenses.`)) return;
  try { await ShareSquad.api(`/friends/${friendId}`, { method: "DELETE" }); ShareSquad.showToast(`${friend.name} was removed from Friends`, "success"); await refresh(); }
  catch (error) { ShareSquad.showToast(error.message || "Could not remove friend", "error"); }
}

function openFriendInvite() { $("#friendInviteModal").classList.remove("hidden"); $("#friendInviteMsg").textContent = ""; $("#friendInviteEmail").focus(); }
function closeFriendInvite() { $("#friendInviteModal").classList.add("hidden"); }
async function createFriendInvite(email = "") {
  try {
    const result = await ShareSquad.api("/friends/invites", { method: "POST", body: JSON.stringify(email ? { email } : {}) });
    $("#friendInviteMsg").textContent = result.message;
    $("#friendInviteMsg").className = "text-xs font-mono mt-3 balance-positive";
    if (result.invite_url) { $("#friendInviteLink").value = result.invite_url; $("#friendInviteLinkWrap").classList.remove("hidden"); }
    if (result.status === "ACTIVE") { closeFriendInvite(); await refresh(); }
  } catch (error) { $("#friendInviteMsg").textContent = error.message || "Could not add friend"; $("#friendInviteMsg").className = "text-xs font-mono mt-3 balance-negative"; }
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
$("#settingsBtn").onclick = () => { location.href = "/settings.html"; };
$("#logoutBtn").onclick = () => { ShareSquad.removeToken(); ShareSquad.removeStoredGroupId(); location.href = "/login.html"; };
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
$("#sendFriendInviteBtn").onclick = () => { const email = $("#friendInviteEmail").value.trim(); if (!email) { $("#friendInviteMsg").textContent = "Enter an email address, or use Create link."; return; } createFriendInvite(email); };
$("#createFriendLinkBtn").onclick = () => createFriendInvite();
$("#copyFriendInviteBtn").onclick = async () => { try { await navigator.clipboard.writeText($("#friendInviteLink").value); ShareSquad.showToast("Friend invite link copied", "success"); } catch { $("#friendInviteLink").select(); document.execCommand("copy"); } };
$("#friendExpenseForm").onsubmit = async (event) => { event.preventDefault(); const friendId = Number(event.currentTarget.dataset.friendId); const type = $("#friendExpenseType").value; const friendPaid = $("#friendPaidBy").value === "friend"; try { await ShareSquad.api("/quick-splits", { method: "POST", body: JSON.stringify({ title: $("#friendExpenseTitle").value.trim(), total_amount: Number($("#friendExpenseAmount").value), currency: $("#friendExpenseCurrency").value, bill_date: $("#friendExpenseDate").value, category: "General", split_type: type, payer_value: type === "EQUAL" ? null : Number($("#friendPayerValue").value), payer_account_id: friendPaid ? friendId : null, silent_invite: true, participants: [{ account_id: friendPaid ? me.account.id : friendId, value: type === "EQUAL" ? null : Number($("#friendOtherValue").value) }] }) }); ShareSquad.showToast("Added to this friend’s history", "success"); $("#friendExpenseForm").reset(); $("#friendExpenseDate").value = todayISO(); await refresh(); openFriend(`account-${friendId}`); } catch (err) { $("#friendMsg").textContent = err.message; $("#friendMsg").className = "text-xs font-mono balance-negative"; } };
(async () => { if (!ShareSquad.getToken()) { location.href = "/login.html"; return; } ShareSquad.fillAllCurrencySelects(); $("#friendExpenseDate").max = todayISO(); $("#friendExpenseDate").value = todayISO(); renderValues(); try { me = await ShareSquad.api("/auth/me"); $("#headerInitials").textContent = ShareSquad.initials(me.account.display_name); if (me.account.has_avatar) { ShareSquad.setAuthImage($("#headerAvatar"), "/auth/me/avatar"); $("#headerInitials").classList.add("hidden"); } await refresh(); const selectedFriendId = Number(new URLSearchParams(location.search).get("friend")); if (selectedFriendId) openFriend(`account-${selectedFriendId}`); } catch (err) { ShareSquad.showToast(err.message || "Could not load friend splits", "error"); } if (window.lucide) lucide.createIcons(); })();
