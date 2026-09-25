// ShareSquad Group Controller with Custom Splits, Multi-Currency, & Categories

const state = {
  account: null,
  group: null,
  currentGroupId: null,
  members: [],
  periods: [],
  currentPeriod: null,
  currentUploadId: null,
  editingExpenseId: null,
  splitType: "EQUAL", // EQUAL | PERCENTAGE | EXACT | SHARES
  payMode: "single",
  lastBalances: [],
  selectedSplit: new Set(),
  splitValues: {}, // { member_id: number }
  myMemberId: null,
  myRole: null,
  groupSettings: null,
  accountSettings: null,
  currentExpenses: [],
  settleSelectQuiet: false,
  syncVersion: null,
  syncTimer: null,
  syncBusy: false,
  eventAbort: null,
  eventRetryTimer: null,
  eventRefreshTimer: null,
  eventsStopped: false,
  isDraft: false,
  commentingExpenseId: null,
};

function gid() {
  return state.currentGroupId;
}

function groupCurrency() {
  return state.group?.currency || "USD";
}

function expenseCurrency() {
  return ($("#f_currency")?.value || groupCurrency()).toUpperCase();
}

function syncExpenseCurrencyUI() {
  const code = expenseCurrency();
  const symEl = $("#f_currency_symbol");
  if (symEl) {
    symEl.textContent = ShareSquad.currencySymbol(code);
    symEl.classList.add("currency-sym");
  }
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------
const GROUP_PAGE_HELP = {
  upload: "<strong>Add / Scan:</strong> Upload a receipt or enter a bill, choose who paid and who shares it, then save it to this month.",
  expenses: "<strong>Expenses:</strong> Review this month's bills, search or filter them, and open an item to see its split and notes.",
  balances: "<strong>Balances & settlement:</strong> Who pays whom follows who paid each bill. Friends uses that same plan per person, then adds any direct bills between you two. Record payments from Friends after the owner settles the month.",
  insights: "<strong>Insights:</strong> See this month's spending total, top categories, and largest expenses.",
  activity: "<strong>Activity:</strong> Follow group changes, expenses, comments, and payment updates in time order.",
  reports: "<strong>PDF Reports:</strong> Generate or download a clear monthly record of expenses, balances, and confirmed settlements.",
  members: "<strong>Members:</strong> Add friends directly, send an email, or share a link. Email and link joins add members automatically. Only the owner can remove members or change group details.",
};

function updateGroupPageHelp(tabName) {
  const help = $("#groupPageHelp");
  if (help) help.innerHTML = GROUP_PAGE_HELP[tabName] || GROUP_PAGE_HELP.upload;
}

function switchTab(tabName) {
  $$(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tabName));
  $$(".tab-panel").forEach((p) => p.classList.toggle("hidden", p.id !== `tab-${tabName}`));
  updateGroupPageHelp(tabName);

  if (tabName === "expenses") loadExpenses();
  if (tabName === "balances") loadBalances();
  if (tabName === "reports") loadReports();
  if (tabName === "members") loadMembers();
  if (tabName === "insights") loadInsights();
  if (tabName === "activity") loadActivity();
}

function focusNotificationTarget() {
  const params = new URLSearchParams(window.location.search);
  const requestedTab = params.get("tab");
  const focus = params.get("focus");
  if (requestedTab && GROUP_PAGE_HELP[requestedTab]) switchTab(requestedTab);
  if (!focus) return;
  setTimeout(() => {
    // Older notifications only identify the expense list. Highlight its most
    // recent visible item so the user still lands on a concrete change.
    const target = focus === "expensesBody"
      ? document.querySelector("#expensesBody tr, #expensesMobileList [data-expense-id]") || document.getElementById("expensesBody")
      : document.getElementById(focus) || document.querySelector(`[data-expense-id="${CSS.escape(focus.replace("expense-", ""))}"]`);
    if (!target) return;
    target.scrollIntoView({ behavior: "smooth", block: "center" });
    target.classList.add("notification-focus");
    setTimeout(() => target.classList.remove("notification-focus"), 2100);
  }, 280);
}

function applyDraftGroupState() {
  const activeCount = (state.members || []).filter((member) => member.is_active !== false).length;
  state.isDraft = activeCount < 2;
  $("#draftGroupNotice")?.classList.toggle("hidden", !state.isDraft);
  ["#fileInputGallery", "#fileInputCamera", "#pickCameraBtn", "#manualEntryBtn"].forEach((selector) => {
    const control = $(selector);
    if (control) control.disabled = state.isDraft;
  });
  const galleryButton = $("#pickGalleryBtn");
  galleryButton?.classList.toggle("pointer-events-none", state.isDraft);
  galleryButton?.classList.toggle("opacity-50", state.isDraft);
  $("#dropzone")?.classList.toggle("opacity-50", state.isDraft);
  $("#dropzone")?.classList.toggle("pointer-events-none", state.isDraft);
}

// ---------------------------------------------------------------------------
// Group & Period Loading
// ---------------------------------------------------------------------------
async function loadGroup() {
  const group = await ShareSquad.api(`/groups/${gid()}`);
  state.group = group;
  state.members = group.members;
  applyDraftGroupState();

  const me = state.account;
  const myMem = group.members.find((m) => m.email === me.email);
  if (myMem) {
    state.myMemberId = Number(myMem.id);
    state.myRole = myMem.role;
  }

  const isOwner = state.myRole === "owner";
  $("#ownerTools")?.classList.toggle("hidden", !isOwner);
  $("#removeGroupPhotoBtn")?.classList.toggle("hidden", !isOwner || !group.has_cover);
  $("#currentGroupName").textContent = group.name;
  if (group.has_cover) {
    ShareSquad.setAuthImage($("#groupCoverImg"), `/groups/${gid()}/cover`);
    $("#groupCoverImg")?.classList.remove("hidden");
  } else {
    $("#groupCoverImg")?.classList.add("hidden");
  }
  const gCurr = group.currency || "USD";
  $("#groupCurrencyBadge").innerHTML = `<span class="currency-sym">${ShareSquad.escapeHtml(ShareSquad.currencySymbol(gCurr))}</span> ${gCurr}`;

  ShareSquad.fillCurrencySelect($("#f_currency"), gCurr);
  syncExpenseCurrencyUI();

  renderPayerOptions();
  populateMemberFilter();
  applyDefaultSplit();
  renderSplitOptions();
}

function populateMemberFilter() {
  const select = $("#expenseMemberFilter");
  if (!select) return;
  const selected = select.value;
  select.innerHTML = '<option value="">All members</option>' + activeMembers().map((member) => `<option value="${member.id}">${ShareSquad.escapeHtml(member.display_name)}</option>`).join("");
  select.value = selected;
}

async function loadGroupSettings() {
  try {
    state.groupSettings = await ShareSquad.api(`/settings/groups/${gid()}`);
  } catch (err) {
    state.groupSettings = { equal_split_default: true };
  }
}

function openRenameGroupModal() {
  const modal = $("#renameGroupModal");
  const input = $("#renameGroupInput");
  const msg = $("#renameGroupMsg");
  if (!modal || !input) return;
  if (msg) {
    msg.textContent = "";
    msg.className = "text-xs font-mono";
  }
  input.value = state.group?.name || "";
  modal.classList.remove("hidden");
  setTimeout(() => {
    input.focus();
    input.select();
  }, 50);
}

function closeRenameGroupModal() {
  $("#renameGroupModal")?.classList.add("hidden");
}

