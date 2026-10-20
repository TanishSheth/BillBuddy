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

  // Category breakdown pie chart
  const barsContainer = $("#categoryBars");
  const emptyCategory = $("#emptyCategory");
  if (catEntries.length === 0) {
    barsContainer.innerHTML = "";
    emptyCategory.classList.remove("hidden");
  } else {
    emptyCategory.classList.add("hidden");
    barsContainer.innerHTML = ShareSquad.renderCategoryPieChart(catEntries, summary.total_spent, currency);
  }

  filterAndRenderExpenses();
}

function categoryMatchesFilter(expenseCategory, filterCat) {
  if (!filterCat || filterCat === "All") return true;
  const aliases = {
    "FOOD & DINING": ["FOOD & DINING", "Food & Dining", "Food", "Dining"],
    Food: ["FOOD & DINING", "Food & Dining", "Food", "Dining"],
    GENERAL: ["GENERAL", "General", "General / Other", "Other"],
  };
  const normalizedFilter = ShareSquad.normalizeCategory(filterCat);
  const set = aliases[normalizedFilter] || [normalizedFilter];
  return set.includes(ShareSquad.normalizeCategory(expenseCategory || "GENERAL"));
}

function filterAndRenderExpenses() {
  if (!currentSummary) return;
  const filterCat = $("#categoryFilter").value;
  const tbody = $("#expensesTableBody");
  const mobileList = $("#expensesMobileList");
  const empty = $("#emptyExpenses");
  tbody.innerHTML = "";
  if (mobileList) mobileList.innerHTML = "";

  const filtered = currentSummary.expenses.filter((e) => categoryMatchesFilter(e.category, filterCat));

  if (filtered.length === 0) {
    empty.classList.remove("hidden");
    empty.textContent = filterCat && filterCat !== "All"
      ? `No expenses in “${filterCat}” for this month.`
      : "No expenses in this month yet.";
    return;
  }
  empty.classList.add("hidden");

  filtered.forEach((e) => {
    const catMark = ShareSquad.categoryMark(e.category);

    const tr = document.createElement("tr");
    tr.className = "border-b border-ink/5 hover:bg-ink/5 transition-colors";
    tr.innerHTML = `
      <td class="py-2.5 px-2 font-mono text-ink/70">${e.bill_date}</td>
      <td class="py-2.5 px-2 font-mono font-bold text-ink">${ShareSquad.escapeHtml(e.store_name)}</td>
      <td class="py-2.5 px-2"><span ${catMark}>${ShareSquad.escapeHtml(e.category)}</span></td>
      <td class="py-2.5 px-2 font-mono text-ink/50 text-[11px]">${ShareSquad.escapeHtml(e.notes || "—")}</td>
      <td class="py-2.5 px-2 font-mono font-bold text-right text-brand">${ShareSquad.formatMoney(e.total_amount, e.currency || "USD")}</td>
      <td class="py-2.5 px-2 text-right">
        <div class="flex items-center justify-end gap-1">
          <button type="button" class="edit-expense p-1 hover:text-brand" data-id="${e.id}" title="Edit"><i data-lucide="edit-2" class="w-3.5 h-3.5"></i></button>
          <button type="button" class="delete-expense p-1 hover:text-stamp" data-id="${e.id}" data-name="${ShareSquad.escapeHtml(e.store_name)}" title="Delete"><i data-lucide="trash-2" class="w-3.5 h-3.5"></i></button>
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
            <span ${ShareSquad.categoryMark(e.category, "category-tag mt-1")}>${ShareSquad.escapeHtml(e.category)}</span>
          </div>
          <div class="card-amount text-brand">${ShareSquad.formatMoney(e.total_amount, e.currency || "USD")}</div>
        </div>
        <div class="card-meta">${e.bill_date} &bull; ${ShareSquad.escapeHtml(e.notes || "No notes")}</div>
        <div class="card-actions">
          <button type="button" class="edit-expense bb-btn-secondary !py-1 !px-2 text-xs flex-1" data-id="${e.id}"><i data-lucide="edit-2" class="w-3 h-3"></i> Edit</button>
          <button type="button" class="delete-expense bb-btn-secondary !py-1 !px-2 text-xs text-stamp border-stamp/30 flex-1" data-id="${e.id}" data-name="${ShareSquad.escapeHtml(e.store_name)}"><i data-lucide="trash-2" class="w-3 h-3"></i> Delete</button>
        </div>
      `;
      mobileList.appendChild(card);
    }
  });

  lucide.createIcons();

}

