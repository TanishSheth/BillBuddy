document.querySelectorAll('a[href="/"]').forEach((link) => { link.href = "/index.html"; });
const $money = (v, c) => ShareSquad.formatMoney(v, c);
const cents = (value) => Math.round((Number(value) || 0) * 100) / 100;
const splitId = () => new URLSearchParams(location.search).get("id");
let currentSplit = null;

function friendAccountId(split) {
  if (!split) return null;
  if (split.is_payer) return split.participants?.[0]?.account_id || null;
  return split.payer_account_id || null;
}

function friendsHref(accountId, extras = {}) {
  const params = new URLSearchParams({ friend: String(accountId), ...extras });
  return `/friends.html?${params}`;
}

function otherName(split = currentSplit) {
  const person = split?.participants?.find((p) => !p.is_me) || split?.participants?.[0];
  return person?.display_name || ShareSquad.maskEmail(person?.email) || "Friend";
}

function hasLinkedFriend(split = currentSplit) {
  return Boolean(friendAccountId(split));
}

function friendShareSelected(person) {
  return $(`#friendShare${person}`)?.dataset.selected === "true";
}

function setFriendShareSelected(person, selected) {
  const chip = $(`#friendShare${person}`);
  if (!chip) return;
  chip.dataset.selected = String(selected);
  chip.classList.toggle("selected", selected);
  chip.setAttribute("aria-pressed", String(selected));
}

function renderValues() {
  const paidBy = $("#friendPaidBy")?.value || "me";
  const friendPaid = paidBy === "friend";
  const shareMe = friendShareSelected("Me");
  const shareFriend = friendShareSelected("Friend");
  const onePersonCovers = shareMe !== shareFriend;
  $("#friendSplitTypeWrap")?.classList.toggle("hidden", onePersonCovers);
  $("#friendValues")?.classList.toggle("hidden", onePersonCovers || $("#friendExpenseType").value === "EQUAL");
  $("#friendPaySplit")?.classList.toggle("hidden", paidBy !== "both");
  const name = otherName();
  if ($("#friendShareFriend")) $("#friendShareFriend").textContent = name;
  if ($("#friendPayerValueLabel")) $("#friendPayerValueLabel").textContent = friendPaid ? `${name}'s value` : "Your value";
  if ($("#friendOtherValueLabel")) $("#friendOtherValueLabel").textContent = friendPaid ? "Your value" : `${name}'s value`;

  const total = Number($("#friendExpenseAmount")?.value) || 0;
  const mine = Number($("#friendMyPaid")?.value) || 0;
  const theirs = Number($("#friendTheirPaid")?.value) || 0;
  const notice = $("#friendPayNotice");
  if (notice) {
    notice.textContent = total
      ? `Paid ${cents(mine + theirs).toFixed(2)} of ${total.toFixed(2)}.`
      : "Enter the cash each of you paid. The two amounts must add up to the bill.";
  }

  const shareNotice = $("#friendShareNotice");
  if (!shareNotice) return;
  const money = ShareSquad.formatMoney(total, $("#friendExpenseCurrency")?.value || "USD");
  const setOutcome = (message, tone = "is-neutral") => {
    shareNotice.className = `text-[10px] font-mono mt-2 split-outcome ${tone}`;
    shareNotice.innerHTML = message;
  };
  const splitType = $("#friendExpenseType")?.value || "EQUAL";
  const payerValue = Number($("#friendPayerValue")?.value) || 0;
  const otherValue = Number($("#friendOtherValue")?.value) || 0;
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
  const myPaid = Number($("#friendMyPaid")?.value) || 0;
  const friendPaidAmount = Number($("#friendTheirPaid")?.value) || 0;
  const bothPaidOutcome = (myOwedShare) => {
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
    if (paidBy === "both") bothPaidOutcome(shareMe ? total : 0);
    else if (person === payer) setOutcome(`${ShareSquad.escapeHtml(personLabel)} paid and covers the full ${money}.`);
    else if (person === "you") setOutcome(`You owe ${ShareSquad.escapeHtml(name)} the full ${money}.`, "is-owed");
    else setOutcome(`${ShareSquad.escapeHtml(name)} owes you the full ${money}.`, "is-lent");
  } else if (paidBy === "both") bothPaidOutcome(myShare);
  else if (paidBy === "me" && total) setOutcome(`${ShareSquad.escapeHtml(name)} owes you ${ShareSquad.formatMoney(friendShare, $("#friendExpenseCurrency").value || "USD")}.`, "is-lent");
  else if (paidBy === "friend" && total) setOutcome(`You owe ${ShareSquad.escapeHtml(name)} ${ShareSquad.formatMoney(myShare, $("#friendExpenseCurrency").value || "USD")}.`, "is-owed");
  else setOutcome(`You and ${ShareSquad.escapeHtml(name)} share this cost.`);
}