async function submitRenameGroup(e) {
  e.preventDefault();
  const input = $("#renameGroupInput");
  const msg = $("#renameGroupMsg");
  const next = (input?.value || "").trim();
  if (!next) {
    if (msg) {
      msg.textContent = "Enter a group name.";
      msg.className = "text-xs font-mono balance-negative";
    }
    return;
  }
  const btn = $("#saveRenameGroupBtn");
  if (btn) btn.disabled = true;
  if (msg) {
    msg.textContent = "Saving…";
    msg.className = "text-xs font-mono text-ink/50";
  }
  try {
    const updated = await ShareSquad.api(`/groups/${gid()}`, {
      method: "PATCH",
      body: JSON.stringify({ name: next }),
    });
    state.group = updated;
    $("#currentGroupName").textContent = updated.name;
    closeRenameGroupModal();
    ShareSquad.showToast("Group name updated", "success");
  } catch (err) {
    if (msg) {
      msg.textContent = err.message;
      msg.className = "text-xs font-mono balance-negative";
    }
    ShareSquad.showToast(err.message, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

function applyDefaultSplit() {
  const useAll = state.groupSettings?.equal_split_default !== false;
  const active = (state.members || []).filter((m) => m.is_active !== false);
  if (useAll) {
    state.selectedSplit = new Set(active.map((m) => Number(m.id)));
  } else if (state.myMemberId != null) {
    state.selectedSplit = new Set([Number(state.myMemberId)]);
  } else {
    state.selectedSplit = new Set();
  }
}

async function loadPeriods() {
  const periods = await ShareSquad.api(`/groups/${gid()}/periods`);
  state.periods = periods;

  const select = $("#periodSelect");
  const urlPeriod = new URLSearchParams(window.location.search).get("period");
  const keep = state.currentPeriod || (urlPeriod && !state._periodFromUrlApplied ? urlPeriod : null);
  if (urlPeriod) state._periodFromUrlApplied = true;
  select.innerHTML = "";

  if (!periods.length) {
    const d = new Date();
    const currentKey = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
    const opt = document.createElement("option");
    opt.value = currentKey;
    opt.textContent = `${currentKey} (Current)`;
    select.appendChild(opt);
    state.currentPeriod = keep || currentKey;
  } else {
    periods.forEach((p) => {
      const opt = document.createElement("option");
      opt.value = p.period_key;
      opt.textContent = p.period_key;
      select.appendChild(opt);
    });
    const keys = periods.map((p) => p.period_key);
    state.currentPeriod = keep && keys.includes(keep) ? keep : periods[0].period_key;
  }

  select.value = state.currentPeriod;
  updatePeriodStatus();
}

function isExpenseFormOpen() {
  const panel = $("#extractionPanel");
  return panel && !panel.classList.contains("hidden");
}

async function refreshFromServer() {
  if (isExpenseFormOpen()) {
    const group = await ShareSquad.api(`/groups/${gid()}`);
    state.group = group;
    state.members = group.members;
  } else {
    await loadGroup();
  }
  await loadPeriods();
  await loadExpenses();
  await loadBalances();
  await loadReports();
  await loadMembers();
}

async function pollGroupSync() {
  if (document.hidden || state.syncBusy) return;
  state.syncBusy = true;
  try {
    const data = await ShareSquad.api(`/groups/${gid()}/sync`);
    if (!state.syncVersion) {
      state.syncVersion = data.version;
      return;
    }
    if (data.version !== state.syncVersion) {
      state.syncVersion = data.version;
      await refreshFromServer();
    }
  } catch (err) {
    if (err && /session expired/i.test(err.message || "")) {
      stopGroupSync();
    }
  } finally {
    state.syncBusy = false;
  }
}

function startGroupSync() {
  stopGroupSync();
  state.eventsStopped = false;
  pollGroupSync();
  // The event stream is immediate. This backup covers a dropped stream and
  // servers running in more than one process.
  state.syncTimer = setInterval(pollGroupSync, 5000);
  connectGroupEvents();
  document.addEventListener("visibilitychange", onSyncVisibility);
}

function stopGroupSync() {
  state.eventsStopped = true;
  if (state.syncTimer) {
    clearInterval(state.syncTimer);
    state.syncTimer = null;
  }
  if (state.eventRetryTimer) {
    clearTimeout(state.eventRetryTimer);
    state.eventRetryTimer = null;
  }
  state.eventAbort?.abort();
  state.eventAbort = null;
  document.removeEventListener("visibilitychange", onSyncVisibility);
}

function onSyncVisibility() {
  if (!document.hidden) pollGroupSync();
}

function scheduleLiveRefresh() {
  if (isExpenseFormOpen()) {
    ShareSquad.showToast("This group changed. Finish or cancel your entry to refresh.", "info");
    return;
  }
  clearTimeout(state.eventRefreshTimer);
  state.eventRefreshTimer = setTimeout(async () => {
    if (state.syncBusy) return;
    state.syncBusy = true;
    try {
      await refreshFromServer();
      ShareSquad.showToast("Group updated just now", "info", 1800);
    } catch (_) {
      // The periodic backup will retry if a transient network issue occurs.
    } finally {
      state.syncBusy = false;
    }
  }, 180);
}

async function connectGroupEvents() {
  if (state.eventsStopped || !gid()) return;
  const controller = new AbortController();
  state.eventAbort = controller;
  try {
    const response = await fetch(`/api/groups/${gid()}/events`, {
      headers: { Authorization: `Bearer ${ShareSquad.getToken()}` },
      signal: controller.signal,
    });
    if (!response.ok || !response.body) throw new Error("Live updates unavailable");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (!state.eventsStopped) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split("\n\n");
      buffer = chunks.pop() || "";
      chunks.forEach((chunk) => {
        if (chunk.startsWith("event: group-change")) scheduleLiveRefresh();
      });
    }
  } catch (_) {
    // Reconnect below; polling remains active in the meantime.
  } finally {
    if (state.eventAbort === controller) state.eventAbort = null;
    if (!state.eventsStopped) state.eventRetryTimer = setTimeout(connectGroupEvents, 3000);
  }
}

function updatePeriodStatus() {
  const periodRec = state.periods.find((p) => p.period_key === state.currentPeriod);
  const badge = $("#periodStatus");
  const reopenBtn = $("#reopenPeriodBtn");

  if (periodRec && periodRec.status === "SETTLED") {
    badge.textContent = "Settled";
    badge.className = "status-chip status-settled";
    reopenBtn.classList.remove("hidden");
  } else {
    badge.textContent = "Active";
    badge.className = "status-chip status-active";
    reopenBtn.classList.add("hidden");
  }
}

function isCurrentPeriodSettled() {
  return state.periods.some((period) => period.period_key === state.currentPeriod && period.status === "SETTLED");
}

// ---------------------------------------------------------------------------
// Expense Split UI & Math
// ---------------------------------------------------------------------------
function setSplitType(type) {
  state.splitType = type;
  $$("#splitTypeSelector .split-type-pill").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.type === type);
  });
  renderSplitOptions();
}

function activeMembers() {
  return (state.members || []).filter((m) => m.is_active !== false);
}

function renderPayerOptions() {
  const sel = $("#f_payer");
  const list = activeMembers();
  sel.innerHTML = list
    .map((m) => `<option value="${m.id}" ${m.id === state.myMemberId ? "selected" : ""}>${ShareSquad.escapeHtml(m.display_name)}</option>`)
    .join("");
  renderMultiPayers();
}

function setPayMode(mode) {
  state.payMode = mode === "multi" ? "multi" : "single";
  $$("#payModeSelector .split-type-pill").forEach((btn) => btn.classList.toggle("active", btn.dataset.payMode === state.payMode));
  $("#f_payer").classList.toggle("hidden", state.payMode === "multi");
  $("#multiPayerBox").classList.toggle("hidden", state.payMode !== "multi");
  renderMultiPayers();
}

function payerLines(expense) {
  const names = new Map(state.members.map((member) => [Number(member.id), member.display_name]));
  const currency = expense.currency || groupCurrency();
  if (expense.payers?.length) {
    return expense.payers.map((payer) => ({
      name: names.get(Number(payer.member_id)) || "Member",
      amount: ShareSquad.formatMoneyText(payer.amount_paid, currency),
    }));
  }
  return [{ name: names.get(Number(expense.payer_member_id)) || "Unknown", amount: "" }];
}

function renderMultiPayers() {
  const box = $("#multiPayerBox");
  if (!box) return;
  const members = activeMembers();
  const existing = [...box.querySelectorAll(".payer-amount")].map((input) => input.dataset.member).join(",");
  const next = members.map((member) => String(member.id)).join(",");
  if (existing !== next) {
    box.innerHTML = members.map((member) => {
      const value = state.payerAmounts?.[member.id] ?? "";
      return `<label class="flex items-center gap-2 text-sm"><span class="min-w-0 flex-1 truncate">${ShareSquad.escapeHtml(member.display_name)}</span><input class="bb-input payer-amount !w-28 shrink-0" data-member="${member.id}" type="text" inputmode="decimal" placeholder="0.00" value="${value}"></label>`;
    }).join("");
    box.querySelectorAll(".payer-amount").forEach((input) => {
      input.addEventListener("input", () => {
        state.payerAmounts = state.payerAmounts || {};
        state.payerAmounts[input.dataset.member] = input.value;
      });
    });
  }
}

function renderSplitOptions() {
  const container = $("#f_splitwith");
  const notice = $("#splitStatusNotice");
  if (!container || !notice) return;
  const totalAmt = parseFloat($("#f_amount").value) || 0;
  const curr = expenseCurrency();
  container.innerHTML = "";

  if (state.splitType === "EQUAL") {
    notice.textContent = "Select members to split equally:";
    const chipsWrap = document.createElement("div");
    chipsWrap.className = "flex flex-wrap gap-2";

    state.members.forEach((m) => {
      if (m.is_active === false) return;
      const memberId = Number(m.id);
      const isSelected = state.selectedSplit.has(memberId);
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = `bb-chip ${isSelected ? "selected" : ""}`;
      chip.dataset.id = String(memberId);
      chip.textContent = m.display_name;
      chip.addEventListener("click", () => {
        if (state.selectedSplit.has(memberId)) {
          if (state.selectedSplit.size > 1) state.selectedSplit.delete(memberId);
        } else {
          state.selectedSplit.add(memberId);
        }
        renderSplitOptions();
      });
      chipsWrap.appendChild(chip);
    });
    container.appendChild(chipsWrap);
    return;
  }

  // Percentage, Exact, or Shares custom inputs
  activeMembers().forEach((m) => {
    const val = state.splitValues[m.id] !== undefined ? state.splitValues[m.id] : (state.splitType === "SHARES" ? 1 : 0);
    const row = document.createElement("div");
    row.className = "flex items-center justify-between gap-3 p-2 bg-ink/5 rounded-lg text-xs font-mono";

    let label = ShareSquad.escapeHtml(m.display_name);
    let unit = state.splitType === "PERCENTAGE" ? "%" : state.splitType === "EXACT" ? curr : "shares";

    row.innerHTML = `
      <span class="font-semibold text-ink">${label}</span>
      <div class="flex items-center gap-1.5 w-36">
        <input type="text" step="${state.splitType === 'SHARES' ? '1' : '0.01'}" min="0" inputmode="decimal"
               class="split-val-input bb-input !p-1 text-right font-bold bg-white rounded border border-line" 
               data-id="${m.id}" value="${val}">
        <span class="text-ink/60 text-[11px] font-bold w-10">${unit}</span>
      </div>
    `;

    row.querySelector(".split-val-input").addEventListener("input", (e) => {
      state.splitValues[m.id] = parseFloat(e.target.value) || 0;
      updateSplitNotice();
    });

    container.appendChild(row);
  });

  updateSplitNotice();
}

