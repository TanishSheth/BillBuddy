// ShareSquad Settings Controller

const state = {
  catalog: [],
  accountSettings: null,
  groups: [],
  selectedGroupId: null,
  groupSettings: null,
};
let currentProfileEmail = "";

function switchSettingsTab(tabName) {
  $$("[data-settings-tab]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.settingsTab === tabName);
  });
  $$(".settings-panel").forEach((panel) => {
    panel.classList.toggle("hidden", panel.id !== `tab-${tabName}`);
  });
}

function renderProviderSelect(selectEl, selectedId) {
  selectEl.innerHTML = state.catalog
    .map((p) => `<option value="${p.id}" ${p.id === selectedId ? "selected" : ""}>${ShareSquad.escapeHtml(p.label)}</option>`)
    .join("");
}

function renderModelSelect(selectEl, providerId, selectedModelId) {
  const provider = state.catalog.find((p) => p.id === providerId) || state.catalog[0];
  if (!provider) {
    selectEl.innerHTML = "";
    return;
  }
  selectEl.innerHTML = provider.models
    .map((m) => `<option value="${m.id}" ${m.id === selectedModelId ? "selected" : ""}>${ShareSquad.escapeHtml(m.label)}</option>`)
    .join("");
}

async function loadProfile() {
  const me = await ShareSquad.api("/auth/me");
  if ($("#profileName")) $("#profileName").value = me.account.display_name || "";
  currentProfileEmail = me.account.email || "";
  if ($("#profileEmail")) {
    $("#profileEmail").value = "";
    $("#profileEmail").placeholder = ShareSquad.maskEmail(currentProfileEmail);
  }
  if (me.account.has_avatar) {
    ShareSquad.setAuthImage($("#profileAvatar"), "/auth/me/avatar");
  }
  $("#removeProfileAvatarBtn")?.classList.toggle("hidden", !me.account.has_avatar);
}

async function saveProfile() {
  const msg = $("#profileMsg");
  msg.textContent = "Saving…";
  msg.className = "text-xs font-mono text-ink/50";
  const body = {
    display_name: $("#profileName").value.trim(),
    email: $("#profileEmail").value.trim() || currentProfileEmail,
  };
  const current = $("#profileCurrentPassword").value;
  const next = $("#profileNewPassword").value;
  if (current) body.current_password = current;
  if (next) {
    const strong =
      next.trim().length >= 8 &&
      /[A-Z]/.test(next) &&
      (next.match(/\d/g) || []).length >= 2 &&
      /[^A-Za-z0-9]/.test(next);
    if (!strong) {
      msg.textContent = "New password must be at least 8 characters and include 1 uppercase letter, 2 numbers, and 1 special character.";
      msg.className = "text-xs font-mono balance-negative";
      return;
    }
    body.new_password = next;
  }
  try {
    const updated = await ShareSquad.api("/auth/me", { method: "PATCH", body: JSON.stringify(body) });
    currentProfileEmail = updated.email || currentProfileEmail;
    $("#profileEmail").value = "";
    $("#profileEmail").placeholder = ShareSquad.maskEmail(currentProfileEmail);
    $("#profileCurrentPassword").value = "";
    $("#profileNewPassword").value = "";
    msg.textContent = "Profile saved.";
    msg.className = "text-xs font-mono balance-positive";
    ShareSquad.showToast("Profile saved", "success");
  } catch (err) {
    msg.textContent = err.message;
    msg.className = "text-xs font-mono balance-negative";
  }
}

async function requestAccountDeletion() {
  const password = $("#deleteAccountPassword")?.value || "";
  const msg = $("#deleteAccountMsg");
  if (!password) {
    msg.textContent = "Enter your current password to continue.";
    msg.className = "text-xs font-mono balance-negative";
    return;
  }
  const accepted = await ShareSquad.confirmDialog({
    title: "Schedule account deletion?",
    message: "You will be signed out now. You can still restore this account from the login page for the next 30 days.",
    confirmLabel: "Delete account",
    danger: true,
  });
  if (!accepted) return;
  try {
    const result = await ShareSquad.api("/auth/me/delete-account", {
      method: "POST",
      body: JSON.stringify({ current_password: password }),
    });
    msg.textContent = result.message;
    msg.className = "text-xs font-mono balance-positive";
    ShareSquad.removeToken();
    ShareSquad.removeStoredGroupId();
    window.location.href = "/login.html?recovery=1";
  } catch (err) {
    msg.textContent = err.message || "Could not schedule account deletion.";
    msg.className = "text-xs font-mono balance-negative";
  }
}

