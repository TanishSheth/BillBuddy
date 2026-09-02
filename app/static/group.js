const TOKEN_KEY = "billbuddy_token";
const GROUP_KEY = "billbuddy_group_id";

const state = {
  account: null,
  groups: [],
  currentGroupId: null,
  members: [],
  periods: [],
  currentPeriod: null,
  currentUploadId: null,
  editingExpenseId: null,
  selectedSplit: new Set(),
  myMemberId: null,
  myRole: null,
  groupSettings: null,
  accountSettings: null,
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

function gid() {
  return state.currentGroupId;
}

function groupPath(path) {
  return `/groups/${gid()}${path}`;
}

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opts.body && !(opts.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }

  const res = await fetch(`/api${path}`, { ...opts, headers });
  if (res.status === 401) {
    localStorage.removeItem(TOKEN_KEY);
    window.location.href = "/login.html";
    return;
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    const detail = err.detail;
    const msg = typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((d) => d.msg).join(", ") : "Request failed";
    throw new Error(msg);
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res;
}

// ---------------- Auth / Groups ----------------
async function loadMe() {
  const me = await api("/auth/me");
  state.account = me.account;
}

async function loadCurrentGroup() {
  if (!gid()) return;
  const detail = await api(`/groups/${gid()}`);
  state.currentGroupName = detail.name;
  const title = $("#currentGroupName");
  if (title) title.textContent = detail.name;
  document.title = `${detail.name} — BillBuddy`;
}

function openGroup(groupId) {
  localStorage.setItem(GROUP_KEY, String(groupId));
  state.currentGroupId = groupId;
}

// ---------------- Tabs ----------------
function initTabs() {
  $$(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });
}

