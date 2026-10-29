// ShareSquad Dashboard Controller ($ / $$ come from api.js)

const EXPENSE_VIEW_KEY = "sharesquad_expense_view";
const EXPENSE_VIEW_HELP = {
  groups: "<strong>Groups:</strong> Shared multi-person expenses for trips, roommates, and projects.",
  friends: "<strong>Friends:</strong> Total = group Who pays whom with that person + private bills. Shared group expenses appear for viewing only and are not added again. Use Manage friends to add or remove people.",
};

function normalizeExpenseView(value) {
  return value === "groups" ? "groups" : "friends";
}

function resolveExpenseView() {
  const fromQuery = new URLSearchParams(location.search).get("view");
  if (fromQuery === "groups") return "groups";
  if (fromQuery === "friends") return "friends";
  // A sandbox visitor should immediately see the groups they can create and
  // edit. Normal signed-in users keep Friends as their dashboard default.
  if (localStorage.getItem("sharesquad_demo_sandbox") === "1") return "groups";
  // Friends is the dashboard landing view. Groups remain one tap away.
  return "friends";
}

function setExpenseView(view, { updateUrl = true } = {}) {
  const next = normalizeExpenseView(view);
  try { sessionStorage.setItem(EXPENSE_VIEW_KEY, next); } catch (_) {}

  $$("#expenseViewSelector [data-expense-view]").forEach((btn) => {
    const active = btn.dataset.expenseView === next;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  $$("[data-main-screen]").forEach((btn) => {
    const active = btn.dataset.mainScreen === next;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  $("#expenseGroupsPanel")?.classList.toggle("hidden", next !== "groups");
  $("#expenseFriendsPanel")?.classList.toggle("hidden", next !== "friends");
  const activePanel = next === "friends" ? $("#expenseFriendsPanel") : $("#expenseGroupsPanel");
  activePanel?.animate(
    [{ transform: "translateX(10px)", opacity: 0 }, { transform: "translateX(0)", opacity: 1 }],
    { duration: 180, easing: "ease-out" },
  );

  const help = $("#expenseViewHelp");
  if (help) help.innerHTML = EXPENSE_VIEW_HELP[next];

  // Bottom nav: Groups / Friends match the expense-view toggles
  $$(".bottom-nav a").forEach((link) => link.classList.remove("active"));
  const bottomKey = next === "friends" ? "friends" : "groups";
  document.querySelector(`.bottom-nav a[data-bottom-nav="${bottomKey}"]`)?.classList.add("active");

  if (updateUrl) {
    const url = new URL(location.href);
    url.searchParams.set("view", next);
    history.replaceState({}, "", url.pathname + url.search + url.hash);
  }
}

function openGroup(groupId, options = {}) {
  ShareSquad.setStoredGroupId(groupId);
  const params = new URLSearchParams({ group: String(groupId) });
  if (options.tab) params.set("tab", options.tab);
  window.location.href = `/group.html?${params.toString()}`;
}

async function detectDemoSandbox() {
  // Only trust the flag set when a sandbox is created. Probing GET /demo/sandbox
  // for every normal account produced recurring 404 noise in the console.
  return localStorage.getItem("sharesquad_demo_sandbox") === "1";
}

function navigateMainScreen(screen, direction = 1) {
  if (screen === "friends" || screen === "groups") {
    setExpenseView(screen);
    return;
  }
  if (screen !== "personal") return;
  const destination = "/personal.html";
  // A short exit animation keeps the move to the separate Personal page from
  // feeling abrupt. Browsers that support cross-document view transitions can
  // enhance this naturally; others use this safe fallback.
  document.querySelector("main.page-main")?.animate(
    [{ transform: "translateX(0)", opacity: 1 }, { transform: `translateX(${direction > 0 ? "-4%" : "4%"})`, opacity: 0 }],
    { duration: 140, easing: "ease-out", fill: "forwards" },
  ).finished.finally(() => { window.location.href = destination; });
}

let createGroupFriends = [];
let createGroupEmails = [];
let createGroupSelectedFriendIds = new Set();

function renderCreateGroupMembers() {
  const friendList = $("#createGroupFriends");
  const chips = $("#createGroupMemberChips");
  if (friendList) {
    friendList.innerHTML = createGroupFriends.length
      ? createGroupFriends.map((friend) => `<label class="flex items-center gap-2 rounded px-2 py-1.5 hover:bg-ink/[0.03] cursor-pointer"><input type="checkbox" class="create-group-friend" value="${friend.id}" ${createGroupSelectedFriendIds.has(Number(friend.id)) ? "checked" : ""}><span class="min-w-0 truncate">${ShareSquad.escapeHtml(friend.display_name)} <span class="text-ink/45 text-xs">${ShareSquad.escapeHtml(ShareSquad.maskEmail(friend.email))}</span></span></label>`).join("")
      : '<p class="text-xs text-ink/45">No friends available yet. Add someone by email instead.</p>';
    friendList.querySelectorAll(".create-group-friend").forEach((input) => {
      input.addEventListener("change", () => {
        const friendId = Number(input.value);
        if (input.checked) createGroupSelectedFriendIds.add(friendId);
        else createGroupSelectedFriendIds.delete(friendId);
      });
    });
  }
  if (chips) {
    chips.innerHTML = createGroupEmails.map((email) => `<span class="inline-flex items-center gap-1.5 rounded-full bg-brand/10 text-brand px-2.5 py-1 text-xs font-mono">${ShareSquad.escapeHtml(ShareSquad.maskEmail(email))}<button type="button" class="remove-create-group-email email-chip-remove" data-email="${ShareSquad.escapeHtml(email)}" aria-label="Remove ${ShareSquad.escapeHtml(ShareSquad.maskEmail(email))}">×</button></span>`).join("");
    chips.querySelectorAll(".remove-create-group-email").forEach((button) => {
      button.addEventListener("click", () => {
        createGroupEmails = createGroupEmails.filter((email) => email !== button.dataset.email);
        renderCreateGroupMembers();
      });
    });
  }
}

async function loadCreateGroupFriends() {
  try {
    createGroupFriends = await ShareSquad.api("/friends");
  } catch {
    createGroupFriends = [];
  }
  renderCreateGroupMembers();
}

function showCreateGroupModal() {
  $("#createGroupModal").classList.remove("hidden");
  $("#createGroupName").value = "";
  $("#createGroupMsg").textContent = "";
  $("#createGroupMemberEmail").value = "";
  createGroupEmails = [];
  createGroupSelectedFriendIds = new Set();
  loadCreateGroupFriends();
  $("#createGroupName").focus();
}

function hideCreateGroupModal() {
  $("#createGroupModal").classList.add("hidden");
}

function openQuickSplit(id) {
  window.location.href = `/quick-split.html?id=${encodeURIComponent(id)}`;
}

function openFriendsBillFlow() {
  window.location.href = "/friends.html?new=1";
}

/** Reminder available whenever this friend currently owes you (any currency). */
function consolidatedReminderEligible(consolidated) {
  const balances = Array.isArray(consolidated?.balances) ? consolidated.balances : (consolidated ? [consolidated] : []);
  return balances.some((total) => Number(total.balance || 0) > 0.005);
}

async function sendFriendPaymentReminder(friendId, friendName) {
  if (!await ShareSquad.confirmDialog({
    title: `Remind ${friendName}?`,
    message: `Send a polite email only to ${friendName} with the total they still owe you? Nothing is sent to anyone else.`,
    confirmLabel: "Send reminder",
    danger: false,
  })) return;
  try {
    const result = await ShareSquad.api(`/friends/${friendId}/payment-reminder`, { method: "POST" });
    ShareSquad.showToast(result.message || `Reminder sent to ${friendName}.`, "success");
  } catch (error) {
    ShareSquad.showToast(error.message || "Could not send reminder", "error");
  }
}

function renderQuickSplits(_splits, contacts = [], consolidatedByFriend = new Map()) {
  const grid = $("#friendSplitsGrid");
  const empty = $("#emptyFriendSplits");
  const activeFriendIds = new Set(contacts.map((person) => person.id));
  const splits = (_splits || []).filter((split) => {
    const otherId = split.is_payer ? split.participants?.find((person) => !person.is_me)?.account_id : split.payer_account_id;
    return otherId && activeFriendIds.has(otherId);
  });
  const people = new Map();
  splits.forEach((split) => {
    const other = split.is_payer ? split.participants?.find((person) => !person.is_me) : { id: split.payer_account_id, display_name: split.payer_name || "Friend" };
    const personName = other?.display_name || ShareSquad.maskEmail(other?.email) || "Friend";
    const key = other?.account_id || other?.id || `split-${split.id}`;
    const entry = people.get(key) || { id: other?.account_id || other?.id || null, name: personName, balance: 0, count: 0, currency: split.currency || "USD", splitId: split.id };
    entry.count += 1;
    people.set(key, entry);
  });
  // Friend cards must use the same consolidated API as Friends detail.
  // Raw bill math ignores confirmed Friend payments and can disagree across tabs.
  contacts.forEach((contact) => {
    const key = contact.id;
    const existing = people.get(key) || { id: contact.id, name: contact.display_name, balance: 0, count: 0, currency: "USD", splitId: null };
    const consolidated = consolidatedByFriend.get(contact.id);
    if (consolidated) {
      const balances = Array.isArray(consolidated.balances) ? consolidated.balances : [consolidated];
      existing.balances = balances
        .map((item) => ({ balance: Number(item.balance || 0), currency: item.currency || existing.currency }))
        .filter((item) => Math.abs(item.balance) >= 0.005);
      const primary = existing.balances[0];
      existing.balance = primary?.balance || 0;
      existing.currency = primary?.currency || existing.currency;
      existing.hasConsolidatedBalance = true;
      existing.balancePending = false;
      existing.reminderEligible = consolidatedReminderEligible(consolidated);
    } else {
      existing.balances = [];
      existing.balance = 0;
      existing.hasConsolidatedBalance = false;
      existing.balancePending = true;
      existing.reminderEligible = false;
    }
    people.set(key, existing);
  });
  if (!people.size) { grid.innerHTML = ""; empty.classList.remove("hidden"); return; }
  empty.classList.add("hidden");
  grid.innerHTML = [...people.values()].map((person) => {
    if (person.balancePending) {
      const href = person.id ? `/friends.html?friend=${encodeURIComponent(person.id)}` : `/quick-split.html?id=${encodeURIComponent(person.splitId)}`;
      return `<div class="bb-card friend-split-card flex flex-col justify-between gap-3 border-l-4 cursor-pointer border-l-slate-300" style="border-top-color:var(--line);border-right-color:var(--line);border-bottom-color:var(--line);" data-href="${href}" role="link" tabindex="0">
  <div>
    <div class="flex items-center justify-between gap-2">
      <p class="font-mono text-[10px] tracking-widest text-brand uppercase">Friend</p>
      <span class="text-[10px] font-bold px-2 py-0.5 rounded-full bg-slate-200 text-slate-600">…</span>
    </div>
    <h3 class="font-mono font-bold text-base leading-tight text-ink mt-2">${ShareSquad.escapeHtml(person.name)}</h3>
    <p class="text-xl font-bold font-mono mt-2 text-slate-500">—</p>
    <p class="text-xs mt-1 text-ink/60">Open details for the live balance</p>
    <p class="text-[10px] font-mono mt-2 text-ink/40">${person.count ? `${person.count} direct bill${person.count === 1 ? "" : "s"} in history` : "Friend"}</p>
  </div>
  <a class="bb-btn-secondary w-full !py-1.5 text-[11px]" href="${href}"><i data-lucide="receipt-text" class="w-3.5 h-3.5"></i> Open details</a>
</div>`;
    }
    const balanceLines = person.balances || [{ balance: person.balance, currency: person.currency }];
    const nonZeroBalances = balanceLines.filter((item) => Math.abs(item.balance) >= 0.005);
    const hasMultipleBalances = nonZeroBalances.length > 1;
    const amount = ShareSquad.formatMoney(Math.abs(person.balance), person.currency);
    const isPositive = nonZeroBalances.length && nonZeroBalances.every((item) => item.balance > 0);
    const isNegative = nonZeroBalances.length && nonZeroBalances.every((item) => item.balance < 0);
    const status = hasMultipleBalances
      ? "Balances by currency"
      : isPositive
        ? `${ShareSquad.escapeHtml(person.name)} owes you ${amount}`
        : isNegative
          ? `You owe ${ShareSquad.escapeHtml(person.name)} ${amount}`
          : "Settled up";
    const leftBorder = isPositive ? "border-l-emerald-500" : isNegative ? "border-l-red-500" : "border-l-slate-300";
    const amtCls     = isPositive ? "text-emerald-700"    : isNegative ? "text-red-700"      : "text-slate-500";
    const badgeCls   = isPositive ? "bg-emerald-500 text-white" : isNegative ? "bg-red-500 text-white" : "bg-slate-200 text-slate-600";
    const icon       = isPositive ? "↓ they owe"          : isNegative ? "↑ you owe"         : hasMultipleBalances ? "↕ multi-currency" : "✓ settled";
    const href = person.id ? `/friends.html?friend=${encodeURIComponent(person.id)}` : `/quick-split.html?id=${encodeURIComponent(person.splitId)}`;
    const amountMarkup = hasMultipleBalances
      ? nonZeroBalances.map((item) => {
          const itemPositive = item.balance > 0;
          return `<p class="text-lg font-bold font-mono ${itemPositive ? "text-emerald-700" : "text-red-700"}">${itemPositive ? "+" : "−"}${ShareSquad.formatMoney(Math.abs(item.balance), item.currency)}</p>`;
        }).join("")
      : `<p class="text-xl font-bold font-mono mt-2 ${amtCls}">${isPositive || isNegative ? amount : "—"}</p>`;
    const remindBtn = person.id && person.reminderEligible
      ? `<button type="button" class="remind-friend-pay bb-btn-secondary w-full !py-1.5 text-[11px]" data-remind-friend="${person.id}"><i data-lucide="mail" class="w-3.5 h-3.5"></i> Send payment reminder</button>`
      : "";
    return `<div class="bb-card friend-split-card flex flex-col justify-between gap-3 border-l-4 cursor-pointer ${leftBorder}" style="border-top-color:var(--line);border-right-color:var(--line);border-bottom-color:var(--line);" data-href="${href}" role="link" tabindex="0">
  <div>
    <div class="flex items-center justify-between gap-2">
      <p class="font-mono text-[10px] tracking-widest text-brand uppercase">Friend</p>
      <span class="text-[10px] font-bold px-2 py-0.5 rounded-full ${badgeCls}">${icon}</span>
    </div>
    <h3 class="font-mono font-bold text-base leading-tight text-ink mt-2">${ShareSquad.escapeHtml(person.name)}</h3>
    <div class="mt-2">${amountMarkup}</div>
    <p class="text-xs mt-1 text-ink/60">${status}</p>
    <p class="text-[10px] font-mono mt-2 text-ink/40">${person.hasConsolidatedBalance ? "Total to pay" : `${person.count} direct bill${person.count === 1 ? "" : "s"} in history`}</p>
  </div>
  <div class="space-y-2">
    <a class="bb-btn-secondary w-full !py-1.5 text-[11px]" href="${href}"><i data-lucide="receipt-text" class="w-3.5 h-3.5"></i> Open details</a>
    ${remindBtn}
  </div>
</div>`;
  }).join("");
  grid.querySelectorAll(".friend-split-card").forEach((card) => {
    const open = () => { window.location.href = card.dataset.href; };
    card.addEventListener("click", (event) => { if (!event.target.closest("a, button")) open(); });
    card.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } });
  });
  const peopleById = new Map([...people.values()].filter((person) => person.id).map((person) => [Number(person.id), person]));
  grid.querySelectorAll(".remind-friend-pay").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      const friendId = Number(button.dataset.remindFriend);
      sendFriendPaymentReminder(friendId, peopleById.get(friendId)?.name || "this friend");
    });
  });
  lucide.createIcons();
}

