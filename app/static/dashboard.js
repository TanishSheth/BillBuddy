const TOKEN_KEY = "billbuddy_token";
const GROUP_KEY = "billbuddy_group_id";

const $ = (sel) => document.querySelector(sel);

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  const token = localStorage.getItem(TOKEN_KEY);
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

function openGroup(groupId) {
  localStorage.setItem(GROUP_KEY, String(groupId));
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

  if (!groups.length) {
    grid.innerHTML = "";
    empty.classList.remove("hidden");
    lucide.createIcons();
    return;
  }

  empty.classList.add("hidden");
  grid.innerHTML = groups
    .map(
      (g) => `<div class="bb-card group-card flex flex-col gap-3">
        <div class="flex-1">
          <h3 class="font-mono font-bold text-base leading-tight">${escapeHtml(g.name)}</h3>
          <p class="text-xs text-ink/50 mt-1 font-mono">
            ${g.member_count} member${g.member_count === 1 ? "" : "s"}
            ${g.role === "owner" ? ' · <span class="text-brand">owner</span>' : ""}
          </p>
          <p class="text-[10px] text-ink/40 mt-1 font-mono">Created ${formatDate(g.created_at)}</p>
        </div>
        <div class="flex gap-2 pt-2 border-t border-ink/10">
          <button type="button" class="open-group bb-btn-primary flex-1 !py-2 text-[11px]" data-id="${g.id}">
            <i data-lucide="folder-open" class="w-3.5 h-3.5"></i> Open
          </button>
          ${
            g.role === "owner"
              ? `<button type="button" class="delete-group bb-btn-secondary !py-2 !px-3 text-[11px] text-stamp border-stamp/30 hover:bg-stamp/5" data-id="${g.id}" data-name="${escapeHtml(g.name)}" title="Delete group">
                   <i data-lucide="trash-2" class="w-3.5 h-3.5"></i>
                 </button>`
              : `<button type="button" class="leave-group bb-btn-secondary !py-2 !px-3 text-[11px] text-stamp border-stamp/30 hover:bg-stamp/5" data-id="${g.id}" data-name="${escapeHtml(g.name)}" title="Leave group">
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
        await api(`/groups/${id}`, { method: "DELETE" });
        await loadDashboard();
      } catch (err) {
        alert(err.message);
      }
    });
  });

  grid.querySelectorAll(".leave-group").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const name = btn.dataset.name;
      const id = Number(btn.dataset.id);
      if (!confirm(`Leave "${name}"?\n\nYou will lose access to this group's expenses and reports.`)) return;
      try {
        await api(`/groups/${id}/leave`, { method: "POST" });
        await loadDashboard();
      } catch (err) {
        alert(err.message);
      }
    });
  });
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

function formatDate(iso) {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

async function loadDashboard() {
  const me = await api("/auth/me");
  const greeting = $("#userGreeting");
  if (greeting) greeting.textContent = me.account.display_name;
  renderGroups(await api("/groups"));
}

async function submitCreateGroup(e) {
  e.preventDefault();
  const name = $("#createGroupName").value.trim();
  const msg = $("#createGroupMsg");
  if (!name) {
    msg.textContent = "Enter a group name.";
    msg.className = "text-xs font-mono balance-negative";
    return;
  }
  msg.textContent = "Creating…";
  msg.className = "text-xs font-mono text-ink/50";
  try {
    const group = await api("/groups", { method: "POST", body: JSON.stringify({ name }) });
    hideCreateGroupModal();
    openGroup(group.id);
  } catch (err) {
    msg.textContent = err.message;
    msg.className = "text-xs font-mono balance-negative";
  }
}

(async function init() {
  if (!localStorage.getItem(TOKEN_KEY)) {
    window.location.href = "/login.html";
    return;
  }

  $("#newGroupBtn")?.addEventListener("click", showCreateGroupModal);
  $("#settingsBtn")?.addEventListener("click", () => { window.location.href = "/settings.html"; });
  $("#createFirstGroupBtn")?.addEventListener("click", showCreateGroupModal);
  $("#createGroupForm")?.addEventListener("submit", submitCreateGroup);
  $("#cancelCreateGroupBtn")?.addEventListener("click", hideCreateGroupModal);
  $("#createGroupModal")?.addEventListener("click", (e) => {
    if (e.target.id === "createGroupModal") hideCreateGroupModal();
  });
  $("#logoutBtn")?.addEventListener("click", () => {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(GROUP_KEY);
    window.location.href = "/login.html";
  });

  await loadDashboard();
})();
