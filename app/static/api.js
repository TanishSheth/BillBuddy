// ShareSquad Unified API & Utility Client

const TOKEN_KEY = "sharesquad_token";
const LEGACY_TOKEN_KEY = "billbuddy_token";
const GROUP_KEY = "sharesquad_group_id";
const LEGACY_GROUP_KEY = "billbuddy_group_id";
const PRE_DEMO_TOKEN_KEY = "sharesquad_pre_demo_token";
const PRE_DEMO_GROUP_KEY = "sharesquad_pre_demo_group_id";

function getToken() {
  return localStorage.getItem(TOKEN_KEY) || localStorage.getItem(LEGACY_TOKEN_KEY);
}

function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(LEGACY_TOKEN_KEY, token);
}

function clearPreDemoSession() {
  localStorage.removeItem(PRE_DEMO_TOKEN_KEY);
  localStorage.removeItem(PRE_DEMO_GROUP_KEY);
}

function stashRealSessionForDemo() {
  // Already in a sandbox (e.g. Reset demo) — keep the original stash intact.
  if (localStorage.getItem("sharesquad_demo_sandbox") === "1") return;
  const token = getToken();
  if (!token) {
    clearPreDemoSession();
    return;
  }
  localStorage.setItem(PRE_DEMO_TOKEN_KEY, token);
  const groupId = getStoredGroupId();
  if (groupId) localStorage.setItem(PRE_DEMO_GROUP_KEY, groupId);
  else localStorage.removeItem(PRE_DEMO_GROUP_KEY);
}

function beginDemoSession(accessToken, groupId) {
  stashRealSessionForDemo();
  setToken(accessToken);
  localStorage.setItem("sharesquad_demo_sandbox", "1");
  if (groupId != null && groupId !== "") setStoredGroupId(groupId);
}

/** Leave demo; restore the pre-demo account when one was stashed. Returns true if restored. */
function exitDemoSession() {
  const savedToken = localStorage.getItem(PRE_DEMO_TOKEN_KEY);
  const savedGroup = localStorage.getItem(PRE_DEMO_GROUP_KEY);
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(LEGACY_TOKEN_KEY);
  localStorage.removeItem("sharesquad_demo_sandbox");
  clearPreDemoSession();
  if (savedToken) {
    setToken(savedToken);
    if (savedGroup) setStoredGroupId(savedGroup);
    else removeStoredGroupId();
    return true;
  }
  removeStoredGroupId();
  return false;
}

function removeToken() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(LEGACY_TOKEN_KEY);
  localStorage.removeItem("sharesquad_demo_sandbox");
  clearPreDemoSession();
}

function renderDemoBanner() {
  if (localStorage.getItem("sharesquad_demo_sandbox") !== "1" || document.getElementById("demoModeBanner")) return;
  const header = document.querySelector(".page-header");
  if (!header) return;
  const banner = document.createElement("aside");
  banner.id = "demoModeBanner";
  banner.className = "tone-banner tone-banner-success rounded-none border-x-0";
  banner.setAttribute("role", "status");
  banner.innerHTML = '<div class="max-w-5xl mx-auto px-4 sm:px-5 py-2 text-center text-xs sm:text-sm w-full"><strong>Demo mode</strong> · Same current ShareSquad as a regular account, filled with sample group data. Sandboxes expire after two hours. <a class="tone-banner-action font-semibold underline underline-offset-2" href="/login.html?mode=signup">Sign up free</a> to keep your own data. <button type="button" id="resetDemoSandbox" class="ml-1 tone-banner-action font-semibold underline underline-offset-2">Reset demo</button></div>';
  header.insertAdjacentElement("afterend", banner);
  banner.querySelector("#resetDemoSandbox")?.addEventListener("click", async () => {
    if (!await confirmDialog({
      title: "Reset this demo?",
      message: "Your demo changes will be removed and a fresh sample workspace will open.",
      confirmLabel: "Reset demo",
    })) return;
    try {
      const result = await api("/demo/sandbox/reset", { method: "POST" });
      setToken(result.access_token);
      localStorage.setItem("sharesquad_demo_sandbox", "1");
      setStoredGroupId(result.group_id);
      // Return to the app dashboard so Reset demo matches Try the full demo.
      window.location.href = "/app?view=groups&demo=1";
    } catch (error) {
      showToast(error.message || "Could not reset the demo", "error");
    }
  });
}

function getStoredGroupId() {
  return localStorage.getItem(GROUP_KEY) || localStorage.getItem(LEGACY_GROUP_KEY);
}

function setStoredGroupId(id) {
  localStorage.setItem(GROUP_KEY, String(id));
  localStorage.setItem(LEGACY_GROUP_KEY, String(id));
}

function removeStoredGroupId() {
  localStorage.removeItem(GROUP_KEY);
  localStorage.removeItem(LEGACY_GROUP_KEY);
}

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
window.$ = $;
window.$$ = $$;

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

function initials(name) {
  return String(name || "?").trim().split(/\s+/).slice(0, 2).map((part) => part[0] || "").join("").toUpperCase() || "?";
}

function maskEmail(email) {
  const value = String(email || "").trim();
  if (!value) return "";
  const at = value.lastIndexOf("@");
  if (at <= 0 || at === value.length - 1) {
    if (value.length <= 8) return value;
    return `${value.slice(0, 4)}••••${value.slice(-4)}`;
  }
  const local = value.slice(0, at);
  const domain = value.slice(at + 1);
  if (local.length <= 8) return `${local}@${domain}`;
  return `${local.slice(0, 4)}••••${local.slice(-4)}@${domain}`;
}

function showToast(msg, type = "info", duration = 3500, undoCallback = null) {
  let container = $("#toastContainer");
  if (!container) {
    container = document.createElement("div");
    container.id = "toastContainer";
    container.className = "fixed right-5 z-50 flex flex-col gap-2 max-w-sm w-full px-4 pointer-events-none";
    container.style.bottom = "calc(5.5rem + var(--safe-bottom, 0px))";
    document.body.appendChild(container);
  }

  const toast = document.createElement("div");
  const kind = type === "error" ? "toast-error" : type === "success" ? "toast-success" : "toast-info";
  toast.className = `toast-msg ${kind} p-3 rounded-lg shadow-xl text-xs font-mono transition-all transform duration-300 opacity-0 translate-y-2 pointer-events-auto flex items-center justify-between gap-2`;
  const undoHtml = undoCallback ? `<button type="button" class="underline font-bold whitespace-nowrap opacity-90 hover:opacity-100 undo-toast-btn">Undo</button>` : "";
  toast.innerHTML = `<span>${escapeHtml(String(msg).slice(0, 180))}</span><div class="flex items-center gap-2 shrink-0">${undoHtml}<button type="button" class="opacity-70 hover:opacity-100 font-bold toast-close-btn" aria-label="Dismiss">&times;</button></div>`;

  let closed = false;
  let remaining = duration;
  let startedAt = Date.now();
  let timer = null;

  const dismiss = () => {
    if (closed) return;
    closed = true;
    clearTimeout(timer);
    toast.classList.add("opacity-0", "translate-y-2");
    setTimeout(() => toast.remove(), 300);
  };

  const schedule = () => {
    clearTimeout(timer);
    startedAt = Date.now();
    timer = setTimeout(dismiss, Math.max(remaining, 0));
  };

  toast.querySelector(".toast-close-btn").onclick = dismiss;
  if (undoCallback) {
    toast.querySelector(".undo-toast-btn").onclick = () => {
      Promise.resolve(undoCallback()).catch(() => {});
      dismiss();
    };
    toast.addEventListener("mouseenter", () => {
      remaining -= Date.now() - startedAt;
      clearTimeout(timer);
    });
    toast.addEventListener("mouseleave", schedule);
  }

  container.appendChild(toast);
  requestAnimationFrame(() => { toast.classList.remove("opacity-0", "translate-y-2"); });
  schedule();
  return { dismiss, el: toast };
}

const pendingUndoCommits = new Set();

/**
 * Optimistic delete with Undo. UI hides immediately; the API delete runs after
 * the toast window unless the user taps Undo (or the page is unloading, in
 * which case pending deletes are flushed).
 */