let dashboardGroups = [];

function renderGroups(groups) {
  dashboardGroups = Array.isArray(groups) ? groups : [];
  const grid = $("#groupsGrid");
  const empty = $("#emptyGroups");

  if (!dashboardGroups.length) {
    grid.innerHTML = "";
    empty.classList.remove("hidden");
    lucide.createIcons();
    return;
  }

  empty.classList.add("hidden");
  grid.innerHTML = dashboardGroups
    .map(
      (g) => `<div class="bb-card group-card flex flex-col justify-between gap-3 cursor-pointer" data-id="${g.id}" data-draft="${g.member_count < 2 ? "1" : "0"}" role="link" tabindex="0">
        <div>
          <div class="flex items-start justify-between gap-2">
            <div class="flex items-start gap-2 min-w-0">
              <img alt="" class="group-cover-thumb ${g.has_cover ? "" : "hidden"}" data-cover-id="${g.id}">
              <h3 class="font-mono font-bold text-base leading-tight text-ink">${ShareSquad.escapeHtml(g.name)}</h3>
            </div>
            <span class="px-2 py-0.5 rounded text-[10px] font-bold bg-brand/10 text-brand"><span class="currency-sym">${ShareSquad.escapeHtml(ShareSquad.currencySymbol(g.currency || "USD"))}</span> ${g.currency || "USD"}</span>
          </div>
          <p class="text-xs text-ink/50 mt-1 font-mono flex items-center gap-1.5">
            <span>${g.member_count} member${g.member_count === 1 ? "" : "s"}</span>
            ${g.role === "owner" ? ' &bull; <span class="text-brand font-bold">Owner</span>' : ""}
          </p>
          ${g.member_count < 2
            ? '<span class="status-chip status-draft mt-2">Draft · invite a member</span><p class="text-[11px] text-ink/55 mt-1.5">Open Members to invite someone before shared bills unlock.</p>'
            : '<span class="status-chip status-active mt-2">Active</span>'}
          <p class="text-[10px] text-ink/40 mt-1.5 font-mono">Created ${ShareSquad.formatDate(g.created_at)}</p>
        </div>
        <div class="flex gap-2 pt-3 border-t border-ink/10">
          <button type="button" class="open-group bb-btn-primary flex-1 !py-1.5 text-[11px]" data-id="${g.id}" data-draft="${g.member_count < 2 ? "1" : "0"}">
            <i data-lucide="folder-open" class="w-3.5 h-3.5"></i> ${g.member_count < 2 ? "Finish setup" : "Open Group"}
          </button>
          ${
            g.role === "owner"
              ? `<button type="button" class="delete-group bb-btn-secondary !py-1.5 !px-2.5 text-[11px] text-stamp border-stamp/30 hover:bg-stamp/5" data-id="${g.id}" data-name="${ShareSquad.escapeHtml(g.name)}" title="Delete group">
                   <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
                 </button>`
              : `<button type="button" class="leave-group bb-btn-secondary !py-1.5 !px-2.5 text-[11px] text-stamp border-stamp/30 hover:bg-stamp/5" data-id="${g.id}" data-name="${ShareSquad.escapeHtml(g.name)}" title="Leave group">
                   <svg class="leave-group-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M14 3h6v18h-6M14 6h2"/><circle cx="8" cy="6" r="2"/><path d="M8 9v5m0-4-3 2m3-2 3 2m-3 2-3 5m3-5 3 5M2 13h8m-2-2 2 2-2 2"/></svg>
                 </button>`
          }
        </div>
      </div>`
    )
    .join("");

  lucide.createIcons();
  grid.querySelectorAll("[data-cover-id]").forEach((img) => {
    if (!img.classList.contains("hidden")) {
      ShareSquad.setAuthImage(img, `/groups/${img.dataset.coverId}/cover`);
    }
  });

  grid.querySelectorAll(".open-group").forEach((btn) => {
    btn.addEventListener("click", () => openGroup(Number(btn.dataset.id), {
      tab: btn.dataset.draft === "1" ? "members" : undefined,
    }));
  });

  grid.querySelectorAll(".group-card").forEach((card) => {
    const open = () => openGroup(Number(card.dataset.id), {
      tab: card.dataset.draft === "1" ? "members" : undefined,
    });
    card.addEventListener("click", (event) => { if (!event.target.closest("button, a")) open(); });
    card.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } });
  });

  grid.querySelectorAll(".delete-group").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const name = btn.dataset.name;
      const id = Number(btn.dataset.id);
      if (!await ShareSquad.confirmDialog({
        title: `Delete “${name}”?`,
        message: "This removes the group, its expenses, reports, and members. You can undo for a few seconds.",
        confirmLabel: "Delete group",
      })) return;
      const snapshot = dashboardGroups.slice();
      ShareSquad.runWithUndo({
        message: `Group “${name}” deleted`,
        onHide: () => renderGroups(dashboardGroups.filter((g) => g.id !== id)),
        onUndo: () => renderGroups(snapshot),
        onCommit: () => ShareSquad.api(`/groups/${id}`, { method: "DELETE" }),
      });
    });
  });

  grid.querySelectorAll(".leave-group").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const name = btn.dataset.name;
      const id = Number(btn.dataset.id);
      if (!await ShareSquad.confirmDialog({
        title: `Leave “${name}”?`,
        message: "Your active expenses in this group will be removed and you will lose access. You can undo for a few seconds.",
        confirmLabel: "Leave group",
      })) return;
      const snapshot = dashboardGroups.slice();
      ShareSquad.runWithUndo({
        message: `Left group “${name}”`,
        onHide: () => renderGroups(dashboardGroups.filter((g) => g.id !== id)),
        onUndo: () => renderGroups(snapshot),
        onCommit: () => ShareSquad.api(`/groups/${id}/leave`, { method: "POST" }),
      });
    });
  });
}

