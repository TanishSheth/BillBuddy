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
  balances: "<strong>Balances & settlement:</strong> Review who owes whom for this period. After the owner settles the month, those amounts roll into each person’s consolidated Friend total—record and confirm payments from Friends.",
  insights: "<strong>Insights:</strong> See this month's spending total, top categories, and largest expenses.",
  activity: "<strong>Activity:</strong> Follow group changes, expenses, comments, and payment updates in time order.",
  reports: "<strong>PDF Reports:</strong> Generate or download a clear monthly record of expenses, balances, and confirmed settlements.",
  members: "<strong>Members:</strong> Invite people, share the invite link, and manage the group roster. Only the owner can remove members or change group details.",
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
  const keep = state.currentPeriod;
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
    badge.className = "text-[10px] uppercase tracking-wider px-2 py-0.5 rounded-full border status-settled font-bold";
    reopenBtn.classList.remove("hidden");
  } else {
    badge.textContent = "Active";
    badge.className = "text-[10px] uppercase tracking-wider px-2 py-0.5 rounded-full border status-active font-bold";
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
        <input type="number" step="${state.splitType === 'SHARES' ? '1' : '0.01'}" min="0" 
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
  const totalAmt = parseFloat($("#f_amount").value) || 0;
  const curr = expenseCurrency();

  if (state.splitType === "PERCENTAGE") {
    const sumPct = Object.values(state.splitValues).reduce((a, b) => a + Number(b), 0);
    const diff = (100 - sumPct).toFixed(1);
    if (Math.abs(sumPct - 100) < 0.01) {
      notice.textContent = `Total: 100% (Balanced)`;
      notice.className = "text-[10px] font-mono mb-2 text-emerald-700 font-bold";
    } else {
      notice.textContent = `Total: ${sumPct.toFixed(1)}% (${diff}% remaining to reach 100%)`;
      notice.className = "text-[10px] font-mono mb-2 text-stamp font-bold";
    }
  } else if (state.splitType === "EXACT") {
    const sumAmt = Object.values(state.splitValues).reduce((a, b) => a + Number(b), 0);
    const diff = (totalAmt - sumAmt).toFixed(2);
    if (Math.abs(sumAmt - totalAmt) < 0.01) {
      notice.textContent = `Total: ${curr} ${sumAmt.toFixed(2)} (Matches bill total)`;
      notice.className = "text-[10px] font-mono mb-2 text-emerald-700 font-bold";
    } else {
      notice.textContent = `Total: ${curr} ${sumAmt.toFixed(2)} (${curr} ${diff} remaining to match total ${curr} ${totalAmt.toFixed(2)})`;
      notice.className = "text-[10px] font-mono mb-2 text-stamp font-bold";
    }
  } else if (state.splitType === "SHARES") {
    const totalShares = Object.values(state.splitValues).reduce((a, b) => a + Number(b), 0);
    notice.textContent = `Total Shares: ${totalShares}. Each share is worth approximately ${curr} ${(totalAmt / (totalShares || 1)).toFixed(2)}`;
    notice.className = "text-[10px] font-mono mb-2 text-ink/70 font-semibold";
  }
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
    const payerName = memberMap.get(e.payer_member_id) || "Unknown";
    const splitNames = e.splits.map((s) => memberMap.get(s.member_id) || "?").join(", ");
    const catClass = `cat-${(e.category || "General").split(" ")[0]}`;

    // Desktop Row
    const tr = document.createElement("tr");
    tr.className = "border-b border-ink/5 hover:bg-ink/5 transition-colors";
    tr.innerHTML = `
      <td class="py-2.5 pr-3 text-ink/70">${e.bill_date}</td>
      <td class="py-2.5 pr-3 font-bold text-ink">${ShareSquad.escapeHtml(e.store_name)}</td>
      <td class="py-2.5 pr-3"><span class="category-tag ${catClass}">${ShareSquad.escapeHtml(e.category)}</span></td>
      <td class="py-2.5 pr-3 text-ink/80">${ShareSquad.escapeHtml(payerName)}</td>
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
    card.innerHTML = `
      <div class="card-top">
        <div>
          <div class="card-store">${ShareSquad.escapeHtml(e.store_name)}</div>
          <span class="category-tag ${catClass} mt-1">${ShareSquad.escapeHtml(e.category)}</span>
        </div>
        <div class="card-amount text-brand">${ShareSquad.formatMoney(e.total_amount, e.currency || groupCurrency())}</div>
      </div>
      <div class="card-meta">
        <div>${e.bill_date} &bull; Paid by ${ShareSquad.escapeHtml(payerName)}</div>
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
      if (!confirm(`Delete "${name}"?`)) return;
      try {
        const override = state.myRole === "owner" ? "?owner_override=true" : "";
        await ShareSquad.api(`/groups/${gid()}/expenses/${id}${override}`, { method: "DELETE" });
        ShareSquad.showToast("Expense deleted", "info");
        await loadExpenses();
      } catch (err) {
        ShareSquad.showToast(err.message, "error");
      }
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
  $("#f_amount").value = exp.total_amount;
  ShareSquad.fillCurrencySelect($("#f_currency"), exp.currency || groupCurrency());
  syncExpenseCurrencyUI();
  $("#f_category").value = exp.category || "General";
  $("#f_payer").value = exp.payer_member_id;

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
async function saveExpense() {
  if (state.isDraft) {
    const draftMessage = $("#saveMsg");
    draftMessage.textContent = "Invite at least one member before adding shared expenses.";
    draftMessage.className = "text-xs mt-2 font-mono balance-negative";
    return;
  }
  const store = $("#f_store").value.trim();
  const billDate = $("#f_date").value;
  const totalAmount = parseFloat($("#f_amount").value);
  const payerId = Number($("#f_payer").value);
  const category = $("#f_category").value;
  const msg = $("#saveMsg");

  if (!store || !billDate || !totalAmount || !payerId) {
    msg.textContent = "Please fill in all required fields.";
    msg.className = "text-xs mt-2 font-mono balance-negative";
    return;
  }

  let splitDetails = [];
  let splitWith = [];

  if (state.splitType === "EQUAL") {
    splitWith = Array.from(state.selectedSplit);
    if (!splitWith.length) {
      msg.textContent = "Select at least one member to split with.";
      msg.className = "text-xs mt-2 font-mono balance-negative";
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
      msg.textContent = "Enter split proportions for group members.";
      msg.className = "text-xs mt-2 font-mono balance-negative";
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
    split_with: splitWith.length ? splitWith : null,
    split_details: splitDetails.length ? splitDetails : null,
    period_key: state.currentPeriod,
  };

  try {
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
    msg.textContent = err.message;
    msg.className = "text-xs mt-2 font-mono balance-negative";
  }
}

// ---------------------------------------------------------------------------
// Balances & Currency Auto-Conversion
// ---------------------------------------------------------------------------
async function loadBalances() {
  const [balances, settlements, payments] = await Promise.all([
    ShareSquad.api(`/groups/${gid()}/periods/${state.currentPeriod}/balances`),
    ShareSquad.api(`/groups/${gid()}/periods/${state.currentPeriod}/settlements`),
    ShareSquad.api(`/groups/${gid()}/periods/${state.currentPeriod}/payments`),
  ]);

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
    const settlementActionsOpen = isCurrentPeriodSettled();
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
      const pendingPayment = payments.find((payment) => payment.status === "PAID" && Number(payment.payer_member_id) === Number(s.from_member_id) && Number(payment.recipient_member_id) === Number(s.to_member_id));
      const payerAction = "";
      const recipientAction = "";
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

  $$(".mark-payment").forEach((button) => button.addEventListener("click", async () => {
    const amount = Number(prompt(`How much of the ${button.dataset.currency} ${Number(button.dataset.amount).toFixed(2)} balance did you pay?`, button.dataset.amount));
    if (!Number.isFinite(amount) || amount <= 0) return;
    if (amount > Number(button.dataset.amount)) return ShareSquad.showToast("A partial payment cannot exceed the remaining balance.", "error");
    button.disabled = true;
    button.textContent = "Request sent";
    try {
      await ShareSquad.api(`/groups/${gid()}/periods/${state.currentPeriod}/payments`, {
        method: "POST",
        body: JSON.stringify({ payer_member_id: Number(button.dataset.payer), recipient_member_id: Number(button.dataset.recipient), amount, currency: button.dataset.currency }),
      });
      ShareSquad.showToast("Payment request sent to the recipient.", "success");
      await loadBalances();
    } catch (err) { ShareSquad.showToast(err.message, "error"); button.disabled = false; button.textContent = "Mark paid - request confirmation"; }
  }));
  $$(".mark-received").forEach((button) => button.addEventListener("click", async () => {
    const amount = Number(prompt(`How much of the ${button.dataset.currency} ${Number(button.dataset.amount).toFixed(2)} balance did you receive?`, button.dataset.amount));
    if (!Number.isFinite(amount) || amount <= 0) return;
    if (amount > Number(button.dataset.amount)) return ShareSquad.showToast("A partial payment cannot exceed the remaining balance.", "error");
    button.disabled = true;
    button.textContent = "Paid";
    try {
      await ShareSquad.api(`/groups/${gid()}/periods/${state.currentPeriod}/payments/received`, {
        method: "POST",
        body: JSON.stringify({ payer_member_id: Number(button.dataset.payer), recipient_member_id: Number(button.dataset.recipient), amount, currency: button.dataset.currency }),
      });
      ShareSquad.showToast("Payment recorded as paid and balances recalculated.", "success");
      await loadBalances();
    } catch (err) { ShareSquad.showToast(err.message, "error"); button.disabled = false; button.textContent = "Mark received & settle"; }
  }));
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
    console.error("Could not fetch rate:", err);
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
    root.innerHTML = `
      <div class="insight-kpis"><article class="bb-card"><p class="insight-label">Total spending</p><p class="insight-value">${ShareSquad.formatMoney(data.total_spent, data.currency)}</p><p class="text-xs text-ink/45">${state.currentPeriod}</p></article><article class="bb-card"><p class="insight-label">Top category</p><p class="insight-value text-lg">${ShareSquad.escapeHtml(categoryRows[0]?.[0] || "No expenses")}</p><p class="text-xs text-ink/45">${categoryRows[0] ? ShareSquad.formatMoney(categoryRows[0][1], data.currency) : ""}</p></article></div>
      <div class="grid md:grid-cols-2 gap-4"><article class="bb-card"><h3 class="font-mono uppercase text-xs tracking-widest text-brand font-bold mb-3">By category</h3>${categoryRows.length ? categoryRows.map(([name, amount]) => `<div class="insight-row"><span>${ShareSquad.escapeHtml(name)}</span><strong>${ShareSquad.formatMoney(amount, data.currency)}</strong></div>`).join("") : '<p class="text-sm text-ink/45">No expenses in this period.</p>'}</article><article class="bb-card"><h3 class="font-mono uppercase text-xs tracking-widest text-brand font-bold mb-3">Largest expenses</h3>${top.length ? top.map((expense) => `<div class="insight-row"><span>${ShareSquad.escapeHtml(expense.store_name)}</span><strong>${ShareSquad.formatMoney(expense.total_amount, expense.currency)}</strong></div>`).join("") : '<p class="text-sm text-ink/45">No expenses in this period.</p>'}</article></div>`;
  } catch (err) { root.innerHTML = `<div class="bb-card balance-negative">${ShareSquad.escapeHtml(err.message)}</div>`; }
}

async function openNotifications() {
  const modal = $("#notificationsModal");
  const list = $("#notificationsList");
  modal.classList.remove("hidden");
  list.innerHTML = '<p class="text-xs font-mono text-ink/40">Loading notifications...</p>';
  try {
    const items = await ShareSquad.api("/notifications");
    list.innerHTML = items.length ? items.map((item) => `<button class="notification-item ${item.read_at ? "" : "unread"}" data-id="${item.id}" data-link="${ShareSquad.escapeHtml(item.link || "")}"><span>${ShareSquad.escapeHtml(item.message)}</span><small>${ShareSquad.formatDate(item.created_at)}</small></button>`).join("") : '<p class="text-xs font-mono text-ink/40 py-4">You are all caught up.</p>';
    $$(".notification-item").forEach((button) => button.addEventListener("click", async () => {
      await ShareSquad.api(`/notifications/${button.dataset.id}/read`, { method: "POST" });
      if (button.dataset.link) window.location.href = button.dataset.link;
      else { button.classList.remove("unread"); loadNotificationCount(); }
    }));
    loadNotificationCount();
  } catch (err) { list.innerHTML = `<p class="text-xs balance-negative">${ShareSquad.escapeHtml(err.message)}</p>`; }
}

async function loadNotificationCount() {
  const badge = $("#notificationCount");
  if (!badge) return;
  try { const items = await ShareSquad.api("/notifications?unread_only=true"); badge.textContent = items.length > 9 ? "9+" : items.length; badge.classList.toggle("hidden", !items.length); } catch (_) { badge.classList.add("hidden"); }
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
          <span class="member-email">${ShareSquad.escapeHtml(m.email)}</span>
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
      if (!confirm(`Remove ${btn.dataset.name} from active group activity?\n\nActive-period expenses involving them will be removed. Completed, settled history and reports remain available.`)) return;
      try {
        await ShareSquad.api(`/groups/${gid()}/members/${btn.dataset.id}`, { method: "DELETE" });
        ShareSquad.showToast(`${btn.dataset.name} was removed`, "success");
        await loadGroup();
        await loadMembers();
      } catch (err) {
        ShareSquad.showToast(err.message, "error");
      }
    });
  });

  const url = `${window.location.origin}/join.html?code=${group.invite_code}`;
  $("#inviteLink").value = url;
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
      if (data.extraction.store_name) $("#f_store").value = data.extraction.store_name;
      if (data.extraction.bill_date) $("#f_date").value = data.extraction.bill_date;
      if (data.extraction.total_amount) $("#f_amount").value = data.extraction.total_amount;
      if (data.extraction.category) $("#f_category").value = data.extraction.category;
      ShareSquad.fillCurrencySelect($("#f_currency"), groupCurrency());
      syncExpenseCurrencyUI();
    }

    $("#sourceTag").textContent = "Scanned";
    applyDefaultSplit();
    renderSplitOptions();
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
    if (!confirm(`Re-open period ${state.currentPeriod} for editing?`)) return;
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

  $("#f_amount").addEventListener("input", () => {
    if (state.splitType !== "EQUAL") renderSplitOptions();
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
    $("#f_store").value = "";
    $("#f_date").value = new Date().toISOString().split("T")[0];
    $("#f_amount").value = "";
    $("#f_category").value = "Food & Dining";
    ShareSquad.fillCurrencySelect($("#f_currency"), groupCurrency());
    syncExpenseCurrencyUI();
    applyDefaultSplit();
    renderSplitOptions();
    requestAnimationFrame(() => { $("#extractionPanel").scrollIntoView({ behavior: "smooth", block: "start" }); setTimeout(() => $("#f_store").focus(), 300); });
  });

  $("#cancelEditBtn").addEventListener("click", cancelEdit);
  $("#saveExpenseBtn").addEventListener("click", saveExpense);

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
    if (!confirm("Remove this group photo?")) return;
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
      msg.textContent = result.message || `Invite sent to ${email}!`;
      msg.className = result.email_sent || result.already_member
        ? "text-xs font-mono balance-positive font-bold"
        : "text-xs font-mono text-ink/70";
      ShareSquad.showToast(result.message, result.email_sent || result.already_member ? "success" : "info");
      $("#inviteEmail").value = "";
      if (result.already_member) await loadMembers();
    } catch (err) {
      msg.textContent = err.message;
      msg.className = "text-xs font-mono balance-negative";
      ShareSquad.showToast(err.message, "error");
    } finally {
      if (submitBtn) submitBtn.disabled = false;
    }
  });

  $("#copyInviteBtn").addEventListener("click", () => {
    const input = $("#inviteLink");
    input.select();
    navigator.clipboard.writeText(input.value);
    ShareSquad.showToast("Invite link copied to clipboard!", "success");
  });

  $("#logoutBtn")?.addEventListener("click", () => {
    ShareSquad.removeToken();
    ShareSquad.removeStoredGroupId();
    window.location.href = "/login.html";
  });


  await loadGroupSettings();
  await loadGroup();
  await loadPeriods();
  await loadExpenses();
  await loadNotificationCount();
  startGroupSync();
})();
