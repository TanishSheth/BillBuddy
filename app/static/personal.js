// ShareSquad Personal Monthly Expense Tracker Controller ($ comes from api.js)

let currentSummary = null;

function getCurrentMonthKey() {
  const d = new Date();
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  return `${y}-${m}`;
}

async function loadPersonalSummary(periodKey) {
  try {
    const summary = await ShareSquad.api(`/personal/summary?period_key=${periodKey}`);
    currentSummary = summary;
    renderSummary(summary);
  } catch (err) {
    ShareSquad.showToast(err.message, "error");
  }
}

function renderSummary(summary) {
  const currency = summary.currency || "USD";
  $("#totalSpendDisplay").innerHTML = ShareSquad.formatMoney(summary.total_spent, currency);
  $("#expenseCountDisplay").textContent = `${summary.expense_count} transaction${summary.expense_count === 1 ? "" : "s"}`;
  const hint = $("#totalSpendHint");
  if (hint) {
    hint.textContent = `Totals in ${currency}. Each row still shows the original currency.`;
  }

  // Top Category
  const catEntries = Object.entries(summary.category_breakdown || {}).sort((a, b) => b[1] - a[1]);
  if (catEntries.length > 0) {
    $("#topCategoryDisplay").textContent = catEntries[0][0];
    $("#topCategoryAmount").innerHTML = ShareSquad.formatMoney(catEntries[0][1], currency);
  } else {
    $("#topCategoryDisplay").textContent = "—";
    $("#topCategoryAmount").innerHTML = ShareSquad.formatMoney(0, currency);
  }

  // Category Breakdown Bars
  const barsContainer = $("#categoryBars");
  const emptyCategory = $("#emptyCategory");
  barsContainer.innerHTML = "";

  if (catEntries.length === 0) {
    emptyCategory.classList.remove("hidden");
  } else {
    emptyCategory.classList.add("hidden");
    catEntries.forEach(([cat, amt]) => {
      const pct = summary.total_spent > 0 ? ((amt / summary.total_spent) * 100).toFixed(1) : 0;
      const bar = document.createElement("div");
      bar.className = "space-y-1";
      bar.innerHTML = `
        <div class="flex justify-between items-center text-xs font-mono">
          <span class="font-semibold text-ink">${ShareSquad.escapeHtml(cat)}</span>
          <span class="text-ink/60">${ShareSquad.formatMoney(amt, currency)} (${pct}%)</span>
        </div>
        <div class="w-full h-2 bg-ink/5 rounded-full overflow-hidden">
          <div class="h-full bg-brand rounded-full transition-all duration-500" style="width: ${pct}%"></div>
        </div>
      `;
      barsContainer.appendChild(bar);
    });
  }

  filterAndRenderExpenses();
}

function filterAndRenderExpenses() {
  if (!currentSummary) return;
  const filterCat = $("#categoryFilter").value;
  const tbody = $("#expensesTableBody");
  const mobileList = $("#expensesMobileList");
  const empty = $("#emptyExpenses");
  tbody.innerHTML = "";
  if (mobileList) mobileList.innerHTML = "";

  const filtered = currentSummary.expenses.filter((e) => filterCat === "All" || e.category === filterCat);

  if (filtered.length === 0) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");

  filtered.forEach((e) => {
    const catClass = `cat-${(e.category || "General").split(" ")[0]}`;

    const tr = document.createElement("tr");
    tr.className = "border-b border-ink/5 hover:bg-ink/5 transition-colors";
    tr.innerHTML = `
      <td class="py-2.5 px-2 font-mono text-ink/70">${e.bill_date}</td>
      <td class="py-2.5 px-2 font-mono font-bold text-ink">${ShareSquad.escapeHtml(e.store_name)}</td>
      <td class="py-2.5 px-2"><span class="category-tag ${catClass}">${ShareSquad.escapeHtml(e.category)}</span></td>
      <td class="py-2.5 px-2 font-mono text-ink/50 text-[11px]">${ShareSquad.escapeHtml(e.notes || "—")}</td>
      <td class="py-2.5 px-2 font-mono font-bold text-right text-brand">${ShareSquad.formatMoney(e.total_amount, e.currency || "USD")}</td>
      <td class="py-2.5 px-2 text-right">
        <div class="flex items-center justify-end gap-1">
          <button class="edit-expense p-1 hover:text-brand" data-id="${e.id}" title="Edit"><i data-lucide="edit-2" class="w-3.5 h-3.5"></i></button>
          <button class="delete-expense p-1 hover:text-stamp" data-id="${e.id}" data-name="${ShareSquad.escapeHtml(e.store_name)}" title="Delete"><i data-lucide="trash-2" class="w-3.5 h-3.5"></i></button>
        </div>
      </td>
    `;
    tbody.appendChild(tr);

    if (mobileList) {
      const card = document.createElement("div");
      card.className = "bb-mobile-card";
      card.innerHTML = `
        <div class="card-top">
          <div>
            <div class="card-store">${ShareSquad.escapeHtml(e.store_name)}</div>
            <span class="category-tag ${catClass} mt-1">${ShareSquad.escapeHtml(e.category)}</span>
          </div>
          <div class="card-amount text-brand">${ShareSquad.formatMoney(e.total_amount, e.currency || "USD")}</div>
        </div>
        <div class="card-meta">${e.bill_date} &bull; ${ShareSquad.escapeHtml(e.notes || "No notes")}</div>
        <div class="card-actions">
          <button class="edit-expense bb-btn-secondary !py-1 !px-2 text-xs flex-1" data-id="${e.id}"><i data-lucide="edit-2" class="w-3 h-3"></i> Edit</button>
          <button class="delete-expense bb-btn-secondary !py-1 !px-2 text-xs text-stamp border-stamp/30 flex-1" data-id="${e.id}" data-name="${ShareSquad.escapeHtml(e.store_name)}"><i data-lucide="trash-2" class="w-3 h-3"></i> Delete</button>
        </div>
      `;
      mobileList.appendChild(card);
    }
  });

  lucide.createIcons();

  $$(".edit-expense").forEach((btn) => {
    btn.addEventListener("click", () => {
      const exp = currentSummary.expenses.find((x) => x.id === Number(btn.dataset.id));
      if (exp) openExpenseModal(exp);
    });
  });

  $$(".delete-expense").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const name = btn.dataset.name;
      const id = Number(btn.dataset.id);
      if (!confirm(`Delete expense "${name}"?`)) return;
      try {
        await ShareSquad.api(`/personal/expenses/${id}`, { method: "DELETE" });
        ShareSquad.showToast("Expense deleted", "info");
        await loadPersonalSummary($("#monthPicker").value);
      } catch (err) {
        ShareSquad.showToast(err.message, "error");
      }
    });
  });
}