function renderOnboarding(groups, friends, quickSplits) {
  const card = $("#onboardingChecklist");
  if (!card || localStorage.getItem("sharesquad_onboarding_dismissed")) return;
  const steps = [
    [groups.length > 0, "Create your first group", "#newGroupBtn"],
    [groups.some((group) => group.member_count > 1), "Invite a member", "/index.html?view=friends"],
    [quickSplits.length > 0, "Log your first shared bill", "/friends.html?new=1"],
    [groups.length > 0 || friends.length > 0, "Review balances and settlements", groups[0] ? `/group.html?group=${encodeURIComponent(groups[0].id)}&tab=balances` : "/index.html?view=friends"],
  ];
  const complete = steps.filter(([done]) => done).length;
  if (complete === steps.length) { card.classList.add("hidden"); return; }
  card.classList.remove("hidden");
  card.innerHTML = `<div class="flex justify-between gap-3"><div><h2 class="font-semibold text-base">Getting started with ShareSquad</h2><p class="text-sm text-ink/60 mt-1">${complete}/4 complete</p></div><button type="button" class="text-ink/45" aria-label="Dismiss checklist">×</button></div><div class="onboarding-progress"><span style="width:${complete * 25}%"></span></div><div class="mt-4 grid sm:grid-cols-2 gap-2">${steps.map(([done, label, target]) => `<a href="${target.startsWith("#") ? "javascript:void(0)" : target}" data-onboarding-target="${target}" class="onboarding-step ${done ? "done" : ""}">${done ? "✓" : "○"} ${label}</a>`).join("")}</div>`;
  card.querySelector("button").onclick = () => { localStorage.setItem("sharesquad_onboarding_dismissed", "1"); card.classList.add("hidden"); };
  card.querySelectorAll("[data-onboarding-target]").forEach((item) => item.onclick = (event) => { const target = item.dataset.onboardingTarget; if (target.startsWith("#")) { event.preventDefault(); $(target)?.click(); } });
}