async function handleExpenseAction(event) {
  const btn = event.target.closest("button.edit-expense, button.delete-expense");
  if (!btn) return;
  event.preventDefault();
  const id = Number(btn.dataset.id);
  const expense = currentSummary?.expenses?.find((item) => item.id === id);
  if (!expense) {
    ShareSquad.showToast("That expense is no longer available. Refreshing the list.", "info");
    await loadPersonalSummary($("#monthPicker").value);
    return;
  }
  if (btn.classList.contains("edit-expense")) {
    openExpenseModal(expense);
    return;
  }

  const name = expense.store_name || btn.dataset.name || "this expense";
  if (!await ShareSquad.confirmDialog({
    title: `Delete “${name}”?`,
    message: "This personal expense will be removed. You can undo for a few seconds.",
    confirmLabel: "Delete expense",
  })) return;
  const periodKey = $("#monthPicker").value;
  ShareSquad.runWithUndo({
    message: `Expense “${name}” deleted`,
    onHide: () => {
      if (currentSummary?.expenses) {
        currentSummary.expenses = currentSummary.expenses.filter((item) => item.id !== id);
        currentSummary.expense_count = currentSummary.expenses.length;
        currentSummary.total_spent = currentSummary.expenses.reduce((sum, item) => sum + Number(item.total_amount || 0), 0);
        const breakdown = {};
        currentSummary.expenses.forEach((item) => {
          const cat = ShareSquad.normalizeCategory(item.category || "General");
          breakdown[cat] = (breakdown[cat] || 0) + Number(item.total_amount || 0);
        });
        currentSummary.category_breakdown = breakdown;
        renderSummary(currentSummary);
      }
    },
    onUndo: () => loadPersonalSummary(periodKey),
    onCommit: () => ShareSquad.api(`/personal/expenses/${id}`, { method: "DELETE" }),
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
    $("#billDate")._flatpickr?.setDate(expense.bill_date, true);
    ShareSquad.clampDateInputToToday($("#billDate"));
    $("#totalAmount").value = expense.total_amount;
    ShareSquad.fillCategorySelect($("#expenseCategory"), expense.category || "General", { allowCustom: true });
    if (!ShareSquad.isKnownCategory(expense.category) && $("#expenseCategoryCustom")) {
      $("#expenseCategoryCustom").value = expense.category || "";
    }
    ShareSquad.clearOcrFieldMarks($("#expenseModal") || document);
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
    $("#billDate")._flatpickr?.setDate($("#billDate").value, true);
    ShareSquad.clampDateInputToToday($("#billDate"));
    $("#totalAmount").value = "";
    ShareSquad.fillCategorySelect($("#expenseCategory"), "FOOD & DINING", { allowCustom: true });
    if ($("#expenseCategoryCustom")) $("#expenseCategoryCustom").value = "";
    ShareSquad.clearOcrFieldMarks($("#expenseModal") || document);
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

  let prepared = file;
  try {
    prepared = await ShareSquad.prepareReceiptFile(file);
  } catch (err) {
    setScanBusy(false);
    ShareSquad.showToast(err.message || "Could not read that photo.", "error");
    return;
  }

  const preview = $("#previewImg");
  if (preview.src && preview.src.startsWith("blob:")) URL.revokeObjectURL(preview.src);
  // Preview the compressed copy — never the full phone-camera original.
  preview.src = URL.createObjectURL(prepared);
  preview.classList.remove("hidden");

  try {
    const data = await ShareSquad.uploadReceipt("/personal/receipts/upload", prepared);
    $("#uploadId").value = data.upload_id;
    if (data.extraction) {
      const ex = data.extraction;
      ShareSquad.clearOcrFieldMarks($("#expenseModal") || document);
      if (ex.store_name) $("#storeName").value = ex.store_name;
      if (ex.bill_date) {
        const billDate = ex.bill_date > ShareSquad.todayISODate() ? ShareSquad.todayISODate() : ex.bill_date;
        $("#billDate").value = billDate;
        $("#billDate")._flatpickr?.setDate(billDate, true);
        ShareSquad.clampDateInputToToday($("#billDate"));
      }
      if (ex.total_amount != null && ex.total_amount !== "") {
        $("#totalAmount").value = ex.total_amount;
      }
      if (ex.category) ShareSquad.fillCategorySelect($("#expenseCategory"), ex.category, { allowCustom: true });
      ShareSquad.fillCurrencySelect(
        $("#expenseCurrency"),
        ShareSquad.pickScannedCurrency(ex, currentSummary?.currency || "USD")
      );
      ShareSquad.markOcrFields({
        "#storeName": Boolean(ex.store_name),
        "#billDate": Boolean(ex.bill_date),
        "#totalAmount": Boolean(ex.total_amount != null && ex.total_amount !== ""),
        "#expenseCategory": Boolean(ex.category),
      });
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
    category: ShareSquad.resolveCategoryValue($("#expenseCategory"), $("#expenseCategoryCustom")),
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
    ShareSquad.goToLogin({ replace: true });
    return;
  }

  ShareSquad.fillAllCurrencySelects();
  ShareSquad.bindAllCategorySelects();

  const initialMonth = getCurrentMonthKey();
  const monthPicker = $("#monthPicker");
  monthPicker.value = initialMonth;

  monthPicker.addEventListener("change", () => {
    loadPersonalSummary(monthPicker.value);
  });

  $("#categoryFilter").addEventListener("change", filterAndRenderExpenses);
  // Delegate actions from the permanent containers. The transaction cards are
  // rebuilt after filters and refreshes, so direct listeners on each card can
  // otherwise disappear on mobile.
  $("#expensesTableBody").addEventListener("click", handleExpenseAction);
  $("#expensesMobileList").addEventListener("click", handleExpenseAction);

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

  $("#downloadDigestBtn").addEventListener("click", async () => {
    const period = monthPicker.value;
    const btn = $("#downloadDigestBtn");
    btn.disabled = true;
    try {
      const res = await ShareSquad.api(`/reports/monthly-digest/${encodeURIComponent(period)}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = `ShareSquad ${period} Monthly Digest.pdf`;
      document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
      ShareSquad.showToast("Monthly digest downloaded", "success");
    } catch (err) {
      ShareSquad.showToast(err.message || "Could not create monthly digest", "error");
    } finally { btn.disabled = false; }
  });

  $("#settingsBtn")?.addEventListener("click", () => { ShareSquad.navigateWithReturn("/settings.html"); });

  function navigateMainScreen(screen, direction = -1) {
    const destination = screen === "groups" ? "/index.html?view=groups" : "/index.html?view=friends";
    document.querySelector("main.page-main")?.animate(
      [{ transform: "translateX(0)", opacity: 1 }, { transform: `translateX(${direction > 0 ? "-4%" : "4%"})`, opacity: 0 }],
      { duration: 140, easing: "ease-out", fill: "forwards" },
    ).finished.finally(() => { window.location.href = destination; });
  }

  // Personal is the end of the non-looping sequence. A left swipe returns to
  // Groups; a right swipe stays on Personal.
  const SWIPE_BLOCK =
    ".page-header, .bottom-nav, .mobile-quick-action, .mobile-quick-actions, " +
    "input, textarea, select, button, a, label, [role='dialog'], .modal, .modal-overlay, [data-no-swipe]";
  let _swipe = null;

  function swipeIsBlocked(target) {
    if (!(target instanceof Element)) return true;
    if (target.closest(SWIPE_BLOCK)) return true;
    let node = target;
    while (node && node !== document.body) {
      if (node instanceof HTMLElement) {
        const style = window.getComputedStyle(node);
        const ox = style.overflowX;
        if ((ox === "auto" || ox === "scroll") && node.scrollWidth > node.clientWidth + 4) {
          return true;
        }
      }
      node = node.parentElement;
    }
    return false;
  }

  document.addEventListener("touchstart", (e) => {
    const touch = e.touches[0];
    if (!touch || e.touches.length !== 1 || swipeIsBlocked(e.target)) {
      _swipe = null;
      return;
    }
    if (!e.target.closest?.("main.page-main")) {
      _swipe = null;
      return;
    }
    _swipe = { x: touch.clientX, y: touch.clientY };
  }, { passive: true });

  document.addEventListener("touchmove", (e) => {
    if (!_swipe || e.touches.length !== 1) return;
    const touch = e.touches[0];
    const dx = touch.clientX - _swipe.x;
    const dy = touch.clientY - _swipe.y;
    if (Math.abs(dy) > 24 && Math.abs(dy) > Math.abs(dx)) _swipe = null;
  }, { passive: true });

  document.addEventListener("touchend", (e) => {
    if (!_swipe) return;
    const touch = e.changedTouches[0];
    const dx = touch.clientX - _swipe.x;
    const dy = touch.clientY - _swipe.y;
    _swipe = null;
    if (Math.abs(dx) < 70 || Math.abs(dx) < Math.abs(dy) * 1.35) return;
    if (dx < 0) navigateMainScreen("groups", -1);
  }, { passive: true });

  document.addEventListener("touchcancel", () => { _swipe = null; }, { passive: true });

  try {
    const me = await ShareSquad.api("/auth/me");
    const greeting = $("#userGreeting");
    if (greeting) greeting.textContent = me.account.display_name;
    $("#headerInitials").textContent = ShareSquad.initials(me.account.display_name);
    if (me.account.has_avatar) {
      ShareSquad.setAuthImage($("#headerAvatar"), "/auth/me/avatar");
      $("#headerInitials").classList.add("hidden");
    }
  } catch (_err) { /* Page data still provides the useful error state. */ }

  await loadPersonalSummary(initialMonth);
})();
