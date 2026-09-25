// ShareSquad Unified API & Utility Client

const TOKEN_KEY = "sharesquad_token";
const LEGACY_TOKEN_KEY = "billbuddy_token";
const GROUP_KEY = "sharesquad_group_id";
const LEGACY_GROUP_KEY = "billbuddy_group_id";

function getToken() {
  return localStorage.getItem(TOKEN_KEY) || localStorage.getItem(LEGACY_TOKEN_KEY);
}

function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(LEGACY_TOKEN_KEY, token);
}

function removeToken() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(LEGACY_TOKEN_KEY);
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
  if (value.length <= 7) return "•••";
  return `${value.slice(0, 3)}•••${value.slice(-4)}`;
}

function showToast(msg, type = "info", duration = 3500) {
  let container = $("#toastContainer");
  if (!container) {
    container = document.createElement("div");
    container.id = "toastContainer";
    container.className = "fixed bottom-5 right-5 z-50 flex flex-col gap-2 max-w-sm w-full px-4 pointer-events-none";
    document.body.appendChild(container);
  }

  const toast = document.createElement("div");
  const bg = type === "error" ? "bg-red-800 text-white" : type === "success" ? "bg-emerald-700 text-white" : "bg-ink text-white";
  toast.className = `p-3 rounded-lg shadow-xl text-xs font-mono transition-all transform duration-300 opacity-0 translate-y-2 pointer-events-auto flex items-center justify-between gap-2 ${bg}`;
    toast.innerHTML = `<span>${escapeHtml(String(msg).slice(0, 180))}</span><button class="opacity-70 hover:opacity-100 font-bold">&times;</button>`;
  
  toast.querySelector("button").onclick = () => {
    toast.classList.add("opacity-0", "translate-y-2");
    setTimeout(() => toast.remove(), 300);
  };

  container.appendChild(toast);
  requestAnimationFrame(() => {
    toast.classList.remove("opacity-0", "translate-y-2");
  });

  setTimeout(() => {
    if (toast.parentElement) {
      toast.classList.add("opacity-0", "translate-y-2");
      setTimeout(() => toast.remove(), 300);
    }
  }, duration);
}

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
  modal.innerHTML = `<div class="bb-card modal-card"><div class="flex items-center justify-between mb-4 border-b border-ink/10 pb-3"><h2 class="font-mono uppercase text-xs tracking-widest text-brand font-bold">Notifications</h2><button type="button" class="text-ink/40 hover:text-ink font-bold" aria-label="Close notifications">×</button></div><div id="notificationsList" class="space-y-2 max-h-[60vh] overflow-y-auto"></div></div>`;
  document.body.appendChild(modal);
  const load = async () => {
    const list = $("#notificationsList");
    list.innerHTML = '<div class="ui-skeleton-card"><span></span><span></span></div>';
    try {
      const items = await api("/notifications");
      list.innerHTML = items.length ? items.map((item) => `<button class="notification-item ${item.read_at ? "" : "unread"}" data-id="${item.id}" data-link="${escapeHtml(item.link || "")}" data-message="${escapeHtml(item.message)}"><span>${escapeHtml(item.message)}</span><small>${formatDate(item.created_at)}</small>${item.link ? '<span class="notification-action">View details →</span>' : ""}</button>`).join("") : '<p class="text-sm text-ink/55 py-5 text-center">You are all caught up.</p>';
      list.querySelectorAll(".notification-item").forEach((item) => item.addEventListener("click", async () => { await api(`/notifications/${item.dataset.id}/read`, { method: "POST" }); if (item.dataset.link) openNotificationLink(item.dataset.link, item.dataset.message); else { item.classList.remove("unread"); refreshCount(); } }));
      refreshCount();
    } catch (error) { list.innerHTML = `<p class="text-sm balance-negative">${escapeHtml(error.message)}</p>`; }
  };
  const refreshCount = async () => { try { const unread = await api("/notifications?unread_only=true"); const badge = $("#notificationCount"); badge.textContent = unread.length > 9 ? "9+" : unread.length; badge.classList.toggle("hidden", !unread.length); } catch (_) {} };
  button.addEventListener("click", () => { modal.classList.remove("hidden"); load(); });
  modal.querySelector("button").addEventListener("click", () => modal.classList.add("hidden"));
  modal.addEventListener("click", (event) => { if (event.target === modal) modal.classList.add("hidden"); });
  refreshCount();
}