function switchTab(tab) {
  $$(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $$(".tab-panel").forEach((p) => p.classList.toggle("hidden", p.id !== `tab-${tab}`));
  const periodBar = $("#periodBar");
  if (periodBar) periodBar.classList.toggle("hidden", tab === "members");
  if (tab === "expenses") loadExpenses();
  if (tab === "balances") loadBalances();
  if (tab === "reports") loadReports();
  if (tab === "members") loadMembersTab();
}

// ---------------- Period selector ----------------
function currentPeriodKey() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

let periodsListenerBound = false;

async function loadPeriods() {
  if (!gid()) return;
  state.periods = await api(groupPath("/periods"));
  const nowKey = currentPeriodKey();
  if (!state.periods.find((p) => p.period_key === nowKey)) {
    state.periods.unshift({ period_key: nowKey, status: "ACTIVE", pdf_filename: null });
  }
  const sel = $("#periodSelect");
  const previous = state.currentPeriod || sel.value || nowKey;
  sel.innerHTML = state.periods
    .sort((a, b) => b.period_key.localeCompare(a.period_key))
    .map((p) => `<option value="${p.period_key}">${p.period_key}</option>`)
    .join("");
  const hasPrevious = state.periods.some((p) => p.period_key === previous);
  sel.value = hasPrevious ? previous : nowKey;
  state.currentPeriod = sel.value;
  updatePeriodStatusTag();
  if (!periodsListenerBound) {
    sel.addEventListener("change", () => {
      state.currentPeriod = sel.value;
      updatePeriodStatusTag();
      const activeTab = $(".tab-btn.active")?.dataset.tab;
      switchTab(activeTab || "expenses");
    });
    periodsListenerBound = true;
  }
}

function updatePeriodStatusTag() {
  const p = state.periods.find((p) => p.period_key === state.currentPeriod);
  const tag = $("#periodStatus");
  const status = p ? p.status : "ACTIVE";
  tag.textContent = status;
  tag.className = `text-[10px] uppercase tracking-wider px-2 py-0.5 rounded-full border ${status === "SETTLED" ? "status-settled" : "status-active"}`;
  const reopenBtn = $("#reopenPeriodBtn");
  if (reopenBtn) {
    reopenBtn.classList.toggle("hidden", status !== "SETTLED");
    lucide.createIcons();
  }
}

async function reopenCurrentPeriod() {
  const p = state.periods.find((period) => period.period_key === state.currentPeriod);
  const reportName = p?.pdf_filename || "the archived PDF";
  const confirmed = confirm(
    `Re-open ${state.currentPeriod} for editing?\n\nThis will permanently delete "${reportName}".`
  );
  if (!confirmed) return;
  try {
    await api(groupPath(`/periods/${state.currentPeriod}/reopen`), { method: "POST" });
    await loadPeriods();
    updatePeriodStatusTag();
    switchTab($(".tab-btn.active")?.dataset.tab || "expenses");
  } catch (err) {
    alert(err.message);
  }
}

// ---------------- Members ----------------
async function loadMembers() {
  if (!gid()) return;
  const detail = await api(`/groups/${gid()}`);
  state.members = detail.members;
  const mine = state.members.find((m) => m.email === state.account.email);
  state.myMemberId = mine?.id ?? null;
  state.myRole = mine?.role ?? null;
  populatePayerSelect();
  populateSplitChips();
}

function populatePayerSelect() {
  const sel = $("#f_payer");
  if (!sel) return;
  sel.innerHTML = state.members.map((m) => `<option value="${m.id}">${m.display_name}</option>`).join("");
  if (state.myMemberId) sel.value = state.myMemberId;
}

function populateSplitChips(selectedIds = null) {
  const container = $("#f_splitwith");
  if (!container) return;
  container.innerHTML = state.members
    .map((m) => `<button type="button" class="bb-chip" data-mid="${m.id}">${escapeHtml(m.display_name)}</button>`)
    .join("");
  const defaultAll = state.groupSettings?.equal_split_default !== false;
  if (selectedIds) {
    state.selectedSplit = new Set(selectedIds);
  } else if (defaultAll) {
    state.selectedSplit = new Set(state.members.map((m) => m.id));
  } else {
    state.selectedSplit = state.myMemberId ? new Set([state.myMemberId]) : new Set();
  }
  $$(".bb-chip", container).forEach((chip) => {
    const mid = Number(chip.dataset.mid);
    chip.classList.toggle("selected", state.selectedSplit.has(mid));
    chip.addEventListener("click", () => {
      if (state.selectedSplit.has(mid)) {
        state.selectedSplit.delete(mid);
        chip.classList.remove("selected");
      } else {
        state.selectedSplit.add(mid);
        chip.classList.add("selected");
      }
    });
  });
}

async function loadMembersTab() {
  await loadMembers();
  const list = $("#memberList");
  list.innerHTML = state.members
    .map(
      (m) => `<div class="member-row">
        <div class="min-w-0 flex-1">
          <span class="font-medium">${escapeHtml(m.display_name)}</span>
          ${m.role === "owner" ? '<span class="text-[10px] uppercase text-brand ml-1">owner</span>' : ""}
          <span class="member-email">${escapeHtml(m.email)}</span>
        </div>
        ${
          state.myRole === "owner" && m.role !== "owner"
            ? `<button data-mid="${m.id}" class="remove-member text-ink/30 hover:text-stamp shrink-0 p-1"><i data-lucide="user-minus" class="w-4 h-4"></i></button>`
            : ""
        }
      </div>`
    )
    .join("") || `<p class="text-ink/40">No members yet.</p>`;
  lucide.createIcons();

  $$(".remove-member", list).forEach((btn) =>
    btn.addEventListener("click", async () => {
      if (!confirm("Remove this member from the group?")) return;
      await api(groupPath(`/members/${btn.dataset.mid}`), { method: "DELETE" });
      await loadMembers();
      loadMembersTab();
    })
  );

  try {
    const link = await api(`/groups/${gid()}/invite-link`);
    $("#inviteLink").value = link.url;
    const hint = $("#inviteLinkHint");
    if (hint) {
      if (link.url.includes("localhost") || link.url.includes("127.0.0.1")) {
        hint.textContent = "Tip: open this app using your PC's IP (e.g. http://192.168.1.5:8001) so the link works on phones.";
        hint.classList.remove("hidden");
      } else {
        hint.classList.add("hidden");
      }
    }
  } catch (_) {
    $("#inviteLink").value = "";
  }
}

$("#inviteForm")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#inviteMsg");
  const email = $("#inviteEmail").value.trim();
  if (!email) return;
  try {
    const result = await api(`/groups/${gid()}/invites`, {
      method: "POST",
      body: JSON.stringify({ email }),
    });
    const messages = {
      joined: "They already had an account and were added to the group.",
      joined_emailed: "They were added and a notification email was sent.",
      emailed: "Invite email sent! They can join from the link in their inbox.",
      pending: "Invite saved. Restart the server if you recently added SMTP settings.",
      pending_no_email: result.message || "Copy the invite link below and send it to them.",
    };
    msg.textContent = messages[result.status] || "Done.";
    msg.className = "text-xs font-mono balance-positive";
    $("#inviteEmail").value = "";
    if (result.invite_url) $("#inviteLink").value = result.invite_url;
    await loadMembers();
  } catch (err) {
    msg.textContent = err.message;
    msg.className = "text-xs font-mono balance-negative";
  }
});