function runWithUndo({
  message,
  duration = 7000,
  onHide,
  onUndo,
  onCommit,
} = {}) {
  let settled = false;
  let timer = null;
  let remaining = duration;
  let startedAt = Date.now();

  const settleCommit = async () => {
    if (settled) return;
    settled = true;
    clearTimeout(timer);
    pendingUndoCommits.delete(handle);
    try {
      await onCommit?.();
    } catch (err) {
      try { await onUndo?.(); } catch (_) {}
      showToast(err?.message || "Could not complete delete", "error");
    }
  };

  const settleUndo = async () => {
    if (settled) return;
    settled = true;
    clearTimeout(timer);
    pendingUndoCommits.delete(handle);
    try { await onUndo?.(); } catch (_) {}
  };

  const handle = { commit: settleCommit };
  pendingUndoCommits.add(handle);

  try { onHide?.(); } catch (_) {}
  try { haptic(30); } catch (_) {}

  const toast = showToast(message, "info", duration, settleUndo);
  timer = setTimeout(settleCommit, duration);

  // Keep commit timer in sync with toast hover pause.
  toast?.el?.addEventListener("mouseenter", () => {
    remaining -= Date.now() - startedAt;
    clearTimeout(timer);
  });
  toast?.el?.addEventListener("mouseleave", () => {
    startedAt = Date.now();
    if (!settled) timer = setTimeout(settleCommit, Math.max(remaining, 0));
  });

  return handle;
}

function flushPendingUndos() {
  [...pendingUndoCommits].forEach((entry) => {
    try { entry.commit(); } catch (_) {}
  });
}

try {
  window.addEventListener("pagehide", flushPendingUndos);
  window.addEventListener("beforeunload", flushPendingUndos);
} catch (_) {}

// A shared, accessible replacement for browser confirm() dialogs. Keeping it
// here means every destructive workflow speaks and behaves the same way.
function confirmDialog({ title = "Are you sure?", message = "This action cannot be undone.", confirmLabel = "Confirm", danger = true } = {}) {
  return new Promise((resolve) => {
    const previousFocus = document.activeElement;
    const overlay = document.createElement("div");
    overlay.className = "app-dialog-overlay";
    overlay.innerHTML = `
      <section class="app-dialog" role="dialog" aria-modal="true" aria-labelledby="appDialogTitle" aria-describedby="appDialogMessage">
        <div class="app-dialog-icon ${danger ? "is-danger" : ""}" aria-hidden="true">${danger ? "!" : "?"}</div>
        <h2 id="appDialogTitle">${escapeHtml(title)}</h2>
        <p id="appDialogMessage">${escapeHtml(message)}</p>
        <div class="app-dialog-actions">
          <button type="button" class="bb-btn-secondary" data-dialog-cancel>Cancel</button>
          <button type="button" class="${danger ? "bb-btn-danger" : "bb-btn-primary"}" data-dialog-confirm>${escapeHtml(confirmLabel)}</button>
        </div>
      </section>`;
    const finish = (accepted) => {
      document.removeEventListener("keydown", onKeyDown);
      overlay.remove();
      previousFocus?.focus?.();
      resolve(accepted);
    };
    const onKeyDown = (event) => { if (event.key === "Escape") finish(false); };
    overlay.addEventListener("click", (event) => { if (event.target === overlay) finish(false); });
    overlay.querySelector("[data-dialog-cancel]").addEventListener("click", () => finish(false));
    overlay.querySelector("[data-dialog-confirm]").addEventListener("click", () => finish(true));
    document.addEventListener("keydown", onKeyDown);
    document.body.appendChild(overlay);
    overlay.querySelector("[data-dialog-cancel]").focus();
  });
}

function renderSkeleton(root, variant = "cards") {
  if (!root) return;
  const blocks = variant === "rows" ? 4 : 3;
  root.setAttribute("aria-busy", "true");
  root.innerHTML = Array.from({ length: blocks }, () => `<div class="ui-skeleton-card" aria-hidden="true"><span></span><span></span><span></span></div>`).join("");
}

function initGlobalNotifications() {
  if (!getToken() || $("#notificationsBtn")) return;
  const actions = $(".header-actions");
  if (!actions) return;
  const button = document.createElement("button");
  button.type = "button";
  button.id = "notificationsBtn";
  button.className = "bb-btn-secondary !py-1.5 !px-2.5 text-[11px] relative";
  button.title = "Notifications";
  button.setAttribute("aria-label", "Notifications");
  button.innerHTML = `<i data-lucide="bell" class="w-3.5 h-3.5"></i><span id="notificationCount" class="hidden notification-count">0</span>`;
  actions.prepend(button);
  window.lucide?.createIcons();
  const modal = document.createElement("div");
  modal.id = "notificationsModal";
  modal.className = "modal-overlay hidden";
  modal.innerHTML = `<div class="bb-card modal-card"><div class="flex items-center justify-between gap-3 mb-4 border-b border-ink/10 pb-3"><h2 class="font-mono uppercase text-xs tracking-widest text-brand font-bold">Notifications</h2><div class="flex items-center gap-2"><button type="button" id="markAllNotificationsReadBtn" class="bb-btn-secondary !py-1 !px-2.5 text-[10px] hidden">Mark all read</button><button type="button" class="text-ink/40 hover:text-ink font-bold" aria-label="Close notifications">×</button></div></div><div id="notificationsList" class="space-y-2 max-h-[60vh] overflow-y-auto"></div></div>`;
  document.body.appendChild(modal);
  const markAllBtn = $("#markAllNotificationsReadBtn");
  const refreshCount = async () => {
    try {
      const unread = await api("/notifications?unread_only=true");
      const badge = $("#notificationCount");
      if (badge) {
        badge.textContent = unread.length > 9 ? "9+" : unread.length;
        badge.classList.toggle("hidden", !unread.length);
      }
      markAllBtn?.classList.toggle("hidden", !unread.length);
      return unread.length;
    } catch (_) {
      return 0;
    }
  };
  const load = async () => {
    const list = $("#notificationsList");
    list.innerHTML = '<div class="ui-skeleton-card"><span></span><span></span></div>';
    try {
      const [items, unreadCount] = await Promise.all([api("/notifications"), refreshCount()]);
      markAllBtn?.classList.toggle("hidden", unreadCount === 0);
      list.innerHTML = items.length ? items.map((item) => `<button class="notification-item ${item.read_at ? "" : "unread"}" data-id="${item.id}" data-link="${escapeHtml(item.link || "")}" data-message="${escapeHtml(item.message)}"><span>${escapeHtml(item.message)}</span><small>${formatDate(item.created_at)}</small>${item.link ? '<span class="notification-action">View details →</span>' : ""}</button>`).join("") : '<p class="text-sm text-ink/55 py-5 text-center">You are all caught up.</p>';
      list.querySelectorAll(".notification-item").forEach((item) => item.addEventListener("click", async () => {
        await api(`/notifications/${item.dataset.id}/read`, { method: "POST" });
        if (item.dataset.link) openNotificationLink(item.dataset.link, item.dataset.message);
        else {
          item.classList.remove("unread");
          await refreshCount();
        }
      }));
    } catch (error) {
      list.innerHTML = `<p class="text-sm balance-negative">${escapeHtml(error.message)}</p>`;
      markAllBtn?.classList.add("hidden");
    }
  };
  markAllBtn?.addEventListener("click", async () => {
    try {
      markAllBtn.disabled = true;
      await api("/notifications/read-all", { method: "POST" });
      $$("#notificationsList .notification-item.unread").forEach((item) => item.classList.remove("unread"));
      markAllBtn.classList.add("hidden");
      await refreshCount();
      showToast("All notifications marked as read", "success");
    } catch (error) {
      showToast(error.message || "Could not mark notifications as read", "error");
    } finally {
      markAllBtn.disabled = false;
    }
  });
  button.addEventListener("click", () => { modal.classList.remove("hidden"); load(); });
  modal.querySelector('button[aria-label="Close notifications"]').addEventListener("click", () => modal.classList.add("hidden"));
  modal.addEventListener("click", (event) => { if (event.target === modal) modal.classList.add("hidden"); });
  refreshCount();
}