function initMobileQuickAction() {
  if (!getToken() || $(".mobile-quick-action")) return;
  const onDashboard = /(^|\/)index\.html$|\/$/.test(window.location.pathname);
  const onFriends = /friends\.html$/.test(window.location.pathname);
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
  if (target.pathname.endsWith("/group.html") && !target.searchParams.get("tab")) {
    const text = String(message).toLowerCase();
    if (/payment|paid|received|confirmed/.test(text)) { target.searchParams.set("tab", "balances"); target.searchParams.set("focus", "paymentHistory"); }
    else if (/member|invite/.test(text)) { target.searchParams.set("tab", "members"); target.searchParams.set("focus", "groupMembers"); }
    else if (/settled|reopened/.test(text)) { target.searchParams.set("tab", "balances"); target.searchParams.set("focus", "periodSelect"); }
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

document.addEventListener("DOMContentLoaded", () => setTimeout(() => { initGlobalNotifications(); initMobileQuickAction(); initSearchPalette(); if (!/(^|\/)index\.html$|\/$/.test(window.location.pathname)) $("#logoutBtn")?.remove(); }, 0));

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
      if (!window.location.pathname.includes("login.html") && !window.location.pathname.includes("reset-password.html")) {
        removeToken();
        showToast("Session expired. Please sign in again.", "error");
        setTimeout(() => {
          window.location.href = "/login.html";
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

    const ct = res.headers.get("content-type") || "";
    return ct.includes("application/json") ? res.json() : res;
  } catch (err) {
    throw err;
  }
}

// Currency formatters. Symbols render in .currency-sym (not Space Mono, which lacks ₹).
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
  return `<span class="currency-sym">${escapeHtml(meta.symbol)}</span>${formatted}`;
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

async function prepareReceiptFile(file) {
  if (!file) return file;
  const type = (file.type || "").toLowerCase();
  if (type.includes("heic") || type.includes("heif")) return file;
  if (!type.startsWith("image/") && !isReceiptImageFile(file)) return file;
  try {
    const bitmap = await createImageBitmap(file);
    const maxEdge = 1600;
    const scale = Math.min(1, maxEdge / Math.max(bitmap.width, bitmap.height));
    if (scale >= 1 && file.size < 1.2 * 1024 * 1024) {
      if (bitmap.close) bitmap.close();
      return file;
    }
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(bitmap.width * scale));
    canvas.height = Math.max(1, Math.round(bitmap.height * scale));
    const ctx = canvas.getContext("2d");
    ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    if (bitmap.close) bitmap.close();
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.82));
    if (!blob) return file;
    const base = (file.name || "receipt").replace(/\.[^.]+$/, "");
    return new File([blob], `${base}.jpg`, { type: "image/jpeg" });
  } catch (err) {
    return file;
  }
}

async function uploadReceipt(path, file) {
  const prepared = await prepareReceiptFile(file);
  let lastErr;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const formData = new FormData();
    formData.append("file", prepared);
    try {
      return await api(path, { method: "POST", body: formData });
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
  const modal = $(opts.modalId || "#liveCameraModal");
  const video = $(opts.videoId || "#liveCameraVideo");
  const captureBtn = $(opts.captureBtn || "#liveCameraCapture");
  const cancelBtn = $(opts.cancelBtn || "#liveCameraCancel");
  const fallbackBtn = $(opts.fallbackBtn || "#liveCameraFallback");
  const openBtn = $(opts.openBtn);
  const fileInput = $(opts.fileInput);
  let stream = null;

  async function stopCamera() {
    if (stream) {
      stream.getTracks().forEach((t) => t.stop());
      stream = null;
    }
    if (video) video.srcObject = null;
    modal?.classList.add("hidden");
  }

  async function startCamera() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      fileInput?.click();
      return;
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: {
          facingMode: { ideal: "environment" },
          width: { ideal: 1280 },
          height: { ideal: 720 },
        },
      });
      if (video) {
        video.srcObject = stream;
        await video.play().catch(() => {});
      }
      modal?.classList.remove("hidden");
    } catch (err) {
      showToast("Could not open the in-app camera. Using the phone camera instead.", "info");
      fileInput?.click();
    }
  }

  openBtn?.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    startCamera();
  });
  cancelBtn?.addEventListener("click", (e) => {
    e.preventDefault();
    stopCamera();
  });
  fallbackBtn?.addEventListener("click", (e) => {
    e.preventDefault();
    stopCamera();
    fileInput?.click();
  });
  captureBtn?.addEventListener("click", async (e) => {
    e.preventDefault();
    if (!video || !video.videoWidth) {
      showToast("Camera is not ready yet. Try again.", "info");
      return;
    }
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d").drawImage(video, 0, 0);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.82));
    await stopCamera();
    if (!blob) {
      showToast("Could not capture the photo.", "error");
      return;
    }
    const file = new File([blob], "receipt.jpg", { type: "image/jpeg" });
    if (typeof opts.onCapture === "function") opts.onCapture(file);
  });
}

window.ShareSquad = {
  $,
  $$,
  api,
  getToken,
  setToken,
  removeToken,
  getStoredGroupId,
  setStoredGroupId,
  removeStoredGroupId,
  showToast,
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
};
