document.querySelectorAll('a[href="/"]').forEach((link) => { link.href = "/index.html"; });
try { sessionStorage.setItem("sharesquad_expense_view", "friends"); } catch (_) {}
let me; let friends = []; let activeFriend = null; let editingSplitId = null; let composingNewBill = false;
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

/** Reminder available whenever this friend currently owes you (any currency). */
function friendReminderEligible(friend) {
  const totals = friend.consolidatedBalances?.balances || [];
  return totals.some((total) => Number(total.balance || 0) > 0.005);
}

async function sendPaymentReminder(friendId) {
  const entry = friends.find((item) => Number(item.friend.id) === Number(friendId));
  const name = entry?.friend?.name || "this friend";
  if (!await ShareSquad.confirmDialog({
    title: `Remind ${name}?`,
    message: `Send a polite email only to ${name} with the total they still owe you? Nothing is sent to anyone else.`,
    confirmLabel: "Send reminder",
    danger: false,
  })) return;
  try {
    const result = await ShareSquad.api(`/friends/${friendId}/payment-reminder`, { method: "POST" });
    ShareSquad.showToast(result.message || `Reminder sent to ${name}.`, "success");
  } catch (error) {
    ShareSquad.showToast(error.message || "Could not send reminder", "error");
  }
}

function statusFor(friend) {
  const balance = cents(friend.balance);
  if (balance > 0) return { tone: "text-green-800 bg-green-600/10 border-green-700/20", text: `${ShareSquad.escapeHtml(friend.friend.name)} owes you ${money(balance, friend.currency)}`, hint: "They owe you" };
  if (balance < 0) return { tone: "text-red-800 bg-red-600/10 border-red-700/20", text: `You owe ${ShareSquad.escapeHtml(friend.friend.name)} ${money(balance, friend.currency)}`, hint: "You owe them" };
  return { tone: "text-ink/65 bg-ink/5 border-ink/10", text: "No one owes anything", hint: "Settled up" };
}

function statusForBill(row, settled = false, friend = null) {
  if (settled) {
    const audit = friend ? privateBillAuditHint(row, friend) : null;
    return { tone: "text-ink/50 line-through", text: "Paid", hint: audit?.label || "Settled", paymentId: audit?.paymentId || null };
  }
  const balance = cents(row.balance);
  if (balance > 0) return { tone: "text-green-700", text: `+${money(balance, row.split.currency)}`, hint: "They owe you" };
  if (balance < 0) return { tone: "text-red-700", text: `−${money(balance, row.split.currency)}`, hint: "You owe them" };
  return { tone: "text-ink/60", text: "Settled", hint: "Settled up" };
}

function privateBillAuditHint(row, friend) {
  const coverage = friend.paymentCoverageBySplitId?.get(Number(row.split.id));
  if (coverage) {
    const when = coverage.date ? ShareSquad.formatDate(coverage.date) : "";
    return {
      paymentId: coverage.paymentId,
      label: `Paid via ${ShareSquad.formatMoneyText(coverage.total, coverage.currency)} payment${when ? ` (${when})` : ""}`,
    };
  }
  const monthKey = String(row.split.bill_date || "").slice(0, 7);
  const settlement = friend.settlements?.get(monthKey);
  if (settlement && (settlement.status === "SETTLED" || settlement.settled_at)) {
    return { paymentId: null, label: `Settled in month lock (${monthKey})` };
  }
  return { paymentId: null, label: "Marked paid" };
}

function friendPaymentsById(friend) {
  const paymentsById = new Map();
  (friend.consolidatedBalances?.balances || []).forEach((total) => {
    (total.payments || []).forEach((payment) => paymentsById.set(Number(payment.id), payment));
  });
  return paymentsById;
}

function indexPaidDirectBills(friend) {
  // A confirmed Friend payment can carry exact bill IDs (newer payments), or
  // only a direct-balance allocation (older payments). Build one shared index
  // for the whole friend ledger so every card and every month uses the same
  // payment state instead of relying on a row-specific UI shortcut.
  const paidIds = new Set();
  const coveredBySplit = new Map();
  const paymentCoverageBySplitId = new Map();
  const rows = friend.rows || [];
  const rowById = new Map(rows.map((row) => [Number(row.split.id), row]));
  const amountFor = (row) => {
    const total = Number(row.split.total_amount || 0);
    const paid = row.split.payer_paid_amount == null ? total : Number(row.split.payer_paid_amount);
    return Math.abs(paid - Number(row.split.payer_share_amount || 0));
  };
  const linkPayment = (splitId, payment, sliceAmount) => {
    const id = Number(splitId);
    if (!Number.isFinite(id) || id <= 0) return;
    paymentCoverageBySplitId.set(id, {
      paymentId: Number(payment.id),
      amount: Math.abs(Number(sliceAmount || 0)),
      date: payment.confirmed_at || payment.created_at,
      currency: payment.currency,
      total: Number(payment.amount || 0),
    });
  };
  const addCoverage = (splitId, amount, payment = null) => {
    const row = rowById.get(Number(splitId));
    if (!row) return;
    const total = (coveredBySplit.get(Number(splitId)) || 0) + Math.abs(Number(amount || 0));
    coveredBySplit.set(Number(splitId), total);
    if (total + 0.005 >= amountFor(row) || ["PAID", "CONFIRMED"].includes(String(row.person?.payment_status || "").toUpperCase())) {
      paidIds.add(Number(splitId));
    }
    if (payment) linkPayment(splitId, payment, amount);
  };
  const paymentsById = friendPaymentsById(friend);

  [...paymentsById.values()]
    .filter((payment) => payment.status === "CONFIRMED")
    .sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")) || Number(a.id) - Number(b.id))
    .forEach((payment) => {
      const allocations = (payment.allocations || []).filter((allocation) => allocation.kind === "DIRECT");
      // First honor exact references. These are authoritative and are written
      // by newer payment flows when a bill can be identified directly.
      allocations.filter((allocation) => allocation.quick_split_id).forEach((allocation) => {
        addCoverage(allocation.quick_split_id, allocation.amount, payment);
      });
      // Legacy month-only transfers stay in payment history. They cannot
      // identify a particular bill, so they never infer its Paid status here.
    });
  friend.paidDirectSplitIds = paidIds;
  friend.paymentCoverageBySplitId = paymentCoverageBySplitId;
}

