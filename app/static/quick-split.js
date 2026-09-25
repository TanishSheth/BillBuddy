document.querySelectorAll('a[href="/"]').forEach((link) => { link.href = "/index.html"; });
let activeType = "EQUAL";
let editing = null;
let groupPeople = [];
const $money = (v, c) => ShareSquad.formatMoney(v, c);
const splitId = () => new URLSearchParams(location.search).get("id");
const todayISO = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
const people = () => {
  if ($("#qsInviteMode").value === "group") {
    const chosen = groupPeople.find((person) => String(person.accountId) === $("#qsGroupPerson").value);
    return chosen ? [{ key: `account-${chosen.accountId}`, label: chosen.name, account_id: chosen.accountId }] : [];
  }
  if ($("#qsInviteMode").value === "links") return [{ key: "person", label: "The other person", email: null }];
  const email = $("#qsEmails").value.trim().toLowerCase() || (editing?.participants?.[0]?.email || "");
  return email ? [{ key: email, label: ShareSquad.maskEmail(email), email }] : [];
};

function showCreate(show) { $("#createPane").classList.toggle("hidden", !show); $("#detailPane").classList.toggle("hidden", show); }
function renderValues(saved = {}) {
  const wrap = $("#qsSplitValues");
  const hint = { EQUAL: "You and every invited person will pay an equal share.", PERCENTAGE: "Enter percentages for everyone. They must total 100%.", EXACT: "Enter exact amounts for everyone. They must equal the bill total.", SHARES: "Enter relative shares, such as 1, 1, 2." }[activeType];
  $("#qsSplitHint").textContent = hint;
  wrap.classList.toggle("hidden", activeType === "EQUAL");
  if (activeType === "EQUAL") { wrap.innerHTML = ""; return; }
  const label = activeType === "PERCENTAGE" ? "%" : activeType === "EXACT" ? "amount" : "shares";
  const rows = [{ key: "payer", name: "You" }, ...people().map((person) => ({ key: person.key, name: person.label }))];
  wrap.innerHTML = rows.map((row) => `<div class="flex items-center gap-3"><span class="text-xs flex-1 truncate">${ShareSquad.escapeHtml(row.name)}</span><input class="bb-input !w-28 split-value" data-key="${ShareSquad.escapeHtml(row.key)}" type="number" min="0" step="0.01" placeholder="${label}" value="${saved[row.key] ?? ""}"></div>`).join("");
}
function chooseType(type, saved = {}) { activeType = type; $$("#qsSplitType .split-type-pill").forEach((b) => b.classList.toggle("active", b.dataset.type === type)); renderValues(saved); }
function payload() {
  const values = Object.fromEntries($$(".split-value").map((el) => [el.dataset.key, el.value === "" ? null : Number(el.value)]));
  return { title: $("#qsTitle").value.trim(), total_amount: Number($("#qsAmount").value), currency: $("#qsCurrency").value, bill_date: $("#qsDate").value, category: $("#qsCategory").value, split_type: activeType, payer_value: activeType === "EQUAL" ? null : values.payer, silent_invite: $("#qsInviteMode").value === "group", participants: people().map((person) => ({ email: person.email, account_id: person.account_id, label: person.label, value: activeType === "EQUAL" ? null : values[person.key] })) };
}
function render(split) {
  editing = null; showCreate(false); $("#detailTitle").textContent = split.title; $("#detailMeta").textContent = `${ShareSquad.formatDate(split.bill_date)} · ${split.category} · ${split.split_type.toLowerCase()} split`; $("#detailStatus").textContent = split.status === "SETTLED" ? "Settled" : "Open"; $("#detailPayer").innerHTML = `${ShareSquad.escapeHtml(split.payer_name)} paid · their share ${$money(split.payer_share_amount, split.currency)}`; $("#detailTotal").innerHTML = $money(split.total_amount, split.currency);
  const edit = $("#editQuickSplit"); edit.classList.toggle("hidden", !split.is_owner || split.participants.some((p) => p.payment_status !== "PENDING")); edit.onclick = () => openEdit(split);
  const remove = $("#deleteQuickSplit"); remove.classList.toggle("hidden", !split.is_owner); remove.onclick = async () => { if (!await ShareSquad.confirmDialog({ title: `Delete “${split.title}”?`, message: "This private split will be permanently deleted.", confirmLabel: "Delete split" })) return; try { await ShareSquad.api(`/quick-splits/${split.id}`, { method: "DELETE" }); ShareSquad.showToast("Split deleted", "success"); location.href = "/index.html"; } catch (err) { ShareSquad.showToast(err.message || "Could not delete split", "error"); } };
  const list = $("#participantList"); list.innerHTML = split.participants.map((p) => { const label = p.display_name || ShareSquad.maskEmail(p.email); const button = p.is_me && p.payment_status === "PENDING" ? `<button class="bb-btn-primary !py-1.5 !px-3 text-[10px] mark-paid" data-id="${p.id}">Mark paid</button>` : split.is_payer && p.payment_status === "PAID" ? `<button class="bb-btn-primary !py-1.5 !px-3 text-[10px] confirm-paid" data-id="${p.id}">Confirm</button>` : `<span class="text-[10px] font-mono text-ink/50">${p.payment_status === "CONFIRMED" ? "Paid" : p.payment_status === "PAID" ? "Awaiting confirmation" : "Awaiting payment"}</span>`; return `<div class="member-row"><div><div class="font-semibold text-sm">${ShareSquad.escapeHtml(label)}${p.is_me ? " (you)" : ""}</div><div class="text-xs text-ink/60">Owes ${$money(p.share_amount, split.currency)}</div></div>${button}</div>`; }).join("");
  list.querySelectorAll(".mark-paid").forEach((b) => b.onclick = async () => render(await ShareSquad.api(`/quick-splits/${split.id}/participants/${b.dataset.id}/mark-paid`, { method: "POST" })));
  list.querySelectorAll(".confirm-paid").forEach((b) => b.onclick = async () => render(await ShareSquad.api(`/quick-splits/${split.id}/participants/${b.dataset.id}/confirm`, { method: "POST" })));
  if (split.is_payer) { $("#sharePanel").classList.remove("hidden"); const person = split.participants[0]; $("#shareLink").value = `${location.origin}/quick-join.html?code=${encodeURIComponent(split.invite_code)}&slot=${encodeURIComponent(person.invite_token || "")}`; }
}
function openEdit(split) {
  if (split.participants.length !== 1) { ShareSquad.showToast("This older multi-person split should be managed in a group.", "info"); return; }
  editing = split; showCreate(true); $("#qsTitle").value = split.title; $("#qsAmount").value = split.total_amount; ShareSquad.fillCurrencySelect($("#qsCurrency"), split.currency); $("#qsDate").value = split.bill_date; $("#qsCategory").value = split.category; const person = split.participants[0]; $("#qsInviteMode").value = person.email ? "email" : "links"; $("#qsEmails").value = ""; $("#qsEmails").placeholder = person.email ? ShareSquad.maskEmail(person.email) : "friend@email.com"; toggleInviteMode(); const saved = { payer: split.payer_share_amount, person: person.split_value, [person.email]: person.split_value }; chooseType(split.split_type, saved); $("#quickSplitForm button[type='submit']").textContent = "Save changes"; scrollTo({ top: 0, behavior: "smooth" });
}
async function load() { if (!splitId()) return showCreate(true); try { render(await ShareSquad.api(`/quick-splits/${splitId()}`)); } catch (err) { ShareSquad.showToast(err.message, "error"); } }

