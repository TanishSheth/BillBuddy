// ShareSquad Dashboard Controller ($ / $$ come from api.js)

function openGroup(groupId) {
  ShareSquad.setStoredGroupId(groupId);
  window.location.href = "/group.html";
}

function showCreateGroupModal() {
  $("#createGroupModal").classList.remove("hidden");
  $("#createGroupName").value = "";
  $("#createGroupMsg").textContent = "";
  $("#createGroupName").focus();
}

function hideCreateGroupModal() {
  $("#createGroupModal").classList.add("hidden");
}

function openQuickSplit(id) {
  window.location.href = `/quick-split.html?id=${encodeURIComponent(id)}`;
}

function renderQuickSplits(_splits, contacts = []) {
  const grid = $("#friendSplitsGrid");
  const empty = $("#emptyFriendSplits");
  const activeFriendIds = new Set(contacts.map((person) => person.id));
  const splits = (_splits || []).filter((split) => {
    const otherId = split.is_payer ? split.participants?.find((person) => !person.is_me)?.account_id : split.payer_account_id;
    return otherId && activeFriendIds.has(otherId);
  });
  if (!splits.length) { grid.innerHTML = ""; empty.classList.remove("hidden"); return; }
  empty.classList.add("hidden");
  const people = new Map();
  splits.forEach((split) => {
    const other = split.is_payer ? split.participants?.find((person) => !person.is_me) : { id: split.payer_account_id, display_name: split.payer_name || "Friend" };
    const personName = other?.display_name || other?.email || "Friend";
    const key = other?.account_id || other?.id || `split-${split.id}`;
    const entry = people.get(key) || { id: other?.account_id || other?.id || null, name: personName, balance: 0, count: 0, currency: split.currency || "USD", splitId: split.id };
    const share = Number(split.participants?.find((person) => split.is_payer ? !person.is_me : person.is_me)?.share_amount || 0);
    entry.balance += split.is_payer ? share : -share;
    entry.count += 1;
    people.set(key, entry);
  });
  grid.innerHTML = [...people.values()].map((person) => {
    const amount = ShareSquad.formatMoney(Math.abs(person.balance), person.currency);
    const status = person.balance > 0 ? `${ShareSquad.escapeHtml(person.name)} owes you ${amount}` : person.balance < 0 ? `You owe ${ShareSquad.escapeHtml(person.name)} ${amount}` : "No one owes anything";
    const tone = person.balance > 0 ? "bg-green-600/10 border-green-700/30" : person.balance < 0 ? "bg-red-600/10 border-red-700/30" : "bg-ink/5 border-ink/15";
    const href = person.id ? `/friends.html?friend=${encodeURIComponent(person.id)}` : `/quick-split.html?id=${encodeURIComponent(person.splitId)}`;
    return `<div class="bb-card friend-split-card flex flex-col justify-between gap-3 border-l-4 border-brand/60 cursor-pointer ${tone}" data-href="${href}" role="link" tabindex="0"><div><p class="font-mono text-[10px] tracking-widest text-brand">FRIEND SPLITS</p><h3 class="font-mono font-bold text-base leading-tight text-ink mt-1">${ShareSquad.escapeHtml(person.name)}</h3><p class="text-sm font-semibold mt-3">${status}</p><p class="text-[10px] font-mono mt-2 text-ink/55">${person.count} bill${person.count === 1 ? "" : "s"} in combined history</p></div><a class="bb-btn-secondary w-full !py-1.5 text-[11px]" href="${href}"><i data-lucide="receipt-text" class="w-3.5 h-3.5"></i> Open friend details</a></div>`;
  }).join("");
  grid.querySelectorAll(".friend-split-card").forEach((card) => {
    const open = () => { window.location.href = card.dataset.href; };
    card.addEventListener("click", (event) => { if (!event.target.closest("a, button")) open(); });
    card.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } });
  });
  lucide.createIcons();
}

function renderGroups(groups) {
  const grid = $("#groupsGrid");
  const empty = $("#emptyGroups");

  if (!groups || !groups.length) {
    grid.innerHTML = "";
    empty.classList.remove("hidden");
    lucide.createIcons();
    return;
  }

  empty.classList.add("hidden");
  grid.innerHTML = groups
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
          ${g.member_count < 2 ? '<span class="inline-flex mt-2 text-[10px] font-mono font-bold text-amber-800 bg-amber-100 border border-amber-300 rounded px-1.5 py-0.5">DRAFT · INVITE A MEMBER TO START SPLITTING</span>' : ""}
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
                   <i data-lucide="log-out" class="w-3.5 h-3.5"></i>
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
      if (!confirm(`Delete "${name}"?\n\nThis permanently removes all expenses, reports, and members from this group.`)) return;
      try {
        await ShareSquad.api(`/groups/${id}`, { method: "DELETE" });
        ShareSquad.showToast(`Group "${name}" deleted`, "info");
        await loadDashboard();
      } catch (err) {
        ShareSquad.showToast(err.message, "error");
      }
    });
  });

  grid.querySelectorAll(".leave-group").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const name = btn.dataset.name;
      const id = Number(btn.dataset.id);
      if (!confirm(`Leave "${name}"?\n\nYour expenses in this group will be deleted, and you will lose access to the group.`)) return;
      try {
        await ShareSquad.api(`/groups/${id}/leave`, { method: "POST" });
        ShareSquad.showToast(`Left group "${name}"`, "info");
        await loadDashboard();
      } catch (err) {
        ShareSquad.showToast(err.message, "error");
      }
    });
  });
}

async function loadDashboard() {
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
    renderGroups(groups);
    renderQuickSplits(quickSplits, friends);
  } catch (err) {
    console.error("Failed to load dashboard:", err);
    ShareSquad.showToast(err.message || "Could not load groups", "error");
  }
}

async function submitCreateGroup(e) {
  e.preventDefault();
  const name = $("#createGroupName").value.trim();
  const currency = $("#createGroupCurrency").value || "USD";
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
      body: JSON.stringify({ name, currency }),
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
  await loadDashboard();
  if (new URLSearchParams(location.search).get("createGroup") === "1") {
    history.replaceState({}, "", "/");
    showCreateGroupModal();
  }
})();