function updateSplitNotice() {
  const notice = $("#splitStatusNotice");
  if (!notice) return;
  const totalAmt = parseFloat($("#f_amount").value) || 0;
  const curr = expenseCurrency();

  if (state.splitType === "PERCENTAGE") {
    const sumPct = Object.values(state.splitValues).reduce((a, b) => a + Number(b), 0);
    const diff = (100 - sumPct).toFixed(1);
    const ok = Math.abs(sumPct - 100) < 0.01;
    notice.textContent = ok ? "Total: 100% (Balanced)" : `Total: ${sumPct.toFixed(1)}% (${diff}% remaining to reach 100%)`;
    notice.className = `split-total-bar ${ok ? "ok" : sumPct > 100 ? "over" : "under"}`;
  } else if (state.splitType === "EXACT") {
    const sumAmt = Object.values(state.splitValues).reduce((a, b) => a + Number(b), 0);
    const diff = (totalAmt - sumAmt).toFixed(2);
    const ok = Math.abs(sumAmt - totalAmt) < 0.01;
    notice.textContent = ok
      ? `Total: ${ShareSquad.formatMoneyText(sumAmt, curr)} (Matches bill total)`
      : `Total: ${ShareSquad.formatMoneyText(sumAmt, curr)} (${ShareSquad.formatMoneyText(Math.abs(diff), curr)} ${Number(diff) >= 0 ? "remaining" : "over"})`;
    notice.className = `split-total-bar ${ok ? "ok" : sumAmt > totalAmt ? "over" : "under"}`;
  } else if (state.splitType === "SHARES") {
    const totalShares = Object.values(state.splitValues).reduce((a, b) => a + Number(b), 0);
    notice.textContent = `Total shares: ${totalShares}. Each share ≈ ${ShareSquad.formatMoneyText(totalAmt / (totalShares || 1), curr)}`;
    notice.className = "split-total-bar ok";
  } else {
    notice.textContent = "";
    notice.className = "text-[10px] font-mono mb-2 text-ink/60";
  }
}

function renderBalanceStrip(balances) {
  const strip = $("#balanceStrip");
  if (!strip) return;
  state.lastBalances = Array.isArray(balances) ? balances : [];
  if (!state.lastBalances.length) {
    strip.classList.add("hidden");
    strip.innerHTML = "";
    return;
  }
  const ordered = state.lastBalances.slice().sort((a, b) => {
    const aMe = Number(a.member_id) === Number(state.myMemberId) ? 0 : 1;
    const bMe = Number(b.member_id) === Number(state.myMemberId) ? 0 : 1;
    if (aMe !== bMe) return aMe - bMe;
    return String(a.name || "").localeCompare(String(b.name || ""), undefined, { sensitivity: "base" });
  });
  const chips = ordered.map((b) => {
    const net = b.converted_net_balance != null ? b.converted_net_balance : b.net_balance;
    const curr = b.converted_currency || b.currency || groupCurrency();
    const tone = net > 0.009 ? "owed" : net < -0.009 ? "owe" : "settled";
    const label = net > 0.009 ? "is owed" : net < -0.009 ? "owes" : "settled";
    const amount = Math.abs(net) < 0.005 ? ShareSquad.formatMoneyText(0, curr) : ShareSquad.formatMoneyText(Math.abs(net), curr);
    const mine = Number(b.member_id) === Number(state.myMemberId) ? " is-me" : "";
    return `<div class="balance-chip ${tone}${mine}" title="${ShareSquad.escapeHtml(b.name)} ${label} ${amount}">
      <span class="balance-chip-name">${ShareSquad.escapeHtml(b.name)}</span>
      <span class="balance-chip-amt">${tone === "settled" ? "✓" : (tone === "owed" ? "+" : "−")}${tone === "settled" ? "" : amount}</span>
    </div>`;
  }).join("");
  strip.innerHTML = `<div class="balance-strip-label">This month</div><div class="balance-strip-grid">${chips}</div>`;
  strip.classList.remove("hidden");
}

// ---------------------------------------------------------------------------
// Expenses Tab
// ---------------------------------------------------------------------------
async function loadExpenses() {
  const filterCat = $("#groupCategoryFilter").value;
  const params = new URLSearchParams({ period_key: state.currentPeriod });
  if (filterCat !== "All") params.set("category", filterCat);
  if ($("#expenseSearch")?.value.trim()) params.set("search", $("#expenseSearch").value.trim());
  if ($("#expenseMemberFilter")?.value) params.set("member_id", $("#expenseMemberFilter").value);
  if ($("#onlyMyExpenses")?.checked) params.set("involved_me", "true");
  const expenses = await ShareSquad.api(`/groups/${gid()}/expenses?${params}`);
  state.currentExpenses = expenses;

  const tbody = $("#expensesBody");
  const mobileList = $("#expensesMobileList");
  const empty = $("#expensesEmpty");
  tbody.innerHTML = "";
  mobileList.innerHTML = "";

  if (!expenses.length) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");

  const memberMap = new Map(state.members.map((m) => [m.id, m.display_name]));

  expenses.forEach((e) => {
    const payers = payerLines(e);
    const payerHtml = payers.map((payer) => `<div class="whitespace-nowrap">${ShareSquad.escapeHtml(payer.name)}${payer.amount ? ` <span class="text-ink/55">${ShareSquad.escapeHtml(payer.amount)}</span>` : ""}</div>`).join("");
    const splitNames = e.splits.map((s) => memberMap.get(s.member_id) || "?").join(", ");
    const catMark = ShareSquad.categoryMark(e.category);

    // Desktop Row
    const tr = document.createElement("tr");
    tr.className = "border-b border-ink/5 hover:bg-ink/5 transition-colors";
    tr.dataset.expenseId = e.id;
    tr.innerHTML = `
      <td class="py-2.5 pr-3 text-ink/70">${e.bill_date}</td>
      <td class="py-2.5 pr-3 font-bold text-ink">${ShareSquad.escapeHtml(e.store_name)}</td>
      <td class="py-2.5 pr-3"><span ${catMark}>${ShareSquad.escapeHtml(e.category)}</span></td>
      <td class="py-2.5 pr-3 text-ink/80">${payerHtml}</td>
      <td class="py-2.5 pr-3 text-ink/60 text-[11px]">${ShareSquad.escapeHtml(e.split_type || "EQUAL")} (${splitNames})</td>
      <td class="py-2.5 pr-3 text-right font-bold text-brand">${ShareSquad.formatMoney(e.total_amount, e.currency || groupCurrency())}</td>
      <td class="py-2.5 text-right">
        <div class="flex items-center justify-end gap-1">
          <button class="edit-exp p-1 hover:text-brand" data-id="${e.id}" title="Edit"><i data-lucide="edit-2" class="w-3.5 h-3.5"></i></button>
          <button class="comment-exp p-1 hover:text-brand" data-id="${e.id}" title="Comments"><i data-lucide="message-circle" class="w-3.5 h-3.5"></i></button>
          <button class="delete-exp p-1 hover:text-stamp" data-id="${e.id}" data-name="${ShareSquad.escapeHtml(e.store_name)}" title="Delete"><i data-lucide="trash-2" class="w-3.5 h-3.5"></i></button>
        </div>
      </td>
    `;
    tbody.appendChild(tr);

    // Mobile Card
    const card = document.createElement("div");
    card.className = "bb-mobile-card";
    card.dataset.expenseId = e.id;
    card.innerHTML = `
      <div class="card-top">
        <div>
          <div class="card-store">${ShareSquad.escapeHtml(e.store_name)}</div>
          <span ${ShareSquad.categoryMark(e.category, "category-tag mt-1")}>${ShareSquad.escapeHtml(e.category)}</span>
        </div>
        <div class="card-amount text-brand">${ShareSquad.formatMoney(e.total_amount, e.currency || groupCurrency())}</div>
      </div>
      <div class="card-meta">
        <div>${e.bill_date} &bull; Paid by</div>
        <div class="mt-1">${payerHtml}</div>
        <div class="text-[10px] text-ink/40 mt-0.5">Split: ${splitNames}</div>
      </div>
      <div class="card-actions">
        <button class="edit-exp bb-btn-secondary !py-1 !px-2 text-xs flex-1" data-id="${e.id}"><i data-lucide="edit-2" class="w-3 h-3"></i> Edit</button>
        <button class="comment-exp bb-btn-secondary !py-1 !px-2 text-xs flex-1" data-id="${e.id}"><i data-lucide="message-circle" class="w-3 h-3"></i> Notes</button>
        <button class="delete-exp bb-btn-secondary !py-1 !px-2 text-xs text-stamp border-stamp/30 hover:bg-stamp/5 flex-1" data-id="${e.id}" data-name="${ShareSquad.escapeHtml(e.store_name)}"><i data-lucide="trash-2" class="w-3 h-3"></i> Delete</button>
      </div>
    `;
    mobileList.appendChild(card);
  });

  lucide.createIcons();

  $$(".edit-exp").forEach((btn) => {
    btn.addEventListener("click", () => {
      const exp = state.currentExpenses.find((x) => x.id === Number(btn.dataset.id));
      if (exp) startEditExpense(exp);
    });
  });

  $$(".delete-exp").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const name = btn.dataset.name;
      const id = Number(btn.dataset.id);
      if (!await ShareSquad.confirmDialog({
        title: `Delete “${name}”?`,
        message: "This expense will be removed and balances will update. You can undo for a few seconds.",
        confirmLabel: "Delete expense",
      })) return;
      const override = state.myRole === "owner" ? "?owner_override=true" : "";
      ShareSquad.runWithUndo({
        message: `Expense “${name}” deleted`,
        onHide: () => {
          state.currentExpenses = state.currentExpenses.filter((x) => x.id !== id);
          $$(`.delete-exp[data-id="${id}"]`).forEach((el) => {
            el.closest("tr")?.remove();
            el.closest(".bb-mobile-card")?.remove();
          });
          if (!state.currentExpenses.length) $("#expensesEmpty")?.classList.remove("hidden");
        },
        onUndo: () => loadExpenses(),
        onCommit: async () => {
          await ShareSquad.api(`/groups/${gid()}/expenses/${id}${override}`, { method: "DELETE" });
          if (typeof loadBalances === "function") {
            try { await loadBalances(); } catch (_) {}
          }
        },
      });
    });
  });

  $$(".comment-exp").forEach((btn) => btn.addEventListener("click", () => openComments(Number(btn.dataset.id))));
}

