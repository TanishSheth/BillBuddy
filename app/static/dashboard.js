// ShareSquad Dashboard Controller ($ / $$ come from api.js)

const EXPENSE_VIEW_KEY = "sharesquad_expense_view";
const EXPENSE_VIEW_HELP = {
  groups: "<strong>Groups:</strong> Shared multi-person expenses for trips, roommates, and projects.",
  friends: "<strong>Friends:</strong> Private two-person bills and balances. Open a friend to add or settle.",
};

function normalizeExpenseView(value) {
  return value === "friends" ? "friends" : "groups";
}

function resolveExpenseView() {
  const fromQuery = new URLSearchParams(location.search).get("view");
  if (fromQuery === "friends" || fromQuery === "groups") return fromQuery;
  try {
    return normalizeExpenseView(sessionStorage.getItem(EXPENSE_VIEW_KEY));
  } catch {
    return "groups";
  }
}

function setExpenseView(view, { updateUrl = true } = {}) {
  const next = normalizeExpenseView(view);
  try { sessionStorage.setItem(EXPENSE_VIEW_KEY, next); } catch (_) {}

  $$("#expenseViewSelector [data-expense-view]").forEach((btn) => {
    const active = btn.dataset.expenseView === next;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  $("#expenseGroupsPanel")?.classList.toggle("hidden", next !== "groups");
  $("#expenseFriendsPanel")?.classList.toggle("hidden", next !== "friends");

  const help = $("#expenseViewHelp");
  if (help) help.innerHTML = EXPENSE_VIEW_HELP[next];

  // Bottom nav: highlight Friends when that sub-tab is open, else Expenses
  $$(".bottom-nav a").forEach((link) => link.classList.remove("active"));
  const bottomKey = next === "friends" ? "friends" : "expenses";
  document.querySelector(`.bottom-nav a[data-bottom-nav="${bottomKey}"]`)?.classList.add("active");

  if (updateUrl) {
    const url = new URL(location.href);
    if (next === "friends") url.searchParams.set("view", "friends");
    else url.searchParams.delete("view");
    history.replaceState({}, "", url.pathname + url.search + url.hash);
  }
}

function openGroup(groupId) {
  ShareSquad.setStoredGroupId(groupId);
  window.location.href = "/group.html";
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
    chips.innerHTML = createGroupEmails.map((email) => `<span class="inline-flex items-center gap-1.5 rounded-full bg-brand/10 text-brand px-2.5 py-1 text-xs font-mono">${ShareSquad.escapeHtml(ShareSquad.maskEmail(email))}<button type="button" class="remove-create-group-email text-brand/70 hover:text-stamp" data-email="${ShareSquad.escapeHtml(email)}" aria-label="Remove ${ShareSquad.escapeHtml(ShareSquad.maskEmail(email))}">×</button></span>`).join("");
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

function renderQuickSplits(_splits, contacts = [], settlementsByFriend = new Map(), consolidatedByFriend = new Map()) {
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
    const personShare = Number(split.participants?.find((person) => split.is_payer ? !person.is_me : person.is_me)?.share_amount || 0);
    const billTotal = Number(split.total_amount || 0);
    const payerPaid = split.payer_paid_amount == null ? billTotal : Number(split.payer_paid_amount);
    const share = split.is_payer ? payerPaid - Number(split.payer_share_amount || 0) : (billTotal - payerPaid) - personShare;
    const monthKey = String(split.bill_date).slice(0, 7);
    const settlement = settlementsByFriend.get(entry.id)?.get(monthKey);
    const wasIncludedInSettlement = settlement?.status === "SETTLED"
      || (settlement?.settled_at && new Date(split.created_at) <= new Date(settlement.settled_at));
    if (!wasIncludedInSettlement) entry.balance += split.is_payer ? share : -share;
    entry.count += 1;
    people.set(key, entry);
  });
  // Shared source of truth with Friends: direct splits plus the same smart
  // Who-pays-whom edges used on the group Balances card (preview + settled).
  contacts.forEach((contact) => {
    const key = contact.id;
    const existing = people.get(key) || { id: contact.id, name: contact.display_name, balance: 0, count: 0, currency: "USD", splitId: null };
    const consolidated = consolidatedByFriend.get(contact.id);
    if (consolidated) {
      existing.balance = Number(consolidated.balance || 0);
      existing.currency = consolidated.currency || existing.currency;
      existing.hasConsolidatedBalance = true;
    }
    people.set(key, existing);
  });
  if (!people.size) { grid.innerHTML = ""; empty.classList.remove("hidden"); return; }
  empty.classList.add("hidden");
  grid.innerHTML = [...people.values()].map((person) => {
    const amount = ShareSquad.formatMoney(Math.abs(person.balance), person.currency);
    const isPositive = person.balance > 0;
    const isNegative = person.balance < 0;
    const status = isPositive
      ? `${ShareSquad.escapeHtml(person.name)} owes you ${amount}`
      : isNegative
        ? `You owe ${ShareSquad.escapeHtml(person.name)} ${amount}`
        : "Settled up";
    const leftBorder = isPositive ? "border-l-emerald-500" : isNegative ? "border-l-red-500" : "border-l-slate-300";
    const amtCls     = isPositive ? "text-emerald-700"    : isNegative ? "text-red-700"      : "text-slate-500";
    const badgeCls   = isPositive ? "bg-emerald-500 text-white" : isNegative ? "bg-red-500 text-white" : "bg-slate-200 text-slate-600";
    const icon       = isPositive ? "↓ they owe"          : isNegative ? "↑ you owe"         : "✓ settled";
    const href = person.id ? `/friends.html?friend=${encodeURIComponent(person.id)}` : `/quick-split.html?id=${encodeURIComponent(person.splitId)}`;
    return `<div class="bb-card friend-split-card flex flex-col justify-between gap-3 border-l-4 cursor-pointer ${leftBorder}" style="border-top-color:var(--line);border-right-color:var(--line);border-bottom-color:var(--line);" data-href="${href}" role="link" tabindex="0">
  <div>
    <div class="flex items-center justify-between gap-2">
      <p class="font-mono text-[10px] tracking-widest text-brand uppercase">Friend</p>
      <span class="text-[10px] font-bold px-2 py-0.5 rounded-full ${badgeCls}">${icon}</span>
    </div>
    <h3 class="font-mono font-bold text-base leading-tight text-ink mt-2">${ShareSquad.escapeHtml(person.name)}</h3>
    <p class="text-xl font-bold font-mono mt-2 ${amtCls}">${isPositive || isNegative ? amount : "—"}</p>
    <p class="text-xs mt-1 text-ink/60">${status}</p>
    <p class="text-[10px] font-mono mt-2 text-ink/40">${person.hasConsolidatedBalance ? "Group Who pays whom + direct bills" : `${person.count} direct bill${person.count === 1 ? "" : "s"} in history`}</p>
  </div>
  <a class="bb-btn-secondary w-full !py-1.5 text-[11px]" href="${href}"><i data-lucide="receipt-text" class="w-3.5 h-3.5"></i> Open details</a>
</div>`;
  }).join("");
  grid.querySelectorAll(".friend-split-card").forEach((card) => {
    const open = () => { window.location.href = card.dataset.href; };
    card.addEventListener("click", (event) => { if (!event.target.closest("a, button")) open(); });
    card.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } });
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
      (g) => `<div class="bb-card group-card flex flex-col justify-between gap-3 cursor-pointer" data-id="${g.id}" role="link" tabindex="0">
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
          ${g.member_count < 2 ? '<span class="status-chip status-draft mt-2">Draft · invite a member</span>' : '<span class="status-chip status-active mt-2">Active</span>'}
          <p class="text-[10px] text-ink/40 mt-1.5 font-mono">Created ${ShareSquad.formatDate(g.created_at)}</p>
        </div>
        <div class="flex gap-2 pt-3 border-t border-ink/10">
          <button type="button" class="open-group bb-btn-primary flex-1 !py-1.5 text-[11px]" data-id="${g.id}">
            <i data-lucide="folder-open" class="w-3.5 h-3.5"></i> Open Group
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
    btn.addEventListener("click", () => openGroup(Number(btn.dataset.id)));
  });

  grid.querySelectorAll(".group-card").forEach((card) => {
    const open = () => openGroup(Number(card.dataset.id));
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
    [quickSplits.length > 0, "Log your first shared bill", "/quick-split.html"],
    [groups.length > 0 || friends.length > 0, "Review balances and settlements", groups[0] ? "/group.html" : "/index.html?view=friends"],
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
    const settlementResults = await Promise.all(friends.map(async (friend) => [friend.id, await ShareSquad.api(`/friends/${friend.id}/settlements`).catch(() => [])]));
    const consolidatedResults = await Promise.all(friends.map(async (friend) => [friend.id, await ShareSquad.api(`/friends/${friend.id}/consolidated-balance?currency=USD`).catch(() => null)]));
    const settlementsByFriend = new Map(settlementResults.map(([friendId, settlements]) => [friendId, new Map(settlements.map((settlement) => [settlement.month_key, settlement]))]));
    const consolidatedByFriend = new Map(consolidatedResults.filter(([, total]) => total).map(([friendId, total]) => [friendId, total]));
    renderGroups(groups);
    renderQuickSplits(quickSplits, friends, settlementsByFriend, consolidatedByFriend);
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
    window.location.href = "/login.html";
    return;
  }

  $("#newGroupBtn")?.addEventListener("click", showCreateGroupModal);
  $("#splitGroupBtn")?.addEventListener("click", showCreateGroupModal);
  $("#quickSplitBtn")?.addEventListener("click", () => { window.location.href = "/quick-split.html"; });
  $("#settingsBtn")?.addEventListener("click", () => { window.location.href = "/settings.html"; });
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
  $("#logoutBtn")?.addEventListener("click", () => {
    ShareSquad.removeToken();
    ShareSquad.removeStoredGroupId();
    window.location.href = "/login.html";
  });

  ShareSquad.fillAllCurrencySelects();
  $$("#expenseViewSelector [data-expense-view]").forEach((btn) => {
    btn.addEventListener("click", () => setExpenseView(btn.dataset.expenseView));
  });
  setExpenseView(resolveExpenseView(), { updateUrl: true });

  // Swipe: Groups ↔ Friends ↔ Personal (Personal is a page navigate)
  const EXPENSE_VIEW_ORDER = ["groups", "friends", "personal"];
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

  function goExpenseSwipeTarget(next) {
    if (!next) return;
    if (next === "personal") {
      try { sessionStorage.setItem(EXPENSE_VIEW_KEY, "friends"); } catch (_) {}
      window.location.href = "/personal.html";
      return;
    }
    setExpenseView(next);
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
    const next = dx < 0 ? EXPENSE_VIEW_ORDER[idx + 1] : EXPENSE_VIEW_ORDER[idx - 1];
    goExpenseSwipeTarget(next);
  }, { passive: true });

  document.addEventListener("touchcancel", () => { _swipe = null; }, { passive: true });

  await loadDashboard();
  if (new URLSearchParams(location.search).get("createGroup") === "1") {
    history.replaceState({}, "", "/");
    setExpenseView("groups", { updateUrl: true });
    showCreateGroupModal();
  }
})();