function initMobileQuickAction() {
  if (!getToken() || $(".mobile-quick-action")) return;
  const path = window.location.pathname;
  if (/\/settings(?:\.html)?$/.test(path) || /\/login(?:\.html)?$/.test(path)) return;
  const onDashboard = /(^|\/)(index\.html|app)?$/.test(path) || path === "/app";
  const onFriends = /\/friends(?:\.html)?$/.test(path);
  if (onFriends) return;
  const target = onDashboard ? null : ["#addExpenseBtn", "#openAddBill", "#quickSplitBtn"].map((selector) => $(selector)).find(Boolean);
  if (!onDashboard && !target) return;
  if (onDashboard) {
    const actions = document.createElement("div");
    actions.className = "mobile-quick-actions";
    actions.innerHTML = '<button type="button" class="mobile-quick-action quick-split">＋ <span>Quick split</span></button><button type="button" class="mobile-quick-action create-group">＋ <span>Create group</span></button>';
    actions.querySelector(".quick-split").addEventListener("click", () => { window.location.href = "/quick-split.html"; });
    actions.querySelector(".create-group").addEventListener("click", () => $("#newGroupBtn")?.click());
    document.body.appendChild(actions);
    return;
  }
  const button = document.createElement("button");
  button.type = "button";
  button.className = "mobile-quick-action";
  button.innerHTML = "＋ <span>Quick add</span>";
  button.addEventListener("click", () => target.click());
  document.body.appendChild(button);
}

function openNotificationLink(link, message = "") {
  if (!link) return false;
  const target = new URL(link, window.location.origin);
  if ((target.pathname.endsWith("/group.html") || target.pathname === "/group") && !target.searchParams.get("tab")) {
    const text = String(message).toLowerCase();
    if (/payment|paid|received|confirmed/.test(text)) { target.searchParams.set("tab", "balances"); target.searchParams.set("focus", "paymentHistory"); }
    else if (/member|invite/.test(text)) { target.searchParams.set("tab", "members"); target.searchParams.set("focus", "groupMembers"); }
    else if (/settled|reopened|unlocked/.test(text)) { target.searchParams.set("tab", "balances"); }
    else if (/added|updated|deleted|comment/.test(text)) { target.searchParams.set("tab", "expenses"); target.searchParams.set("focus", "expensesBody"); }
  }
  const groupId = target.searchParams.get("group");
  if (groupId) setStoredGroupId(groupId);
  window.location.href = `${target.pathname}${target.search}${target.hash}`;
  return true;
}

function initSearchPalette() {
  if (!getToken() || $("#globalSearchPalette")) return;
  const overlay = document.createElement("div");
  overlay.id = "globalSearchPalette";
  overlay.className = "app-dialog-overlay hidden";
  overlay.innerHTML = `<section class="app-dialog search-palette" role="dialog" aria-modal="true" aria-label="Search ShareSquad"><div class="flex items-center justify-between gap-3"><h2>Search ShareSquad</h2><kbd>Esc</kbd></div><input class="bb-input mt-4" placeholder="Search groups and friends…" aria-label="Search"><div class="search-results mt-3"></div></section>`;
  document.body.appendChild(overlay);
  const input = overlay.querySelector("input"); const results = overlay.querySelector(".search-results");
  let all = [];
  const close = () => overlay.classList.add("hidden");
  const open = async () => {
    overlay.classList.remove("hidden"); input.value = ""; results.innerHTML = '<p class="text-sm text-ink/55">Start typing to search groups and friends.</p>'; input.focus();
    if (!all.length) { const [groups, friends] = await Promise.all([api("/groups").catch(() => []), api("/friends").catch(() => [])]); all = [...groups.map((x) => ({ label: x.name, type: "Group", href: "/group.html", groupId: x.id })), ...friends.map((x) => ({ label: x.display_name || x.name, type: "Friend", href: `/friends.html?friend=${x.id}` }))]; }
  };
  input.addEventListener("input", () => { const q = input.value.trim().toLowerCase(); const matches = all.filter((x) => x.label.toLowerCase().includes(q)).slice(0, 7); results.innerHTML = q && matches.length ? matches.map((x, i) => `<button class="search-result" data-i="${i}"><small>${x.type}</small>${escapeHtml(x.label)}</button>`).join("") : q ? '<p class="text-sm text-ink/55">No matches found.</p>' : '<p class="text-sm text-ink/55">Start typing to search groups and friends.</p>'; results.querySelectorAll("button").forEach((button) => button.addEventListener("click", () => { const item = matches[Number(button.dataset.i)]; if (item.groupId) setStoredGroupId(item.groupId); location.href = item.href; })); });
  document.addEventListener("keydown", (event) => { if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); open().catch((err) => showToast(err.message, "error")); } else if (event.key === "Escape" && !overlay.classList.contains("hidden")) close(); });
  overlay.addEventListener("click", (event) => { if (event.target === overlay) close(); });
}

function bindLogoutButton() {
  // Sign out belongs only on Expenses (Friends/Groups) and Personal — not group,
  // friends detail, settings, or other focused pages.
  const path = (window.location.pathname || "/").replace(/\/+$/, "") || "/";
  const keepLogout = new Set(["/", "/app", "/index", "/index.html", "/personal", "/personal.html"]).has(path);
  if (!keepLogout) {
    $("#logoutBtn")?.remove();
    return;
  }
  const button = $("#logoutBtn");
  if (!button) return;
  const inDemo = localStorage.getItem("sharesquad_demo_sandbox") === "1";
  const label = inDemo ? "Exit demo" : "Sign out";
  // Always restore the same icon + label (covers stale cached HTML that was icon-only).
  button.className = "bb-btn-secondary !py-1.5 !px-2.5 text-[11px] hover:text-stamp hover:border-stamp/40";
  button.type = "button";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.innerHTML = `<i data-lucide="log-out" class="w-3.5 h-3.5" aria-hidden="true"></i><span>${label}</span>`;
  if (window.lucide?.createIcons) window.lucide.createIcons();
  if (button.dataset.boundLogout === "1") return;
  button.dataset.boundLogout = "1";
  button.addEventListener("click", () => {
    const leavingDemo = localStorage.getItem("sharesquad_demo_sandbox") === "1";
    if (leavingDemo) {
      exitDemoSession();
      // Landing shows Open app when the real session was restored.
      window.location.replace("/");
      return;
    }
    removeToken();
    removeStoredGroupId();
    allowLoginOnce();
    // Replace so browser Back cannot return to a signed-in screen after logout.
    window.location.replace("/login.html");
  });
}

document.addEventListener("DOMContentLoaded", () => setTimeout(() => {
  initGlobalNotifications();
  initMobileQuickAction();
  initSearchPalette();
  initDatePickers();
  bindLogoutButton();
}, 0));

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opts.body && !(opts.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }

  try {
    const res = await fetch(`/api${path}`, { ...opts, headers });
    
    if (res.status === 401) {
      // If we are already on login or reset password page, do not infinite redirect
      if (!isLoginPath() && !window.location.pathname.includes("reset-password.html")) {
        removeToken();
        showToast("Session expired. Please sign in again.", "error");
        setTimeout(() => {
          goToLogin({ replace: true, keepNext: true });
        }, 800);
      }
      throw new Error("Session expired. Please sign in.");
    }

    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      const detail = err.detail;
      const msg = typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((d) => d.msg).join(", ") : "Request failed";
      throw new Error(msg);
    }

    // Sliding 30-day idle session: server mints a fresh JWT on each authenticated call.
    const refreshed = res.headers.get("X-Access-Token");
    if (refreshed) setToken(refreshed);

    const ct = res.headers.get("content-type") || "";
    return ct.includes("application/json") ? res.json() : res;
  } catch (err) {
    throw err;
  }
}

