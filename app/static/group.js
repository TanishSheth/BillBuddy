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
function switchTab(tabName) {
  $$(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tabName));
  $$(".tab-panel").forEach((p) => p.classList.toggle("hidden", p.id !== `tab-${tabName}`));

  if (tabName === "expenses") loadExpenses();
  if (tabName === "balances") loadBalances();
  if (tabName === "reports") loadReports();
  if (tabName === "members") loadMembers();
}

// ---------------------------------------------------------------------------
// Group & Period Loading
// ---------------------------------------------------------------------------
async function loadGroup() {
  const group = await ShareSquad.api(`/groups/${gid()}`);
  state.group = group;
  state.members = group.members;

  $("#currentGroupName").textContent = group.name;
  const gCurr = group.currency || "USD";
  $("#groupCurrencyBadge").innerHTML = `<span class="currency-sym">${ShareSquad.escapeHtml(ShareSquad.currencySymbol(gCurr))}</span> ${gCurr}`;

  ShareSquad.fillCurrencySelect($("#f_currency"), gCurr);
  syncExpenseCurrencyUI();

  const me = state.account;
  const myMem = group.members.find((m) => m.email === me.email);
  if (myMem) {
    state.myMemberId = myMem.id;
    state.myRole = myMem.role;
  }

  renderPayerOptions();
  renderSplitOptions();
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
  pollGroupSync();
  state.syncTimer = setInterval(pollGroupSync, 2000);
  document.addEventListener("visibilitychange", onSyncVisibility);
}

function stopGroupSync() {
  if (state.syncTimer) {
    clearInterval(state.syncTimer);
    state.syncTimer = null;
  }
  document.removeEventListener("visibilitychange", onSyncVisibility);
}

function onSyncVisibility() {
  if (!document.hidden) pollGroupSync();
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

function renderPayerOptions() {
  const sel = $("#f_payer");
  sel.innerHTML = state.members
    .map((m) => `<option value="${m.id}" ${m.id === state.myMemberId ? "selected" : ""}>${ShareSquad.escapeHtml(m.display_name)}</option>`)
    .join("");
}

function renderSplitOptions() {
  const container = $("#f_splitwith");
  const totalAmt = parseFloat($("#f_amount").value) || 0;
  const curr = expenseCurrency();
  const notice = $("#splitStatusNotice");
  container.innerHTML = "";

  if (state.splitType === "EQUAL") {
    notice.textContent = "Select members to split equally:";
    const chipsWrap = document.createElement("div");
    chipsWrap.className = "flex flex-wrap gap-2";

    state.members.forEach((m) => {
      const isSelected = state.selectedSplit.has(m.id);
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = `bb-chip ${isSelected ? "selected" : ""}`;
      chip.dataset.id = m.id;
      chip.textContent = m.display_name;
      chip.addEventListener("click", () => {
        if (state.selectedSplit.has(m.id)) {
          if (state.selectedSplit.size > 1) state.selectedSplit.delete(m.id);
        } else {
          state.selectedSplit.add(m.id);
        }
        renderSplitOptions();
      });
      chipsWrap.appendChild(chip);
    });
    container.appendChild(chipsWrap);
    return;
  }

  // Percentage, Exact, or Shares custom inputs
  state.members.forEach((m) => {
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
  const expenses = await ShareSquad.api(`/groups/${gid()}/expenses?period_key=${state.currentPeriod}${filterCat !== "All" ? `&category=${encodeURIComponent(filterCat)}` : ""}`);
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
        await ShareSquad.api(`/groups/${gid()}/expenses/${id}`, { method: "DELETE" });
        ShareSquad.showToast("Expense deleted", "info");
        await loadExpenses();
      } catch (err) {
        ShareSquad.showToast(err.message, "error");
      }
    });
  });
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
}