function toggleInviteMode() { const mode = $("#qsInviteMode").value; $("#qsEmailsWrap").classList.toggle("hidden", mode !== "email"); $("#qsGroupPeopleWrap").classList.toggle("hidden", mode !== "group"); $("#qsInviteHelp").textContent = mode === "group" ? "Choose an existing group member to add them directly." : mode === "email" ? "We will send them an invitation by email." : "A private link is easiest when you do not know their email address."; renderValues(); }
async function loadGroupPeople() {
  const [me, groups] = await Promise.all([ShareSquad.api("/auth/me"), ShareSquad.api("/groups")]);
  const details = await Promise.all(groups.map((group) => ShareSquad.api(`/groups/${group.id}`).catch(() => null)));
  const seen = new Set();
  groupPeople = details.flatMap((group) => (group?.members || []).filter((member) => member.account_id && member.account_id !== me.account.id && !seen.has(member.account_id) && (seen.add(member.account_id) || true)).map((member) => ({ accountId: member.account_id, name: member.display_name, groupName: group.name })));
  const select = $("#qsGroupPerson");
  select.innerHTML = `<option value="">Choose a person</option>${groupPeople.map((person) => `<option value="${person.accountId}">${ShareSquad.escapeHtml(person.name)} · ${ShareSquad.escapeHtml(person.groupName)}</option>`).join("")}`;
  if (!groupPeople.length) { select.innerHTML = '<option value="">No other group members yet</option>'; }
}
$("#qsInviteMode")?.addEventListener("change", toggleInviteMode);
$("#qsGroupPerson")?.addEventListener("change", () => renderValues());
$("#qsEmails")?.addEventListener("input", () => renderValues());
$$("#qsSplitType .split-type-pill").forEach((b) => b.onclick = () => chooseType(b.dataset.type));
$("#quickSplitForm")?.addEventListener("submit", async (event) => { event.preventDefault(); const msg = $("#qsMsg"); try { const split = editing ? await ShareSquad.api(`/quick-splits/${editing.id}`, { method: "PUT", body: JSON.stringify(payload()) }) : await ShareSquad.api("/quick-splits", { method: "POST", body: JSON.stringify(payload()) }); history.replaceState({}, "", `/quick-split.html?id=${split.id}`); render(split); ShareSquad.showToast("Quick split saved", "success"); } catch (err) { msg.textContent = err.message; msg.className = "text-xs font-mono balance-negative"; } });
$("#copyShareLink")?.addEventListener("click", async () => { await navigator.clipboard.writeText($("#shareLink").value); ShareSquad.showToast("Private invite link copied", "success"); });
$("#createGroupFromSplit")?.addEventListener("click", () => { location.href = "/?createGroup=1"; });
(async () => { if (!ShareSquad.getToken()) { location.href = "/login.html"; return; } ShareSquad.fillAllCurrencySelects(); if ($("#qsDate")) { $("#qsDate").max = todayISO(); $("#qsDate").value = todayISO(); } await loadGroupPeople(); toggleInviteMode(); await load(); })();