$("#copyInviteBtn")?.addEventListener("click", () => {
  const input = $("#inviteLink");
  input.select();
  navigator.clipboard.writeText(input.value).then(() => {
    const msg = $("#inviteMsg");
    msg.textContent = "Invite link copied!";
    msg.className = "text-xs font-mono balance-positive";
  });
});

async function loadUserPreferences() {
  try {
    state.accountSettings = await api("/settings");
    document.body.classList.toggle("compact-list", !!state.accountSettings?.compact_list_view);
  } catch (_) {
    state.accountSettings = null;
  }
  if (!gid()) return;
  try {
    state.groupSettings = await api(`/groups/${gid()}/settings`);
  } catch (_) {
    state.groupSettings = null;
  }
}

function todayIsoDate() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

function billDateDefaultForPeriod(periodKey) {
  if (!periodKey) return todayIsoDate();
  const [y, m] = periodKey.split("-").map(Number);
  const now = new Date();
  if (now.getFullYear() === y && now.getMonth() + 1 === m) return todayIsoDate();
  const lastDay = new Date(y, m, 0).getDate();
  return `${y}-${String(m).padStart(2, "0")}-${String(lastDay).padStart(2, "0")}`;
}

function updateSaveExpenseBtn() {
  const btn = $("#saveExpenseBtn");
  if (!btn) return;
  if (state.editingExpenseId) {
    btn.innerHTML = '<i data-lucide="check" class="w-4 h-4"></i> Update Expense';
    $("#cancelEditBtn").classList.remove("hidden");
  } else {
    btn.innerHTML = '<i data-lucide="check" class="w-4 h-4"></i> Save Expense';
    $("#cancelEditBtn").classList.add("hidden");
  }
  lucide.createIcons();
}

function resetExpenseForm() {
  state.currentUploadId = null;
  state.editingExpenseId = null;
  $("#extractionPanel")?.classList.add("hidden");
  $("#previewImg")?.classList.add("hidden");
  $("#previewImg")?.removeAttribute("src");
  const saveMsg = $("#saveMsg");
  if (saveMsg) saveMsg.textContent = "";
  updateSaveExpenseBtn();
}

function showExpenseForm({ uploadId = null, source = "manual", extraction = null, expenseId = null } = {}) {
  state.currentUploadId = uploadId;
  state.editingExpenseId = expenseId;

  const preview = $("#previewImg");
  if (uploadId) {
    preview.classList.remove("hidden");
  } else {
    preview.classList.add("hidden");
    preview.removeAttribute("src");
  }

  $("#f_store").value = extraction?.store_name || "";
  $("#f_date").value = extraction?.bill_date || billDateDefaultForPeriod(state.currentPeriod);
  $("#f_amount").value = extraction?.total_amount ?? "";

  const tag = $("#sourceTag");
  const titles = { vlm: "Verify extracted data", stub: "Verify extracted data", error: "Verify extracted data", edit: "Edit expense", manual: "Enter expense details" };
  const tagClasses = {
    vlm: "tag-vlm", stub: "tag-stub", error: "tag-error", edit: "tag-manual", manual: "tag-manual",
  };
  const tagTexts = {
    vlm: "AI extracted", stub: "no API key — add in Settings", error: "extraction failed", edit: "editing", manual: "manual entry",
  };
  tag.textContent = tagTexts[source] || "manual entry";
  tag.className = `text-[10px] font-mono px-2 py-0.5 rounded-full border ${tagClasses[source] || "tag-manual"}`;
  $("#extractionPanelTitle").textContent = titles[source] || "Enter expense details";

  populatePayerSelect();
  populateSplitChips(extraction?.split_with);
  if (extraction?.payer_member_id) $("#f_payer").value = extraction.payer_member_id;

  updateSaveExpenseBtn();
  $("#extractionPanel").classList.remove("hidden");
  lucide.createIcons();
}