// ---------------------------------------------------------------------------
// Save Expense
// ---------------------------------------------------------------------------
async function saveExpense() {
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
      await ShareSquad.api(`/groups/${gid()}/expenses/${state.editingExpenseId}`, {
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
  const balances = await ShareSquad.api(`/groups/${gid()}/periods/${state.currentPeriod}/balances`);
  const settlements = await ShareSquad.api(`/groups/${gid()}/periods/${state.currentPeriod}/settlements`);

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
  list.innerHTML = group.members
    .map(
      (m) => `
      <div class="member-row">
        <div>
          <span class="font-bold text-ink">${ShareSquad.escapeHtml(m.display_name)}</span>
          ${m.role === "owner" ? '<span class="text-[10px] px-1.5 py-0.5 rounded bg-brand/10 text-brand font-bold ml-1.5 uppercase">Owner</span>' : ""}
          <span class="member-email">${ShareSquad.escapeHtml(m.email)}</span>
        </div>
      </div>`
    )
    .join("");

  const url = `${window.location.origin}/join.html?code=${group.invite_code}`;
  $("#inviteLink").value = url;
}

// ---------------------------------------------------------------------------
// Receipt Upload & VLM Handler
// ---------------------------------------------------------------------------
async function handleReceiptUpload(file) {
  if (!file) return;

  const preview = $("#previewImg");
  preview.src = URL.createObjectURL(file);
  preview.classList.remove("hidden");

  const formData = new FormData();
  formData.append("file", file);

  try {
    ShareSquad.showToast("Uploading receipt and reading it…", "info");
    const data = await ShareSquad.api(`/groups/${gid()}/receipts/upload`, {
      method: "POST",
      body: formData,
    });

    state.currentUploadId = data.upload_id;
    $("#extractionPanel").classList.remove("hidden");

    if (data.extraction) {
      if (data.extraction.store_name) $("#f_store").value = data.extraction.store_name;
      if (data.extraction.bill_date) $("#f_date").value = data.extraction.bill_date;
      if (data.extraction.total_amount) $("#f_amount").value = data.extraction.total_amount;
      if (data.extraction.category) $("#f_category").value = data.extraction.category;
      if (data.extraction.currency) {
        ShareSquad.fillCurrencySelect($("#f_currency"), data.extraction.currency);
      } else {
        ShareSquad.fillCurrencySelect($("#f_currency"), groupCurrency());
      }
      syncExpenseCurrencyUI();
    }

    $("#sourceTag").textContent = "Scanned";
    renderSplitOptions();
    const warn = Boolean(data.message);
    ShareSquad.showToast(
      data.message || "Receipt scanned. Please verify and save.",
      warn ? "info" : "success"
    );
  } catch (err) {
    ShareSquad.showToast(err.message, "error");
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
    window.location.href = "/";
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
    $("#extractionPanel").classList.remove("hidden");
    $("#previewImg").classList.add("hidden");
    $("#f_store").value = "";
    $("#f_date").value = new Date().toISOString().split("T")[0];
    $("#f_amount").value = "";
    $("#f_category").value = "Food & Dining";
    ShareSquad.fillCurrencySelect($("#f_currency"), groupCurrency());
    syncExpenseCurrencyUI();
    state.selectedSplit = new Set(state.members.map((m) => m.id));
    renderSplitOptions();
  });

  $("#cancelEditBtn").addEventListener("click", cancelEdit);
  $("#saveExpenseBtn").addEventListener("click", saveExpense);

  // File dropzone
  const dropzone = $("#dropzone");
  const fileInput = $("#fileInput");
  dropzone.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", (e) => handleReceiptUpload(e.target.files[0]));

  dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("dragover"); });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
  dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropzone.classList.remove("dragover");
    if (e.dataTransfer.files.length) handleReceiptUpload(e.dataTransfer.files[0]);
  });

  // Category filter on expenses
  $("#groupCategoryFilter").addEventListener("change", loadExpenses);

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

  await loadGroup();
  await loadPeriods();
  await loadExpenses();
  startGroupSync();
})();