function isDirectBillSettled(row, friend) {
  // A private bill is paid only with bill-level evidence: an explicit status
  // or a confirmed payment allocation naming this bill. A zero pair/month
  // balance can merely mean different bills offset one another; it must never
  // strike through either bill.
  if (["PAID", "CONFIRMED"].includes(String(row.person?.payment_status || "").toUpperCase())) return true;
  if (friend.paidDirectSplitIds?.has(Number(row.split.id))) return true;
  return false;
}

function pendingInviteSplitId(friend) {
  const open = (friend.rows || []).find((row) => String(row.split?.status || "").toUpperCase() !== "SETTLED");
  return open?.split?.id || friend.rows?.[0]?.split?.id || null;
}

function renderList() {
  const grid = $("#friendsGrid");
  const accountFriends = friends.filter((friend) => friend.friend.id);
  const pendingInvites = friends.filter((friend) => !friend.friend.id && (friend.rows || []).length);
  if (!accountFriends.length && !pendingInvites.length) {
    grid.innerHTML = '<div class="bb-card sm:col-span-2 lg:col-span-3 text-center py-10"><p class="font-mono font-bold">No friends yet</p><p class="text-sm text-ink/60 mt-2">Add an existing ShareSquad user by email. Group members will also appear here automatically.</p></div>';
    return;
  }
  const friendCards = accountFriends.map((friend) => {
    const remind = friendReminderEligible(friend)
      ? `<button type="button" class="remind-friend-pay bb-btn-secondary w-full !py-1.5 mt-2 text-[10px]" data-remind-friend="${friend.friend.id}">Send payment reminder</button>`
      : "";
    return `<article class="bb-card border border-ink/10"><div class="flex items-center justify-between gap-3"><button class="text-left grow flex items-center gap-3" data-friend-key="${friend.key}">${friendAvatar(friend.friend)}<p class="font-mono font-bold text-base">${ShareSquad.escapeHtml(friend.friend.name)}</p></button><button type="button" class="remove-friend bb-btn-secondary !py-1.5 !px-2.5 text-[10px] text-stamp border-stamp/30" data-remove-friend="${friend.friend.id}">Remove</button></div><button type="button" class="invite-friend-group bb-btn-secondary w-full !py-1.5 mt-3 text-[10px]" data-invite-friend="${friend.friend.id}">Invite to group</button>${remind}</article>`;
  });
  // Private-link bills are not friends yet — no Remove / Invite to group.
  const pendingCards = pendingInvites.map((friend) => {
    const splitId = pendingInviteSplitId(friend);
    const billCount = friend.rows.length;
    const title = friend.rows[0]?.split?.title || "Private-link bill";
    return `<article class="bb-card border border-dashed border-ink/20"><p class="text-[10px] font-mono uppercase tracking-widest text-ink/45">Awaiting join</p><p class="font-mono font-bold text-base mt-1">${ShareSquad.escapeHtml(title)}</p><p class="text-xs text-ink/55 mt-1">${billCount} private-link bill${billCount === 1 ? "" : "s"} · not a friend until they join</p>${splitId ? `<a class="bb-btn-secondary w-full !py-1.5 mt-3 text-[10px] inline-flex justify-center" href="/quick-split.html?id=${encodeURIComponent(splitId)}">Open share link</a>` : ""}</article>`;
  });
  grid.innerHTML = [...friendCards, ...pendingCards].join("");
  loadFriendAvatars(grid);
  grid.querySelectorAll("[data-friend-key]").forEach((button) => button.onclick = () => {
    openedFromBalances = false;
    openFriend(button.dataset.friendKey);
  });
  grid.querySelectorAll("[data-remove-friend]").forEach((button) => button.onclick = () => removeFriend(Number(button.dataset.removeFriend)));
  grid.querySelectorAll("[data-invite-friend]").forEach((button) => button.onclick = () => inviteFriendToGroup(Number(button.dataset.inviteFriend)));
  grid.querySelectorAll("[data-remind-friend]").forEach((button) => button.onclick = () => sendPaymentReminder(Number(button.dataset.remindFriend)));
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

function billWithMode() {
  return $("#billWithMode")?.value || "friend";
}

function fillBillWithFriends() {
  const select = $("#billWithFriend");
  if (!select) return;
  const current = select.value;
  const accountFriends = friends.filter((item) => item.friend.id);
  select.innerHTML = accountFriends.length
    ? `<option value="">Choose a friend</option>${accountFriends.map((item) => `<option value="${item.friend.id}">${ShareSquad.escapeHtml(item.friend.name)}</option>`).join("")}`
    : `<option value="">No friends yet — use email or a private link</option>`;
  if ([...select.options].some((opt) => opt.value === current)) select.value = current;
}

function otherPersonLabel() {
  if (activeFriend?.friend?.name) return activeFriend.friend.name;
  if (billWithMode() === "email") {
    const email = ($("#billWithEmail")?.value || "").trim();
    return email ? ShareSquad.maskEmail(email) : "Friend";
  }
  if (billWithMode() === "links") return "The other person";
  const selectedId = Number($("#billWithFriend")?.value || 0);
  const match = friends.find((item) => item.friend.id === selectedId);
  return match?.friend?.name || "Friend";
}

function syncBillWithUi() {
  const wrap = $("#billWithWrap");
  if (!wrap) return;
  const composing = composingNewBill && !editingSplitId;
  wrap.classList.toggle("hidden", !composing);
  if (!composing) return;
  const mode = billWithMode();
  $("#billWithFriendWrap")?.classList.toggle("hidden", mode !== "friend");
  $("#billWithEmailWrap")?.classList.toggle("hidden", mode !== "email");
  const help = $("#billWithHelp");
  if (help) {
    help.textContent = mode === "email"
      ? "We email them an invite to this bill. Existing users are also added as friends."
      : mode === "links"
        ? "After you save, copy the private link and send it yourself."
        : "Choose someone already on your friends list.";
  }
  const paidBy = $("#friendPaidBy");
  if (paidBy) {
    const allowOtherPayer = mode === "friend" || mode === "email";
    [...paidBy.options].forEach((opt) => {
      if (opt.value === "friend" || opt.value === "both") opt.disabled = !allowOtherPayer;
    });
    if (!allowOtherPayer && paidBy.value !== "me") paidBy.value = "me";
  }
}

function renderValues() {
  syncBillWithUi();
  const paidBy = $("#friendPaidBy").value;
  const friendPaid = paidBy === "friend";
  const shareMe = friendShareSelected("Me");
  const shareFriend = friendShareSelected("Friend");
  const onePersonCovers = shareMe !== shareFriend;
  $("#friendSplitTypeWrap").classList.toggle("hidden", onePersonCovers);
  $("#friendValues").classList.toggle("hidden", onePersonCovers || $("#friendExpenseType").value === "EQUAL");
  $("#friendPaySplit").classList.toggle("hidden", paidBy !== "both");
  const name = otherPersonLabel();
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

function canEditBill(split, settled = false) {
  // Settled / payment-covered bills stay locked so edits cannot break the
  // allocation trail. Open owner-created bills remain editable.
  if (settled) return false;
  return !!split?.is_owner;
}

function resolveGroupName(friend, groupId) {
  const id = Number(groupId);
  if (!Number.isFinite(id) || id <= 0) return "Group";
  const contrib = (friend.groupContributions || []).find((row) => Number(row.group_id) === id);
  if (contrib?.group_name) return contrib.group_name;
  for (const total of (friend.consolidatedBalances?.balances || [])) {
    for (const source of (total.sources || [])) {
      if (source.kind === "GROUP" && Number(source.group_id) === id) {
        return String(source.label || "Group").split(" · ")[0];
      }
    }
  }
  return "Group";
}

function allocationSliceLabel(allocation, friend) {
  const kind = String(allocation.kind || "").toUpperCase();
  if (kind === "DIRECT") {
    const splitId = Number(allocation.quick_split_id);
    const row = Number.isFinite(splitId)
      ? (friend.rows || []).find((item) => Number(item.split.id) === splitId)
      : null;
    const title = row?.split?.title || allocation.label || "Private bill";
    const month = allocation.month_key || allocation.period_key || String(row?.split?.bill_date || "").slice(0, 7);
    return `Direct: “${title}”${month ? ` (${month})` : ""}`;
  }
  if (kind === "GROUP") {
    if (allocation.bill_title) return `Group: ${allocation.label || resolveGroupName(friend, allocation.group_id)} · “${allocation.bill_title}”`;
    if (allocation.label) return `Group: ${allocation.label}`;
    const name = resolveGroupName(friend, allocation.group_id);
    const period = allocation.month_key || allocation.period_key || "";
    return `Group: ${name}${period ? ` (${period})` : ""}`;
  }
  if (kind === "CREDIT") {
    if (allocation.payment_kind === "CREDIT_APPLIED") return "Prior Extra pay applied";
    return "Extra pay (Credit balance added)";
  }
  return allocation.label || kind || "Allocation";
}

function paymentAllocationRowsHtml(payment, friend) {
  const slices = (payment.allocations || []).filter((allocation) => {
    const kind = String(allocation.kind || "").toUpperCase();
    if (kind === "CREDIT" && allocation.payment_kind === "CREDIT_APPLIED") return false;
    return Math.abs(Number(allocation.amount || 0)) >= 0.005;
  });
  if (!slices.length) {
    return '<p class="text-[10px] text-ink/50 mt-1.5">No allocation detail saved for this payment.</p>';
  }
  return `<ul class="mt-1.5 space-y-1 border-l border-ink/15 pl-2.5">${slices.map((allocation) => {
    const amount = Math.abs(Number(allocation.amount || 0));
    return `<li class="flex justify-between gap-3 text-[10px] text-ink/65"><span class="min-w-0">${ShareSquad.escapeHtml(allocationSliceLabel(allocation, friend))}</span><span class="font-mono shrink-0">${ShareSquad.formatMoney(amount, payment.currency)}</span></li>`;
  }).join("")}</ul>`;
}

function settledGroupPeriods(friend) {
  const openIds = new Set(overallGroupSettlements(friend).map((block) => Number(block.groupId)));
  const byKey = new Map();
  [...friendPaymentsById(friend).values()]
    .filter((payment) => payment.status === "CONFIRMED")
    .forEach((payment) => {
      (payment.allocations || []).filter((allocation) => allocation.kind === "GROUP").forEach((allocation) => {
        const groupId = Number(allocation.group_id);
        if (!Number.isFinite(groupId) || groupId <= 0) return;
        if (openIds.has(groupId)) return;
        const period = allocation.month_key || allocation.period_key || allocation.period_id || "period";
        const key = `${groupId}:${period}`;
        const when = payment.confirmed_at || payment.created_at;
        const existing = byKey.get(key);
        if (existing && String(existing.date || "") >= String(when || "")) return;
        byKey.set(key, {
          groupId,
          name: resolveGroupName(friend, groupId),
          period: String(period),
          amount: Math.abs(Number(allocation.amount || 0)),
          currency: payment.currency,
          date: when,
          paymentId: Number(payment.id),
          via: "friends",
        });
      });
    });
  return [...byKey.values()].sort((a, b) => String(b.date || "").localeCompare(String(a.date || "")) || String(a.name).localeCompare(String(b.name)));
}

function focusFriendPayment(paymentId) {
  const id = Number(paymentId);
  if (!Number.isFinite(id) || id <= 0) return;
  const host = $("#consolidatedPayments");
  const row = host?.querySelector(`[data-payment-id="${id}"]`);
  if (!row) {
    ShareSquad.showToast("That payment is not in the history panel.", "info");
    return;
  }
  // Payment rows are <details> themselves. closest("details") would match the
  // row and leave the outer "payment history" accordion closed — open ancestors first.
  const ancestors = [];
  for (let el = row.parentElement; el && el !== host; el = el.parentElement) {
    if (el.tagName === "DETAILS") ancestors.push(el);
  }
  ancestors.reverse().forEach((details) => { details.open = true; });
  if (row.tagName === "DETAILS") row.open = true;
  requestAnimationFrame(() => {
    row.scrollIntoView({ behavior: "smooth", block: "center" });
    row.classList.add("ring-2", "ring-brand/40");
    window.setTimeout(() => row.classList.remove("ring-2", "ring-brand/40"), 1600);
  });
}

function setExpenseFormMode(editing) {
  editingSplitId = editing ? Number(editing) : null;
  $("#friendExpenseEditId").value = editingSplitId || "";
  $("#friendExpenseHeading").textContent = editingSplitId
    ? "Edit bill"
    : (composingNewBill ? "New private bill" : "Add another bill");
  $("#friendExpenseHelp").textContent = editingSplitId
    ? "Save changes to update this private bill for both of you."
    : (composingNewBill
      ? "Use the same bill form for a friend, email invite, or private link."
      : "Both people can add, edit, or delete a private bill from here.");
  $("#friendExpenseSubmit").textContent = editingSplitId ? "Save changes" : "Add private bill";
  $("#cancelFriendExpenseEdit")?.classList.toggle("hidden", !editingSplitId && !composingNewBill);
  $("#friendMsg").textContent = "";
  syncBillWithUi();
}

function resetExpenseForm() {
  const keepMode = billWithMode();
  const keepFriend = $("#billWithFriend")?.value || "";
  const keepEmail = $("#billWithEmail")?.value || "";
  $("#friendExpenseForm").reset();
  $("#friendExpenseDate").value = todayISO();
  if (activeFriend) ShareSquad.fillCurrencySelect($("#friendExpenseCurrency"), activeFriend.currency);
  else ShareSquad.fillCurrencySelect($("#friendExpenseCurrency"), "USD");
  ShareSquad.fillCategorySelect($("#friendExpenseCategory"), "FOOD & DINING", { allowCustom: true });
  $("#friendExpenseCategoryCustom").value = "";
  setFriendShareSelected("Me", true);
  setFriendShareSelected("Friend", true);
  if (composingNewBill) {
    if ($("#billWithMode")) $("#billWithMode").value = keepMode || "friend";
    fillBillWithFriends();
    if ($("#billWithFriend") && keepFriend) $("#billWithFriend").value = keepFriend;
    if ($("#billWithEmail")) $("#billWithEmail").value = keepEmail;
  }
  setExpenseFormMode(null);
  renderValues();
}

function openComposeBill() {
  composingNewBill = true;
  activeFriend = null;
  openedFromBalances = false;
  $("#friendsListPane")?.classList.add("hidden");
  const pane = $("#friendDetailPane");
  pane.classList.remove("hidden");
  $("#openAddBill")?.classList.add("hidden");
  $("#friendLedgerCard")?.classList.add("hidden");
  $("#friendSummaryKicker").textContent = "New private bill";
  $("#friendName").textContent = "Split with someone";
  $("#friendBalance").textContent = "";
  $("#friendBalance").className = "font-mono font-bold text-xl mt-3 text-ink/50";
  $("#friendBalanceHelp").textContent = "Pick a friend, email, or private link in the bill form. For 3+ people, create a group.";
  $("#consolidatedBreakdown").innerHTML = "";
  $("#consolidatedPayments").innerHTML = "";
  const remindHost = $("#friendRemindWrap");
  if (remindHost) remindHost.innerHTML = "";
  $("#friendExpensePanel").classList.remove("hidden");
  $("#friendExpenseForm").dataset.friendId = "";
  const paidByFriendOpt = $("#friendPaidBy")?.querySelector('option[value="friend"]');
  if (paidByFriendOpt) paidByFriendOpt.textContent = "Friend paid the full bill";
  fillBillWithFriends();
  ShareSquad.fillCurrencySelect($("#friendExpenseCurrency"), "USD");
  resetExpenseForm();
  history.replaceState({}, "", "/friends.html?new=1");
  setTimeout(() => ($("#billWithFriend")?.value ? $("#friendExpenseTitle") : $("#billWithMode"))?.focus?.(), 200);
}

function startEditBill(row) {
  const split = row.split;
  const settled = isDirectBillSettled(row, activeFriend);
  if (!canEditBill(split, settled)) {
    return ShareSquad.showToast(
      settled
        ? "This bill is settled. Open View allocation to see which payment covered it."
        : "Only someone on this bill can edit it.",
      "info",
    );
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
  const paid = isDirectBillSettled(row, activeFriend);
  if (!canEditBill(split, paid)) {
    return ShareSquad.showToast(
      paid
        ? "This bill is settled and locked so the payment trail stays intact."
        : "Only someone on this bill can delete it.",
      "info",
    );
  }
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
  const bill = statusForBill(row, settled, friend);
  const split = row.split;
  const editable = canEditBill(split, settled);
  const actions = editable
    ? `<div class="flex justify-end gap-1.5 mt-2"><button type="button" class="edit-friend-bill bb-btn-secondary !py-1 !px-2 text-[10px]" data-split-id="${split.id}">Edit</button><button type="button" class="delete-friend-bill bb-btn-secondary !py-1 !px-2 text-[10px] text-stamp border-stamp/30" data-split-id="${split.id}">Delete</button></div>`
    : (settled && bill.paymentId
      ? `<div class="flex justify-end gap-1.5 mt-2"><button type="button" class="audit-friend-bill bb-btn-secondary !py-1 !px-2 text-[10px]" data-payment-id="${bill.paymentId}">View allocation</button></div>`
      : "");
  const settledClass = settled ? "line-through text-ink/45" : "";
  const editedChip = split.edited_after_payment
    ? '<span class="status-chip status-waiting mt-1">Edited after payment</span>'
    : "";
  const hintHtml = bill.paymentId
    ? `<button type="button" class="audit-friend-bill text-[10px] text-ink/55 underline-offset-2 hover:underline text-right" data-payment-id="${bill.paymentId}">${ShareSquad.escapeHtml(bill.hint)}</button>`
    : `<p class="text-[10px] text-ink/50">${ShareSquad.escapeHtml(bill.hint)}</p>`;
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
  return `<div class="member-row ${settled ? "opacity-70" : ""}" data-direct-split="${split.id}" data-settled="${settled ? "1" : "0"}"><div class="min-w-0"><p class="font-semibold text-sm ${settledClass}" style="${settled ? "text-decoration:line-through" : ""}">${ShareSquad.escapeHtml(split.title)}</p><p class="text-xs text-ink/55 ${settledClass}" style="${settled ? "text-decoration:line-through" : ""}">${ShareSquad.formatDate(split.bill_date)} · ${split.payer_paid_amount == null ? (split.is_payer ? "You paid" : `${ShareSquad.escapeHtml(friend.friend.name)} paid`) : "You both paid"} ${money(split.total_amount, split.currency)}</p>${editedChip}${historyHtml}${actions}</div><div class="text-right shrink-0"><p class="text-xs font-mono font-bold ${bill.tone}" style="${settled ? "text-decoration:line-through" : ""}">${bill.text}</p>${hintHtml}</div></div>`;
}

function renderLedger(friend) {
  const groupBlocks = overallGroupSettlements(friend);
  const rows = friend.rows || [];
  const openRows = rows.filter((row) => !isDirectBillSettled(row, friend)).sort((a, b) => String(b.split.bill_date).localeCompare(String(a.split.bill_date)));
  const paidRows = rows.filter((row) => isDirectBillSettled(row, friend)).sort((a, b) => String(b.split.bill_date).localeCompare(String(a.split.bill_date)));
  if ($("#ledgerTitle")) $("#ledgerTitle").textContent = "Overall remaining";
  const settledGroups = settledGroupPeriods(friend);
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
  const settledGroupsHtml = settledGroups.length ? `<details class="${groupHtml ? "pt-3 mt-3 border-t border-ink/10" : ""}"><summary class="cursor-pointer text-xs font-semibold text-ink/60">Settled group periods · ${settledGroups.length}</summary><div class="space-y-2 mt-2">${settledGroups.map((group) => {
    const when = group.date ? ShareSquad.formatDate(group.date) : "";
    const badge = when
      ? `Settled via Friends payment on ${when}`
      : "Settled via Friends payment";
    return `<button type="button" class="member-row group-ledger-row opacity-70 w-full text-left" data-group-id="${group.groupId}" data-payment-id="${group.paymentId || ""}"><div class="min-w-0"><p class="font-semibold text-sm">${ShareSquad.escapeHtml(group.name)}</p><p class="text-xs text-ink/55">${ShareSquad.escapeHtml(String(group.period))} · ${ShareSquad.escapeHtml(badge)}</p></div><div class="text-right shrink-0"><p class="text-xs font-mono font-bold text-ink/50">${ShareSquad.formatMoney(group.amount, group.currency)}</p><p class="text-[10px] text-ink/50">Open group archive</p></div></button>`;
  }).join("")}</div></details>` : "";
  const hasPrivate = openRows.length || paidRows.length;
  // Header must match the open private bills listed below — never the
  // consolidated DIRECT monthly net (that can include Extra pay / payment
  // residuals and diverge from individual open bill balances).
  const openPrivateByCurrency = new Map();
  openRows.forEach((row) => {
    const currency = String(row.split?.currency || friend.currency || "USD");
    openPrivateByCurrency.set(currency, (openPrivateByCurrency.get(currency) || 0) + Number(row.balance || 0));
  });
  const openPrivateParts = [...openPrivateByCurrency.entries()]
    .map(([currency, amount]) => ({ currency, amount: cents(amount) }))
    .filter((item) => Math.abs(item.amount) >= 0.005 || openPrivateByCurrency.size === 1);
  const openPrivateNet = openPrivateParts.reduce((sum, item) => sum + item.amount, 0);
  const privateHeaderTone = !openRows.length
    ? "text-ink/50"
    : openPrivateParts.length > 1
      ? "text-ink/70"
      : openPrivateNet > 0.005 ? "text-green-700" : openPrivateNet < -0.005 ? "text-red-700" : "text-ink/50";
  const privateHeaderValue = !openRows.length
    ? "Settled"
    : openPrivateParts.map((item) => signedMoney(item.amount, item.currency)).join(" · ");
  const groupSection = groupHtml || settledGroupsHtml;
  const directHeader = hasPrivate ? `<div class="flex items-center justify-between gap-3"><p class="text-[10px] font-mono uppercase tracking-widest text-ink/50">Private bills</p><span class="text-[10px] font-mono ${privateHeaderTone}">${privateHeaderValue}</span></div>` : "";
  const privateHtml = hasPrivate ? `<section class="space-y-2 ${groupSection ? "pt-4 mt-4 border-t border-ink/10" : ""}">${directHeader}${openRows.map((row) => directLedgerRow(row, friend)).join("")}${paidRows.length ? `<details class="pt-2"><summary class="cursor-pointer text-xs font-semibold text-ink/60">Paid private bills · ${paidRows.length}</summary><div class="space-y-2 mt-2 paid-private-bills">${paidRows.map((row) => directLedgerRow(row, friend)).join("")}</div></details>` : ""}</section>` : "";
  $("#friendLedger").innerHTML = groupHtml || settledGroupsHtml || privateHtml
    ? `${groupHtml}${settledGroupsHtml}${privateHtml}`
    : '<p class="text-sm text-ink/55 py-3">No open group settlements or private bills with this person.</p>';
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
  $("#friendLedger").querySelectorAll(".audit-friend-bill").forEach((button) => button.addEventListener("click", (event) => {
    event.stopPropagation();
    focusFriendPayment(button.dataset.paymentId);
  }));
  $("#monthlySettlement").innerHTML = `<p class="text-[10px] text-ink/55">Each group shows the overall remaining Who pays whom (every unpaid month combined). Settled group periods stay under the archive. Private bills stay separate.</p>`;
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
  breakdown.innerHTML = `<p class="text-[10px] font-mono uppercase tracking-widest text-ink/50 mb-2">Balance breakdown</p><p class="text-[10px] text-ink/55 mb-3">Each bill records who owes whom immediately. Remaining balances include bill corrections and confirmed payments, separately for each currency. Open payment history to see which bills each payment covered.</p>${totals.map((total) => {
    const sources = total.sources || [];
    return `<section class="${multiple ? "pt-3 mt-3 border-t border-ink/10" : ""}">${multiple ? `<p class="text-xs font-mono font-bold text-brand mb-2">${ShareSquad.escapeHtml(total.currency)} balance</p>` : ""}<div class="space-y-2">${sources.length ? sources.map((source) => {
      const label = source.label || (source.kind === "CREDIT" ? "Extra pay" : source.kind === "GROUP" ? "Group" : "Direct splits");
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
      const statusNote = payment.status === "PENDING" ? " · pending" : "";
      return `<details class="rounded bg-ink/5 px-2.5 py-2" data-payment-id="${payment.id}"><summary class="cursor-pointer list-none flex items-center justify-between gap-3 text-xs"><span>${youPaid ? "You paid" : `${ShareSquad.escapeHtml(friend.friend.name)} paid`} · ${ShareSquad.formatDate(date)}${statusNote}</span><span class="font-mono font-bold ${youPaid ? "text-red-700" : "text-green-700"}">${youPaid ? "−" : "+"}${ShareSquad.formatMoney(payment.amount, payment.currency)}</span></summary>${paymentAllocationRowsHtml(payment, friend)}</details>`;
    }).join("")}</div></details>` : "";
    const owed = Math.abs(settleable);
    return `<section class="${multiple ? "pt-4 mt-4 border-t border-ink/10" : ""}">${multiple ? `<p class="text-xs font-mono font-bold text-brand mb-2">Settle in ${currency}</p>` : ""}${settleable ? `<div class="flex flex-wrap gap-2"><button type="button" class="bb-btn-primary !py-1.5 !px-3 text-[10px] mark-full-consolidated-payment" data-currency="${currency}" data-amount="${owed}">${iOwe ? "Mark" : "Record"} ${ShareSquad.formatMoney(owed, total.currency)} ${iOwe ? "payment" : "received"}</button><button type="button" class="bb-btn-secondary !py-1.5 !px-3 text-[10px] show-partial-consolidated-payment" data-currency="${currency}">${iOwe ? "Mark partial / overpay" : "Record partial / overpay received"}</button></div><div class="partial-consolidated-payment-form hidden mt-3 flex flex-wrap items-end gap-2" data-currency="${currency}"><label class="text-[10px] font-mono text-ink/55">${currency} amount<input class="consolidated-payment-amount bb-input block !w-32 !py-1.5 mt-1 text-xs" type="number" min="0.01" step="0.01" placeholder="0.00" aria-describedby="partialPayHint-${currency}"></label><button type="button" class="record-consolidated-payment bb-btn-primary !py-1.5 !px-3 text-[10px]" data-currency="${currency}" data-owed="${owed}">Save payment</button><p id="partialPayHint-${currency}" class="basis-full text-[10px] text-ink/55 m-0">Owed ${ShareSquad.formatMoney(owed, total.currency)}. Paying more records Extra pay.</p></div>${groupAction}` : waitMsg}${pending.map((payment) => `<div class="mt-3 p-2 rounded bg-amber-500/10 text-xs flex items-center justify-between gap-2"><span>${ShareSquad.formatMoney(payment.amount, payment.currency)} pending payment</span><button class="confirm-consolidated-payment bb-btn-secondary !py-1 !px-2 text-[10px]" data-id="${payment.id}">Apply now</button></div>`).join("")}${history}</section>`;
  }).join("");
  const savePayment = async (amount, currency, { exact = false, owed = null } = {}) => {
    if (!Number.isFinite(amount) || amount <= 0) {
      return ShareSquad.showToast("Enter a payment amount greater than zero.", "error");
    }
    if (exact && Number.isFinite(owed) && Math.abs(amount - owed) >= 0.005) {
      return ShareSquad.showToast("Use the exact-balance button, or enter any amount in partial / overpay.", "error");
    }
    try {
      await ShareSquad.api(`/friends/${friend.friend.id}/consolidated-payments`, { method: "POST", body: JSON.stringify({ amount, currency }) });
      const over = Number.isFinite(owed) && amount > owed + 0.005;
      ShareSquad.showToast(over ? "Overpayment recorded. Extra pay was added to the balance." : "Payment recorded and applied to the balance.", "success");
      await refresh();
      openFriend(friend.key);
    } catch (err) {
      ShareSquad.showToast(err.message || "Could not record payment", "error");
    }
  };
  payments.querySelectorAll(".mark-full-consolidated-payment").forEach((button) => button.addEventListener("click", () => {
    const owed = Number(button.dataset.amount);
    savePayment(owed, button.dataset.currency, { exact: true, owed });
  }));
  payments.querySelectorAll(".show-partial-consolidated-payment").forEach((button) => button.addEventListener("click", () => payments.querySelector(`.partial-consolidated-payment-form[data-currency=\"${button.dataset.currency}\"]`)?.classList.remove("hidden")));
  payments.querySelectorAll(".record-consolidated-payment").forEach((button) => button.addEventListener("click", () => {
    const form = button.closest(".partial-consolidated-payment-form");
    savePayment(Number(form?.querySelector(".consolidated-payment-amount")?.value), button.dataset.currency, { owed: Number(button.dataset.owed) });
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
  // Unclaimed private-link people are not Friends — open the share page instead.
  if (!friend.friend.id) {
    const splitId = pendingInviteSplitId(friend);
    if (splitId) {
      location.href = `/quick-split.html?id=${encodeURIComponent(splitId)}`;
      return;
    }
    ShareSquad.showToast("This private-link bill has no ShareSquad account yet.", "info");
    return;
  }
  composingNewBill = false;
  activeFriend = friend;
  $("#friendsListPane").classList.add("hidden");
  const pane = $("#friendDetailPane");
  pane.classList.remove("hidden");
  pane.classList.add("sliding-in");
  pane.addEventListener("animationend", () => pane.classList.remove("sliding-in"), { once: true });
  $("#openAddBill")?.classList.remove("hidden");
  $("#friendLedgerCard")?.classList.remove("hidden");
  $("#friendSummaryKicker").textContent = "Total balance with";
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
  let remindHost = $("#friendRemindWrap");
  if (!remindHost) {
    remindHost = document.createElement("div");
    remindHost.id = "friendRemindWrap";
    remindHost.className = "mt-3";
    $("#friendBalanceHelp")?.after(remindHost);
  }
  if (friend.friend.id && friendReminderEligible(friend)) {
    remindHost.innerHTML = `<button type="button" id="friendRemindBtn" class="bb-btn-secondary !py-1.5 !px-3 text-[10px]">Send payment reminder</button><p class="text-[10px] text-ink/50 mt-1.5">Emails only ${ShareSquad.escapeHtml(friend.friend.name)} with the total they owe you. Manual — nothing is sent automatically.</p>`;
    $("#friendRemindBtn").onclick = () => sendPaymentReminder(friend.friend.id);
  } else {
    remindHost.innerHTML = "";
  }
  renderConsolidated(friend);
  setupHistory(friend);
  $("#friendExpensePanel").classList.toggle("hidden", !friend.friend.id);
  $("#friendExpenseForm").dataset.friendId = friend.friend.id || "";
  $("#friendPaidBy").options[1].textContent = `${friend.friend.name} paid`;
  ShareSquad.fillCurrencySelect($("#friendExpenseCurrency"), friend.currency);
  resetExpenseForm();
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

function focusBillForm() {
  const panel = $("#friendExpensePanel");
  if (!panel || panel.classList.contains("hidden")) return;
  resetExpenseForm();
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
  setTimeout(() => $("#friendExpenseTitle")?.focus(), 350);
}
function openFriendInvite() { $("#friendInviteModal").classList.remove("hidden"); $("#friendInviteMsg").textContent = ""; $("#friendInviteEmail").focus(); }
function closeFriendInvite() { $("#friendInviteModal").classList.add("hidden"); }
async function createFriendInvite(email = "") {
  try {
    const normalized = String(email || "").trim().toLowerCase();
    const result = await ShareSquad.api("/friends/invites", { method: "POST", body: JSON.stringify(normalized ? { email: normalized } : {}) });
    $("#friendInviteMsg").textContent = result.message;
    $("#friendInviteMsg").className = "text-xs font-mono mt-3 balance-positive";
    if (result.status === "ACTIVE") {
      closeFriendInvite();
      await refresh();
      const match = friends.find((item) => String(item.friend.email || "").toLowerCase() === normalized);
      if (match?.friend?.id) {
        history.replaceState({}, "", `/friends.html?friend=${match.friend.id}&bill=1`);
        openFriend(match.key);
        focusBillForm();
      }
    }
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
  composingNewBill = false;
  resetExpenseForm();
  if (openedFromBalances) {
    location.href = "/index.html?view=friends";
    return;
  }
  $("#friendDetailPane").classList.add("hidden");
  $("#friendsListPane").classList.remove("hidden");
  $("#openAddBill")?.classList.remove("hidden");
  $("#friendLedgerCard")?.classList.remove("hidden");
  history.replaceState({}, "", "/friends.html");
};
$("#openAddBill").onclick = () => focusBillForm();
$("#cancelFriendExpenseEdit")?.addEventListener("click", () => {
  if (composingNewBill && !editingSplitId) {
    composingNewBill = false;
    $("#backFriends").onclick();
    return;
  }
  resetExpenseForm();
});
$("#billWithMode")?.addEventListener("change", () => { syncBillWithUi(); renderValues(); });
$("#billWithFriend")?.addEventListener("change", () => {
  const id = Number($("#billWithFriend").value || 0);
  $("#friendExpenseForm").dataset.friendId = id ? String(id) : "";
  const match = friends.find((item) => item.friend.id === id);
  const paidOpt = $("#friendPaidBy")?.querySelector('option[value="friend"]');
  if (paidOpt) paidOpt.textContent = match ? `${match.friend.name} paid` : "Friend paid the full bill";
  renderValues();
});
$("#billWithEmail")?.addEventListener("input", renderValues);
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
$("#splitBillBtn")?.addEventListener("click", (event) => {
  event.preventDefault();
  openComposeBill();
});
$("#closeFriendInviteBtn").onclick = closeFriendInvite;
$("#friendInviteModal").onclick = (event) => { if (event.target.id === "friendInviteModal") closeFriendInvite(); };
$("#sendFriendInviteBtn").onclick = () => { const email = $("#friendInviteEmail").value.trim(); if (!email) { $("#friendInviteMsg").textContent = "Enter an existing user's email address."; return; } createFriendInvite(email); };
$("#friendExpenseForm").onsubmit = async (event) => {
  event.preventDefault();
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

  let mode = "friend";
  let friendId = Number(event.currentTarget.dataset.friendId) || Number(activeFriend?.friend?.id) || 0;
  let participant = null;
  let silentInvite = true;
  if (composingNewBill && !editId) {
    mode = billWithMode();
    if (mode === "friend") {
      friendId = Number($("#billWithFriend")?.value || 0);
      if (!friendId) {
        $("#friendMsg").textContent = "Choose a friend, or switch to email / private link.";
        $("#friendMsg").className = "text-xs font-mono balance-negative";
        return;
      }
      participant = { account_id: paidBy === "friend" ? me.account.id : friendId, value: null };
    } else if (mode === "email") {
      const email = ($("#billWithEmail")?.value || "").trim().toLowerCase();
      if (!email) {
        $("#friendMsg").textContent = "Enter their email address.";
        $("#friendMsg").className = "text-xs font-mono balance-negative";
        return;
      }
      if (paidBy === "friend" || paidBy === "both") {
        // Email may belong to a non-user; only "I paid" is safe without an account id.
        // Existing users still work when payer is me; friend-paid needs account resolution server-side.
      }
      silentInvite = false;
      participant = { email, value: null };
    } else {
      if (paidBy !== "me") {
        $("#friendMsg").textContent = "Private-link bills start with you as the payer. They can settle after joining.";
        $("#friendMsg").className = "text-xs font-mono balance-negative";
        return;
      }
      silentInvite = true;
      participant = { label: "The other person", value: null };
    }
  } else if (!friendId) {
    $("#friendMsg").textContent = "Open a friend before adding a bill.";
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

  let payerAccountId = null;
  let payerPaidAmount = null;
  if (!participant) {
    payerAccountId = paidBy === "friend" ? friendId : null;
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
    participant = { account_id: participantId, value: participantValue };
  } else {
    participant.value = participantValue;
    if (mode === "friend") {
      payerAccountId = paidBy === "friend" ? friendId : null;
      participant.account_id = paidBy === "friend" ? me.account.id : friendId;
      if (paidBy === "both") {
        const mine = cents(Number($("#friendMyPaid").value));
        const theirs = cents(Number($("#friendTheirPaid").value));
        if (mine <= 0 || theirs <= 0 || cents(mine + theirs) !== cents(total)) {
          $("#friendMsg").textContent = "The two payments must both be greater than zero and add up to the bill.";
          $("#friendMsg").className = "text-xs font-mono balance-negative";
          return;
        }
        payerAccountId = null;
        participant.account_id = friendId;
        payerPaidAmount = mine;
      }
    } else if (mode === "email" && (paidBy === "friend" || paidBy === "both")) {
      $("#friendMsg").textContent = "For email invites, choose “I paid” unless they are already a friend on your list.";
      $("#friendMsg").className = "text-xs font-mono balance-negative";
      return;
    }
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
    silent_invite: silentInvite,
    participants: [participant],
  };
  try {
    let created = null;
    if (editId) {
      await ShareSquad.api(`/quick-splits/${editId}`, { method: "PUT", body: JSON.stringify(body) });
      ShareSquad.showToast("Bill updated", "success");
    } else {
      created = await ShareSquad.api("/quick-splits", { method: "POST", body: JSON.stringify(body) });
      ShareSquad.showToast(mode === "links" ? "Bill created — copy the private link" : "Private bill added", "success");
    }
    composingNewBill = false;
    resetExpenseForm();
    await refresh();
    const otherId = friendId
      || created?.participants?.find((p) => p.account_id && p.account_id !== me.account.id)?.account_id
      || ((created?.payer_account_id && created.payer_account_id !== me.account.id) ? created.payer_account_id : null);
    if (mode === "links" && created?.id) {
      location.href = `/quick-split.html?id=${created.id}`;
      return;
    }
    if (otherId) openFriend(`account-${otherId}`);
    else if (created?.id) location.href = `/quick-split.html?id=${created.id}`;
  } catch (err) {
    $("#friendMsg").textContent = err.message;
    $("#friendMsg").className = "text-xs font-mono balance-negative";
  }
};
(async () => {
  if (!ShareSquad.getToken()) { ShareSquad.goToLogin({ replace: true }); return; }
  ShareSquad.fillAllCurrencySelects();
  ShareSquad.bindAllCategorySelects();
  $("#friendExpenseDate").max = todayISO();
  $("#friendExpenseDate").value = todayISO();
  renderValues();
  try {
    me = await ShareSquad.api("/auth/me");
    $("#headerInitials").textContent = ShareSquad.initials(me.account.display_name);
    if (me.account.has_avatar) {
      ShareSquad.setAuthImage($("#headerAvatar"), "/auth/me/avatar");
      $("#headerInitials").classList.add("hidden");
    }
    await refresh();
    fillBillWithFriends();
    const params = new URLSearchParams(location.search);
    const selectedFriendId = Number(params.get("friend"));
    if (selectedFriendId) {
      openedFromBalances = true;
      openFriend(`account-${selectedFriendId}`);
      if (params.get("bill") === "1") focusBillForm();
    } else if (params.get("new") === "1" || params.get("add") === "1") {
      openComposeBill();
    }
  } catch (err) {
    ShareSquad.showToast(err.message || "Could not load friend splits", "error");
  }
  if (window.lucide) lucide.createIcons();
})();