function openEditExpense(expense) {
  switchTab("upload");
  showExpenseForm({
    expenseId: expense.id,
    source: "edit",
    extraction: {
      store_name: expense.store_name,
      bill_date: expense.bill_date,
      total_amount: expense.total_amount,
      payer_member_id: expense.payer_member_id,
      split_with: expense.splits.map((s) => s.member_id),
    },
  });
}

function initUpload() {
  const dz = $("#dropzone");
  const input = $("#fileInput");
  if (!dz || !input) return;
  dz.addEventListener("click", () => input.click());
  ["dragover", "dragenter"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("dragover"); })
  );
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("dragover"); })
  );
  dz.addEventListener("drop", (e) => {
    const file = e.dataTransfer.files[0];
    if (file) handleFile(file);
  });
  input.addEventListener("change", () => {
    if (input.files[0]) handleFile(input.files[0]);
  });
  $("#manualEntryBtn")?.addEventListener("click", () => showExpenseForm());
  $("#cancelEditBtn")?.addEventListener("click", () => resetExpenseForm());
}

async function handleFile(file) {
  const preview = $("#previewImg");
  preview.src = URL.createObjectURL(file);
  preview.classList.remove("hidden");
  $("#extractionPanel").classList.add("hidden");
  $("#saveMsg").textContent = "";

  const dz = $("#dropzone");
  dz.querySelector("p").textContent = "Reading receipt…";

  const form = new FormData();
  form.append("file", file);
  try {
    const result = await api(groupPath("/receipts/extract"), { method: "POST", body: form });
    preview.src = result.image_url;
    showExpenseForm({
      uploadId: result.upload_id,
      source: result.source,
      extraction: { ...result.extraction, message: result.message },
    });
  } catch (err) {
    alert(`Extraction failed: ${err.message}`);
  } finally {
    dz.querySelector("p").textContent = "Drop a receipt photo here";
  }
}

$("#saveExpenseBtn")?.addEventListener("click", async () => {
  const msg = $("#saveMsg");
  const payerId = Number($("#f_payer").value);
  const splitWith = Array.from(state.selectedSplit);
  const storeName = $("#f_store").value.trim();
  const billDate = $("#f_date").value;
  const totalAmount = Number($("#f_amount").value);

  if (!storeName) { msg.textContent = "Enter a store or description."; msg.className = "text-xs mt-2 font-mono balance-negative"; return; }
  if (!billDate) { msg.textContent = "Select a bill date."; msg.className = "text-xs mt-2 font-mono balance-negative"; return; }
  if (!Number.isFinite(totalAmount) || totalAmount <= 0) { msg.textContent = "Enter a valid total amount."; msg.className = "text-xs mt-2 font-mono balance-negative"; return; }
  if (splitWith.length === 0) { msg.textContent = "Select at least one member to split with."; msg.className = "text-xs mt-2 font-mono balance-negative"; return; }

  const payload = {
    store_name: storeName,
    bill_date: billDate,
    total_amount: totalAmount,
    payer_member_id: payerId,
    split_with: splitWith,
    period_key: state.currentPeriod,
  };

  try {
    if (state.editingExpenseId) {
      await api(groupPath(`/expenses/${state.editingExpenseId}`), { method: "PUT", body: JSON.stringify(payload) });
      msg.textContent = "Expense updated.";
    } else {
      if (state.currentUploadId) payload.upload_id = state.currentUploadId;
      await api(groupPath("/expenses"), { method: "POST", body: JSON.stringify(payload) });
      msg.textContent = "Expense saved.";
    }
    msg.className = "text-xs mt-2 font-mono balance-positive";
    setTimeout(() => resetExpenseForm(), 1200);
    await loadPeriods();
    switchTab("expenses");
  } catch (err) {
    msg.textContent = err.message;
    msg.className = "text-xs mt-2 font-mono balance-negative";
  }
});