function syncPendingInvitePaidByOptions() {
  const linked = hasLinkedFriend();
  const paidBy = $("#friendPaidBy");
  if (!paidBy) return;
  [...paidBy.options].forEach((opt) => {
    if (opt.value === "friend" || opt.value === "both") opt.disabled = !linked;
  });
  if (!linked && paidBy.value !== "me") paidBy.value = "me";
  const friendOpt = paidBy.querySelector('option[value="friend"]');
  if (friendOpt) friendOpt.textContent = `${otherName()} paid the full bill`;
}

function openEdit(split) {
  currentSplit = split;
  const panel = $("#friendExpensePanel");
  panel.classList.remove("hidden");
  $("#friendExpenseEditId").value = split.id;
  $("#friendExpenseHeading").textContent = "Edit bill";
  $("#friendExpenseHelp").textContent = "Same private-bill form as Friends. The invite link stays the same.";
  $("#friendExpenseSubmit").textContent = "Save changes";
  $("#cancelFriendExpenseEdit").classList.remove("hidden");
  $("#friendMsg").textContent = "";

  $("#friendExpenseTitle").value = String(split.title || "").trim();
  $("#friendExpenseAmount").value = split.total_amount;
  ShareSquad.fillCurrencySelect($("#friendExpenseCurrency"), split.currency || "USD");
  ShareSquad.fillCategorySelect($("#friendExpenseCategory"), split.category || "General", { allowCustom: true });
  if (!ShareSquad.isKnownCategory(split.category) && $("#friendExpenseCategoryCustom")) {
    $("#friendExpenseCategoryCustom").value = split.category || "";
  }
  $("#friendExpenseDate").value = String(split.bill_date || "").slice(0, 10);
  $("#friendExpenseDate")._flatpickr?.setDate($("#friendExpenseDate").value, true);

  let paidBy = "me";
  if (split.payer_paid_amount != null) paidBy = "both";
  else if (!split.is_payer) paidBy = "friend";
  syncPendingInvitePaidByOptions();
  if (!hasLinkedFriend() && paidBy !== "me") paidBy = "me";
  $("#friendPaidBy").value = paidBy;
  if (paidBy === "both") {
    const mine = split.is_payer ? Number(split.payer_paid_amount) : cents(Number(split.total_amount) - Number(split.payer_paid_amount));
    $("#friendMyPaid").value = mine;
    $("#friendTheirPaid").value = cents(Number(split.total_amount) - mine);
  } else {
    $("#friendMyPaid").value = "";
    $("#friendTheirPaid").value = "";
  }

  $("#friendExpenseType").value = split.split_type || "EQUAL";
  const meParticipant = split.participants.find((item) => item.is_me);
  const friendParticipant = split.participants.find((item) => !item.is_me) || split.participants[0];
  const myShare = paidBy === "friend" ? Number(meParticipant?.share_amount || 0) : Number(split.payer_share_amount || 0);
  const friendShareAmt = paidBy === "friend" ? Number(split.payer_share_amount || 0) : Number(friendParticipant?.share_amount || 0);
  setFriendShareSelected("Me", myShare > 0.004 || !friendShareAmt);
  setFriendShareSelected("Friend", friendShareAmt > 0.004 || !myShare);
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
      if (split.split_type === "PERCENTAGE" && theirValue != null) {
        $("#friendPayerValue").value = cents(100 - Number(theirValue));
        $("#friendOtherValue").value = theirValue;
      } else if (split.split_type === "SHARES" && theirValue != null) {
        const otherShare = Number(friendParticipant?.share_amount || 0);
        $("#friendOtherValue").value = theirValue;
        if (Number(theirValue) > 0 && otherShare > 0) {
          const unit = otherShare / Number(theirValue);
          $("#friendPayerValue").value = unit ? (Number(split.payer_share_amount) / unit).toFixed(4).replace(/\.?0+$/, "") : "1";
        } else {
          $("#friendPayerValue").value = "1";
        }
      } else {
        $("#friendPayerValue").value = split.payer_share_amount;
        $("#friendOtherValue").value = theirValue ?? friendParticipant?.share_amount ?? "";
      }
    }
  } else {
    $("#friendPayerValue").value = "";
    $("#friendOtherValue").value = "";
  }

  renderValues();
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
  setTimeout(() => $("#friendExpenseTitle")?.focus(), 250);
}