// Currency formatters. Symbols render in .currency-sym (Inter tabular nums).
const CURRENCIES = [
  { code: "USD", symbol: "$", name: "US Dollar" },
  { code: "INR", symbol: "₹", name: "Indian Rupee" },
  { code: "EUR", symbol: "€", name: "Euro" },
  { code: "GBP", symbol: "£", name: "British Pound" },
  { code: "CAD", symbol: "CA$", name: "Canadian Dollar" },
  { code: "AUD", symbol: "A$", name: "Australian Dollar" },
  { code: "JPY", symbol: "¥", name: "Japanese Yen" },
  { code: "AED", symbol: "د.إ", name: "UAE Dirham" },
  { code: "SGD", symbol: "S$", name: "Singapore Dollar" },
  { code: "CHF", symbol: "Fr", name: "Swiss Franc" },
  { code: "CNY", symbol: "¥", name: "Chinese Yuan" },
  { code: "NZD", symbol: "NZ$", name: "New Zealand Dollar" },
  { code: "THB", symbol: "฿", name: "Thai Baht" },
  { code: "MXN", symbol: "MX$", name: "Mexican Peso" },
  { code: "BRL", symbol: "R$", name: "Brazilian Real" },
];

const CURRENCY_SYMBOLS = Object.fromEntries(CURRENCIES.map((c) => [c.code, c.symbol]));

function currencyMeta(code) {
  const upper = (code || "USD").toUpperCase();
  return CURRENCIES.find((c) => c.code === upper) || { code: upper, symbol: `${upper} `, name: upper };
}

function currencySymbol(code) {
  return currencyMeta(code).symbol;
}

function currencyOptionLabel(c) {
  return `${c.code}  ${c.symbol}  ·  ${c.name}`;
}

function pickScannedCurrency(_extracted, fallback) {
  return (fallback || "USD").toUpperCase();
}

function fillCurrencySelect(selectEl, selected) {
  if (!selectEl) return;
  const wanted = (selected || selectEl.value || "USD").toUpperCase();
  selectEl.innerHTML = CURRENCIES.map(
    (c) => `<option value="${c.code}">${currencyOptionLabel(c)}</option>`
  ).join("");
  selectEl.value = CURRENCIES.some((c) => c.code === wanted) ? wanted : "USD";
}

function fillAllCurrencySelects(root = document) {
  root.querySelectorAll("select[data-currency-select]").forEach((el) => {
    fillCurrencySelect(el, el.getAttribute("data-selected") || el.value);
  });
}

function formatMoney(amount, currency = "USD") {
  const num = Number(amount || 0);
  const meta = currencyMeta(currency);
  const formatted = num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  // Isolate LTR so RTL glyphs (e.g. AED د.إ) cannot reorder amount vs following %.
  return `<span class="money-amount" dir="ltr"><span class="currency-sym">${escapeHtml(meta.symbol)}</span>${formatted}</span>`;
}