async function loadGeneralSettings() {
  const settings = await ShareSquad.api("/settings/account");
  state.accountSettings = settings;

  $("#useOwnVlm").checked = settings.use_own_vlm;
  $("#emailMonthlyReports").checked = settings.email_monthly_reports;
  $("#compactListView").checked = settings.compact_list_view;
  if ($("#preferredCurrency")) $("#preferredCurrency").value = settings.preferred_currency || "USD";

  const providerSel = $("#generalProvider");
  const modelSel = $("#generalModel");
  renderProviderSelect(providerSel, settings.vlm_provider);
  renderModelSelect(modelSel, settings.vlm_provider, settings.vlm_model);

  const statusEl = $("#generalKeyStatus");
  statusEl.textContent = settings.has_api_key ? "✓ An API key is currently saved on server." : "No API key configured.";

  toggleGeneralVlmFields();
}

function toggleGeneralVlmFields() {
  const on = $("#useOwnVlm").checked;
  const fields = $("#generalVlmFields");
  if (fields) {
    fields.style.opacity = on ? "1" : "0.5";
    fields.style.pointerEvents = on ? "auto" : "none";
  }
}

async function saveGeneralSettings() {
  const msg = $("#generalMsg");
  msg.textContent = "Saving…";
  msg.className = "text-xs font-mono text-ink/50";

  const body = {
    use_own_vlm: $("#useOwnVlm").checked,
    vlm_provider: $("#generalProvider").value,
    vlm_model: $("#generalModel").value,
    email_monthly_reports: $("#emailMonthlyReports").checked,
    compact_list_view: $("#compactListView").checked,
    preferred_currency: $("#preferredCurrency") ? $("#preferredCurrency").value : "USD",
  };

  const keyVal = $("#generalApiKey").value.trim();
  if (keyVal) body.vlm_api_key = keyVal;

  try {
    const updated = await ShareSquad.api("/settings/account", {
      method: "PUT",
      body: JSON.stringify(body),
    });
    state.accountSettings = updated;
    $("#generalApiKey").value = "";
    $("#generalKeyStatus").textContent = updated.has_api_key ? "✓ Key saved." : "No key saved.";
    msg.textContent = "General settings saved!";
    msg.className = "text-xs font-mono balance-positive";
    ShareSquad.showToast("Settings saved", "success");
  } catch (err) {
    msg.textContent = err.message;
    msg.className = "text-xs font-mono balance-negative";
  }
}

async function clearGeneralKey() {
  if (!await ShareSquad.confirmDialog({ title: "Remove custom API key?", message: "The saved key will be removed from your account.", confirmLabel: "Remove key" })) return;
  const msg = $("#generalMsg");
  try {
    const updated = await ShareSquad.api("/settings/account", {
      method: "PUT",
      body: JSON.stringify({ clear_api_key: true }),
    });
    state.accountSettings = updated;
    $("#generalApiKey").value = "";
    $("#generalKeyStatus").textContent = "No key saved.";
    msg.textContent = "API key cleared.";
    msg.className = "text-xs font-mono balance-positive";
  } catch (err) {
    msg.textContent = err.message;
    msg.className = "text-xs font-mono balance-negative";
  }
}

async function loadGroupsList() {
  const groups = await ShareSquad.api("/groups");
  state.groups = groups;

  const select = $("#groupSelect");
  if (!groups.length) {
    select.innerHTML = '<option value="">No groups</option>';
    return;
  }

  const storedGid = ShareSquad.getStoredGroupId();
  const defaultGid = storedGid && groups.some((g) => g.id === Number(storedGid))
    ? Number(storedGid)
    : groups[0].id;

  select.innerHTML = groups
    .map((g) => `<option value="${g.id}" ${g.id === defaultGid ? "selected" : ""}>${ShareSquad.escapeHtml(g.name)}</option>`)
    .join("");

  state.selectedGroupId = defaultGid;
  await loadGroupSettings(defaultGid);
}

async function loadGroupSettings(groupId) {
  if (!groupId) return;
  try {
    const settings = await ShareSquad.api(`/settings/groups/${groupId}`);
    state.groupSettings = settings;

    $("#overrideGlobalVlm").checked = settings.override_global_vlm;
    $("#equalSplitDefault").checked = settings.equal_split_default;

    const providerSel = $("#groupProvider");
    const modelSel = $("#groupModel");
    renderProviderSelect(providerSel, settings.vlm_provider);
    renderModelSelect(modelSel, settings.vlm_provider, settings.vlm_model);

    $("#groupKeyStatus").textContent = settings.has_api_key ? "✓ Group API key configured." : "Using global or server key.";
    toggleGroupVlmFields();
  } catch (err) {
    console.error("Failed to load group settings:", err);
  }
}

function toggleGroupVlmFields() {
  const on = $("#overrideGlobalVlm").checked;
  $("#groupVlmFields").classList.toggle("hidden", !on);
}