function closeEdit() {
  $("#friendExpensePanel")?.classList.add("hidden");
  $("#friendMsg").textContent = "";
  $("#friendExpenseEditId").value = "";
}

function render(split) {
  currentSplit = split;
  const accountId = friendAccountId(split);
  // Account-linked 1:1 bills are managed on Friends (same panel).
  if (accountId) {
    location.replace(friendsHref(accountId));
    return;
  }

  closeEdit();
  $("#detailPane").classList.remove("hidden");
  $("#detailTitle").textContent = split.title;
  $("#detailMeta").textContent = `${ShareSquad.formatDate(split.bill_date)} · ${split.category} · ${split.split_type.toLowerCase()} split`;
  $("#detailStatus").textContent = split.status === "SETTLED" ? "Settled" : "Open";
  $("#detailPayer").innerHTML = `${ShareSquad.escapeHtml(split.payer_name)} paid · their share ${$money(split.payer_share_amount, split.currency)}`;
  $("#detailTotal").innerHTML = $money(split.total_amount, split.currency);

  const edit = $("#editQuickSplit");
  edit.classList.toggle("hidden", !split.is_owner);
  edit.onclick = () => openEdit(split);

  const remove = $("#deleteQuickSplit");
  remove.classList.toggle("hidden", !split.is_owner);
  remove.onclick = async () => {
    if (!await ShareSquad.confirmDialog({
      title: `Delete “${split.title}”?`,
      message: "This private split will be removed. You can undo for a few seconds.",
      confirmLabel: "Delete split",
    })) return;
    const detail = $("#detailPane");
    ShareSquad.runWithUndo({
      message: `Split “${split.title}” deleted`,
      onHide: () => {
        detail?.classList.add("hidden");
        location.href = "/friends.html";
      },
      onUndo: () => detail?.classList.remove("hidden"),
      onCommit: async () => {
        await ShareSquad.api(`/quick-splits/${split.id}`, { method: "DELETE" });
        ShareSquad.showToast("Quick split deleted", "success");
      },
    });
  };

  const list = $("#participantList");
  list.innerHTML = split.participants.map((p) => {
    const label = p.display_name || ShareSquad.maskEmail(p.email) || "The other person";
    const remaining = Number(p.remaining_amount ?? p.share_amount);
    const paid = Number(p.paid_amount || 0);
    const status = String(p.payment_status || "").toUpperCase();
    const awaiting = status === "MARKED" && remaining > 0.004;
    let button = `<span class="text-[10px] font-mono text-ink/50">Paid</span>`;
    if (awaiting && split.is_payer) {
      button = `<button class="bb-btn-primary !py-1.5 !px-3 text-[10px] confirm-paid" data-id="${p.id}">Confirm received</button>`;
    } else if (awaiting && p.is_me) {
      button = `<span class="text-[10px] font-mono text-ink/50">Waiting for confirmation</span>`;
    } else if (remaining > 0.004 && (p.is_me || split.is_payer) && !awaiting) {
      button = `<button class="bb-btn-primary !py-1.5 !px-3 text-[10px] mark-paid" data-id="${p.id}">${p.is_me ? "Mark remaining paid" : "Mark remaining received"}</button>`;
    }
    const progress = awaiting
      ? ` · marked paid — awaiting confirmation`
      : paid > 0 && remaining > 0
        ? ` · ${$money(paid, split.currency)} paid, ${$money(remaining, split.currency)} remaining`
        : remaining > 0
          ? ` · ${$money(remaining, split.currency)} remaining`
          : " · Paid";
    return `<div class="member-row"><div><div class="font-semibold text-sm">${ShareSquad.escapeHtml(label)}${p.is_me ? " (you)" : ""}</div><div class="text-xs text-ink/60">Share ${$money(p.share_amount, split.currency)}${progress}</div></div>${button}</div>`;
  }).join("");
  list.querySelectorAll(".mark-paid").forEach((b) => {
    b.onclick = async () => render(await ShareSquad.api(`/quick-splits/${split.id}/participants/${b.dataset.id}/mark-paid`, { method: "POST" }));
  });
  list.querySelectorAll(".confirm-paid").forEach((b) => {
    b.onclick = async () => render(await ShareSquad.api(`/quick-splits/${split.id}/participants/${b.dataset.id}/confirm`, { method: "POST" }));
  });

  if (split.is_payer) {
    $("#sharePanel").classList.remove("hidden");
    const person = split.participants[0];
    $("#shareLink").value = `${location.origin}/quick-join.html?code=${encodeURIComponent(split.invite_code)}&slot=${encodeURIComponent(person.invite_token || "")}`;
  } else {
    $("#sharePanel").classList.add("hidden");
  }
}