function formatMoneyText(amount, currency = "USD") {
  const num = Number(amount || 0);
  const meta = currencyMeta(currency);
  const formatted = num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${meta.symbol}${formatted}`;
}

function formatDate(iso) {
  if (!iso) return "";
  // Date-only values are calendar dates. Parsing them as UTC shifts them
  // back a day for users west of UTC.
  const d = new Date(/^\d{4}-\d{2}-\d{2}$/.test(iso) ? `${iso}T00:00:00` : iso);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

async function downloadBlob(path, filename) {
  const res = await api(path);
  const blob = typeof res.blob === "function" ? await res.blob() : res;
  if (!(blob instanceof Blob) || blob.size === 0) {
    throw new Error("The PDF file was empty.");
  }
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename || "download.pdf";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function isReceiptImageFile(file) {
  if (!file) return false;
  const type = (file.type || "").toLowerCase();
  if (type.startsWith("image/")) return true;
  return /\.(jpe?g|png|webp|gif|bmp|heic|heif|tif{1,2})$/i.test(file.name || "");
}

function _isPhoneClient() {
  if (typeof matchMedia === "function" && matchMedia("(pointer: coarse)").matches) return true;
  return /Android|iPhone|iPad|iPod/i.test(navigator.userAgent || "");
}

/** Read width/height from JPEG/PNG headers without decoding pixels (cheap). */
async function _sniffImageSize(file) {
  try {
    const head = new Uint8Array(await file.slice(0, 65536).arrayBuffer());
    // PNG: IHDR after 8-byte signature
    if (head.length >= 24 && head[0] === 0x89 && head[1] === 0x50 && head[2] === 0x4e && head[3] === 0x47) {
      const w = (head[16] << 24) | (head[17] << 16) | (head[18] << 8) | head[19];
      const h = (head[20] << 24) | (head[21] << 16) | (head[22] << 8) | head[23];
      if (w > 0 && h > 0) return { w, h };
    }
    // JPEG: scan for SOF0/SOF2
    if (head.length > 4 && head[0] === 0xff && head[1] === 0xd8) {
      let i = 2;
      while (i + 9 < head.length) {
        if (head[i] !== 0xff) { i += 1; continue; }
        const marker = head[i + 1];
        if (marker === 0xd9 || marker === 0xda) break;
        const size = (head[i + 2] << 8) | head[i + 3];
        if (size < 2) break;
        // SOF0..SOF3, SOF5..SOF7, SOF9..SOF11, SOF13..SOF15
        if (
          (marker >= 0xc0 && marker <= 0xc3)
          || (marker >= 0xc5 && marker <= 0xc7)
          || (marker >= 0xc9 && marker <= 0xcb)
          || (marker >= 0xcd && marker <= 0xcf)
        ) {
          const h = (head[i + 5] << 8) | head[i + 6];
          const w = (head[i + 7] << 8) | head[i + 8];
          if (w > 0 && h > 0) return { w, h };
          break;
        }
        i += 2 + size;
      }
    }
  } catch (_) {
    /* ignore */
  }
  return null;
}

async function _decodeReceiptBitmap(file, maxEdge) {
  // Prefer decode-time resize — avoids holding a full 12MP camera frame in RAM.
  // Gallery picks are often already downsized by the OS; camera captures are not.
  const dims = await _sniffImageSize(file);
  const resizeOpts = { resizeQuality: "medium" };
  try {
    resizeOpts.imageOrientation = "from-image";
  } catch (_) {
    /* older browsers */
  }
  if (dims) {
    if (dims.w >= dims.h) resizeOpts.resizeWidth = maxEdge;
    else resizeOpts.resizeHeight = maxEdge;
  } else if (_isPhoneClient()) {
    // Phone camera receipts are usually portrait — cap height when size unknown.
    resizeOpts.resizeHeight = maxEdge;
  } else {
    resizeOpts.resizeWidth = maxEdge;
  }

  if (typeof createImageBitmap === "function") {
    try {
      return await createImageBitmap(file, resizeOpts);
    } catch (_) {
      /* try the other edge, still resized — never full-res on phones */
    }
    const alt = { ...resizeOpts };
    if (alt.resizeWidth) {
      delete alt.resizeWidth;
      alt.resizeHeight = maxEdge;
    } else {
      delete alt.resizeHeight;
      alt.resizeWidth = maxEdge;
    }
    try {
      return await createImageBitmap(file, alt);
    } catch (_) {
      /* fall through */
    }
    // Desktop-only last resort: full decode. Phones must not do this.
    if (!_isPhoneClient()) {
      try {
        return await createImageBitmap(file);
      } catch (_) {
        /* fall through to <img> */
      }
    }
  }

  if (_isPhoneClient()) {
    throw new Error("MEMORY");
  }

  const url = URL.createObjectURL(file);
  try {
    const img = await new Promise((resolve, reject) => {
      const el = new Image();
      el.onload = () => resolve(el);
      el.onerror = () => reject(new Error("Could not read that photo."));
      el.src = url;
    });
    return img;
  } finally {
    URL.revokeObjectURL(url);
  }
}

async function _compressReceiptAttempt(file, maxEdge, quality) {
  const bitmap = await _decodeReceiptBitmap(file, maxEdge);
  const srcW = bitmap.width || bitmap.naturalWidth || 0;
  const srcH = bitmap.height || bitmap.naturalHeight || 0;
  if (!srcW || !srcH) {
    if (bitmap.close) bitmap.close();
    throw new Error("Could not read that photo.");
  }
  const scale = Math.min(1, maxEdge / Math.max(srcW, srcH));
  const outW = Math.max(1, Math.round(srcW * scale));
  const outH = Math.max(1, Math.round(srcH * scale));
  const canvas = document.createElement("canvas");
  canvas.width = outW;
  canvas.height = outH;
  try {
    const ctx = canvas.getContext("2d", { alpha: false });
    if (!ctx) throw new Error("MEMORY");
    ctx.drawImage(bitmap, 0, 0, outW, outH);
  } finally {
    if (bitmap.close) bitmap.close();
  }
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", quality));
  canvas.width = 0;
  canvas.height = 0;
  if (!blob) throw new Error("MEMORY");
  const base = (file.name || "receipt").replace(/\.[^.]+$/, "") || "receipt";
  const out = new File([blob], `${base}.jpg`, { type: "image/jpeg", lastModified: Date.now() });
  out._sharesquadPrepared = true;
  return out;
}

/**
 * Downscale/compress receipt images before preview + upload.
 * @param {File} file
 * @param {{ fromCamera?: boolean }} [opts] - camera captures need harder limits:
 *   the OS photo picker often already shrinks gallery images, but the camera
 *   returns a full-sensor frame while the WebView is still memory-tight.
 */
async function prepareReceiptFile(file, opts = {}) {
  if (!file) return file;
  if (file._sharesquadPrepared) return file;
  if (!isReceiptImageFile(file)) return file;

  const fromCamera = Boolean(opts.fromCamera);
  const phone = _isPhoneClient();

  // Camera: memory-safe ladder. Gallery (Personal + Group): one shared profile so
  // the same photo is not re-encoded differently per tab before upload.
  if (fromCamera) {
    await new Promise((r) => setTimeout(r, 120));
  }

  const attempts = fromCamera
    ? [
        { maxEdge: 960, quality: 0.68 },
        { maxEdge: 720, quality: 0.62 },
        { maxEdge: 560, quality: 0.55 },
      ]
    : [
        // Match server normalize intent (1600 / ~0.85). Phones use 1280 to avoid OOM.
        { maxEdge: phone ? 1280 : 1600, quality: phone ? 0.75 : 0.85 },
        { maxEdge: phone ? 960 : 1200, quality: 0.7 },
      ];

  // Gallery: always run the shared profile (do not keep the original bytes),
  // so Personal and Group upload the same encoding for the same pick.
  // Already-prepared files short-circuit above via _sharesquadPrepared.
  if (!fromCamera && (file.type || "").toLowerCase().includes("jpeg") && file.size <= 350 * 1024) {
    const dims = await _sniffImageSize(file);
    const edgeCap = phone ? 1280 : 1600;
    if (dims && Math.max(dims.w, dims.h) <= edgeCap) {
      file._sharesquadPrepared = true;
      return file;
    }
  }

  let lastErr;
  for (const attempt of attempts) {
    try {
      return await _compressReceiptAttempt(file, attempt.maxEdge, attempt.quality);
    } catch (err) {
      lastErr = err;
    }
  }

  const msg = String(lastErr?.message || lastErr || "");
  if (/memory|allocat|MEMORY/i.test(msg)) {
    throw new Error(
      fromCamera
        ? "Camera photo was too large for this phone. Try again, hold a bit farther away, or pick the photo from your gallery."
        : "That photo is too large for this device. Try a smaller picture, or pick it from your gallery."
    );
  }
  throw new Error(lastErr?.message || "Could not read that photo.");
}

async function waitForScanJob(jobId, { timeoutMs = 120000, intervalMs = 700 } = {}) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const job = await api(`/scans/${jobId}`);
    if (job.status === "succeeded" && job.extraction) {
      const extraction = job.extraction;
      const weak =
        !extraction.store_name ||
        /^unknown/i.test(extraction.store_name) ||
        !(Number(extraction.total_amount) > 0);
      return {
        upload_id: job.upload_id,
        image_url: job.image_url,
        extraction,
        source: job.source || "ocr",
        message: weak
          ? (job.message || "Could not read the receipt clearly. Enter merchant and total manually, then save.")
          : (job.message || null),
      };
    }
    if (job.status === "failed") {
      throw new Error(job.error || job.message || "Receipt scan failed. Enter the details manually.");
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  throw new Error("Receipt scan timed out. Enter the details manually, or try again.");
}

async function uploadReceipt(path, file, prepareOpts = {}) {
  // Skip a second compress when the caller already prepared (camera path).
  const prepared = file?._sharesquadPrepared ? file : await prepareReceiptFile(file, prepareOpts);
  let lastErr;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const formData = new FormData();
    formData.append("file", prepared);
    try {
      const started = await api(path, { method: "POST", body: formData });
      // Async OCR returns a scan job; sync fallback already includes extraction.
      if (started && started.extraction) return started;
      if (started && started.job_id) return waitForScanJob(started.job_id);
      return started;
    } catch (err) {
      lastErr = err;
      await new Promise((resolve) => setTimeout(resolve, 400 * (attempt + 1)));
    }
  }
  throw lastErr;
}

async function setAuthImage(imgEl, path) {
  if (!imgEl || !path) return;
  try {
    const res = await api(path);
    const blob = typeof res.blob === "function" ? await res.blob() : res;
    if (!(blob instanceof Blob)) return;
    if (imgEl.dataset.blobUrl) URL.revokeObjectURL(imgEl.dataset.blobUrl);
    const url = URL.createObjectURL(blob);
    imgEl.dataset.blobUrl = url;
    imgEl.src = url;
    imgEl.draggable = false;
    imgEl.classList.add("protected-image");
    imgEl.classList.remove("hidden");
  } catch (err) {
    imgEl.removeAttribute("src");
    imgEl.classList.add("hidden");
  }
}

// This blocks ordinary browser save, drag, and mobile callout affordances for
// private profile/group photos. It cannot and should not claim to stop a
// screenshot or a determined user using developer tools.
document.addEventListener("contextmenu", (event) => {
  if (event.target.closest("img.protected-image")) event.preventDefault();
});
document.addEventListener("dragstart", (event) => {
  if (event.target.closest("img.protected-image")) event.preventDefault();
});

function bindReceiptCamera(opts) {
  // Always use the native camera / file picker (capture=environment). The old
  // in-app live-preview modal could not focus or use flash and produced poor
  // scans; the system camera app is sharper and supports flash.
  const openBtn = $(opts.openBtn);
  const fileInput = $(opts.fileInput);
  openBtn?.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (!fileInput) {
      showToast("Camera is not available on this device.", "error");
      return;
    }
    fileInput.click();
  });
}

const RETURN_PATH_KEY = "sharesquad_return_path";

function isLoginPath(pathname = location.pathname) {
  const path = (pathname || "/").replace(/\/+$/, "") || "/";
  return path === "/login" || path === "/login.html";
}

function isAuthedAppPath(pathname = location.pathname) {
  const path = (pathname || "/").replace(/\/+$/, "") || "/";
  if (isLoginPath(path) || path === "/" || path === "/landing" || path === "/landing.html") return false;
  // Public marketing / legal / join pages stay reachable without a session.
  if (/\/(about|contact|help|privacy|terms|receipt-scanning|shared-groups|clear-balances|friend-join|reset-password)(?:\.html)?$/.test(path)) {
    return false;
  }
  if (/\/(join|quick-join)(?:\.html)?$/.test(path)) return false;
  return true;
}

function isBackForwardNavigation() {
  try {
    const nav = performance.getEntriesByType("navigation")[0];
    if (nav && nav.type === "back_forward") return true;
  } catch (_) {
    /* ignore */
  }
  try {
    // Legacy PerformanceNavigation.TYPE_BACK_FORWARD === 2
    if (performance.navigation && performance.navigation.type === 2) return true;
  } catch (_) {
    /* ignore */
  }
  return false;
}

function safeAppRedirect(rawNext) {
  const next = String(rawNext || "").trim();
  // Never bounce signed-in users to the marketing home page.
  if (!next || next === "/" || next === "/landing.html" || next.startsWith("//") || !next.startsWith("/")) {
    return "/app";
  }
  if (isLoginPath(next.split("?")[0])) return "/app";
  return next;
}

function goToLogin({ replace = true, keepNext = false, search = "" } = {}) {
  allowLoginOnce();
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  if (keepNext && !params.has("next") && !isLoginPath()) {
    const here = `${location.pathname}${location.search}`;
    if (here && here !== "/" && !here.startsWith("/landing")) params.set("next", here);
  }
  const qs = params.toString();
  const url = `/login.html${qs ? `?${qs}` : ""}`;
  if (replace) location.replace(url);
  else location.href = url;
}

const ALLOW_LOGIN_KEY = "sharesquad_allow_login";

function allowLoginOnce() {
  try {
    sessionStorage.setItem(ALLOW_LOGIN_KEY, "1");
  } catch (_) {
    /* private mode */
  }
}

function consumeAllowLogin() {
  try {
    const ok = sessionStorage.getItem(ALLOW_LOGIN_KEY) === "1";
    if (ok) sessionStorage.removeItem(ALLOW_LOGIN_KEY);
    return ok;
  } catch (_) {
    return false;
  }
}

/**
 * While signed in, block accidental return to login via browser Back / bfcache.
 * Intentional visits still work: Sign out, typing /login or /login.html, or following a login link.
 */
function initLoginHistoryGuard() {
  const bounceAway = () => {
    if (!getToken() || !isLoginPath()) return false;
    // Sign out (and similar) set a one-shot allow flag so login stays reachable.
    if (consumeAllowLogin()) return false;
    const next = new URLSearchParams(location.search).get("next");
    location.replace(safeAppRedirect(next));
    return true;
  };

  if (isLoginPath()) {
    // Only block real Back/Forward (or bfcache). Never bounce typed /login or Sign out.
    if (isBackForwardNavigation()) bounceAway();
    window.addEventListener("pageshow", (event) => {
      if (!getToken() || !event.persisted) return;
      bounceAway();
    });
    return;
  }

  // After logout, a bfcache Back into an app page must not stay open as if signed in.
  if (!isAuthedAppPath()) return;
  window.addEventListener("pageshow", (event) => {
    if (getToken() || !event.persisted) return;
    allowLoginOnce();
    goToLogin({ replace: true });
  });
}

function currentAppPath() {
  // Prefer the live group tab from the DOM when the address bar is briefly stale
  // (e.g. right after a swipe before replaceState lands).
  try {
    if (/\/group(?:\.html)?$/.test(location.pathname)) {
      const active = document.querySelector(".tab-btn.active[data-tab]");
      if (active?.dataset?.tab) {
        const url = new URL(location.href);
        if (active.dataset.tab !== "balances") url.searchParams.set("tab", active.dataset.tab);
        else url.searchParams.delete("tab");
        return `${url.pathname}${url.search}${url.hash}`;
      }
    }
  } catch (_) {
    /* ignore */
  }
  return `${location.pathname}${location.search}${location.hash}`;
}

function rememberReturnPath(url = currentAppPath()) {
  try {
    sessionStorage.setItem(RETURN_PATH_KEY, url);
  } catch (_) {
    /* private mode / quota */
  }
}

function consumeReturnPath() {
  try {
    const value = sessionStorage.getItem(RETURN_PATH_KEY);
    sessionStorage.removeItem(RETURN_PATH_KEY);
    return value;
  } catch (_) {
    return null;
  }
}

function navigateWithReturn(url) {
  // Don't overwrite the saved origin when already on Settings / Account.
  if (!/\/settings(?:\.html)?(?:$|[?#])/.test(location.pathname + location.search)) {
    const live = currentAppPath();
    // Sync the live path (e.g. group ?tab=insights) into this history entry so
    // browser Back from Settings restores the same screen as the in-app Back.
    try {
      const here = `${location.pathname}${location.search}${location.hash}`;
      if (live !== here) history.replaceState(history.state, "", live);
    } catch (_) {
      /* ignore */
    }
    rememberReturnPath(live);
  }
  location.href = url;
}

function bindSmartBack(el, fallback = "/index.html") {
  // Settings-only: return to the saved page (e.g. group Insights). Never use
  // history.back() — that traps people between group tabs / settings.
  if (!el || el.dataset.smartBackBound === "1") return;
  el.dataset.smartBackBound = "1";
  el.addEventListener("click", (e) => {
    e.preventDefault();
    const stored = consumeReturnPath();
    const here = currentAppPath();
    if (stored && stored !== here && !/\/settings(?:\.html)?(?:$|[?#])/.test(stored)) {
      location.href = stored;
      return;
    }
    location.href = el.getAttribute("href") || fallback;
  });
}

function initSmartNavigation() {
  // Remember the current screen when opening Settings / Account.
  document.querySelectorAll('a[href="/settings.html"], a[href="/settings"]').forEach((link) => {
    if (link.dataset.returnNavBound === "1") return;
    link.dataset.returnNavBound = "1";
    link.addEventListener("click", (e) => {
      e.preventDefault();
      navigateWithReturn("/settings.html");
    });
  });
  // Smart back applies ONLY on the Settings page. Group / Personal / Friends
  // / Quick-split back arrows keep their plain href (Groups list, Friends, …).
  if (!/\/settings(?:\.html)?$/.test(location.pathname)) return;
  const back = document.getElementById("backLink");
  if (back) {
    const fallback = back.getAttribute("href") || "/index.html";
    bindSmartBack(back, fallback);
  }
}

const CATEGORIES = [
  { id: "FOOD & DINING", slug: "Food", label: "FOOD & DINING" },
  { id: "GROCERIES", slug: "Groceries", label: "GROCERIES" },
  { id: "TRAVEL", slug: "Travel", label: "TRAVEL" },
  { id: "TRANSIT", slug: "Transit", label: "TRANSIT" },
  { id: "HOUSING", slug: "Housing", label: "HOUSING" },
  { id: "UTILITIES", slug: "Utilities", label: "UTILITIES" },
  { id: "ENTERTAINMENT", slug: "Entertainment", label: "ENTERTAINMENT" },
  { id: "SHOPPING", slug: "Shopping", label: "SHOPPING" },
  { id: "HEALTHCARE", slug: "Healthcare", label: "HEALTHCARE" },
  { id: "GENERAL", slug: "General", label: "GENERAL / OTHER" },
];
const CUSTOM_CATEGORY_VALUE = "__custom__";

function isKnownCategory(name) {
  const value = String(name || "").trim();
  return CATEGORIES.some(
    (cat) => cat.id.toLowerCase() === value.toLowerCase() || cat.label.toLowerCase() === value.toLowerCase()
  );
}

/** Every category tag is stored/shown UPPERCASE so case variants never diverge. */
function normalizeCategory(name) {
  const value = String(name || "").trim() || "GENERAL";
  const known = CATEGORIES.find(
    (cat) => cat.id.toLowerCase() === value.toLowerCase() || cat.label.toLowerCase() === value.toLowerCase()
  );
  if (known) return known.id;
  const key = value.toLowerCase();
  if (key === "food" || key === "dining" || key === "food & dining") return "FOOD & DINING";
  if (key === "other" || key === "general / other") return "GENERAL";
  return value.toUpperCase().slice(0, 50);
}

function categorySlug(name) {
  const value = String(name || "GENERAL").trim();
  const match = CATEGORIES.find(
    (cat) =>
      cat.id.toLowerCase() === value.toLowerCase() ||
      cat.label.toLowerCase() === value.toLowerCase() ||
      value.toLowerCase().startsWith(cat.slug.toLowerCase())
  );
  if (match) return match.slug;
  const cleaned = value.replace(/[^a-zA-Z0-9]+/g, "").slice(0, 12) || "Custom";
  return cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
}

const CATEGORY_ACCENTS = {
  Food: "#D97706",
  Groceries: "#16A34A",
  Travel: "#4F46E5",
  Transit: "#0284C7",
  Housing: "#DB2777",
  Utilities: "#9333EA",
  Entertainment: "#E11D48",
  Shopping: "#EA580C",
  Healthcare: "#0D9488",
  General: "#64748B",
};

function hashHue(value) {
  let hash = 2166136261;
  const text = String(value || "").toLowerCase();
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return Math.abs(hash) % 360;
}

function categoryAccentColor(name) {
  const value = String(name || "GENERAL").trim() || "GENERAL";
  const match = CATEGORIES.find(
    (cat) => cat.id.toLowerCase() === value.toLowerCase() || cat.label.toLowerCase() === value.toLowerCase()
  );
  if (match) return CATEGORY_ACCENTS[match.slug] || CATEGORY_ACCENTS.General;
  // Stable distinct hue per custom label so Pets / Gifts / etc. don't all share General gray.
  const hue = hashHue(value);
  return `hsl(${hue} 64% 42%)`;
}

function categoryTintStyle(name) {
  const accent = categoryAccentColor(name);
  const dark = typeof document !== "undefined" && document.documentElement.classList.contains("theme-dark");
  if (dark) {
    return `--cat-accent:${accent};--cat-bg:color-mix(in srgb, ${accent} 28%, #0B1220);--cat-fg:color-mix(in srgb, ${accent} 55%, #E2E8F0)`;
  }
  return `--cat-accent:${accent};--cat-bg:color-mix(in srgb, ${accent} 16%, var(--surface));--cat-fg:color-mix(in srgb, ${accent} 72%, var(--ink))`;
}

