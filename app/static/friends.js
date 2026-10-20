document.querySelectorAll('a[href="/"]').forEach((link) => { link.href = "/index.html"; });
try { sessionStorage.setItem("sharesquad_expense_view", "friends"); } catch (_) {}
let me; let friends = []; let activeFriend = null; let editingSplitId = null;
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
    balance = cents(payerPaid - Number(split.payer_share_amount) - Number(person.paid_amount || 0));
  } else if (person.is_me) {
    friend = { id: split.payer_account_id, name: split.payer_name || "Friend", hasAvatar: !!split.payer_has_avatar };
    balance = cents((total - payerPaid) - Number(person.share_amount) + Number(person.paid_amount || 0));
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

function consolidatedTotals(friend) {
  const totals = friend.consolidatedBalances?.balances || (friend.consolidated ? [friend.consolidated] : []);
  return totals.filter((total) => Math.abs(Number(total.balance || 0)) >= 0.005);
}

function statusFor(friend) {
  const balance = cents(friend.balance);
  if (balance > 0) return { tone: "text-green-800 bg-green-600/10 border-green-700/20", text: `${ShareSquad.escapeHtml(friend.friend.name)} owes you ${money(balance, friend.currency)}`, hint: "They owe you" };
  if (balance < 0) return { tone: "text-red-800 bg-red-600/10 border-red-700/20", text: `You owe ${ShareSquad.escapeHtml(friend.friend.name)} ${money(balance, friend.currency)}`, hint: "You owe them" };
  return { tone: "text-ink/65 bg-ink/5 border-ink/10", text: "No one owes anything", hint: "Settled up" };
}

function statusForBill(row, settled = false) {
  if (settled) return { tone: "text-ink/50 line-through", text: "Paid", hint: "Settled" };
  const balance = cents(row.balance);
  if (balance > 0) return { tone: "text-green-700", text: `+${money(balance, row.split.currency)}`, hint: "They owe you" };
  if (balance < 0) return { tone: "text-red-700", text: `−${money(balance, row.split.currency)}`, hint: "You owe them" };
  return { tone: "text-ink/60", text: "Settled", hint: "Settled up" };
}

function indexPaidDirectBills(friend) {
  // A confirmed Friend payment can carry exact bill IDs (newer payments), or
  // only a direct-balance allocation (older payments). Build one shared index
  // for the whole friend ledger so every card and every month uses the same
  // payment state instead of relying on a row-specific UI shortcut.
  const paidIds = new Set();
  const coveredBySplit = new Map();
  const rows = friend.rows || [];
  const rowById = new Map(rows.map((row) => [Number(row.split.id), row]));
  const amountFor = (row) => Math.abs(Number(row.balance || 0));
  const addCoverage = (splitId, amount) => {
    const row = rowById.get(Number(splitId));
    if (!row) return;
    const total = (coveredBySplit.get(Number(splitId)) || 0) + Math.abs(Number(amount || 0));
    coveredBySplit.set(Number(splitId), total);
    if (total + 0.005 >= amountFor(row)) paidIds.add(Number(splitId));
  };
  const paymentsById = new Map();
  (friend.consolidatedBalances?.balances || []).forEach((total) => {
    (total.payments || []).forEach((payment) => paymentsById.set(Number(payment.id), payment));
  });

  [...paymentsById.values()]
    .filter((payment) => payment.status === "CONFIRMED")
    .sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")) || Number(a.id) - Number(b.id))
    .forEach((payment) => {
      const allocations = (payment.allocations || []).filter((allocation) => allocation.kind === "DIRECT");
      // First honor exact references. These are authoritative and are written
      // by newer payment flows when a bill can be identified directly.
      allocations.filter((allocation) => allocation.quick_split_id).forEach((allocation) => {
        addCoverage(allocation.quick_split_id, allocation.amount);
      });
      // Older payments predate bill IDs in allocations. Reconcile them using
      // the same auditable rules everywhere: same currency, month, debtor,
      // and bill existence at payment time. An exact outstanding amount wins;
      // any remaining amount pays the newest eligible bills first.
      allocations.filter((allocation) => !allocation.quick_split_id).forEach((allocation) => {
        let remaining = Math.abs(Number(allocation.amount || 0));
        if (remaining < 0.005) return;
        const monthKey = String(allocation.month_key || allocation.period_key || "");
        const candidates = rows.filter((row) => {
          const split = row.split;
          const expectedPayerId = split.is_payer ? friend.friend.id : me?.account?.id;
          const intrinsicStatus = String(row.person?.payment_status || "").toUpperCase();
          if (["PAID", "CONFIRMED"].includes(intrinsicStatus)) return false;
          if (String(split.bill_date || "").slice(0, 7) !== monthKey) return false;
          if (String(split.currency || "USD") !== String(payment.currency || "USD")) return false;
          if (Number(payment.payer_account_id) !== Number(expectedPayerId)) return false;
          if (payment.created_at && split.created_at && new Date(split.created_at) > new Date(payment.created_at)) return false;
          return amountFor(row) - (coveredBySplit.get(Number(split.id)) || 0) >= 0.005;
        }).sort((a, b) => (
          String(b.split.bill_date || "").localeCompare(String(a.split.bill_date || "")) ||
          String(b.split.created_at || "").localeCompare(String(a.split.created_at || "")) ||
          Number(b.split.id) - Number(a.split.id)
        ));
        const exact = candidates.find((row) => Math.abs(amountFor(row) - (coveredBySplit.get(Number(row.split.id)) || 0) - remaining) < 0.005);
        if (exact) {
          addCoverage(exact.split.id, remaining);
          return;
        }
        candidates.forEach((row) => {
          if (remaining < 0.005) return;
          const due = amountFor(row) - (coveredBySplit.get(Number(row.split.id)) || 0);
          const applied = Math.min(remaining, due);
          addCoverage(row.split.id, applied);
          remaining -= applied;
        });
      });
    });
  friend.paidDirectSplitIds = paidIds;
}

function isDirectBillSettled(row, friend) {
  // A private bill can be closed by its own confirmed payment before (or
  // independently of) the monthly Friend settlement. Keep it visibly paid in
  // either case so the ledger never loses its strike-through state.
  if (["PAID", "CONFIRMED"].includes(String(row.person?.payment_status || "").toUpperCase())) return true;
  if (String(row.split?.status || "").toUpperCase() === "SETTLED") return true;
  if (friend.paidDirectSplitIds?.has(Number(row.split.id))) return true;
  if (Math.abs(Number(row.balance || 0)) < 0.005) return true;
  const monthKey = String(row.split.bill_date).slice(0, 7);
  const currency = row.split.currency || friend.currency || "USD";
  if (monthDirectFullyCovered(friend, monthKey, currency)) return true;
  const settlement = friend.settlements?.get(monthKey);
  if (!settlement) return false;
  if (settlement.status === "SETTLED") return true;
  return Boolean(settlement.settled_at && row.split.created_at && new Date(row.split.created_at) <= new Date(settlement.settled_at));
}

function monthDirectFullyCovered(friend, monthKey, currency) {
  // When confirmed Friend payments have already zeroed this month's direct
  // source, every private bill in that month belongs in Paid private bills —
  // even if an older payment never wrote quick_split_id / payment_status.
  const totals = friend.consolidatedBalances?.balances || (friend.consolidated ? [friend.consolidated] : []);
  const total = totals.find((item) => String(item.currency || "USD") === String(currency || "USD"));
  if (!total) return false;
  const remaining = (total.sources || [])
    .filter((source) => source.kind === "DIRECT" && String(source.period_key || "") === String(monthKey))
    .reduce((sum, source) => sum + Number(source.amount || 0), 0);
  if (Math.abs(remaining) >= 0.005) return false;
  return (total.payments || []).some((payment) => (
    payment.status === "CONFIRMED"
    && (payment.allocations || []).some((allocation) => (
      allocation.kind === "DIRECT"
      && String(allocation.month_key || allocation.period_key || "") === String(monthKey)
    ))
  ));
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

function friendShareSelected(person) {
  return $(`#friendShare${person}`).dataset.selected === "true";
}

function setFriendShareSelected(person, selected) {
  const chip = $(`#friendShare${person}`);
  chip.dataset.selected = String(selected);
  chip.classList.toggle("selected", selected);
  chip.setAttribute("aria-pressed", String(selected));
}

function renderValues() {
  const paidBy = $("#friendPaidBy").value;
  const friendPaid = paidBy === "friend";
  const shareMe = friendShareSelected("Me");
  const shareFriend = friendShareSelected("Friend");
  const onePersonCovers = shareMe !== shareFriend;
  $("#friendSplitTypeWrap").classList.toggle("hidden", onePersonCovers);
  $("#friendValues").classList.toggle("hidden", onePersonCovers || $("#friendExpenseType").value === "EQUAL");
  $("#friendPaySplit").classList.toggle("hidden", paidBy !== "both");
  const name = activeFriend?.friend?.name || "Friend";
  $("#friendShareFriend").textContent = name;
  $("#friendPayerValueLabel").textContent = friendPaid ? `${name}'s value` : "Your value";
  $("#friendOtherValueLabel").textContent = friendPaid ? "Your value" : `${name}'s value`;
  const total = Number($("#friendExpenseAmount").value) || 0;
  const mine = Number($("#friendMyPaid").value) || 0;
  const theirs = Number($("#friendTheirPaid").value) || 0;
  const notice = $("#friendPayNotice");
  if (notice) notice.textContent = total ? `Paid ${cents(mine + theirs).toFixed(2)} of ${total.toFixed(2)}.` : "Enter the cash each of you paid. The two amounts must add up to the bill.";
  const shareNotice = $("#friendShareNotice");
  if (shareNotice) {
    const money = ShareSquad.formatMoney(total, $("#friendExpenseCurrency").value || "USD");
    const setOutcome = (message, tone = "is-neutral") => {
      shareNotice.className = `text-[11px] mt-2 split-outcome ${tone}`;
      shareNotice.innerHTML = message;
    };
    const splitType = $("#friendExpenseType").value;
    const payerValue = Number($("#friendPayerValue").value) || 0;
    const otherValue = Number($("#friendOtherValue").value) || 0;
    const myValue = friendPaid ? otherValue : payerValue;
    const friendValue = friendPaid ? payerValue : otherValue;
    const totalValues = myValue + friendValue;
    const myShare = splitType === "EQUAL" ? total / 2
      : splitType === "PERCENTAGE" ? total * myValue / 100
      : splitType === "EXACT" ? myValue
      : totalValues ? total * myValue / totalValues : 0;
    const friendShare = splitType === "EQUAL" ? total / 2
      : splitType === "PERCENTAGE" ? total * friendValue / 100
      : splitType === "EXACT" ? friendValue
      : totalValues ? total * friendValue / totalValues : 0;
    const myPaid = Number($("#friendMyPaid").value) || 0;
    const friendPaidAmount = Number($("#friendTheirPaid").value) || 0;
    const bothPaidOutcome = (myOwedShare, friendOwedShare) => {
      if (!total || Math.abs((myPaid + friendPaidAmount) - total) > 0.009) {
        setOutcome("Enter both cash amounts so they add up to the bill total.");
        return;
      }
      const myNet = myPaid - myOwedShare;
      if (Math.abs(myNet) < 0.005) setOutcome("You are settled: each of you paid your own share.");
      else if (myNet > 0) setOutcome(`${ShareSquad.escapeHtml(name)} owes you ${ShareSquad.formatMoney(myNet, $("#friendExpenseCurrency").value || "USD")}.`, "is-lent");
      else setOutcome(`You owe ${ShareSquad.escapeHtml(name)} ${ShareSquad.formatMoney(Math.abs(myNet), $("#friendExpenseCurrency").value || "USD")}.`, "is-owed");
    };
    if (!shareMe && !shareFriend) setOutcome("Choose at least one person.");
    else if (onePersonCovers) {
      const person = shareMe ? "you" : name;
      const payer = paidBy === "friend" ? name : "you";
      const personLabel = person === "you" ? "You" : name;
      if (paidBy === "both") bothPaidOutcome(shareMe ? total : 0, shareFriend ? total : 0);
      else if (person === payer) setOutcome(`${ShareSquad.escapeHtml(personLabel)} paid and covers the full ${money}.`);
      else if (person === "you") setOutcome(`You owe ${ShareSquad.escapeHtml(name)} the full ${money}.`, "is-owed");
      else setOutcome(`${ShareSquad.escapeHtml(name)} owes you the full ${money}.`, "is-lent");
    } else if (paidBy === "both") {
      bothPaidOutcome(myShare, friendShare);
    } else if (paidBy === "me" && total) {
      setOutcome(`${ShareSquad.escapeHtml(name)} owes you ${ShareSquad.formatMoney(friendShare, $("#friendExpenseCurrency").value || "USD")}.`, "is-lent");
    } else if (paidBy === "friend" && total) {
      setOutcome(`You owe ${ShareSquad.escapeHtml(name)} ${ShareSquad.formatMoney(myShare, $("#friendExpenseCurrency").value || "USD")}.`, "is-owed");
    } else setOutcome(`You and ${ShareSquad.escapeHtml(name)} share this cost. Enter the cash amounts and split to see the final result.`);
  }
}

function canEditBill(split, _settled = false) {
  // Paid bills stay editable; the API applies only the difference (F5).
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
  ShareSquad.fillCategorySelect($("#friendExpenseCategory"), "FOOD & DINING", { allowCustom: true });
  $("#friendExpenseCategoryCustom").value = "";
  setFriendShareSelected("Me", true);
  setFriendShareSelected("Friend", true);
  setExpenseFormMode(null);
  renderValues();
}

function startEditBill(row) {
  const split = row.split;
  if (!canEditBill(split, isDirectBillSettled(row, activeFriend))) {
    return ShareSquad.showToast("Only someone on this bill can edit it.", "info");
  }
  const panel = $("#friendExpensePanel");
  if (panel.classList.contains("hidden")) return;
  setExpenseFormMode(split.id);
  if (isDirectBillSettled(row, activeFriend)) {
    $("#friendExpenseHelp").textContent = "This bill was already paid. Saving changes only adjusts the difference on your balance; the original payment stays as recorded.";
  }
  $("#friendExpenseTitle").value = split.title || "";
  $("#friendExpenseAmount").value = split.total_amount;
  ShareSquad.fillCurrencySelect($("#friendExpenseCurrency"), split.currency);
  ShareSquad.fillCategorySelect($("#friendExpenseCategory"), split.category || "General", { allowCustom: true });
  if (!ShareSquad.isKnownCategory(split.category) && $("#friendExpenseCategoryCustom")) {
    $("#friendExpenseCategoryCustom").value = split.category || "";
  }
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
  const myShare = paidBy === "friend" ? Number(meParticipant?.share_amount || 0) : Number(split.payer_share_amount || 0);
  const friendShare = paidBy === "friend" ? Number(split.payer_share_amount || 0) : Number(friendParticipant?.share_amount || 0);
  setFriendShareSelected("Me", myShare > 0);
  setFriendShareSelected("Friend", friendShare > 0);
  if (!friendShareSelected("Me") && !friendShareSelected("Friend")) {
    setFriendShareSelected("Me", true);
    setFriendShareSelected("Friend", true);
  }
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
  if (!canEditBill(split, isDirectBillSettled(row, activeFriend))) {
    return ShareSquad.showToast("Only someone on this bill can delete it.", "info");
  }
  const paid = isDirectBillSettled(row, activeFriend);
  if (!await ShareSquad.confirmDialog({
    title: `Delete “${split.title}”?`,
    message: paid
      ? "This bill was paid. Deleting it keeps the paid amount as a credit on your balance; it will not erase the payment record."
      : "This private bill will be removed from both of your histories. You can undo for a few seconds.",
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

function overallGroupSettlements(friend) {
  /** One open row per shared group: remaining across every unpaid month. Settled groups omitted. */
  const totals = friend.consolidatedBalances?.balances || (friend.consolidated ? [friend.consolidated] : []);
  const contributionCount = new Map();
  (friend.groupContributions || []).forEach((row) => {
    const id = Number(row.group_id);
    contributionCount.set(id, (contributionCount.get(id) || 0) + 1);
  });
  const blocks = [];
  totals.forEach((total) => {
    (total.sources || [])
      .filter((source) => source.kind === "GROUP" && source.group_id != null && Math.abs(Number(source.amount || 0)) >= 0.005)
      .forEach((source) => {
        const groupId = Number(source.group_id);
        blocks.push({
          groupId,
          name: source.label || "Group",
          count: contributionCount.get(groupId) || 0,
          amount: Number(source.amount || 0),
          currency: source.currency || total.currency || "USD",
        });
      });
  });
  return blocks.sort((a, b) => Math.abs(b.amount) - Math.abs(a.amount) || String(a.name).localeCompare(String(b.name)));
}

function signedMoney(amount, currency) {
  const value = Number(amount || 0);
  if (!value) return ShareSquad.formatMoney(0, currency);
  return `${value > 0 ? "+" : "−"}${ShareSquad.formatMoney(Math.abs(value), currency)}`;
}

function directLedgerRow(row, friend) {
  const settled = isDirectBillSettled(row, friend);
  const bill = statusForBill(row, settled);
  const split = row.split;
  const editable = canEditBill(split, settled);
  const actions = editable
    ? `<div class="flex justify-end gap-1.5 mt-2"><button type="button" class="edit-friend-bill bb-btn-secondary !py-1 !px-2 text-[10px]" data-split-id="${split.id}">Edit</button><button type="button" class="delete-friend-bill bb-btn-secondary !py-1 !px-2 text-[10px] text-stamp border-stamp/30" data-split-id="${split.id}">Delete</button></div>`
    : "";
  const settledClass = settled ? "line-through text-ink/45" : "";
  const editedChip = split.edited_after_payment
    ? '<span class="status-chip status-waiting mt-1">Edited after payment</span>'
    : "";
  const history = Array.isArray(split.edit_history) ? split.edit_history : [];
  const historyHtml = history.length
    ? `<details class="mt-1"><summary class="cursor-pointer text-[10px] font-mono text-ink/50">Edit history · ${history.length}</summary><div class="mt-1 space-y-1">${history.slice().reverse().map((entry) => {
      const when = entry?.at ? ShareSquad.formatDate(String(entry.at).slice(0, 10)) : "";
      const who = ShareSquad.escapeHtml(entry?.editor_name || "Someone");
      const oldTotal = Number(entry?.old_total);
      const newTotal = Number(entry?.new_total);
      const delta = Number(entry?.delta);
      const deltaText = Number.isFinite(delta) && Math.abs(delta) >= 0.005
        ? ` · balance ${delta > 0 ? "+" : "−"}${ShareSquad.formatMoney(Math.abs(delta), split.currency)}`
        : "";
      return `<p class="text-[10px] text-ink/55">${when ? `${when} · ` : ""}${who}: ${ShareSquad.formatMoney(oldTotal, split.currency)} → ${ShareSquad.formatMoney(newTotal, split.currency)}${deltaText}</p>`;
    }).join("")}</div></details>`
    : "";
  return `<div class="member-row ${settled ? "opacity-70" : ""}" data-direct-split="${split.id}"><div class="min-w-0"><p class="font-semibold text-sm ${settledClass}">${ShareSquad.escapeHtml(split.title)}</p><p class="text-xs text-ink/55 ${settledClass}">${ShareSquad.formatDate(split.bill_date)} · ${split.payer_paid_amount == null ? (split.is_payer ? "You paid" : `${ShareSquad.escapeHtml(friend.friend.name)} paid`) : "You both paid"} ${money(split.total_amount, split.currency)}</p>${editedChip}${historyHtml}${actions}</div><div class="text-right shrink-0"><p class="text-xs font-mono font-bold ${bill.tone}">${bill.text}</p><p class="text-[10px] text-ink/50">${bill.hint}</p></div></div>`;
}

function renderLedger(friend) {
  const groupBlocks = overallGroupSettlements(friend);
  const rows = friend.rows || [];
  const directSummary = [];
  (friend.consolidatedBalances?.balances || (friend.consolidated ? [friend.consolidated] : [])).forEach((total) => {
    (total.sources || []).filter((source) => source.kind === "DIRECT" && Math.abs(Number(source.amount || 0)) >= 0.005).forEach((source) => {
      directSummary.push({ currency: source.currency || total.currency || "USD", amount: Number(source.amount || 0) });
    });
  });
  const openRows = rows.filter((row) => !isDirectBillSettled(row, friend)).sort((a, b) => String(b.split.bill_date).localeCompare(String(a.split.bill_date)));
  const paidRows = rows.filter((row) => isDirectBillSettled(row, friend)).sort((a, b) => String(b.split.bill_date).localeCompare(String(a.split.bill_date)));
  if ($("#ledgerTitle")) $("#ledgerTitle").textContent = "Overall remaining";
  const groupHtml = groupBlocks.length ? `<section class="space-y-2"><div class="flex items-center justify-between gap-3"><p class="text-[10px] font-mono uppercase tracking-widest text-ink/50">Group settlements</p><span class="text-[10px] text-ink/50">${groupBlocks.length} open group${groupBlocks.length === 1 ? "" : "s"}</span></div>${groupBlocks.map((group) => {
    const amount = Number(group.amount || 0);
    const currency = group.currency || "USD";
    const status = amount > 0 ? "They owe you" : "You owe them";
    const amountText = `${amount > 0 ? "+" : "−"}${ShareSquad.formatMoney(Math.abs(amount), currency)}`;
    const amountTone = amount > 0 ? "text-green-700" : "text-red-700";
    const expenseNote = group.count
      ? `${group.count} shared expense${group.count === 1 ? "" : "s"} across all months · View group`
      : "Overall Who pays whom · View group";
    return `<button type="button" class="member-row group-ledger-row w-full text-left" data-group-id="${group.groupId}"><div><p class="font-semibold text-sm">Group settlement · ${ShareSquad.escapeHtml(group.name)}</p><p class="text-xs text-ink/55">${expenseNote}</p></div><div class="text-right"><p class="text-xs font-mono font-bold ${amountTone}">${amountText}</p><p class="text-[10px] text-ink/50">${status}</p></div></button>`;
  }).join("")}</section>` : "";
  const hasPrivate = openRows.length || paidRows.length;
  const directHeader = hasPrivate ? `<div class="flex items-center justify-between gap-3"><p class="text-[10px] font-mono uppercase tracking-widest text-ink/50">Private bills</p><span class="text-[10px] font-mono ${directSummary.length && directSummary.every((item) => item.amount > 0) ? "text-green-700" : directSummary.length && directSummary.every((item) => item.amount < 0) ? "text-red-700" : "text-ink/50"}">${directSummary.length ? directSummary.map((item) => signedMoney(item.amount, item.currency)).join(" · ") : "Settled"}</span></div>` : "";
  const privateHtml = hasPrivate ? `<section class="space-y-2 ${groupHtml ? "pt-4 mt-4 border-t border-ink/10" : ""}">${directHeader}${openRows.map((row) => directLedgerRow(row, friend)).join("")}${paidRows.length ? `<details class="pt-2"><summary class="cursor-pointer text-xs font-semibold text-ink/60">Paid private bills · ${paidRows.length}</summary><div class="space-y-2 mt-2">${paidRows.map((row) => directLedgerRow(row, friend)).join("")}</div></details>` : ""}</section>` : "";
  $("#friendLedger").innerHTML = groupHtml || privateHtml ? `${groupHtml}${privateHtml}` : '<p class="text-sm text-ink/55 py-3">No open group settlements or private bills with this person.</p>';
  $("#friendLedger").querySelectorAll(".group-ledger-row").forEach((button) => button.addEventListener("click", () => {
    ShareSquad.setStoredGroupId(button.dataset.groupId);
    window.location.href = `/group.html?group=${encodeURIComponent(button.dataset.groupId)}&tab=balances`;
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
  $("#monthlySettlement").innerHTML = `<p class="text-[10px] text-ink/55">Each group shows the overall remaining Who pays whom (every unpaid month combined). Settled groups are hidden. Private bills stay separate.</p>`;
}

function renderConsolidated(friend) {
  const breakdown = $("#consolidatedBreakdown"); const payments = $("#consolidatedPayments");
  // Keep a currency section when it has recorded transfers, even once its
  // balance is zero. Otherwise payment history disappears for the people who
  // successfully settled up.
  const totals = (friend.consolidatedBalances?.balances || (friend.consolidated ? [friend.consolidated] : []))
    .filter((total) => Math.abs(Number(total.balance || 0)) >= 0.005 || (total.payments || []).length);
  if (!totals.length) { breakdown.innerHTML = ""; payments.innerHTML = '<p class="text-xs text-ink/55">No balance to settle with this person.</p>'; return; }
  const multiple = totals.length > 1;
  breakdown.innerHTML = `<p class="text-[10px] font-mono uppercase tracking-widest text-ink/50 mb-2">Balance breakdown</p><p class="text-[10px] text-ink/55 mb-3">Balances are separated by currency. Remaining = one Direct total (all unpaid months) + one total per shared group (all unpaid months). Shared group expenses in the ledger are view-only.</p>${totals.map((total) => {
    const sources = total.sources || [];
    return `<section class="${multiple ? "pt-3 mt-3 border-t border-ink/10" : ""}">${multiple ? `<p class="text-xs font-mono font-bold text-brand mb-2">${ShareSquad.escapeHtml(total.currency)} balance</p>` : ""}<div class="space-y-2">${sources.length ? sources.map((source) => {
      const label = source.label || (source.kind === "GROUP" ? "Group" : "Direct splits");
      return `<div class="flex justify-between gap-3 text-xs"><span>${ShareSquad.escapeHtml(label)}</span><span class="font-mono ${source.amount > 0 ? "text-green-700" : "text-red-700"}">${source.amount > 0 ? "+" : "−"}${ShareSquad.formatMoney(Math.abs(source.amount), source.currency)}</span></div>`;
    }).join("") : '<p class="text-xs text-ink/55">No open balance in this currency.</p>'}</div></section>`;
  }).join("")}`;
  payments.innerHTML = totals.map((total) => {
    const balance = Number(total.balance || 0); const settleable = Number(total.settleable_balance || 0);
    const iOwe = settleable < 0; const currency = ShareSquad.escapeHtml(total.currency);
    const deferredAmount = balance - settleable;
    const openGroups = [...new Map((total.sources || []).filter((source) => source.kind === "GROUP" && source.settlement_ready === false && source.group_id).map((source) => [source.group_id, String(source.label || "Group").split(" · ")[0]])).entries()];
    const groupAction = openGroups.length ? `<div class="mt-3 flex flex-wrap items-center gap-2"><span class="text-xs text-ink/55">${Math.abs(deferredAmount) >= 0.005 ? `${ShareSquad.formatMoney(Math.abs(deferredAmount), total.currency)} remains in an open group period.` : "Recording this payment also updates the overall group balance."}</span>${openGroups.map(([groupId, groupName]) => `<button type="button" class="open-group-settlement bb-btn-secondary !py-1 !px-2 text-[10px]" data-group-id="${groupId}">Open ${ShareSquad.escapeHtml(groupName)} balances</button>`).join("")}</div>` : (settleable ? `<p class="mt-3 text-xs text-ink/55">Recording this payment also updates the overall group balance.</p>` : "");
    const waitMsg = !balance ? '<p class="text-xs text-ink/55">No balance to settle in this currency.</p>' : "";
    const pending = (total.payments || []).filter((payment) => payment.status === "PENDING");
    const history = (total.payments || []).length ? `<details class="mt-3 pt-3 border-t border-ink/10"><summary class="cursor-pointer text-[10px] font-mono uppercase tracking-widest text-ink/50">${currency} payment history · ${(total.payments || []).length}</summary><div class="space-y-2 mt-3">${total.payments.map((payment) => {
      const youPaid = Number(payment.payer_account_id) === Number(me?.account?.id); const date = payment.confirmed_at || payment.created_at;
      return `<div class="flex items-center justify-between gap-3 rounded bg-ink/5 px-2.5 py-2 text-xs"><span>${youPaid ? "You paid" : `${ShareSquad.escapeHtml(friend.friend.name)} paid`} · ${ShareSquad.formatDate(date)}</span><span class="font-mono font-bold ${youPaid ? "text-red-700" : "text-green-700"}">${youPaid ? "−" : "+"}${ShareSquad.formatMoney(payment.amount, payment.currency)}</span></div>`;
    }).join("")}</div></details>` : "";
    return `<section class="${multiple ? "pt-4 mt-4 border-t border-ink/10" : ""}">${multiple ? `<p class="text-xs font-mono font-bold text-brand mb-2">Settle in ${currency}</p>` : ""}${settleable ? `<div class="flex flex-wrap gap-2"><button type="button" class="bb-btn-primary !py-1.5 !px-3 text-[10px] mark-full-consolidated-payment" data-currency="${currency}" data-amount="${Math.abs(settleable)}">${iOwe ? "Mark" : "Record"} ${ShareSquad.formatMoney(Math.abs(settleable), total.currency)} ${iOwe ? "payment" : "received"}</button><button type="button" class="bb-btn-secondary !py-1.5 !px-3 text-[10px] show-partial-consolidated-payment" data-currency="${currency}">${iOwe ? "Mark partial payment" : "Record partial payment received"}</button></div><div class="partial-consolidated-payment-form hidden mt-3 flex flex-wrap items-end gap-2" data-currency="${currency}"><label class="text-[10px] font-mono text-ink/55">${currency} amount<input class="consolidated-payment-amount bb-input block !w-32 !py-1.5 mt-1 text-xs" type="number" min="0.01" max="${Math.abs(settleable)}" step="0.01" placeholder="0.00"></label><button type="button" class="record-consolidated-payment bb-btn-primary !py-1.5 !px-3 text-[10px]" data-currency="${currency}" data-max="${Math.abs(settleable)}">Save partial payment</button></div>${groupAction}` : waitMsg}${pending.map((payment) => `<div class="mt-3 p-2 rounded bg-amber-500/10 text-xs flex items-center justify-between gap-2"><span>${ShareSquad.formatMoney(payment.amount, payment.currency)} pending payment</span><button class="confirm-consolidated-payment bb-btn-secondary !py-1 !px-2 text-[10px]" data-id="${payment.id}">Apply now</button></div>`).join("")}${history}</section>`;
  }).join("");
  const savePayment = async (amount, currency, max) => {
    if (!Number.isFinite(amount) || amount <= 0 || amount > max) return ShareSquad.showToast("Enter an amount up to the payment-ready balance.", "error");
    try { await ShareSquad.api(`/friends/${friend.friend.id}/consolidated-payments`, { method: "POST", body: JSON.stringify({ amount, currency }) }); ShareSquad.showToast("Payment recorded and applied to the balance.", "success"); await refresh(); openFriend(friend.key); }
    catch (err) { ShareSquad.showToast(err.message || "Could not record payment", "error"); }
  };
  payments.querySelectorAll(".mark-full-consolidated-payment").forEach((button) => button.addEventListener("click", () => savePayment(Number(button.dataset.amount), button.dataset.currency, Number(button.dataset.amount))));
  payments.querySelectorAll(".show-partial-consolidated-payment").forEach((button) => button.addEventListener("click", () => payments.querySelector(`.partial-consolidated-payment-form[data-currency=\"${button.dataset.currency}\"]`)?.classList.remove("hidden")));
  payments.querySelectorAll(".record-consolidated-payment").forEach((button) => button.addEventListener("click", () => {
    const form = button.closest(".partial-consolidated-payment-form"); savePayment(Number(form?.querySelector(".consolidated-payment-amount")?.value), button.dataset.currency, Number(button.dataset.max));
  }));
  payments.querySelectorAll(".open-group-settlement").forEach((button) => button.addEventListener("click", () => {
    ShareSquad.setStoredGroupId(button.dataset.groupId);
    window.location.href = `/group.html?group=${encodeURIComponent(button.dataset.groupId)}&tab=balances`;
  }));
  payments.querySelectorAll(".confirm-consolidated-payment").forEach((button) => button.addEventListener("click", async () => {
    try { await ShareSquad.api(`/friends/${friend.friend.id}/consolidated-payments/${button.dataset.id}/confirm`, { method: "POST" }); ShareSquad.showToast("Payment applied to the open balances.", "success"); await refresh(); openFriend(friend.key); }
    catch (err) { ShareSquad.showToast(err.message || "Could not apply payment", "error"); }
  }));
}

function setupHistory(friend) {
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
  const totals = consolidatedTotals(friend);
  const positive = totals.length && totals.every((total) => Number(total.balance) > 0);
  const negative = totals.length && totals.every((total) => Number(total.balance) < 0);
  const balanceMarkup = totals.length
    ? (totals.length === 1
      ? (positive ? `${ShareSquad.escapeHtml(friend.friend.name)} owes you ${money(totals[0].balance, totals[0].currency)}` : `You owe ${ShareSquad.escapeHtml(friend.friend.name)} ${money(totals[0].balance, totals[0].currency)}`)
      : `<span class="text-sm text-ink/60">Balances by currency</span><span class="block mt-1">${totals.map((total) => `${Number(total.balance) > 0 ? "+" : "−"}${ShareSquad.formatMoney(Math.abs(total.balance), total.currency)}`).join("<br>")}</span>`)
    : "No one owes anything";
  const balanceTone = positive ? "text-green-700" : negative ? "text-red-700" : totals.length ? "text-ink/70" : "text-ink/60";
  const balanceNote = !totals.length ? "You are settled." : totals.length > 1 ? "Each amount is separate; select its payment controls below." : positive ? "Green: they owe you." : "Red: you owe them.";
  $("#friendName").textContent = friend.friend.name;
  $("#friendBalance").innerHTML = balanceMarkup;
  $("#friendBalance").className = `font-mono font-bold text-xl mt-3 ${balanceTone}`;
  $("#friendBalanceHelp").textContent = balanceNote;
  renderConsolidated(friend);
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
    // consolidated-balances also repairs paid private-bill markers server-side.
    friend.consolidatedBalances = await ShareSquad.api(`/friends/${friend.friend.id}/consolidated-balances`).catch(() => null);
    friend.consolidated = consolidatedTotals(friend)[0] || friend.consolidatedBalances?.balances?.[0] || null;
    friend.groupContributions = await ShareSquad.api(`/friends/${friend.friend.id}/group-contributions`).catch(() => []);
    friend.settlements = new Map(settlements.map((settlement) => [settlement.month_key, settlement]));
    friend.balance = friend.consolidated ? Number(friend.consolidated.balance) : friend.rows.reduce((sum, row) => {
      const settlement = friend.settlements.get(String(row.split.bill_date).slice(0, 7));
      if (settlement?.status === "SETTLED") return sum;
      if (settlement?.settled_at && new Date(row.split.created_at) <= new Date(settlement.settled_at)) return sum;
      return sum + row.balance;
    }, 0);
    indexPaidDirectBills(friend);
  }));
  // Re-load private bills after repair so CONFIRMED payment_status is visible
  // without requiring a second full page refresh.
  const repairedSplits = await ShareSquad.api("/quick-splits").catch(() => null);
  if (repairedSplits) {
    const contactsOnly = friends.map((friend) => ({
      id: friend.friend.id,
      display_name: friend.friend.name,
      email: friend.friend.email,
      has_avatar: friend.friend.hasAvatar,
    })).filter((contact) => contact.id);
    const preserved = new Map(friends.map((friend) => [friend.key, friend]));
    friends = mergeFriends(repairedSplits, contactsOnly).map((friend) => {
      const prior = preserved.get(friend.key);
      if (!prior) return friend;
      friend.consolidatedBalances = prior.consolidatedBalances;
      friend.consolidated = prior.consolidated;
      friend.groupContributions = prior.groupContributions;
      friend.settlements = prior.settlements;
      friend.balance = prior.balance;
      indexPaidDirectBills(friend);
      return friend;
    });
  }
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
$("#openAddBill").onclick = () => { const panel = $("#friendExpensePanel"); if (panel.classList.contains("hidden")) return; resetExpenseForm(); panel.scrollIntoView({ behavior: "smooth", block: "start" }); setTimeout(() => $("#friendExpenseTitle").focus(), 350); };
$("#cancelFriendExpenseEdit")?.addEventListener("click", () => resetExpenseForm());
$("#friendExpenseType").onchange = renderValues;
$("#friendPaidBy").onchange = renderValues;
$("#friendPayerValue").oninput = renderValues;
$("#friendOtherValue").oninput = renderValues;
["Me", "Friend"].forEach((person) => $("#friendShare" + person).addEventListener("click", () => {
  const selected = friendShareSelected(person);
  const other = person === "Me" ? "Friend" : "Me";
  if (selected && !friendShareSelected(other)) return;
  setFriendShareSelected(person, !selected);
  renderValues();
}));
$("#friendExpenseAmount").oninput = renderValues;
$("#friendMyPaid").oninput = renderValues;
$("#friendTheirPaid").oninput = renderValues;
$("#settingsBtn").onclick = () => { ShareSquad.navigateWithReturn("/settings.html"); };
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
  const shareMe = friendShareSelected("Me");
  const shareFriend = friendShareSelected("Friend");
  if (!shareMe && !shareFriend) {
    $("#friendMsg").textContent = "Choose at least one person who should cover this bill.";
    $("#friendMsg").className = "text-xs font-mono balance-negative";
    return;
  }
  const onePersonCovers = shareMe !== shareFriend;
  const effectiveType = onePersonCovers ? "EXACT" : type;
  const payerValue = effectiveType === "EQUAL"
    ? null
    : (onePersonCovers
      ? (paidBy === "friend" ? (shareFriend ? total : 0) : (shareMe ? total : 0))
      : Number($("#friendPayerValue").value));
  const participantValue = effectiveType === "EQUAL"
    ? null
    : (onePersonCovers
      ? (paidBy === "friend" ? (shareMe ? total : 0) : (shareFriend ? total : 0))
      : Number($("#friendOtherValue").value));
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
    category: ShareSquad.resolveCategoryValue($("#friendExpenseCategory"), $("#friendExpenseCategoryCustom")),
    split_type: effectiveType,
    payer_value: payerValue,
    payer_account_id: payerAccountId,
    payer_paid_amount: payerPaidAmount,
    silent_invite: true,
    participants: [{ account_id: participantId, value: participantValue }],
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
(async () => { if (!ShareSquad.getToken()) { ShareSquad.goToLogin({ replace: true }); return; } ShareSquad.fillAllCurrencySelects(); ShareSquad.bindAllCategorySelects(); $("#friendExpenseDate").max = todayISO(); $("#friendExpenseDate").value = todayISO(); renderValues(); try { me = await ShareSquad.api("/auth/me"); $("#headerInitials").textContent = ShareSquad.initials(me.account.display_name); if (me.account.has_avatar) { ShareSquad.setAuthImage($("#headerAvatar"), "/auth/me/avatar"); $("#headerInitials").classList.add("hidden"); } await refresh(); const selectedFriendId = Number(new URLSearchParams(location.search).get("friend")); if (selectedFriendId) { openedFromBalances = true; openFriend(`account-${selectedFriendId}`); } } catch (err) { ShareSquad.showToast(err.message || "Could not load friend splits", "error"); } if (window.lucide) lucide.createIcons(); })();