function openExpenseModal(expense = null) {
  const modal = $("#expenseModal");
  modal.classList.remove("hidden");
  $("#formMsg").textContent = "";
  setScanBusy(false);
  $("#receiptPreviewWrap")?.classList.add("hidden");

  if (expense) {
    $("#modalTitle").textContent = "Edit Personal Expense";
    $("#expenseId").value = expense.id;
    $("#storeName").value = expense.store_name;
    $("#billDate").value = expense.bill_date;
    $("#totalAmount").value = expense.total_amount;
    $("#expenseCategory").value = expense.category || "General";
    ShareSquad.fillCurrencySelect($("#expenseCurrency"), expense.currency || "USD");
    $("#expenseNotes").value = expense.notes || "";
    $("#uploadId").value = "";
    $("#dropzone").classList.add("hidden");
    $("#receiptSourceRow")?.classList.add("hidden");
    $("#previewImg").classList.add("hidden");
    $("#previewImg").removeAttribute("src");
  } else {
    $("#modalTitle").textContent = "Add Personal Expense";
    $("#expenseId").value = "";
    $("#storeName").value = "";
    $("#billDate").value = new Date().toISOString().split("T")[0];
    $("#totalAmount").value = "";
    $("#expenseCategory").value = "Food & Dining";
    ShareSquad.fillCurrencySelect($("#expenseCurrency"), currentSummary?.currency || "USD");
    $("#expenseNotes").value = "";
    $("#uploadId").value = "";
    $("#dropzone").classList.remove("hidden");
    $("#receiptSourceRow")?.classList.remove("hidden");
    $("#previewImg").classList.add("hidden");
    $("#previewImg").removeAttribute("src");
    ["#receiptGalleryInput", "#receiptCameraInput"].forEach((sel) => {
      const el = $(sel);
      if (el) el.value = "";
    });
  }
}

function closeExpenseModal() {
  $("#expenseModal").classList.add("hidden");
}

function setScanBusy(on) {
  const wrap = $("#receiptPreviewWrap");
  if (on) wrap?.classList.remove("hidden");
  $("#scanProgress")?.classList.toggle("hidden", !on);
  $("#scanProgress")?.setAttribute("aria-busy", on ? "true" : "false");
  $("#uploadStatus")?.classList.toggle("hidden", !on);
  $("#receiptSourceRow")?.classList.toggle("is-busy", on);
  $("#dropzone")?.classList.toggle("is-busy", on);
}