function categoryClass(name) {
  const value = String(name || "GENERAL").trim() || "GENERAL";
  const match = CATEGORIES.find(
    (cat) => cat.id.toLowerCase() === value.toLowerCase() || cat.label.toLowerCase() === value.toLowerCase()
  );
  if (match) return `cat-${match.slug}`;
  return "cat-Custom";
}

function categoryMark(name, extraClass = "category-tag") {
  const value = String(name || "General").trim() || "General";
  const cls = `${extraClass} ${categoryClass(value)}`.trim();
  if (isKnownCategory(value)) return `class="${escapeHtml(cls)}"`;
  return `class="${escapeHtml(cls)}" style="${categoryTintStyle(value)}"`;
}

function renderCategoryPieChart(categoryRows, totalSpent, currency) {
  const rows = Array.isArray(categoryRows) ? categoryRows : [];
  if (!rows.length) return '<p class="text-sm text-ink/45">No expenses in this period.</p>';
  const cx = 50;
  const cy = 50;
  const r = 42;
  const total = rows.reduce((sum, [, amount]) => sum + Number(amount || 0), 0) || 1;
  let angle = -Math.PI / 2;
  const slices = rows.map(([name, amount]) => {
    const value = Number(amount || 0);
    const slice = (value / total) * Math.PI * 2;
    const start = angle;
    const end = angle + slice;
    angle = end;
    const x1 = cx + r * Math.cos(start);
    const y1 = cy + r * Math.sin(start);
    const x2 = cx + r * Math.cos(end);
    const y2 = cy + r * Math.sin(end);
    const large = slice > Math.PI ? 1 : 0;
    const color = categoryAccentColor(name);
    if (value <= 0) return "";
    if (slice >= Math.PI * 2 - 1e-6) {
      return `<circle class="category-pie-slice" cx="${cx}" cy="${cy}" r="${r}" fill="${color}"></circle>`;
    }
    return `<path class="category-pie-slice" d="M ${cx} ${cy} L ${x1.toFixed(3)} ${y1.toFixed(3)} A ${r} ${r} 0 ${large} 1 ${x2.toFixed(3)} ${y2.toFixed(3)} Z" fill="${color}"></path>`;
  }).join("");

  const legend = rows.map(([name, amount]) => {
    const share = totalSpent > 0 ? Math.round((Number(amount || 0) / totalSpent) * 100) : 0;
    const color = categoryAccentColor(name);
    return `<div class="category-pie-legend-row">
      <span class="inline-flex items-center gap-2 min-w-0">
        <span class="category-pie-swatch ${categoryClass(name)}" style="background:${color}"></span>
        <span class="truncate">${escapeHtml(name)}</span>
      </span>
      <strong class="category-pie-legend-metrics" dir="ltr">${formatMoney(amount, currency)}<span class="category-chart-share">${share}%</span></strong>
    </div>`;
  }).join("");

  return `<div class="category-chart" role="img" aria-label="Spending by category">
    <div class="category-pie-wrap">
      <svg class="category-pie" viewBox="0 0 100 100" aria-hidden="true">${slices || '<circle class="category-pie-empty" cx="50" cy="50" r="42"></circle>'}</svg>
    </div>
    <div class="category-pie-legend">${legend}</div>
  </div>`;
}