function startEditExpense(exp) {
  state.editingExpenseId = exp.id;
  $("#extractionPanel").classList.remove("hidden");
  $("#extractionPanelTitle").textContent = "Edit Expense";
  $("#f_store").value = exp.store_name;
  $("#f_date").value = exp.bill_date;
  $("#f_date")._flatpickr?.setDate(exp.bill_date, true);
  ShareSquad.clampDateInputToToday($("#f_date"));
  $("#f_amount").value = exp.total_amount;
  ShareSquad.fillCurrencySelect($("#f_currency"), exp.currency || groupCurrency());
  syncExpenseCurrencyUI();
  ShareSquad.fillCategorySelect($("#f_category"), exp.category || "General", { allowCustom: true });
  if (!ShareSquad.isKnownCategory(exp.category) && $("#f_category_custom")) {
    $("#f_category_custom").value = exp.category || "";
  }
  ShareSquad.clearOcrFieldMarks($("#extractionPanel") || document);
  $("#f_payer").value = exp.payer_member_id;
  state.payerAmounts = {};
  (exp.payers || []).forEach((payer) => { state.payerAmounts[payer.member_id] = payer.amount_paid; });
  setPayMode(exp.payers?.length > 1 ? "multi" : "single");

  setSplitType(exp.split_type || "EQUAL");

  state.selectedSplit = new Set(exp.splits.map((s) => s.member_id));
  state.splitValues = {};
  exp.splits.forEach((s) => {
    state.splitValues[s.member_id] = s.split_value !== null ? s.split_value : s.share_amount;
  });

  renderSplitOptions();

  $("#cancelEditBtn").classList.remove("hidden");
  switchTab("upload");
}

function cancelEdit() {
  state.editingExpenseId = null;
  $("#extractionPanel").classList.add("hidden");
  $("#cancelEditBtn").classList.add("hidden");
  $("#previewImg").classList.add("hidden");
  $("#receiptPreviewWrap")?.classList.add("hidden");
  setScanBusy(false);
}

async function openComments(expenseId) {
  const expense = state.currentExpenses.find((item) => item.id === expenseId);
  state.commentingExpenseId = expenseId;
  $("#commentsTitle").textContent = `Notes - ${expense?.store_name || "Expense"}`;
  $("#commentsModal").classList.remove("hidden");
  await loadComments();
}

async function loadComments() {
  if (!state.commentingExpenseId) return;
  const list = $("#commentsList");
  list.innerHTML = '<p class="text-xs font-mono text-ink/40">Loading notes...</p>';
  try {
    const comments = await ShareSquad.api(`/groups/${gid()}/expenses/${state.commentingExpenseId}/comments`);
    list.innerHTML = comments.length ? comments.map((comment) => `<article class="p-3 rounded-lg bg-ink/5"><p class="text-sm text-ink">${ShareSquad.escapeHtml(comment.body)}</p><p class="text-[10px] font-mono text-ink/45 mt-1">${ShareSquad.escapeHtml(comment.author_name)} - ${ShareSquad.formatDate(comment.created_at)}</p></article>`).join("") : '<p class="text-xs font-mono text-ink/40 py-3">No notes yet. Add one only when it helps clarify the expense.</p>';
  } catch (err) {
    list.innerHTML = `<p class="text-xs font-mono balance-negative">${ShareSquad.escapeHtml(err.message)}</p>`;
  }
}

async function submitComment(event) {
  event.preventDefault();
  const input = $("#commentInput");
  const body = input.value.trim();
  if (!body || !state.commentingExpenseId) return;
  try {
    await ShareSquad.api(`/groups/${gid()}/expenses/${state.commentingExpenseId}/comments`, { method: "POST", body: JSON.stringify({ body }) });
    input.value = "";
    await loadComments();
  } catch (err) { ShareSquad.showToast(err.message, "error"); }
}

// ---------------------------------------------------------------------------
// Save Expense
// ---------------------------------------------------------------------------
function saveExpenseError(message) {
  const msg = $("#saveMsg");
  if (msg) {
    msg.textContent = message;
    msg.className = "text-xs mt-2 font-mono balance-negative";
  }
  ShareSquad.showToast(message, "error");
}

function readExpenseBillDate() {
  return ShareSquad.clampDateInputToToday($("#f_date"));
}