async function loadDashboard() {
  ShareSquad.renderSkeleton($("#groupsGrid"));
  ShareSquad.renderSkeleton($("#friendSplitsGrid"));
  try {
    const me = await ShareSquad.api("/auth/me");
    const greeting = $("#userGreeting");
    if (greeting) greeting.textContent = me.account.display_name;
    if (me.account.has_avatar) {
      ShareSquad.setAuthImage($("#headerAvatar"), "/auth/me/avatar");
      $("#headerInitials")?.classList.add("hidden");
    }
    if ($("#headerInitials")) $("#headerInitials").textContent = ShareSquad.initials(me.account.display_name);
    const [groups, quickSplits, friends] = await Promise.all([ShareSquad.api("/groups"), ShareSquad.api("/quick-splits"), ShareSquad.api("/friends")]);
    let consolidatedByFriend = new Map();
    try {
      const summary = await ShareSquad.api("/friends/consolidated-balances-summary");
      consolidatedByFriend = new Map((summary.friends || []).map((row) => [row.friend_id, row]));
    } catch (err) {
      // Fallback for older servers: one request per friend.
      const consolidatedResults = await Promise.all(friends.map(async (friend) => {
        const total = await ShareSquad.api(`/friends/${friend.id}/consolidated-balances`).catch(() => null);
        return [friend.id, total];
      }));
      consolidatedByFriend = new Map(consolidatedResults.filter(([, total]) => total).map(([friendId, total]) => [friendId, total]));
    }
    renderGroups(groups);
    renderQuickSplits(quickSplits, friends, consolidatedByFriend);
    renderOnboarding(groups, friends, quickSplits);
  } catch (err) {
    console.error("Failed to load dashboard");
    ShareSquad.showToast(err.message || "Could not load groups", "error");
  }
}