function fillCategorySelect(select, selected = "", { includeAll = false, allLabel = "ALL CATEGORIES", allowCustom = !includeAll } = {}) {
  if (!select) return;
  let current = selected || select.value || (includeAll ? "All" : "FOOD & DINING");
  if (current && current !== "All") current = normalizeCategory(current);
  const options = includeAll
    ? [{ id: "All", slug: "", label: allLabel }, ...CATEGORIES]
    : [...CATEGORIES];
  if (allowCustom && !includeAll) {
    options.push({ id: CUSTOM_CATEGORY_VALUE, slug: "General", label: "CUSTOM…" });
  }
  const known = options.some((cat) => cat.id === current);
  const useCustom = allowCustom && !includeAll && current && current !== "All" && !isKnownCategory(current);
  select.innerHTML = options.map((cat) => {
    // The "All categories" option needs a themed swatch too. Without a
    // category class, native dark-mode menus can render it as light text on a
    // light system popup.
    const cls = ` class="${escapeHtml(categoryClass(cat.slug ? (cat.id === CUSTOM_CATEGORY_VALUE ? "General" : cat.id) : "General"))}"`;
    return `<option value="${escapeHtml(cat.id)}"${cls}>${escapeHtml(cat.label)}</option>`;
  }).join("");
  select.value = useCustom ? CUSTOM_CATEGORY_VALUE : (known ? current : options[0].id);
  const customInput = document.getElementById(select.dataset.customInput || "") || select.parentElement?.querySelector("[data-category-custom], #f_category_custom, #expenseCategoryCustom, #qsCategoryCustom");
  const customWrap = customInput?.closest("#customCategoryWrap, .custom-category-wrap") || document.getElementById(select.dataset.customWrap || "");
  if (customInput) {
    if (useCustom) customInput.value = current;
    if (customWrap) customWrap.classList.toggle("hidden", select.value !== CUSTOM_CATEGORY_VALUE);
  }
  // A programmatic reset or edit may load a different custom label.
  delete select.dataset.customAccent;
  syncCategorySelect(select);
}

function syncCategorySelect(select) {
  if (!select) return;
  select.classList.remove(...CATEGORIES.map((cat) => `cat-${cat.slug}`), "cat-General", "cat-Custom");
  ["--cat-accent", "--cat-bg", "--cat-fg"].forEach((prop) => select.style.removeProperty(prop));
  const value = select.value;
  const customInput = document.getElementById(select.dataset.customInput || "") || select.parentElement?.querySelector("[data-category-custom], #f_category_custom, #expenseCategoryCustom, #qsCategoryCustom");
  const customWrap = customInput?.closest("#customCategoryWrap, .custom-category-wrap") || document.getElementById(select.dataset.customWrap || "");
  if (value && value !== "All" && value !== CUSTOM_CATEGORY_VALUE) {
    delete select.dataset.customAccent;
    select.classList.add(categoryClass(value));
    if (!isKnownCategory(value)) {
      const accent = categoryAccentColor(value);
      select.style.setProperty("--cat-accent", accent);
      select.style.setProperty("--cat-bg", `color-mix(in srgb, ${accent} 16%, white)`);
      select.style.setProperty("--cat-fg", `color-mix(in srgb, ${accent} 72%, black)`);
    }
  } else if (value === CUSTOM_CATEGORY_VALUE) {
    const custom = (customInput?.value || "").trim();
    if (custom) {
      select.classList.add("cat-Custom");
      // Pick an accent when the custom label starts, then preserve it while
      // the person finishes typing. Rehashing every partial word causes a
      // distracting color flicker ("P" → "Pe" → "Pet" → "Pets").
      const accent = select.dataset.customAccent || categoryAccentColor(custom);
      select.dataset.customAccent = accent;
      select.style.setProperty("--cat-accent", accent);
      select.style.setProperty("--cat-bg", `color-mix(in srgb, ${accent} 16%, white)`);
      select.style.setProperty("--cat-fg", `color-mix(in srgb, ${accent} 72%, black)`);
    } else {
      delete select.dataset.customAccent;
      select.classList.add("cat-General");
    }
  }
  if (customWrap) customWrap.classList.toggle("hidden", value !== CUSTOM_CATEGORY_VALUE);
}