async function saveGroupSettings() {
  const gid = state.selectedGroupId;
  if (!gid) return;
  const msg = $("#groupMsg");
  msg.textContent = "Saving…";
  msg.className = "text-xs font-mono text-ink/50";

  const body = {
    override_global_vlm: $("#overrideGlobalVlm").checked,
    equal_split_default: $("#equalSplitDefault").checked,
    vlm_provider: $("#groupProvider").value,
    vlm_model: $("#groupModel").value,
  };

  const keyVal = $("#groupApiKey").value.trim();
  if (keyVal) body.vlm_api_key = keyVal;

  try {
    const updated = await ShareSquad.api(`/settings/groups/${gid}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
    state.groupSettings = updated;
    $("#groupApiKey").value = "";
    $("#groupKeyStatus").textContent = updated.has_api_key ? "✓ Key saved." : "Using global/server key.";
    msg.textContent = "Group settings saved!";
    msg.className = "text-xs font-mono balance-positive";
    ShareSquad.showToast("Group settings saved", "success");
  } catch (err) {
    msg.textContent = err.message;
    msg.className = "text-xs font-mono balance-negative";
  }
}

async function clearGroupKey() {
  const gid = state.selectedGroupId;
  if (!gid) return;
  if (!await ShareSquad.confirmDialog({ title: "Clear group API key?", message: "This group's custom key will be removed.", confirmLabel: "Clear key" })) return;
  const msg = $("#groupMsg");
  try {
    const updated = await ShareSquad.api(`/settings/groups/${gid}`, {
      method: "PUT",
      body: JSON.stringify({ clear_api_key: true }),
    });
    state.groupSettings = updated;
    $("#groupApiKey").value = "";
    $("#groupKeyStatus").textContent = "Using global/server key.";
    msg.textContent = "Group key cleared.";
    msg.className = "text-xs font-mono balance-positive";
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

  ShareSquad.fillAllCurrencySelects();

  $$("[data-settings-tab]").forEach((btn) => {
    btn.addEventListener("click", () => switchSettingsTab(btn.dataset.settingsTab));
  });

  try {
    state.catalog = await ShareSquad.api("/vlm/providers");
  } catch (err) {
    console.error("Failed to load VLM catalog:", err);
  }

  $("#useOwnVlm").addEventListener("change", toggleGeneralVlmFields);
  $("#generalProvider").addEventListener("change", (e) => {
    renderModelSelect($("#generalModel"), e.target.value);
  });
  $("#saveGeneralBtn").addEventListener("click", saveGeneralSettings);
  $("#clearGeneralKeyBtn").addEventListener("click", clearGeneralKey);

  $("#groupSelect").addEventListener("change", async (e) => {
    state.selectedGroupId = Number(e.target.value);
    await loadGroupSettings(state.selectedGroupId);
  });
  $("#overrideGlobalVlm").addEventListener("change", toggleGroupVlmFields);
  $("#groupProvider").addEventListener("change", (e) => {
    renderModelSelect($("#groupModel"), e.target.value);
  });
  $("#saveGroupBtn").addEventListener("click", saveGroupSettings);
  $("#equalSplitDefault")?.addEventListener("change", saveGroupSettings);
  $("#clearGroupKeyBtn").addEventListener("click", clearGroupKey);
  $("#saveProfileBtn")?.addEventListener("click", saveProfile);
  $("#deleteAccountBtn")?.addEventListener("click", requestAccountDeletion);
  $("#profileAvatarInput")?.addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    const fd = new FormData();
    fd.append("file", await ShareSquad.prepareReceiptFile(file));
    try {
      await ShareSquad.api("/auth/me/avatar", { method: "POST", body: fd });
      ShareSquad.setAuthImage($("#profileAvatar"), "/auth/me/avatar");
      $("#removeProfileAvatarBtn")?.classList.remove("hidden");
      ShareSquad.showToast("Photo updated", "success");
    } catch (err) {
      ShareSquad.showToast(err.message, "error");
    }
  });
  $("#removeProfileAvatarBtn")?.addEventListener("click", async () => {
    if (!await ShareSquad.confirmDialog({ title: "Remove profile photo?", message: "Your current profile photo will be removed.", confirmLabel: "Remove photo" })) return;
    try {
      await ShareSquad.api("/auth/me/avatar", { method: "DELETE" });
      const avatar = $("#profileAvatar");
      avatar.removeAttribute("src"); avatar.classList.add("hidden");
      $("#removeProfileAvatarBtn").classList.add("hidden");
      ShareSquad.showToast("Profile photo removed", "success");
    } catch (err) { ShareSquad.showToast(err.message, "error"); }
  });

  await loadProfile();
  await loadGeneralSettings();
  await loadGroupsList();
})();
