const TOKEN_KEY = "billbuddy_token";
const GROUP_KEY = "billbuddy_group_id";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

let providers = [];
let groups = [];
let selectedGroupId = null;

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  const token = localStorage.getItem(TOKEN_KEY);
  if (token) headers.Authorization = `Bearer ${token}`;
  if (opts.body && !(opts.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(`/api${path}`, { ...opts, headers });
  if (res.status === 401) {
    window.location.href = "/login.html";
    return;
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    const detail = err.detail;
    const msg = typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((d) => d.msg).join(", ") : "Request failed";
    throw new Error(msg);
  }
  return res.json();
}

function fillProviderSelect(selectEl, providerId, modelId) {
  selectEl.innerHTML = providers
    .map((p) => `<option value="${p.id}" ${p.id === providerId ? "selected" : ""}>${p.label}</option>`)
    .join("");
  const modelEl = selectEl.id === "generalProvider" ? $("#generalModel") : $("#groupModel");
  fillModelSelect(modelEl, providerId, modelId);
}

function fillModelSelect(modelEl, providerId, modelId) {
  const provider = providers.find((p) => p.id === providerId);
  if (!provider || !modelEl) return;
  const selected = modelId || provider.models[0]?.id;
  modelEl.innerHTML = provider.models
    .map((m) => `<option value="${m.id}" ${m.id === selected ? "selected" : ""}>${m.label}</option>`)
    .join("");
}

function switchTab(tab) {
  $$("[data-settings-tab]").forEach((b) => b.classList.toggle("active", b.dataset.settingsTab === tab));
  $("#tab-general").classList.toggle("hidden", tab !== "general");
  $("#tab-group").classList.toggle("hidden", tab !== "group");
}

function updateGeneralVlmVisibility() {
  const on = $("#useOwnVlm").checked;
  $("#generalVlmFields").classList.toggle("opacity-50", !on);
  $("#generalProvider").disabled = !on;
  $("#generalModel").disabled = !on;
  $("#generalApiKey").disabled = !on;
}

function updateGroupVlmVisibility() {
  const on = $("#overrideGlobalVlm").checked;
  $("#groupVlmFields").classList.toggle("hidden", !on);
}

async function loadGeneralSettings() {
  const s = await api("/settings");
  $("#useOwnVlm").checked = s.use_own_vlm;
  $("#emailMonthlyReports").checked = s.email_monthly_reports;
  $("#compactListView").checked = s.compact_list_view;
  fillProviderSelect($("#generalProvider"), s.vlm_provider, s.vlm_model);
  $("#generalKeyStatus").textContent = s.has_api_key
    ? "A key is saved. Leave blank to keep it, or paste a new one to replace."
    : "No key saved yet.";
  $("#generalApiKey").value = "";
  updateGeneralVlmVisibility();
}

async function loadGroupSettings() {
  if (!selectedGroupId) return;
  const s = await api(`/groups/${selectedGroupId}/settings`);
  $("#overrideGlobalVlm").checked = s.override_global_vlm;
  $("#equalSplitDefault").checked = s.equal_split_default;
  fillProviderSelect($("#groupProvider"), s.vlm_provider, s.vlm_model);
  $("#groupKeyStatus").textContent = s.has_api_key
    ? "A key is saved for this group. Leave blank to keep it."
    : "No group-specific key saved.";
  $("#groupApiKey").value = "";
  updateGroupVlmVisibility();
}

async function saveGeneralSettings(clearKey = false) {
  const msg = $("#generalMsg");
  msg.textContent = "Saving…";
  msg.className = "text-xs font-mono text-ink/50";
  try {
    const body = {
      use_own_vlm: $("#useOwnVlm").checked,
      vlm_provider: $("#generalProvider").value,
      vlm_model: $("#generalModel").value,
      email_monthly_reports: $("#emailMonthlyReports").checked,
      compact_list_view: $("#compactListView").checked,
    };
    const key = $("#generalApiKey").value.trim();
    if (key) body.vlm_api_key = key;
    if (clearKey) body.clear_api_key = true;
    await api("/settings", { method: "PUT", body: JSON.stringify(body) });
    msg.textContent = "General settings saved.";
    msg.className = "text-xs font-mono balance-positive";
    await loadGeneralSettings();
  } catch (err) {
    msg.textContent = err.message;
    msg.className = "text-xs font-mono balance-negative";
  }
}

async function saveGroupSettings(clearKey = false) {
  if (!selectedGroupId) return;
  const msg = $("#groupMsg");
  msg.textContent = "Saving…";
  msg.className = "text-xs font-mono text-ink/50";
  try {
    const body = {
      override_global_vlm: $("#overrideGlobalVlm").checked,
      vlm_provider: $("#groupProvider").value,
      vlm_model: $("#groupModel").value,
      equal_split_default: $("#equalSplitDefault").checked,
    };
    const key = $("#groupApiKey").value.trim();
    if (key) body.vlm_api_key = key;
    if (clearKey) body.clear_api_key = true;
    await api(`/groups/${selectedGroupId}/settings`, { method: "PUT", body: JSON.stringify(body) });
    msg.textContent = "Group settings saved.";
    msg.className = "text-xs font-mono balance-positive";
    await loadGroupSettings();
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

  const params = new URLSearchParams(window.location.search);
  const groupParam = params.get("group") || localStorage.getItem(GROUP_KEY);
  const tabParam = params.get("tab");

  providers = await api("/settings/vlm-providers");
  const me = await api("/auth/me");
  groups = me.groups || [];

  const groupSelect = $("#groupSelect");
  if (!groups.length) {
    groupSelect.innerHTML = '<option value="">No groups — join or create one first</option>';
    $("#saveGroupBtn").disabled = true;
  } else {
    groupSelect.innerHTML = groups.map((g) => `<option value="${g.id}">${g.name}</option>`).join("");
    selectedGroupId = Number(groupParam) || groups[0].id;
    groupSelect.value = String(selectedGroupId);
  }

  const back = $("#backLink");
  if (groupParam) {
    back.href = "/group.html";
  }

  $$("[data-settings-tab]").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.settingsTab));
  });

  $("#generalProvider").addEventListener("change", () => {
    fillModelSelect($("#generalModel"), $("#generalProvider").value, null);
  });
  $("#groupProvider").addEventListener("change", () => {
    fillModelSelect($("#groupModel"), $("#groupProvider").value, null);
  });
  $("#useOwnVlm").addEventListener("change", updateGeneralVlmVisibility);
  $("#overrideGlobalVlm").addEventListener("change", updateGroupVlmVisibility);
  $("#saveGeneralBtn").addEventListener("click", () => saveGeneralSettings(false));
  $("#clearGeneralKeyBtn").addEventListener("click", () => {
    if (confirm("Remove your saved API key?")) saveGeneralSettings(true);
  });
  $("#saveGroupBtn").addEventListener("click", () => saveGroupSettings(false));
  $("#clearGroupKeyBtn").addEventListener("click", () => {
    if (confirm("Remove the API key saved for this group?")) saveGroupSettings(true);
  });
  groupSelect.addEventListener("change", async () => {
    selectedGroupId = Number(groupSelect.value);
    await loadGroupSettings();
  });

  await loadGeneralSettings();
  if (selectedGroupId) await loadGroupSettings();

  if (tabParam === "group" || groupParam) {
    switchTab("group");
  }

  lucide.createIcons();
})();