function resolveCategoryValue(select, customInput) {
  if (!select) return "GENERAL";
  if (select.value === CUSTOM_CATEGORY_VALUE) {
    const custom = (customInput?.value || "").trim().slice(0, 50);
    return normalizeCategory(custom || "GENERAL");
  }
  return normalizeCategory(select.value || "GENERAL");
}

function bindCategorySelect(select, { includeAll = false, allowCustom = !includeAll } = {}) {
  if (!select || select.dataset.categoryBound === "1") return;
  select.dataset.categoryBound = "1";
  fillCategorySelect(select, select.value, { includeAll, allowCustom });
  select.addEventListener("change", () => syncCategorySelect(select));
  const customInput = document.getElementById(select.dataset.customInput || "") || select.parentElement?.querySelector("[data-category-custom], #f_category_custom, #expenseCategoryCustom, #qsCategoryCustom");
  customInput?.addEventListener("input", () => {
    if (select.value !== CUSTOM_CATEGORY_VALUE) return;
    const start = customInput.selectionStart;
    const end = customInput.selectionEnd;
    const upper = String(customInput.value || "").toUpperCase().slice(0, 50);
    if (customInput.value !== upper) {
      customInput.value = upper;
      if (typeof start === "number" && typeof end === "number") {
        customInput.setSelectionRange(start, end);
      }
    }
    syncCategorySelect(select);
  });
}

function bindAllCategorySelects(root = document) {
  root.querySelectorAll("select#f_category, select#expenseCategory, select#qsCategory, select[data-category-select]").forEach((select) => {
    bindCategorySelect(select, { includeAll: false, allowCustom: true });
  });
  root.querySelectorAll("select#groupCategoryFilter, select#categoryFilter").forEach((select) => {
    bindCategorySelect(select, { includeAll: true, allowCustom: false });
  });
}

function markOcrFields(fieldMap = {}) {
  Object.entries(fieldMap).forEach(([selector, filled]) => {
    const el = $(selector);
    if (!el) return;
    el.classList.toggle("ocr-filled", Boolean(filled));
    el.classList.toggle("ocr-defaulted", !filled);
  });
}

function clearOcrFieldMarks(root = document) {
  root.querySelectorAll(".ocr-filled, .ocr-defaulted").forEach((el) => {
    el.classList.remove("ocr-filled", "ocr-defaulted");
  });
}

function todayISODate() {
  const now = new Date();
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, "0");
  const d = String(now.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

function clampDateInputToToday(input) {
  if (!input) return "";
  const today = todayISODate();
  if (!input.max || input.max > today) input.max = today;
  const raw = (input.value || "").trim();
  if (raw && raw > today) {
    input.value = today;
    input._flatpickr?.setDate(today, true);
  }
  return input.value;
}

function initDatePickers(root = document) {
  const inputs = root.querySelectorAll('input[type="date"], input[data-datepicker]');
  if (!inputs.length) return;
  const today = todayISODate();
  const enhance = () => {
    if (!window.flatpickr) return false;
    inputs.forEach((input) => {
      if (!input.max || input.max > today) input.max = today;
      clampDateInputToToday(input);
      if (input._flatpickr) {
        input._flatpickr.set("maxDate", today);
        return;
      }
      window.flatpickr(input, {
        dateFormat: "Y-m-d",
        allowInput: true,
        disableMobile: true,
        maxDate: today,
        defaultDate: input.value || undefined,
        onChange(selectedDates, dateStr) {
          if (dateStr && dateStr > today) {
            input._flatpickr?.setDate(today, false);
            input.value = today;
          }
        },
        onClose() {
          clampDateInputToToday(input);
        },
      });
    });
    return true;
  };
  if (enhance()) return;
  if (document.querySelector('script[data-flatpickr]')) {
    const timer = setInterval(() => { if (enhance()) clearInterval(timer); }, 200);
    setTimeout(() => clearInterval(timer), 8000);
    return;
  }
  const css = document.createElement("link");
  css.rel = "stylesheet";
  css.href = "https://cdn.jsdelivr.net/npm/flatpickr@4.6.13/dist/flatpickr.min.css";
  document.head.appendChild(css);
  const script = document.createElement("script");
  script.src = "https://cdn.jsdelivr.net/npm/flatpickr@4.6.13/dist/flatpickr.min.js";
  script.dataset.flatpickr = "1";
  script.onload = enhance;
  document.head.appendChild(script);
}

function haptic(pattern = 30) {
  try { navigator.vibrate?.(pattern); } catch (_) {}
}

const THEME_KEY = "sharesquad_theme";

function getThemePreference() {
  const value = localStorage.getItem(THEME_KEY);
  return value === "light" || value === "dark" || value === "system" ? value : "system";
}

function resolveTheme(preference = getThemePreference()) {
  if (preference === "light") return "light";
  if (preference === "dark") return "dark";
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function applyTheme(preference = getThemePreference()) {
  const resolved = resolveTheme(preference);
  document.documentElement.classList.toggle("theme-dark", resolved === "dark");
  document.documentElement.classList.toggle("theme-light", resolved === "light");
  document.documentElement.dataset.theme = resolved;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", resolved === "dark" ? "#0B1220" : "#F8FAFC");
  return resolved;
}

function setThemePreference(preference) {
  const next = preference === "light" || preference === "dark" || preference === "system" ? preference : "system";
  localStorage.setItem(THEME_KEY, next);
  applyTheme(next);
  window.dispatchEvent(new CustomEvent("sharesquad:theme", { detail: { preference: next, resolved: resolveTheme(next) } }));
  return next;
}

applyTheme();
// Numeric fields are frequently populated with a zero/default value by split
// calculators. Make editing predictable everywhere: a default zero disappears
// on focus, and any other existing value is selected so the next keystroke
// replaces it rather than appending to it.
document.addEventListener("focusin", (event) => {
  const field = event.target;
  if (!(field instanceof HTMLInputElement)) return;
  const numeric = field.type === "number" || field.inputMode === "decimal" || field.inputMode === "numeric";
  if (!numeric || field.readOnly || field.disabled) return;
  if (/^0(?:\.0+)?$/.test(field.value.trim())) field.value = "";
  else if (field.value) field.select();
});
window.ShareSquad = {
  $,
  $$,
  api,
  getToken,
  setToken,
  removeToken,
  beginDemoSession,
  exitDemoSession,
  renderDemoBanner,
  getStoredGroupId,
  setStoredGroupId,
  removeStoredGroupId,
  showToast,
  runWithUndo,
  confirmDialog,
  renderSkeleton,
  initGlobalNotifications,
  initMobileQuickAction,
  initSearchPalette,
  openNotificationLink,
  isReceiptImageFile,
  prepareReceiptFile,
  uploadReceipt,
  setAuthImage,
  bindReceiptCamera,
  rememberReturnPath,
  consumeReturnPath,
  navigateWithReturn,
  bindSmartBack,
  initSmartNavigation,
  isLoginPath,
  isBackForwardNavigation,
  safeAppRedirect,
  goToLogin,
  allowLoginOnce,
  initLoginHistoryGuard,
  formatMoney,
  formatMoneyText,
  formatDate,
  escapeHtml,
  initials,
  maskEmail,
  CURRENCIES,
  CURRENCY_SYMBOLS,
  currencySymbol,
  currencyMeta,
  fillCurrencySelect,
  pickScannedCurrency,
  fillAllCurrencySelects,
  downloadBlob,
  haptic,
  CATEGORIES,
  categorySlug,
  categoryClass,
  categoryAccentColor,
  categoryTintStyle,
  categoryMark,
  renderCategoryPieChart,
  isKnownCategory,
  normalizeCategory,
  fillCategorySelect,
  syncCategorySelect,
  resolveCategoryValue,
  bindCategorySelect,
  bindAllCategorySelects,
  markOcrFields,
  clearOcrFieldMarks,
  initDatePickers,
  todayISODate,
  clampDateInputToToday,
  getThemePreference,
  setThemePreference,
  applyTheme,
  resolveTheme,
};

function bootSharedUi() {
  renderDemoBanner();
  initLoginHistoryGuard();
  initSmartNavigation();
}
// Assign ShareSquad before boot so login/inline pages never see a partial client.
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bootSharedUi, { once: true });
else bootSharedUi();
try {
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (getThemePreference() === "system") applyTheme("system");
  });
} catch (_) {}