// ---------------- Expenses ----------------
async function loadExpenses() {
  const expenses = await api(groupPath(`/expenses?period=${state.currentPeriod}`));
  const membersById = Object.fromEntries(state.members.map((m) => [m.id, m.display_name]));
  const periodSettled = state.periods.find((p) => p.period_key === state.currentPeriod)?.status === "SETTLED";
  const body = $("#expensesBody");
  const mobileList = $("#expensesMobileList");
  $("#expensesEmpty").classList.toggle("hidden", expenses.length > 0);

  body.innerHTML = expenses
    .map(
      (e) => `<tr class="border-b border-ink/5">
        <td class="py-2 pr-3 whitespace-nowrap">${escapeHtml(e.bill_date)}</td>
        <td class="py-2 pr-3">${escapeHtml(e.store_name)}</td>
        <td class="py-2 pr-3">${escapeHtml(membersById[e.payer_member_id] || "?")}</td>
        <td class="py-2 pr-3 text-ink/50">${e.splits.map((s) => escapeHtml(membersById[s.member_id] || "?")).join(", ")}</td>
        <td class="py-2 pr-3 text-right whitespace-nowrap">$${e.total_amount.toFixed(2)}</td>
        <td class="py-2 pr-3 text-right whitespace-nowrap">
          ${
            periodSettled
              ? ""
              : `<button data-id="${e.id}" class="edit-expense text-ink/30 hover:text-brand mr-2 p-1"><i data-lucide="pencil" class="w-4 h-4"></i></button>
                 <button data-id="${e.id}" class="delete-expense text-ink/30 hover:text-stamp p-1"><i data-lucide="trash-2" class="w-4 h-4"></i></button>`
          }
        </td>
      </tr>`
    )
    .join("");

  if (mobileList) {
    mobileList.innerHTML = expenses
      .map(
        (e) => `<div class="bb-mobile-card">
          <div class="card-top">
            <div class="card-store">${escapeHtml(e.store_name)}</div>
            <div class="card-amount">$${e.total_amount.toFixed(2)}</div>
          </div>
          <div class="card-meta">
            <div>${escapeHtml(e.bill_date)}</div>
            <div>Paid by ${escapeHtml(membersById[e.payer_member_id] || "?")}</div>
            <div>Split: ${e.splits.map((s) => escapeHtml(membersById[s.member_id] || "?")).join(", ")}</div>
          </div>
          ${
            periodSettled
              ? ""
              : `<div class="card-actions">
                  <button data-id="${e.id}" class="edit-expense text-ink/50 hover:text-brand flex items-center gap-1 text-xs"><i data-lucide="pencil" class="w-3.5 h-3.5"></i> Edit</button>
                  <button data-id="${e.id}" class="delete-expense text-ink/50 hover:text-stamp flex items-center gap-1 text-xs"><i data-lucide="trash-2" class="w-3.5 h-3.5"></i> Delete</button>
                </div>`
          }
        </div>`
      )
      .join("");
  }

  lucide.createIcons();
  const expenseById = Object.fromEntries(expenses.map((e) => [String(e.id), e]));
  const bindActions = (root) => {
    $$(".edit-expense", root).forEach((btn) =>
      btn.addEventListener("click", () => openEditExpense(expenseById[btn.dataset.id]))
    );
    $$(".delete-expense", root).forEach((btn) =>
      btn.addEventListener("click", async () => {
        if (!confirm("Delete this expense?")) return;
        await api(groupPath(`/expenses/${btn.dataset.id}`), { method: "DELETE" });
        loadExpenses();
      })
    );
  };
  bindActions(body);
  if (mobileList) bindActions(mobileList);
}

// ---------------- Balances / Settlements ----------------
async function loadBalances() {
  const [balances, settlements] = await Promise.all([
    api(groupPath(`/periods/${state.currentPeriod}/balances`)).catch(() => []),
    api(groupPath(`/periods/${state.currentPeriod}/settlements`)).catch(() => []),
  ]);
  $("#balanceBody").innerHTML = balances
    .map(
      (b) => `<tr class="border-b border-ink/5">
        <td class="py-2">${b.name}</td>
        <td class="py-2 text-right">$${b.total_paid.toFixed(2)}</td>
        <td class="py-2 text-right">$${b.total_share_owed.toFixed(2)}</td>
        <td class="py-2 text-right ${b.net_balance >= 0 ? "balance-positive" : "balance-negative"}">
          ${b.net_balance >= 0 ? "+" : ""}$${b.net_balance.toFixed(2)}
        </td>
      </tr>`
    )
    .join("");

  const list = $("#settlementList");
  $("#settlementEmpty").classList.toggle("hidden", settlements.length > 0);
  list.innerHTML = settlements
    .map(
      (s) => `<div class="flex items-center justify-between border-b border-ink/5 pb-2">
        <span>${s.from_name} <span class="text-brand">&#8594;</span> ${s.to_name}</span>
        <span class="font-bold">$${s.amount.toFixed(2)}</span>
      </div>`
    )
    .join("");
}