async function submitCreateGroup(e) {
  e.preventDefault();
  const name = $("#createGroupName").value.trim();
  const currency = $("#createGroupCurrency").value || "USD";
  const friend_ids = [...createGroupSelectedFriendIds];
  const msg = $("#createGroupMsg");

  if (!name) {
    msg.textContent = "Enter a group name.";
    msg.className = "text-xs font-mono balance-negative";
    return;
  }
  msg.textContent = "Creating…";
  msg.className = "text-xs font-mono text-ink/50";
  try {
    const group = await ShareSquad.api("/groups", {
      method: "POST",
      body: JSON.stringify({ name, currency, friend_ids, invite_emails: createGroupEmails }),
    });
    hideCreateGroupModal();
    ShareSquad.showToast(`Group "${group.name}" created!`, "success");
    openGroup(group.id);
  } catch (err) {
    msg.textContent = err.message;
    msg.className = "text-xs font-mono balance-negative";
    ShareSquad.showToast(err.message, "error");
  }
}

(async function init() {
  if (!ShareSquad.getToken()) {
    ShareSquad.goToLogin({ replace: true });
    return;
  }

  $("#newGroupBtn")?.addEventListener("click", showCreateGroupModal);
  $("#splitGroupBtn")?.addEventListener("click", showCreateGroupModal);
  $("#quickSplitBtn")?.addEventListener("click", openFriendsBillFlow);
  $("#settingsBtn")?.addEventListener("click", () => { ShareSquad.navigateWithReturn("/settings.html"); });
  $("#createFirstGroupBtn")?.addEventListener("click", showCreateGroupModal);
  $("#createGroupForm")?.addEventListener("submit", submitCreateGroup);
  $("#addCreateGroupEmailBtn")?.addEventListener("click", () => {
    const input = $("#createGroupMemberEmail");
    const email = input.value.trim().toLowerCase();
    if (!email) return;
    if (!input.checkValidity()) {
      input.reportValidity();
      return;
    }
    if (!createGroupEmails.includes(email)) createGroupEmails.push(email);
    input.value = "";
    renderCreateGroupMembers();
  });
  $("#createGroupMemberEmail")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      $("#addCreateGroupEmailBtn")?.click();
    }
  });
  $("#cancelCreateGroupBtn")?.addEventListener("click", hideCreateGroupModal);
  $("#closeCreateModalBtn")?.addEventListener("click", hideCreateGroupModal);
  $("#createGroupModal")?.addEventListener("click", (e) => {
    if (e.target.id === "createGroupModal") hideCreateGroupModal();
  });
  ShareSquad.fillAllCurrencySelects();
  $$("#expenseViewSelector [data-expense-view]").forEach((btn) => {
    btn.addEventListener("click", () => setExpenseView(btn.dataset.expenseView));
  });
  $$('[data-main-screen="personal"]').forEach((btn) => {
    btn.addEventListener("click", () => navigateMainScreen("personal", 1));
  });
  await detectDemoSandbox();
  ShareSquad.renderDemoBanner();
  setExpenseView(resolveExpenseView(), { updateUrl: true });

  // Right swipe advances Friends → Groups → Personal; left swipe reverses it.
  const EXPENSE_VIEW_ORDER = ["friends", "groups", "personal"];
  const SWIPE_BLOCK =
    ".page-header, .bottom-nav, .mobile-quick-action, .mobile-quick-actions, " +
    "#expenseViewSelector, input, textarea, select, button, a, label, " +
    "[role='dialog'], .modal, .modal-overlay, [data-no-swipe], .split-type-pill";
  let _swipe = null;

  function swipeIsBlocked(target) {
    if (!(target instanceof Element)) return true;
    if (target.closest(SWIPE_BLOCK)) return true;
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

  function currentExpenseView() {
    const active = $("#expenseViewSelector [data-expense-view].active");
    return normalizeExpenseView(active?.dataset.expenseView);
  }

  function goExpenseSwipeTarget(next, direction) { if (next) navigateMainScreen(next, direction); }

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
    const panel = e.target.closest?.(".expense-view-panel");
    const help = e.target.closest?.(".how-it-works, #expenseViewHelp, #onboardingChecklist");
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
    if (Math.abs(dy) > 24 && Math.abs(dy) > Math.abs(dx)) _swipe = null;
  }, { passive: true });

  document.addEventListener("touchend", (e) => {
    if (!_swipe) return;
    const touch = e.changedTouches[0];
    const dx = touch.clientX - _swipe.x;
    const dy = touch.clientY - _swipe.y;
    _swipe = null;
    if (Math.abs(dx) < 70 || Math.abs(dx) < Math.abs(dy) * 1.35) return;
    const idx = EXPENSE_VIEW_ORDER.indexOf(currentExpenseView());
    if (idx === -1) return;
    const direction = dx > 0 ? 1 : -1;
    const next = direction > 0 ? EXPENSE_VIEW_ORDER[idx + 1] : EXPENSE_VIEW_ORDER[idx - 1];
    goExpenseSwipeTarget(next, direction);
  }, { passive: true });

  document.addEventListener("touchcancel", () => { _swipe = null; }, { passive: true });

  await loadDashboard();
  if (new URLSearchParams(location.search).get("createGroup") === "1") {
    history.replaceState({}, "", "/");
    setExpenseView("groups", { updateUrl: true });
    showCreateGroupModal();
  }
})();
