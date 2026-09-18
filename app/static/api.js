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

function showToast(msg, type = "info", duration = 3500) {
  let container = $("#toastContainer");
  if (!container) {
    container = document.createElement("div");
    container.id = "toastContainer";
    container.className = "fixed bottom-5 right-5 z-50 flex flex-col gap-2 max-w-sm w-full px-4 pointer-events-none";
    document.body.appendChild(container);
  }

  const toast = document.createElement("div");
  const bg = type === "error" ? "bg-stamp text-white" : type === "success" ? "bg-emerald-700 text-white" : "bg-ink text-paper";
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

function formatDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
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
    imgEl.classList.remove("hidden");
  } catch (err) {
    imgEl.removeAttribute("src");
    imgEl.classList.add("hidden");
  }
}

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
  isReceiptImageFile,
  prepareReceiptFile,
  uploadReceipt,
  setAuthImage,
  bindReceiptCamera,
  formatMoney,
  formatDate,
  escapeHtml,
  CURRENCIES,
  CURRENCY_SYMBOLS,
  currencySymbol,
  currencyMeta,
  fillCurrencySelect,
  fillAllCurrencySelects,
  downloadBlob,
};