// ---------------- Reports ----------------
async function loadReports() {
  state.periods = await api(groupPath("/periods"));
  const list = $("#reportsList");
  const settled = state.periods.filter((p) => p.pdf_filename);
  const token = getToken();
  list.innerHTML =
    settled
      .map(
        (p) => `<div class="report-row border-b border-ink/10 last:border-0">
          <div class="min-w-0">
            <div class="font-mono text-sm font-medium">${escapeHtml(p.period_key)}</div>
            <div class="text-xs text-ink/40 break-all">${escapeHtml(p.pdf_filename)}</div>
          </div>
          <div class="report-actions">
            <a href="/api/groups/${gid()}/periods/${p.period_key}/report" class="bb-btn-primary !py-2 !px-4 text-xs download-report w-full sm:w-auto" data-period="${p.period_key}">
              <i data-lucide="download" class="w-3.5 h-3.5"></i> Download
            </a>
          </div>
        </div>`
      )
      .join("") || `<p class="text-ink/40 text-sm py-4">No reports generated yet.</p>`;
  lucide.createIcons();

  $$(".download-report", list).forEach((link) => {
    link.addEventListener("click", async (e) => {
      e.preventDefault();
      const period = link.dataset.period;
      const res = await fetch(`/api/groups/${gid()}/periods/${period}/report`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) { alert("Download failed"); return; }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `report-${period}.pdf`;
      a.click();
      URL.revokeObjectURL(url);
    });
  });

  const btn = $("#generateReportBtn");
  const currentIsSettled = state.periods.find((p) => p.period_key === state.currentPeriod)?.status === "SETTLED";
  btn.disabled = currentIsSettled;
  btn.classList.toggle("hidden", currentIsSettled);
}

$("#generateReportBtn")?.addEventListener("click", async () => {
  const msg = $("#reportMsg");
  msg.textContent = "Compiling PDF and emailing all members…";
  msg.className = "text-xs mt-3 font-mono text-ink/50";
  try {
    const result = await api(groupPath(`/periods/${state.currentPeriod}/generate-report`), { method: "POST" });
    if (result.emails_sent > 0) {
      msg.textContent = `Report generated and emailed to ${result.emails_sent} member${result.emails_sent === 1 ? "" : "s"}. Receipt images deleted.`;
    } else {
      msg.textContent = "Report generated. Add SMTP settings to email it to all members automatically.";
    }
    msg.className = "text-xs mt-3 font-mono balance-positive";
    await loadPeriods();
    await loadReports();
    updatePeriodStatusTag();
  } catch (err) {
    msg.textContent = err.message;
    msg.className = "text-xs mt-3 font-mono balance-negative";
  }
});

// ---------------- Init ----------------
(async function init() {
  if (!getToken()) {
    window.location.href = "/login.html";
    return;
  }

  const saved = localStorage.getItem(GROUP_KEY);
  if (!saved) {
    window.location.href = "/";
    return;
  }
  state.currentGroupId = Number(saved);

  initTabs();
  initUpload();
  switchTab("upload");
  $("#reopenPeriodBtn")?.addEventListener("click", reopenCurrentPeriod);
  $("#logoutBtn")?.addEventListener("click", () => {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(GROUP_KEY);
    window.location.href = "/login.html";
  });
  $("#backToDashboard")?.addEventListener("click", () => {
    localStorage.removeItem(GROUP_KEY);
    window.location.href = "/";
  });
  const settingsLink = $("#groupSettingsLink");
  if (settingsLink && gid()) {
    settingsLink.href = `/settings.html?group=${gid()}&tab=group`;
  }

  await loadMe();
  await loadUserPreferences();
  try {
    await loadCurrentGroup();
    await loadMembers();
    await loadPeriods();
  } catch (err) {
    localStorage.removeItem(GROUP_KEY);
    window.location.href = "/";
  }
})();