async function load() {
  const id = splitId();
  if (!id) {
    location.replace("/friends.html?new=1");
    return;
  }
  try {
    render(await ShareSquad.api(`/quick-splits/${id}`));
  } catch (err) {
    ShareSquad.showToast(err.message, "error");
  }
}

$("#copyShareLink")?.addEventListener("click", async () => {
  await navigator.clipboard.writeText($("#shareLink").value);
  ShareSquad.showToast("Private invite link copied", "success");
});
$("#cancelFriendExpenseEdit")?.addEventListener("click", closeEdit);
$("#friendExpenseType")?.addEventListener("change", renderValues);
$("#friendPaidBy")?.addEventListener("change", renderValues);
$("#friendPayerValue")?.addEventListener("input", renderValues);
$("#friendOtherValue")?.addEventListener("input", renderValues);
$("#friendExpenseAmount")?.addEventListener("input", renderValues);
$("#friendMyPaid")?.addEventListener("input", renderValues);
$("#friendTheirPaid")?.addEventListener("input", renderValues);
["Me", "Friend"].forEach((person) => {
  $(`#friendShare${person}`)?.addEventListener("click", () => {
    const selected = friendShareSelected(person);
    const other = person === "Me" ? "Friend" : "Me";
    if (selected && !friendShareSelected(other)) return;
    setFriendShareSelected(person, !selected);
    renderValues();
  });
});

$("#friendExpenseForm")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!currentSplit) return;
  const msg = $("#friendMsg");
  const type = $("#friendExpenseType").value;
  const paidBy = $("#friendPaidBy").value;
  const total = Number($("#friendExpenseAmount").value);
  const editId = Number($("#friendExpenseEditId").value) || currentSplit.id;
  const shareMe = friendShareSelected("Me");
  const shareFriend = friendShareSelected("Friend");
  const other = currentSplit.participants.find((p) => !p.is_me) || currentSplit.participants[0];

  if (!shareMe && !shareFriend) {
    msg.textContent = "Choose at least one person who should cover this bill.";
    msg.className = "text-xs font-mono balance-negative";
    return;
  }
  if (!hasLinkedFriend() && paidBy !== "me") {
    msg.textContent = "Until they join, keep yourself as the payer on private-link bills.";
    msg.className = "text-xs font-mono balance-negative";
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
  if (paidBy === "both") {
    const mine = cents(Number($("#friendMyPaid").value));
    const theirs = cents(Number($("#friendTheirPaid").value));
    if (mine <= 0 || theirs <= 0 || cents(mine + theirs) !== cents(total)) {
      msg.textContent = "The two payments must both be greater than zero and add up to the bill.";
      msg.className = "text-xs font-mono balance-negative";
      return;
    }
    payerPaidAmount = mine;
  } else if (paidBy === "friend" && other?.account_id) {
    payerAccountId = other.account_id;
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
    participants: [{
      email: (other?.email && !String(other.email).endsWith("@invite.local")) ? other.email : null,
      account_id: other?.account_id || null,
      label: other?.display_name || "The other person",
      value: participantValue,
    }],
  };

  if (!body.title || !(body.total_amount > 0) || !body.bill_date) {
    msg.textContent = "Enter a title, amount, and date.";
    msg.className = "text-xs font-mono balance-negative";
    return;
  }
  if (effectiveType !== "EQUAL" && !onePersonCovers && (!Number.isFinite(body.payer_value) || !Number.isFinite(body.participants[0].value))) {
    msg.textContent = "Enter values for you and the other person.";
    msg.className = "text-xs font-mono balance-negative";
    return;
  }

  try {
    const updated = await ShareSquad.api(`/quick-splits/${editId}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
    ShareSquad.showToast("Bill updated", "success");
    render(updated);
  } catch (err) {
    msg.textContent = err.message || "Could not save changes";
    msg.className = "text-xs font-mono balance-negative";
  }
});

(async () => {
  if (!ShareSquad.getToken()) {
    ShareSquad.goToLogin({ replace: true });
    return;
  }
  ShareSquad.fillAllCurrencySelects();
  ShareSquad.bindAllCategorySelects();
  if ($("#friendExpenseDate")) $("#friendExpenseDate").max = new Date().toISOString().slice(0, 10);
  await load();
})();
