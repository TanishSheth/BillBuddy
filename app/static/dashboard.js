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
      (g) => `<div class="bb-card group-card flex flex-col justify-between gap-3">
        <div>
          <div class="flex items-start justify-between gap-2">
            <h3 class="font-mono font-bold text-base leading-tight text-ink">${ShareSquad.escapeHtml(g.name)}</h3>
            <span class="px-2 py-0.5 rounded text-[10px] font-bold bg-brand/10 text-brand"><span class="currency-sym">${ShareSquad.escapeHtml(ShareSquad.currencySymbol(g.currency || "USD"))}</span> ${g.currency || "USD"}</span>
          </div>
          <p class="text-xs text-ink/50 mt-1 font-mono flex items-center gap-1.5">
            <span>${g.member_count} member${g.member_count === 1 ? "" : "s"}</span>
            ${g.role === "owner" ? ' &bull; <span class="text-brand font-bold">Owner</span>' : ""}
          </p>
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

  grid.querySelectorAll(".open-group").forEach((btn) => {
    btn.addEventListener("click", () => openGroup(Number(btn.dataset.id)));
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
      if (!confirm(`Leave "${name}"?\n\nYou will lose access to this group's expenses and reports.`)) return;
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
    const groups = await ShareSquad.api("/groups");
    renderGroups(groups);
  } catch (err) {
    console.error("Failed to load dashboard:", err);
  }
}

async function submitCreateGroup(e) {
  e.preventDefault();
  const name = $("#createGroupName").value.trim();
  const currency = $("#createGroupCurrency").value;
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
  }
}

(async function init() {
  if (!ShareSquad.getToken()) {
    window.location.href = "/login.html";
    return;
  }

  $("#newGroupBtn")?.addEventListener("click", showCreateGroupModal);
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
})();