async function handleReceiptUpload(file) {
  if (!file) return;
  if (!ShareSquad.isReceiptImageFile(file)) {
    ShareSquad.showToast("Please choose a photo or image file.", "error");
    return;
  }
  setScanBusy(true);

  const preview = $("#previewImg");
  if (preview.src && preview.src.startsWith("blob:")) URL.revokeObjectURL(preview.src);
  preview.src = URL.createObjectURL(file);
  preview.classList.remove("hidden");

  try {
    const data = await ShareSquad.uploadReceipt("/personal/receipts/upload", file);
    $("#uploadId").value = data.upload_id;
    if (data.extraction) {
      if (data.extraction.store_name) $("#storeName").value = data.extraction.store_name;
      if (data.extraction.bill_date) $("#billDate").value = data.extraction.bill_date;
      if (data.extraction.total_amount != null && data.extraction.total_amount !== "") {
        $("#totalAmount").value = data.extraction.total_amount;
      }
      if (data.extraction.category) $("#expenseCategory").value = data.extraction.category;
      ShareSquad.fillCurrencySelect(
        $("#expenseCurrency"),
        currentSummary?.currency || "USD"
      );
    }
    const warn = Boolean(data.message);
    ShareSquad.showToast(
      data.message || "Receipt scanned. Please verify and save.",
      warn ? "info" : "success"
    );
  } catch (err) {
    ShareSquad.showToast(err.message, "error");
  } finally {
    setScanBusy(false);
    ["#receiptGalleryInput", "#receiptCameraInput"].forEach((sel) => {
      const el = $(sel);
      if (el) el.value = "";
    });
  }
}

async function submitExpenseForm(e) {
  e.preventDefault();
  const id = $("#expenseId").value;
  const isEdit = Boolean(id);

  const payload = {
    store_name: $("#storeName").value.trim(),
    bill_date: $("#billDate").value,
    total_amount: parseFloat($("#totalAmount").value),
    category: $("#expenseCategory").value,
    currency: $("#expenseCurrency").value,
    notes: $("#expenseNotes").value.trim() || null,
    upload_id: $("#uploadId").value || null,
  };

  try {
    if (isEdit) {
      await ShareSquad.api(`/personal/expenses/${id}`, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      ShareSquad.showToast("Expense updated", "success");
    } else {
      await ShareSquad.api("/personal/expenses", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      ShareSquad.showToast("Expense added", "success");
    }
    closeExpenseModal();
    await loadPersonalSummary($("#monthPicker").value);
  } catch (err) {
    $("#formMsg").textContent = err.message;
    $("#formMsg").className = "text-xs font-mono balance-negative";
  }
}

(async function init() {
  if (!ShareSquad.getToken()) {
    window.location.href = "/login.html";
    return;
  }

  ShareSquad.fillAllCurrencySelects();

  const initialMonth = getCurrentMonthKey();
  const monthPicker = $("#monthPicker");
  monthPicker.value = initialMonth;

  monthPicker.addEventListener("change", () => {
    loadPersonalSummary(monthPicker.value);
  });

  $("#categoryFilter").addEventListener("change", filterAndRenderExpenses);

  $("#addExpenseBtn").addEventListener("click", () => openExpenseModal());
  $("#scanReceiptBtn").addEventListener("click", () => {
    openExpenseModal();
  });

  $("#closeExpenseModalBtn").addEventListener("click", closeExpenseModal);
  $("#cancelExpenseBtn").addEventListener("click", closeExpenseModal);
  $("#expenseForm").addEventListener("submit", submitExpenseForm);

  // Receipt Dropzone & File Input
  const dropzone = $("#dropzone");
  const galleryInput = $("#receiptGalleryInput");
  const cameraInput = $("#receiptCameraInput");
  dropzone.addEventListener("click", () => galleryInput?.click());
  galleryInput?.addEventListener("change", (e) => {
    handleReceiptUpload(e.target.files[0]);
    e.target.value = "";
  });
  cameraInput?.addEventListener("change", (e) => {
    handleReceiptUpload(e.target.files[0]);
    e.target.value = "";
  });
  ShareSquad.bindReceiptCamera({
    openBtn: "#pickCameraBtn",
    fileInput: "#receiptCameraInput",
    onCapture: handleReceiptUpload,
  });

  dropzone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropzone.classList.add("dragover");
  });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
  dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropzone.classList.remove("dragover");
    if (e.dataTransfer.files.length) handleReceiptUpload(e.dataTransfer.files[0]);
  });

  $("#exportPdfBtn").addEventListener("click", async () => {
    const period = monthPicker.value;
    const btn = $("#exportPdfBtn");
    btn.disabled = true;
    try {
      const res = await ShareSquad.api(`/personal/reports/${encodeURIComponent(period)}/pdf`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `Personal Expense Report ${period}.pdf`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      ShareSquad.showToast("PDF downloaded", "success");
    } catch (err) {
      ShareSquad.showToast(err.message || "Could not export PDF", "error");
    } finally {
      btn.disabled = false;
    }
  });

  $("#settingsBtn")?.addEventListener("click", () => { window.location.href = "/settings.html"; });
  $("#logoutBtn")?.addEventListener("click", () => {
    ShareSquad.removeToken();
    ShareSquad.removeStoredGroupId();
    window.location.href = "/login.html";
  });

  await loadPersonalSummary(initialMonth);
})();