async function saveExpense() {
  const msg = $("#saveMsg");
  if (msg) msg.textContent = "";

  try {
    if (state.isDraft) {
      saveExpenseError("Invite at least one member before adding shared expenses.");
      return;
    }

    const store = ($("#f_store")?.value || "").trim();
    const billDate = readExpenseBillDate();
    const totalAmount = parseFloat($("#f_amount")?.value);
    let payerId = Number($("#f_payer")?.value);
    let payers = null;

    if (state.payMode === "multi") {
      payers = [...document.querySelectorAll(".payer-amount")]
        .map((input) => ({ member_id: Number(input.dataset.member), amount: parseFloat(input.value) || 0 }))
        .filter((payer) => payer.amount > 0);
      const paid = Math.round(payers.reduce((sum, payer) => sum + payer.amount, 0) * 100) / 100;
      const due = Math.round((totalAmount || 0) * 100) / 100;
      if (payers.length < 2 || paid !== due) {
        saveExpenseError("Several people must pay, and their amounts must add up to the bill total.");
        return;
      }
      payerId = payers.slice().sort((a, b) => b.amount - a.amount)[0].member_id;
    }

    const category = ShareSquad.resolveCategoryValue($("#f_category"), $("#f_category_custom"));

    if (!store || !billDate || !(totalAmount > 0) || !payerId) {
      saveExpenseError("Please fill in merchant, date, amount, and who paid.");
      return;
    }

    let splitDetails = [];
    let splitWith = [];

    if (state.splitType === "EQUAL") {
      splitWith = Array.from(state.selectedSplit);
      if (!splitWith.length) {
        saveExpenseError("Select at least one member to split with.");
        return;
      }
    } else {
      state.members.forEach((m) => {
        const val = state.splitValues[m.id];
        if (val !== undefined && val > 0) {
          splitDetails.push({ member_id: m.id, value: val });
        }
      });
      if (!splitDetails.length) {
        saveExpenseError("Enter split proportions for group members.");
        return;
      }
    }

    const payload = {
      upload_id: state.currentUploadId || null,
      store_name: store,
      bill_date: billDate,
      total_amount: totalAmount,
      currency: expenseCurrency(),
      category: category,
      split_type: state.splitType,
      payer_member_id: payerId,
      payers,
      split_with: splitWith.length ? splitWith : null,
      split_details: splitDetails.length ? splitDetails : null,
      period_key: state.currentPeriod,
    };

    if (state.editingExpenseId) {
      await ShareSquad.api(`/groups/${gid()}/expenses/${state.editingExpenseId}${state.myRole === "owner" ? "?owner_override=true" : ""}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      ShareSquad.showToast("Expense updated successfully", "success");
    } else {
      await ShareSquad.api(`/groups/${gid()}/expenses`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      ShareSquad.showToast("Expense saved", "success");
    }

    cancelEdit();
    await loadPeriods();
    switchTab("expenses");
  } catch (err) {
    saveExpenseError(err.message || "Could not save expense.");
  }
}

// ---------------------------------------------------------------------------
// Balances & Currency Auto-Conversion
// ---------------------------------------------------------------------------
async function loadBalances() {
  const [balances, settlements, payments] = await Promise.all([
    ShareSquad.api(`/groups/${gid()}/periods/${state.currentPeriod}/balances`),
    ShareSquad.api(`/groups/${gid()}/periods/${state.currentPeriod}/settlements?mode=smart`),
    ShareSquad.api(`/groups/${gid()}/periods/${state.currentPeriod}/payments`),
  ]);
  renderBalanceStrip(balances);
  const tbody = $("#balanceBody");
  tbody.innerHTML = "";

  const baseCurr = groupCurrency();
  let targetCurr = null;
  let fxRate = null;

  balances.forEach((b) => {
    if (b.converted_currency) targetCurr = b.converted_currency;
    const settleCurr = b.converted_currency || baseCurr;
    const paid = b.converted_total_paid != null ? b.converted_total_paid : b.total_paid;
    const owed = b.converted_total_share_owed != null ? b.converted_total_share_owed : b.total_share_owed;
    const net = b.converted_net_balance != null ? b.converted_net_balance : b.net_balance;
    const netClass = net >= 0 ? "balance-positive font-bold" : "balance-negative font-bold";
    const plusSign = net > 0 ? "+" : "";

    let origNote = "";
    if (b.converted_currency && b.converted_currency !== baseCurr) {
      origNote = `<div class="text-[10px] text-ink/40 font-normal">orig. ${ShareSquad.formatMoney(b.net_balance, baseCurr)}</div>`;
    }

    const tr = document.createElement("tr");
    tr.className = "border-b border-ink/5";
    tr.innerHTML = `
      <td class="py-2 font-semibold text-ink">${ShareSquad.escapeHtml(b.name)}</td>
      <td class="py-2 text-right text-ink/70">${ShareSquad.formatMoney(paid, settleCurr)}</td>
      <td class="py-2 text-right text-ink/70">${ShareSquad.formatMoney(owed, settleCurr)}</td>
      <td class="py-2 text-right ${netClass}">
        ${plusSign}${ShareSquad.formatMoney(net, settleCurr)}
        ${origNote}
      </td>
    `;
    tbody.appendChild(tr);
  });

  const setList = $("#settlementList");
  const setEmpty = $("#settlementEmpty");
  setList.innerHTML = "";

  if (!settlements.length) {
    setEmpty.classList.remove("hidden");
  } else {
    setEmpty.classList.add("hidden");
    settlements.forEach((s) => {
      fxRate = s.exchange_rate;
      const settleCurr = s.converted_currency || s.currency || baseCurr;
      const settleAmt = s.converted_amount != null ? s.converted_amount : s.amount;
      let origNote = "";
      if (s.converted_currency && s.converted_currency !== (s.currency || baseCurr)) {
        origNote = `<div class="text-[10px] text-ink/40">orig. ${ShareSquad.formatMoney(s.amount, s.currency || baseCurr)}</div>`;
      }

      const div = document.createElement("div");
      div.className = "p-3 rounded-lg bg-ink/5 flex flex-col gap-1";
      div.innerHTML = `
        <div class="flex items-center justify-between text-xs font-mono gap-3">
          <span><strong class="text-ink">${ShareSquad.escapeHtml(s.from_name)}</strong> <span class="text-stamp font-bold">&rarr; pays</span> <strong class="text-ink">${ShareSquad.escapeHtml(s.to_name)}</strong></span>
          <span class="font-bold text-ink text-right">${ShareSquad.formatMoney(settleAmt, settleCurr)}${origNote}</span>
        </div>
      `;
      setList.appendChild(div);
    });
  }

  const banner = $("#conversionBanner");
  const settleSelect = $("#settleCurrencySelect");
  const shownCurr = targetCurr || baseCurr;
  if (settleSelect) {
    state.settleSelectQuiet = true;
    ShareSquad.fillCurrencySelect(settleSelect, shownCurr);
    state.settleSelectQuiet = false;
  }

  if (targetCurr && targetCurr !== baseCurr) {
    banner.classList.remove("hidden");
    const rateLabel = fxRate != null ? ` · 1 ${baseCurr} = ${Number(fxRate).toFixed(4)} ${targetCurr}` : "";
    $("#conversionBannerText").innerHTML = `Settling in <strong>${targetCurr}</strong>${rateLabel}. USD/INR/other receipts convert into ${targetCurr} once.`;
    lucide.createIcons();
  } else {
    banner.classList.add("hidden");
  }

  // Keep payment history wiring from previous implementation.
  await loadPaymentHistory(payments);
}

async function loadPaymentHistory(existingPayments = null) {
  const list = $("#paymentHistory");
  if (!list) return;
  try {
    const payments = existingPayments || await ShareSquad.api(`/groups/${gid()}/periods/${state.currentPeriod}/payments`);
    const paymentOnlyNotice = isCurrentPeriodSettled()
      ? '<p class="text-[11px] font-mono text-ink/55 mb-3">Expenses are locked for this settled period. You can still record or confirm its outstanding payments.</p>'
      : "";
    const pendingNotice = $("#paymentPendingNotice");
    const waitingForConfirmation = payments.filter((payment) => payment.status === "PAID");
    if (pendingNotice) {
      if (waitingForConfirmation.length) {
        const waitingOnMe = waitingForConfirmation.some((payment) => Number(payment.recipient_member_id) === Number(state.myMemberId));
        pendingNotice.classList.remove("hidden");
        pendingNotice.textContent = waitingOnMe
          ? "Payment confirmation needed: confirm received payments below. Balances update immediately after confirmation."
          : "Payment awaiting recipient confirmation. Balances update after the recipient confirms receipt.";
      } else {
        pendingNotice.classList.add("hidden");
        pendingNotice.textContent = "";
      }
    }
    if (!payments.length) { list.innerHTML = ""; return; }
    const members = new Map(state.members.map((member) => [Number(member.id), member.display_name]));
    list.innerHTML = paymentOnlyNotice + `<p class="text-[10px] font-mono uppercase tracking-widest text-ink/45">Payment history</p>` + payments.map((payment) => `<div class="p-2 rounded bg-ink/5 text-xs font-mono flex flex-wrap items-center justify-between gap-2"><span>${ShareSquad.escapeHtml(members.get(payment.payer_member_id) || "Member")} paid ${ShareSquad.escapeHtml(members.get(payment.recipient_member_id) || "Member")} ${ShareSquad.formatMoney(payment.amount, payment.currency)}</span><span class="font-bold ${payment.status === "CONFIRMED" ? "text-emerald-700" : "text-amber-700"}">${payment.status}</span>${Number(payment.recipient_member_id) === Number(state.myMemberId) && payment.status === "PAID" ? `<button class="confirm-payment bb-btn-secondary !py-1 !px-2 text-[10px]" data-id="${payment.id}">Confirm</button>` : ""}</div>`).join("");
    $$(".confirm-payment").forEach((button) => button.addEventListener("click", async () => {
      button.disabled = true;
      try { await ShareSquad.api(`/groups/${gid()}/payments/${button.dataset.id}/confirm`, { method: "POST" }); ShareSquad.showToast("Payment confirmed and balances recalculated.", "success"); await loadBalances(); }
      catch (err) { ShareSquad.showToast(err.message, "error"); button.disabled = false; }
    }));
  } catch (_) { list.innerHTML = ""; }
}

async function applySettlementCurrency(code, customRate = null) {
  const target = (code || groupCurrency()).toUpperCase();
  const body = { target_currency: target };
  if (target === groupCurrency() && (customRate == null || customRate === "")) {
    body.clear = true;
  } else if (customRate != null && customRate !== "") {
    body.custom_exchange_rate = Number(customRate);
  }
  await ShareSquad.api(`/groups/${gid()}/periods/${state.currentPeriod}/convert-currency`, {
    method: "POST",
    body: JSON.stringify(body),
  });
  await loadBalances();
}

// Currency Conversion Modal
async function openCurrencyModal() {
  const modal = $("#currencyModal");
  modal.classList.remove("hidden");
  $("#currencyModalMsg").textContent = "";

  const baseCurr = groupCurrency();
  const periodRec = state.periods.find((p) => p.period_key === state.currentPeriod);
  const current = $("#settleCurrencySelect")?.value || periodRec?.target_currency || baseCurr;
  ShareSquad.fillCurrencySelect($("#targetCurrencySelect"), current);
  $("#ratePrefix").textContent = `1 ${baseCurr} =`;
  $("#rateSuffix").textContent = $("#targetCurrencySelect").value;

  await fetchLiveRate();
}

function roundFxRate(rate) {
  const n = Number(rate);
  if (!Number.isFinite(n) || n <= 0) return null;
  return Math.round(n * 1e6) / 1e6;
}

async function fetchLiveRate() {
  const baseCurr = groupCurrency();
  const targetCurr = $("#targetCurrencySelect").value;
  try {
    const data = await ShareSquad.api(`/currencies/rates?base=${baseCurr}`);
    const rate = roundFxRate(data.rates[targetCurr] || 1.0);
    $("#customRateInput").value = rate ?? "";
    $("#rateSourceHint").textContent = `Live rate: 1 ${baseCurr} = ${rate} ${targetCurr}`;
  } catch (err) {
    console.error("Could not fetch rate");
  }
}

async function submitCurrencyConversion(e) {
  e.preventDefault();
  const targetCurr = $("#targetCurrencySelect").value;
  const rate = roundFxRate($("#customRateInput").value);
  if (rate == null) {
    $("#currencyModalMsg").textContent = "Enter a valid exchange rate.";
    $("#currencyModalMsg").className = "text-xs font-mono balance-negative";
    return;
  }
  $("#customRateInput").value = rate;

  try {
    await applySettlementCurrency(targetCurr, rate);
    $("#currencyModal").classList.add("hidden");
    ShareSquad.showToast(`Settling this period in ${targetCurr}`, "success");
  } catch (err) {
    $("#currencyModalMsg").textContent = err.message;
    $("#currencyModalMsg").className = "text-xs font-mono balance-negative";
  }
}

async function loadActivity() {
  const list = $("#activityList");
  if (!list) return;
  try {
    const events = await ShareSquad.api(`/groups/${gid()}/activity`);
    list.innerHTML = events.length ? events.map((event) => `<article class="activity-item"><span class="activity-dot"></span><div><p class="text-sm text-ink">${ShareSquad.escapeHtml(event.summary)}</p><p class="text-[10px] font-mono text-ink/45 mt-1">${ShareSquad.formatDate(event.created_at)}</p></div></article>`).join("") : '<p class="text-sm font-mono text-ink/40 py-4">Activity will appear as your group adds expenses, payments, and notes.</p>';
  } catch (err) { list.innerHTML = `<p class="text-sm balance-negative">${ShareSquad.escapeHtml(err.message)}</p>`; }
}

async function loadInsights() {
  const root = $("#insightsContent");
  if (!root) return;
  root.innerHTML = '<div class="bb-card text-sm font-mono text-ink/45">Loading insights...</div>';
  try {
    const data = await ShareSquad.api(`/groups/${gid()}/dashboard?period_key=${state.currentPeriod}`);
    const categoryRows = Object.entries(data.category_totals || {}).sort((a, b) => b[1] - a[1]);
    const top = data.largest_expenses || [];
    const chart = ShareSquad.renderCategoryPieChart(categoryRows, data.total_spent, data.currency);
    root.innerHTML = `
      <div class="insight-kpis"><article class="bb-card"><p class="insight-label">Total spending</p><p class="insight-value">${ShareSquad.formatMoney(data.total_spent, data.currency)}</p><p class="text-xs text-ink/45">${state.currentPeriod}</p></article><article class="bb-card"><p class="insight-label">Top category</p><p class="insight-value text-lg"><span ${ShareSquad.categoryMark(categoryRows[0]?.[0] || "General")}>${ShareSquad.escapeHtml(categoryRows[0]?.[0] || "No expenses")}</span></p><p class="text-xs text-ink/45">${categoryRows[0] ? ShareSquad.formatMoney(categoryRows[0][1], data.currency) : ""}</p></article></div>
      <div class="grid md:grid-cols-2 gap-4"><article class="bb-card"><h3 class="font-mono uppercase text-xs tracking-widest text-brand font-bold mb-3">By category</h3>${chart}</article><article class="bb-card"><h3 class="font-mono uppercase text-xs tracking-widest text-brand font-bold mb-3">Largest expenses</h3>${top.length ? top.map((expense) => `<div class="insight-row cat-row"><span class="inline-flex items-center gap-2 min-w-0"><span ${ShareSquad.categoryMark(expense.category || "General")}>${ShareSquad.escapeHtml(expense.category || "General")}</span><span class="truncate">${ShareSquad.escapeHtml(expense.store_name)}</span></span><strong>${ShareSquad.formatMoney(expense.total_amount, expense.currency)}</strong></div>`).join("") : '<p class="text-sm text-ink/45">No expenses in this period.</p>'}</article></div>`;
  } catch (err) { root.innerHTML = `<div class="bb-card balance-negative">${ShareSquad.escapeHtml(err.message)}</div>`; }
}

async function openNotifications() {
  const modal = $("#notificationsModal");
  const list = $("#notificationsList");
  const markAllBtn = $("#markAllNotificationsReadBtn");
  modal.classList.remove("hidden");
  list.innerHTML = '<div class="ui-skeleton-card"><span></span><span></span></div>';
  try {
    const [items, unreadCount] = await Promise.all([
      ShareSquad.api("/notifications"),
      loadNotificationCount(),
    ]);
    markAllBtn?.classList.toggle("hidden", unreadCount === 0);
    list.innerHTML = items.length ? items.map((item) => `<button class="notification-item ${item.read_at ? "" : "unread"}" data-id="${item.id}" data-link="${ShareSquad.escapeHtml(item.link || "")}" data-message="${ShareSquad.escapeHtml(item.message)}"><span>${ShareSquad.escapeHtml(item.message)}</span><small>${ShareSquad.formatDate(item.created_at)}</small>${item.link ? '<span class="notification-action">View details →</span>' : ""}</button>`).join("") : '<p class="text-sm text-ink/55 py-5 text-center">You are all caught up.</p>';
    list.querySelectorAll(".notification-item").forEach((button) => button.addEventListener("click", async () => {
      await ShareSquad.api(`/notifications/${button.dataset.id}/read`, { method: "POST" });
      if (button.dataset.link) ShareSquad.openNotificationLink(button.dataset.link, button.dataset.message);
      else {
        button.classList.remove("unread");
        await loadNotificationCount();
      }
    }));
  } catch (err) {
    list.innerHTML = `<p class="text-xs balance-negative">${ShareSquad.escapeHtml(err.message)}</p>`;
    markAllBtn?.classList.add("hidden");
  }
}

async function markAllNotificationsRead() {
  const markAllBtn = $("#markAllNotificationsReadBtn");
  try {
    if (markAllBtn) markAllBtn.disabled = true;
    await ShareSquad.api("/notifications/read-all", { method: "POST" });
    $$("#notificationsList .notification-item.unread").forEach((item) => item.classList.remove("unread"));
    markAllBtn?.classList.add("hidden");
    await loadNotificationCount();
    ShareSquad.showToast("All notifications marked as read", "success");
  } catch (err) {
    ShareSquad.showToast(err.message || "Could not mark notifications as read", "error");
  } finally {
    if (markAllBtn) markAllBtn.disabled = false;
  }
}

async function loadNotificationCount() {
  const badge = $("#notificationCount");
  const markAllBtn = $("#markAllNotificationsReadBtn");
  if (!badge) return 0;
  try {
    const items = await ShareSquad.api("/notifications?unread_only=true");
    badge.textContent = items.length > 9 ? "9+" : items.length;
    badge.classList.toggle("hidden", !items.length);
    markAllBtn?.classList.toggle("hidden", !items.length);
    return items.length;
  } catch (_) {
    badge.classList.add("hidden");
    markAllBtn?.classList.add("hidden");
    return 0;
  }
}

// ---------------------------------------------------------------------------
// Reports Tab
// ---------------------------------------------------------------------------
async function loadReports() {
  const periods = await ShareSquad.api(`/groups/${gid()}/periods`);
  const list = $("#reportsList");
  list.innerHTML = "";

  const settled = periods.filter((p) => p.pdf_filename);
  if (!settled.length) {
    list.innerHTML = `<p class="py-4 text-xs text-muted font-mono italic">No archived PDF reports yet for this group.</p>`;
    return;
  }

  settled.forEach((p) => {
    const div = document.createElement("div");
    div.className = "py-3 flex items-center justify-between text-xs font-mono";
    div.innerHTML = `
      <div>
        <div class="font-bold text-ink">${ShareSquad.escapeHtml(p.pdf_filename)}</div>
        <div class="text-[10px] text-muted mt-0.5">Period: ${p.period_key} &bull; Settled on ${ShareSquad.formatDate(p.settled_at)}</div>
      </div>
      <button type="button" class="download-report bb-btn-secondary !py-1 !px-2.5 text-[11px] flex items-center gap-1" data-filename="${encodeURIComponent(p.pdf_filename)}">
        <i data-lucide="download" class="w-3.5 h-3.5 text-brand"></i> Download PDF
      </button>
    `;
    list.appendChild(div);
  });

  list.querySelectorAll(".download-report").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const filename = decodeURIComponent(btn.dataset.filename || "");
      btn.disabled = true;
      try {
        await ShareSquad.downloadBlob(
          `/groups/${gid()}/reports/${encodeURIComponent(filename)}`,
          filename
        );
        ShareSquad.showToast("PDF downloaded", "success");
      } catch (err) {
        ShareSquad.showToast(err.message || "Could not download PDF", "error");
      } finally {
        btn.disabled = false;
      }
    });
  });

  lucide.createIcons();
}

async function generateReport() {
  const msg = $("#reportMsg");
  msg.textContent = "Creating PDF…";
  msg.className = "text-xs mt-3 font-mono text-ink/60";

  try {
    const res = await ShareSquad.api(`/groups/${gid()}/periods/${state.currentPeriod}/generate-report`, {
      method: "POST",
    });
    msg.textContent = `Report generated: ${res.filename}`;
    msg.className = "text-xs mt-3 font-mono balance-positive font-bold";
    ShareSquad.showToast("Monthly PDF report generated!", "success");
    await loadPeriods();
    await loadReports();
    try {
      await ShareSquad.downloadBlob(
        `/groups/${gid()}/reports/${encodeURIComponent(res.filename)}`,
        res.filename
      );
    } catch (dlErr) {
      ShareSquad.showToast(dlErr.message || "Report saved, but download failed. Use Download PDF below.", "error");
    }
  } catch (err) {
    msg.textContent = err.message;
    msg.className = "text-xs mt-3 font-mono balance-negative";
  }
}

// ---------------------------------------------------------------------------
// Members Tab & Invites
// ---------------------------------------------------------------------------
async function loadMembers() {
  const group = await ShareSquad.api(`/groups/${gid()}`);
  state.members = group.members;

  const inviteLink = $("#inviteLink");
  if (inviteLink) inviteLink.value = `${window.location.origin}/join.html?code=${encodeURIComponent(group.invite_code)}`;

  const list = $("#memberList");
  if (!list) return;
  const isOwner = state.myRole === "owner";
  list.innerHTML = group.members
    .map((m) => {
      const inactive = m.is_active === false;
      const canRemove = isOwner && !inactive && Number(m.id) !== Number(state.myMemberId);
      return `
      <div class="member-row ${inactive ? "opacity-50" : ""}">
        <div class="flex items-center gap-2 min-w-0">
          <span class="avatar-initials">${ShareSquad.initials(m.display_name)}</span>
          ${m.has_avatar ? `<img class="person-avatar hidden" data-member-avatar="${m.id}" alt="${ShareSquad.escapeHtml(m.display_name)}">` : ""}
          <div>
          <span class="font-bold text-ink">${ShareSquad.escapeHtml(m.display_name)}</span>
          ${m.role === "owner" ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-brand/10 text-brand font-bold ml-1.5 uppercase">Owner</span>' : ""}
          ${inactive ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-ink/10 text-ink/50 font-bold ml-1.5 uppercase">Removed</span>' : ""}
          <span class="member-email">${ShareSquad.escapeHtml(ShareSquad.maskEmail(m.email))}</span>
          </div>
        </div>
        ${canRemove ? `<button type="button" class="remove-member bb-btn-secondary !py-1 !px-2 text-[10px] text-stamp border-stamp/30" data-id="${m.id}" data-name="${ShareSquad.escapeHtml(m.display_name)}">Remove</button>` : ""}
      </div>`;
    })
    .join("");

  list.querySelectorAll("[data-member-avatar]").forEach((img) => {
    ShareSquad.setAuthImage(img, `/groups/${gid()}/members/${img.dataset.memberAvatar}/avatar`).then(() => {
      if (!img.classList.contains("hidden")) img.previousElementSibling?.classList.add("hidden");
    });
  });

  list.querySelectorAll(".remove-member").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const memberId = Number(btn.dataset.id);
      const memberName = btn.dataset.name;
      if (!await ShareSquad.confirmDialog({
        title: `Remove ${memberName}?`,
        message: "Their active-period expenses will be removed. Completed history remains. You can undo for a few seconds.",
        confirmLabel: "Remove member",
      })) return;
      const row = btn.closest(".member-row");
      ShareSquad.runWithUndo({
        message: `${memberName} was removed`,
        onHide: () => { row?.remove(); },
        onUndo: async () => { await loadGroup(); await loadMembers(); },
        onCommit: async () => {
          await ShareSquad.api(`/groups/${gid()}/members/${memberId}`, { method: "DELETE" });
          await loadGroup();
          await loadMembers();
        },
      });
    });
  });

  const friendSelect = $("#existingFriendSelect");
  if (friendSelect) {
    const activeAccountIds = new Set(group.members.filter((member) => member.is_active !== false).map((member) => Number(member.account_id)));
    const friends = await ShareSquad.api("/friends").catch(() => []);
    const available = friends.filter((friend) => !activeAccountIds.has(Number(friend.id)));
    friendSelect.innerHTML = available.length
      ? available.map((friend) => `<option value="${friend.id}">${ShareSquad.escapeHtml(friend.display_name)} · ${ShareSquad.escapeHtml(ShareSquad.maskEmail(friend.email))}</option>`).join("")
      : '<option value="">No available friends</option>';
    $("#addExistingFriendBtn").disabled = !available.length;
  }
}

// ---------------------------------------------------------------------------
// Receipt Upload & VLM Handler
// ---------------------------------------------------------------------------
function setScanBusy(on) {
  const wrap = $("#receiptPreviewWrap");
  if (on) wrap?.classList.remove("hidden");
  $("#scanProgress")?.classList.toggle("hidden", !on);
  $("#scanProgress")?.setAttribute("aria-busy", on ? "true" : "false");
  $("#uploadStatus")?.classList.toggle("hidden", !on);
  $("#receiptSourceRow")?.classList.toggle("is-busy", on);
  $("#dropzone")?.classList.toggle("is-busy", on);
}

async function handleReceiptUpload(file) {
  if (state.isDraft) { ShareSquad.showToast("Invite at least one member before adding shared expenses", "info"); return; }
  if (!file) return;
  if (!ShareSquad.isReceiptImageFile(file)) {
    ShareSquad.showToast("Please choose a photo or image file.", "error");
    return;
  }

  const preview = $("#previewImg");
  if (preview.src && preview.src.startsWith("blob:")) URL.revokeObjectURL(preview.src);
  preview.src = URL.createObjectURL(file);
  preview.classList.remove("hidden");
  setScanBusy(true);

  try {
    const data = await ShareSquad.uploadReceipt(`/groups/${gid()}/receipts/upload`, file);

    state.currentUploadId = data.upload_id;
    $("#extractionPanel").classList.remove("hidden");

    if (data.extraction) {
      const ex = data.extraction;
      ShareSquad.clearOcrFieldMarks($("#extractionPanel") || document);
      if (ex.store_name) $("#f_store").value = ex.store_name;
      if (ex.bill_date) {
        const billDate = ex.bill_date > ShareSquad.todayISODate() ? ShareSquad.todayISODate() : ex.bill_date;
        $("#f_date").value = billDate;
        $("#f_date")._flatpickr?.setDate(billDate, true);
        ShareSquad.clampDateInputToToday($("#f_date"));
      }
      if (ex.total_amount) $("#f_amount").value = ex.total_amount;
      if (ex.category) ShareSquad.fillCategorySelect($("#f_category"), ex.category, { allowCustom: true });
      ShareSquad.fillCurrencySelect($("#f_currency"), ShareSquad.pickScannedCurrency(ex, groupCurrency()));
      syncExpenseCurrencyUI();
      ShareSquad.markOcrFields({
        "#f_store": Boolean(ex.store_name),
        "#f_date": Boolean(ex.bill_date),
        "#f_amount": Boolean(ex.total_amount),
        "#f_category": Boolean(ex.category),
      });
      const found = ["Merchant", "Date", "Total", "Category"].filter((label, i) => [ex.store_name, ex.bill_date, ex.total_amount, ex.category][i]);
      $("#sourceTag").textContent = found.length ? `Scanned · ${found.join(" · ")} found — check before saving` : "Scanned";
    } else {
      $("#sourceTag").textContent = "Scanned";
    }

    applyDefaultSplit();
    renderPayerOptions();
    setPayMode("single");
    renderSplitOptions();
    if ($("#saveMsg")) $("#saveMsg").textContent = "";
    const warn = Boolean(data.message);
    ShareSquad.showToast(
      data.message || "Receipt scanned. Please verify and save.",
      warn ? "info" : "success"
    );
  } catch (err) {
    ShareSquad.showToast(err.message, "error");
  } finally {
    setScanBusy(false);
  }
}

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------
(async function init() {
  if (!ShareSquad.getToken()) {
    window.location.href = "/login.html";
    return;
  }

  ShareSquad.fillAllCurrencySelects();
  ShareSquad.bindAllCategorySelects();

  const storedGid = ShareSquad.getStoredGroupId();
  if (!storedGid) {
    window.location.href = "/index.html";
    return;
  }
  state.currentGroupId = Number(storedGid);

  try {
    state.account = (await ShareSquad.api("/auth/me")).account;
  } catch (err) {
    return;
  }

  // Navigation tabs
  $$(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  // Period select
  $("#periodSelect").addEventListener("change", (e) => {
    state.currentPeriod = e.target.value;
    updatePeriodStatus();
    loadExpenses();
    loadBalances();
  });

  // Reopen period
  $("#reopenPeriodBtn").addEventListener("click", async () => {
    if (!await ShareSquad.confirmDialog({ title: `Re-open ${state.currentPeriod}?`, message: "Members can edit this period again and balances may change.", confirmLabel: "Re-open period", danger: false })) return;
    try {
      await ShareSquad.api(`/groups/${gid()}/periods/${state.currentPeriod}/reopen`, { method: "POST" });
      ShareSquad.showToast("Period re-opened for editing", "info");
      await loadPeriods();
    } catch (err) {
      ShareSquad.showToast(err.message, "error");
    }
  });

  // Split type buttons
  $$("#splitTypeSelector .split-type-pill").forEach((btn) => {
    btn.addEventListener("click", () => setSplitType(btn.dataset.type));
  });
  $$("#payModeSelector .split-type-pill").forEach((btn) => {
    btn.addEventListener("click", () => setPayMode(btn.dataset.payMode));
  });

  $("#f_amount").addEventListener("input", () => {
    if (state.splitType !== "EQUAL") renderSplitOptions();
    if (state.payMode === "multi") renderMultiPayers();
  });
  $("#f_currency")?.addEventListener("change", () => {
    syncExpenseCurrencyUI();
    if (state.splitType !== "EQUAL") renderSplitOptions();
  });

  $("#manualEntryBtn").addEventListener("click", () => {
    state.editingExpenseId = null;
    // Manual entries deliberately have no receipt. Clear a previous scan so
    // its already-consumed upload id is never sent with this new expense.
    state.currentUploadId = null;
    $("#extractionPanel").classList.remove("hidden");
    $("#previewImg").classList.add("hidden");
    $("#receiptPreviewWrap")?.classList.add("hidden");
    setScanBusy(false);
    ShareSquad.clearOcrFieldMarks($("#extractionPanel") || document);
    $("#f_store").value = "";
    const today = new Date().toISOString().split("T")[0];
    $("#f_date").value = today;
    $("#f_date")._flatpickr?.setDate(today, true);
    $("#f_amount").value = "";
    ShareSquad.fillCategorySelect($("#f_category"), "Food & Dining", { allowCustom: true });
    if ($("#f_category_custom")) $("#f_category_custom").value = "";
    ShareSquad.fillCurrencySelect($("#f_currency"), groupCurrency());
    syncExpenseCurrencyUI();
    applyDefaultSplit();
    state.payerAmounts = {};
    renderPayerOptions();
    setPayMode("single");
    renderSplitOptions();
    if ($("#saveMsg")) $("#saveMsg").textContent = "";
    requestAnimationFrame(() => { $("#extractionPanel").scrollIntoView({ behavior: "smooth", block: "start" }); setTimeout(() => $("#f_store").focus(), 300); });
  });

  $("#cancelEditBtn").addEventListener("click", cancelEdit);
  $("#saveExpenseBtn")?.addEventListener("click", (e) => {
    e.preventDefault();
    saveExpense();
  });

  // File dropzone
  const dropzone = $("#dropzone");
  const galleryInput = $("#fileInputGallery");
  const cameraInput = $("#fileInputCamera");
  dropzone.addEventListener("click", () => galleryInput?.click());
  galleryInput?.addEventListener("change", (e) => {
    handleReceiptUpload(e.target.files[0]);
    e.target.value = "";
  });
  cameraInput?.addEventListener("change", (e) => {
    handleReceiptUpload(e.target.files[0]);
    e.target.value = "";
  });
  ShareSquad.bindReceiptCamera({
    openBtn: "#pickCameraBtn",
    fileInput: "#fileInputCamera",
    onCapture: handleReceiptUpload,
  });
  $("#renameGroupBtn")?.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    openRenameGroupModal();
  });
  $("#changeGroupPhotoBtn")?.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    $("#groupCoverInput")?.click();
  });
  $("#removeGroupPhotoBtn")?.addEventListener("click", async () => {
    if (!await ShareSquad.confirmDialog({ title: "Remove group photo?", message: "The current group photo will be removed.", confirmLabel: "Remove photo" })) return;
    try {
      const updated = await ShareSquad.api(`/groups/${gid()}/cover`, { method: "DELETE" });
      state.group = updated;
      $("#groupCoverImg").removeAttribute("src");
      $("#groupCoverImg").classList.add("hidden");
      $("#removeGroupPhotoBtn").classList.add("hidden");
      ShareSquad.showToast("Group photo removed", "success");
    } catch (err) { ShareSquad.showToast(err.message, "error"); }
  });
  $("#renameGroupForm")?.addEventListener("submit", submitRenameGroup);
  $("#cancelRenameGroupBtn")?.addEventListener("click", closeRenameGroupModal);
  $("#closeRenameGroupBtn")?.addEventListener("click", closeRenameGroupModal);
  $("#renameGroupModal")?.addEventListener("click", (e) => {
    if (e.target.id === "renameGroupModal") closeRenameGroupModal();
  });
  $("#groupCoverInput")?.addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    const fd = new FormData();
    fd.append("file", await ShareSquad.prepareReceiptFile(file));
    try {
      const updated = await ShareSquad.api(`/groups/${gid()}/cover`, { method: "POST", body: fd });
      state.group = updated;
      ShareSquad.setAuthImage($("#groupCoverImg"), `/groups/${gid()}/cover`);
      ShareSquad.showToast("Group photo updated", "success");
    } catch (err) {
      ShareSquad.showToast(err.message, "error");
    }
  });

  dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("dragover"); });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
  dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropzone.classList.remove("dragover");
    if (e.dataTransfer.files.length) handleReceiptUpload(e.dataTransfer.files[0]);
  });

  // Category filter on expenses
  $("#groupCategoryFilter").addEventListener("change", loadExpenses);
  $("#expenseMemberFilter")?.addEventListener("change", loadExpenses);
  $("#onlyMyExpenses")?.addEventListener("change", loadExpenses);
  let searchDelay;
  $("#expenseSearch")?.addEventListener("input", () => {
    clearTimeout(searchDelay);
    searchDelay = setTimeout(loadExpenses, 250);
  });

  $("#notificationsBtn")?.addEventListener("click", openNotifications);
  $("#markAllNotificationsReadBtn")?.addEventListener("click", markAllNotificationsRead);
  $("#closeNotificationsBtn")?.addEventListener("click", () => $("#notificationsModal").classList.add("hidden"));
  $("#closeCommentsBtn")?.addEventListener("click", () => $("#commentsModal").classList.add("hidden"));
  $("#commentForm")?.addEventListener("submit", submitComment);

  // Currency auto-conversion
  $("#convertCurrencyBtn").addEventListener("click", openCurrencyModal);
  $("#closeCurrencyModalBtn").addEventListener("click", () => $("#currencyModal").classList.add("hidden"));
  $("#cancelCurrencyBtn").addEventListener("click", () => $("#currencyModal").classList.add("hidden"));
  $("#targetCurrencySelect").addEventListener("change", () => {
    $("#rateSuffix").textContent = $("#targetCurrencySelect").value;
    fetchLiveRate();
  });
  $("#fetchLiveRateBtn").addEventListener("click", fetchLiveRate);
  $("#customRateInput").addEventListener("blur", () => {
    const rounded = roundFxRate($("#customRateInput").value);
    if (rounded != null) $("#customRateInput").value = rounded;
  });
  $("#currencyForm").addEventListener("submit", submitCurrencyConversion);
  $("#resetConversionBtn").addEventListener("click", openCurrencyModal);
  $("#customizeSettleRateBtn")?.addEventListener("click", openCurrencyModal);
  $("#settleCurrencySelect")?.addEventListener("change", async (e) => {
    if (state.settleSelectQuiet) return;
    const code = e.target.value;
    try {
      await applySettlementCurrency(code);
      ShareSquad.showToast(`Settling this period in ${code}`, "success");
    } catch (err) {
      ShareSquad.showToast(err.message, "error");
    }
  });

  // Generate Report
  $("#generateReportBtn").addEventListener("click", generateReport);

  function setInviteMethod(method) {
    const emailMode = method === "email";
    $("#inviteForm")?.classList.toggle("hidden", !emailMode);
    $("#inviteLinkPanel")?.classList.toggle("hidden", emailMode);
    $("#showInviteEmailBtn")?.classList.toggle("bb-btn-primary", emailMode);
    $("#showInviteEmailBtn")?.classList.toggle("bb-btn-secondary", !emailMode);
    $("#showInviteLinkBtn")?.classList.toggle("bb-btn-primary", !emailMode);
    $("#showInviteLinkBtn")?.classList.toggle("bb-btn-secondary", emailMode);
  }

  $("#showInviteEmailBtn")?.addEventListener("click", () => setInviteMethod("email"));
  $("#showInviteLinkBtn")?.addEventListener("click", () => setInviteMethod("link"));
  $("#copyInviteLinkBtn")?.addEventListener("click", async () => {
    const link = $("#inviteLink")?.value;
    if (!link) return;
    try {
      await navigator.clipboard.writeText(link);
      ShareSquad.showToast("Group link copied", "success");
    } catch {
      $("#inviteLink").select();
      document.execCommand("copy");
      ShareSquad.showToast("Group link copied", "success");
    }
  });
  setInviteMethod("email");

  // Invite Form
  $("#inviteForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = $("#inviteEmail").value.trim();
    const msg = $("#inviteMsg");
    const submitBtn = e.target.querySelector("button[type='submit']");
    if (submitBtn) submitBtn.disabled = true;
    try {
      const result = await ShareSquad.api(`/groups/${gid()}/invites`, {
        method: "POST",
        body: JSON.stringify({ email }),
      });
      msg.textContent = result.message || `${email} was added.`;
      msg.className = "text-xs font-mono balance-positive font-bold";
      ShareSquad.showToast(result.message, "success");
      $("#inviteEmail").value = "";
      await loadGroup();
      await loadMembers();
    } catch (err) {
      msg.textContent = err.message;
      msg.className = "text-xs font-mono balance-negative";
      ShareSquad.showToast(err.message, "error");
    } finally {
      if (submitBtn) submitBtn.disabled = false;
    }
  });

  $("#addExistingFriendBtn")?.addEventListener("click", async () => {
    const friendId = Number($("#existingFriendSelect").value);
    const msg = $("#existingFriendMsg");
    if (!friendId) return;
    try {
      const result = await ShareSquad.api(`/groups/${gid()}/members`, { method: "POST", body: JSON.stringify({ account_id: friendId }) });
      msg.textContent = result.message;
      msg.className = "text-xs font-mono balance-positive font-bold";
      ShareSquad.showToast(result.message, "success");
      await loadGroup();
      await loadMembers();
    } catch (err) {
      msg.textContent = err.message;
      msg.className = "text-xs font-mono balance-negative";
      ShareSquad.showToast(err.message, "error");
    }
  });

  $("#logoutBtn")?.addEventListener("click", () => {
    ShareSquad.removeToken();
    ShareSquad.removeStoredGroupId();
    window.location.href = "/login.html";
  });

  // Mobile FAB: jump to Add/Scan tab and open manual entry
  document.getElementById("mobileFab")?.addEventListener("click", () => {
    switchTab("upload");
    setTimeout(() => {
      document.getElementById("manualEntryBtn")?.click();
    }, 80);
  });

  // Swipe left/right between group tabs — content only, not chrome/controls
  const TAB_ORDER = ["upload", "expenses", "balances", "insights", "activity", "reports", "members"];
  const SWIPE_BLOCK =
    "#mainNav, .page-header, #periodBar, #balanceStrip, .bottom-nav, .mobile-quick-action, " +
    "input, textarea, select, button, a, label, .tab-nav, [role='dialog'], .modal, " +
    "[data-no-swipe], .split-type-pill, .payer-amount";
  let _swipe = null;

  function swipeIsBlocked(target) {
    if (!(target instanceof Element)) return true;
    if (target.closest(SWIPE_BLOCK)) return true;
    // Horizontal scroll strips (member chips, etc.)
    let node = target;
    while (node && node !== document.body) {
      if (node instanceof HTMLElement) {
        const style = window.getComputedStyle(node);
        const ox = style.overflowX;
        if ((ox === "auto" || ox === "scroll") && node.scrollWidth > node.clientWidth + 4) {
          return true;
        }
      }
      node = node.parentElement;
    }
    return false;
  }

  document.addEventListener("touchstart", (e) => {
    const touch = e.touches[0];
    if (!touch || e.touches.length !== 1) {
      _swipe = null;
      return;
    }
    if (swipeIsBlocked(e.target)) {
      _swipe = null;
      return;
    }
    // Active tab body, or shared help blurb (outside panels, not interactive)
    const panel = e.target.closest?.(".tab-panel");
    const help = e.target.closest?.(".how-it-works, #groupPageHelp");
    if (help) {
      _swipe = { x: touch.clientX, y: touch.clientY };
      return;
    }
    if (!panel || panel.classList.contains("hidden")) {
      _swipe = null;
      return;
    }
    _swipe = { x: touch.clientX, y: touch.clientY };
  }, { passive: true });

  document.addEventListener("touchmove", (e) => {
    if (!_swipe || e.touches.length !== 1) return;
    const touch = e.touches[0];
    const dx = touch.clientX - _swipe.x;
    const dy = touch.clientY - _swipe.y;
    // Vertical scroll intent — cancel tab swipe
    if (Math.abs(dy) > 24 && Math.abs(dy) > Math.abs(dx)) _swipe = null;
  }, { passive: true });

  document.addEventListener("touchend", (e) => {
    if (!_swipe) return;
    const touch = e.changedTouches[0];
    const dx = touch.clientX - _swipe.x;
    const dy = touch.clientY - _swipe.y;
    _swipe = null;
    if (Math.abs(dx) < 70 || Math.abs(dx) < Math.abs(dy) * 1.35) return;
    const active = $$(".tab-btn").find((b) => b.classList.contains("active"));
    if (!active) return;
    const idx = TAB_ORDER.indexOf(active.dataset.tab);
    if (idx === -1) return;
    const next = dx < 0 ? TAB_ORDER[idx + 1] : TAB_ORDER[idx - 1];
    if (next) switchTab(next);
  }, { passive: true });

  document.addEventListener("touchcancel", () => { _swipe = null; }, { passive: true });

  await loadGroupSettings();
  await loadGroup();
  await loadPeriods();
  await loadExpenses();
  await loadNotificationCount();
  startGroupSync();
  focusNotificationTarget();
})();
