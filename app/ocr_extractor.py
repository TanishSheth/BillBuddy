"""
Local, offline receipt extraction — no AI API key required.

Runs Tesseract OCR on the receipt image, then pulls store name / date /
total out of the raw text with a handful of regex + positional heuristics.

This is intentionally the "free tier" of extraction: fast, works without
any configuration, and good enough for most clean printed receipts. It
reports a confidence flag per field so the caller (vlm.py) can decide
whether to escalate to a paid VLM when one is configured.
"""
import io
import logging
import re
from datetime import datetime, timedelta

from PIL import Image, ImageOps

from .schemas import ReceiptExtraction

logger = logging.getLogger(__name__)

try:
    import pytesseract
    TESSERACT_AVAILABLE = True
except ImportError:  # pragma: no cover - only hit if pytesseract isn't installed
    TESSERACT_AVAILABLE = False

try:
    import cv2
    import numpy as np
    CV2_AVAILABLE = True
except ImportError:
    cv2 = None
    np = None
    CV2_AVAILABLE = False

_TESSERACT_PSM = "--oem 3 --psm 4"
_MIN_TESSERACT_CONF = 55.0
_MAX_PLAUSIBLE_TOTAL = 200000.0


# ---------------------------------------------------------------------------
# Image cleanup + OCR
# ---------------------------------------------------------------------------

def _pil_to_bgr(image_bytes: bytes):
    img = Image.open(io.BytesIO(image_bytes))
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def _order_quad(pts):
    pts = pts.reshape(4, 2).astype(np.float32)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array(
        [pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]],
        dtype=np.float32,
    )


def _find_document_quad(bgr):
    h, w = bgr.shape[:2]
    scale = 900 / max(h, w)
    small = cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else bgr
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(gray, 50, 150)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    page_area = small.shape[0] * small.shape[1]
    best = None
    best_area = 0.0
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < page_area * 0.12:
            continue
        peri = cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)
        if len(approx) == 4 and area > best_area:
            best = approx
            best_area = area
    if best is None:
        return None
    quad = _order_quad(best)
    if scale < 1:
        quad = quad / scale
    if cv2.contourArea(quad.reshape(-1, 1, 2)) < h * w * 0.12:
        return None
    return quad


def _warp_perspective(bgr, quad):
    tl, tr, br, bl = _order_quad(quad)
    width = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
    height = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
    width = max(width, 80)
    height = max(height, 80)
    dst = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(np.array([tl, tr, br, bl], dtype=np.float32), dst)
    return cv2.warpPerspective(bgr, matrix, (width, height))


def _crop_to_document(bgr):
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    _, mask = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if np.mean(mask) < 127:
        mask = cv2.bitwise_not(mask)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return bgr
    cnt = max(contours, key=cv2.contourArea)
    if cv2.contourArea(cnt) < bgr.shape[0] * bgr.shape[1] * 0.08:
        return bgr
    x, y, w, h = cv2.boundingRect(cnt)
    pad = 8
    x0, y0 = max(x - pad, 0), max(y - pad, 0)
    x1, y1 = min(x + w + pad, bgr.shape[1]), min(y + h + pad, bgr.shape[0])
    return bgr[y0:y1, x0:x1]


def _deskew(bgr):
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    inverted = cv2.bitwise_not(gray)
    thresh = cv2.threshold(inverted, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
    coords = np.column_stack(np.where(thresh > 0))
    if len(coords) < 50:
        return bgr
    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle
    if abs(angle) < 0.4 or abs(angle) > 20:
        return bgr
    h, w = bgr.shape[:2]
    center = (w // 2, h // 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    return cv2.warpAffine(bgr, matrix, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)


def _flatten_illumination(gray):
    blur = cv2.GaussianBlur(gray, (0, 0), sigmaX=25, sigmaY=25)
    blur = np.maximum(blur, 1)
    flat = cv2.divide(gray, blur, scale=255)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(flat)


def _denoise_unsharp(gray):
    den = cv2.medianBlur(gray, 3)
    den = cv2.bilateralFilter(den, 5, 40, 40)
    blur = cv2.GaussianBlur(den, (0, 0), 1.2)
    sharp = cv2.addWeighted(den, 1.45, blur, -0.45, 0)
    return np.clip(sharp, 0, 255).astype(np.uint8)


def _sauvola(gray, window: int = 25, k: float = 0.2):
    if window % 2 == 0:
        window += 1
    mean = cv2.boxFilter(gray.astype(np.float32), -1, (window, window), normalize=True)
    sqmean = cv2.boxFilter((gray.astype(np.float32) ** 2), -1, (window, window), normalize=True)
    std = np.sqrt(np.maximum(sqmean - mean * mean, 0))
    thresh = mean * (1 + k * ((std / 128.0) - 1))
    return np.where(gray.astype(np.float32) > thresh, 255, 0).astype(np.uint8)


def _ensure_min_width(gray, min_width: int = 1400):
    if gray.shape[1] >= min_width:
        return gray
    scale = min_width / gray.shape[1]
    return cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)


def _prepare_variants(image_bytes: bytes):
    bgr = _pil_to_bgr(image_bytes)
    quad = _find_document_quad(bgr)
    if quad is not None:
        bgr = _warp_perspective(bgr, quad)
    else:
        bgr = _crop_to_document(bgr)
    bgr = _deskew(bgr)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    gray = _flatten_illumination(gray)
    gray = _denoise_unsharp(gray)
    gray = _ensure_min_width(gray)
    return [("gray", gray), ("binary", _sauvola(gray))]


def _tesseract_text_and_conf(image) -> tuple[str, float]:
    if not TESSERACT_AVAILABLE:
        return "", 0.0
    try:
        data = pytesseract.image_to_data(image, config=_TESSERACT_PSM, output_type=pytesseract.Output.DICT)
        confs = []
        for raw in data.get("conf", []):
            try:
                val = float(raw)
            except (TypeError, ValueError):
                continue
            if val >= 0:
                confs.append(val)
        text = pytesseract.image_to_string(image, config=_TESSERACT_PSM)
        mean = sum(confs) / len(confs) if confs else 0.0
        return text, mean
    except Exception as exc:
        logger.warning("Tesseract failed: %s", exc)
        return "", 0.0


def _run_ocr(image_bytes: bytes) -> tuple[str, float]:
    """Return (text, mean Tesseract word confidence 0-100)."""
    if not TESSERACT_AVAILABLE:
        logger.warning("pytesseract not installed — local OCR extraction unavailable.")
        return "", 0.0

    if CV2_AVAILABLE:
        try:
            best_text, best_conf = "", -1.0
            for name, variant in _prepare_variants(image_bytes):
                text, conf = _tesseract_text_and_conf(variant)
                logger.info("OCR variant %s mean confidence %.1f", name, conf)
                if conf > best_conf or (conf == best_conf and len(text) > len(best_text)):
                    best_text, best_conf = text, conf
            return best_text, max(best_conf, 0.0)
        except Exception as exc:
            logger.warning("OpenCV cleanup failed, using basic OCR: %s", exc)

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)
        img = img.convert("L")
        img = ImageOps.autocontrast(img)
        if img.width < 1500:
            scale = 1500 / img.width
            img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
        return _tesseract_text_and_conf(img)
    except Exception as exc:
        logger.warning("Local OCR failed: %s", exc)
        return "", 0.0


def _looks_implausible(extraction: ReceiptExtraction) -> bool:
    store = (extraction.store_name or "").strip().lower()
    if not store or store.startswith("unknown"):
        return True
    try:
        total = float(extraction.total_amount or 0)
    except (TypeError, ValueError):
        return True
    if total <= 0 or total > _MAX_PLAUSIBLE_TOTAL:
        return True
    return False


# ---------------------------------------------------------------------------
# Field extraction
# ---------------------------------------------------------------------------

_AMOUNT_RE = re.compile(r"(?<!\d)(\d{1,3}(?:,\d{3})*(?:\.\d{2})|\d+\.\d{2})(?!\d)")

_AMBIGUOUS_TOTALS = {"due", "net", "sum", "tot", "charged", "balance"}


def _norm_label_text(line: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", line.lower()).split())


def _prep_phrases(words: list[str], drop: set[str] | None = None) -> list[str]:
    drop = drop or set()
    seen: set[str] = set()
    out: list[str] = []
    for raw in words:
        phrase = _norm_label_text(raw)
        if not phrase or phrase in seen or phrase in drop:
            continue
        seen.add(phrase)
        out.append(phrase)
    out.sort(key=lambda s: (-len(s), s))
    return out


# Ignore-lists are checked first so tax / subtotal / change are never used as the total.
_TAX_PHRASES = _prep_phrases([
    "Tax", "Sales Tax", "VAT", "Value Added Tax", "GST", "Goods & Services Tax",
    "HST", "Harmonized Sales Tax", "PST", "Provincial Sales Tax", "QST",
    "Quebec Sales Tax", "CGST", "SGST", "IGST", "UTGST", "State Tax", "Local Tax",
    "City Tax", "County Tax", "Fed Tax", "Federal Tax", "Consumption Tax",
    "Service Tax", "Cess", "Swachh Bharat Cess", "Krishi Kalyan Cess", "Luxury Tax",
    "Excise", "Excise Duty", "Surcharge", "Addl Tax", "Additional Tax", "Levies",
    "Levy", "Govt Tax", "Tax Amt", "Tax Amount", "Total Tax", "Txs", "Tx",
    "Sales Tx", "Vat Amount", "Vat Amt", "Gst Amount", "Gst Amt", "Hst Amt",
    "Pst Amt", "Rounding Tax", "Non-Tax", "Taxable", "Tax 1", "Tax 2", "Tax A",
    "Tax B", "Fiscal Tax", "Municipal Tax", "Entertainment Tax",
    "sales tax", "custom duty", "customs duty", "taxation", "tariff",
])

_SUBTOTAL_PHRASES = _prep_phrases([
    "Subtotal", "Sub-Total", "Net Total", "Sub Tot", "Sub-Tot",
    "Sub Amt", "Sub Amount", "Net Amt", "Net Amount", "Before Tax", "Ex Tax",
    "Excl Tax", "Excluding Tax", "Pre-Tax", "Pre Tax", "Total Before Tax",
    "Items Total", "Goods Total", "Merchandise Total", "Mdse Total", "MDSE",
    "Line Total", "Items Subtotal", "Basket Total", "Subtotal Amount",
    "Subtotal Amt", "Amount Before Tax", "Net Items", "Product Total",
    "Menu Total", "Order Subtotal", "Ticket Subtotal", "Prev Total",
    "Preliminary Total", "Initial Total", "Base Amount", "Base Amt",
    "Taxable Amt", "Taxable Amount", "Untaxed Amount", "Untaxed Amt",
    "Net Order", "Value Before Tax", "Summary Subtotal", "Raw Total",
    "Sub Total Amount", "Sub-Total Amount", "Total Excl Tax",
    "pre-tax total", "running total", "section total", "interim total",
    "item total", "base value", "gross total",
])

_CHANGE_PHRASES = _prep_phrases([
    "Change", "Cash Change", "Change Due", "Tendered Change", "Balance Return",
    "Balance Returned", "Refund", "Refund Amount", "Amount Returned", "Cash Back",
    "Cashback", "Return Amt", "Return Amount", "Change Given", "Given Back",
    "Cash Return", "Cash Returned", "Change Cash", "Due Back", "Due To Customer",
    "Customer Change", "Dispensed", "Dispensed Amt", "Dispensed Amount",
    "Coin Change", "Notes Change", "Overpayment", "Remainder", "Residual",
    "Surplus", "Payout", "Cash Payout", "Tender Change", "Change Tendered",
    "Out Change", "Change Out", "Bal Return", "Net Change", "Final Change",
    "Leftover", "Leftover Cash", "Change Amount", "Change Amt", "Paid Out",
    "Paid Back", "Chg", "Change Tender",
])

_AMBIGUOUS_PAYMENT = {
    "cs", "cc", "c c", "crd", "dr", "qr", "p2p", "p2m",
    "bill", "bills", "notes", "coins", "charge", "card", "credit", "debit",
    "chip", "swipe", "insert", "tap", "plastic", "currency", "moneys",
    "greenbacks", "dbt", "wallet", "eft", "jcb",
}

_PAYMENT_PHRASES = _prep_phrases([
    "Cash", "Cash Tendered", "Cash Paid", "Cash Amount", "Cash Amt", "Csh",
    "Cashier Cash", "Hard Cash", "Physical Cash", "Cash Sale", "Cash Received",
    "Cash Recd", "Cash Rec'd", "Tendered", "Amount Tendered", "Amt Tendered",
    "Total Tendered", "Cash Total", "Paid Cash", "Paper Currency", "Cash Reg",
    "Register Cash", "Drawer Cash", "Cash In", "Cash Payment", "Cash Pymt",
    "Pymt Cash", "Payment Cash", "Hand Cash", "Cash Trans", "Cash Transaction",
    "Cash Drawer", "Counter Cash", "Store Cash", "Local Currency",
    "Cash Collected", "Collected Cash", "Liquid Cash", "Immediate Cash",
    "Cash Value", "Cash Net", "Cash Gross",
    "Credit Card", "Debit Card", "Visa", "Mastercard", "MasterCard", "Amex",
    "American Express", "Discover", "Rupay", "RuPay", "Maestro", "Diners",
    "Diners Club", "Card Charge", "Card Payment", "Card Pymt", "Credit Payment",
    "Debit Payment", "Card Sale", "EFTPOS", "POS Payment", "Terminal Payment",
    "Contactless", "Tap to Pay", "Apple Pay", "Google Pay", "GPay",
    "Samsung Pay", "Mobile Wallet", "Digital Wallet", "Online Payment",
    "Card Tendered", "Charged to Card", "Auth Code", "Approval Code",
    "Card Total", "Card Number", "Card No",
    "UPI", "UPI Payment", "BHIM", "BHIM UPI", "PhonePe", "Paytm", "Amazon Pay",
    "WhatsApp Pay", "Cred Pay", "QR Code", "QR Payment", "Scan QR", "Scan & Pay",
    "E-Wallet", "Wallet Payment", "Venmo", "PayPal", "Cash App", "Zelle",
    "Online Pymt", "Net Banking", "NetBanking", "Internet Banking", "E-Payment",
    "Electronic Payment", "IMPS", "NEFT", "RTGS", "Bank Transfer",
    "Direct Transfer", "Virtual Payment", "VPA", "UPI ID", "Payee VPA",
    "Instant Pay", "Mobile Pay", "Contactless Pay", "Digital Pay",
    "App Payment", "Electronic Cash",
    "Debit Sale", "Debit Pymt", "Debit Charge", "Debit Tendered", "EFT Debit",
    "Visa Debit", "Mastercard Debit", "RuPay Debit", "Interac", "Interac Debit",
    "Checking Card", "Savings Card", "ATM Card", "Check Card", "Cheque Card",
    "Bank Card", "Direct Debit", "Direct Pay", "POS Debit", "Terminal Debit",
    "Chip Debit", "Swipe Debit", "Pin Debit", "PIN Payment", "Debit Auth",
    "Debited", "Amount Debited", "Amt Debited", "Account Debit", "Local Debit",
    "Retail Debit", "Store Debit", "Card Debit", "Secured Debit", "Instant Debit",
    "Debit Trans", "Debit Transaction", "DB Card", "Dbt Card", "D-Card",
    "DR Card", "Debit Net", "Debit Total",
], drop=_AMBIGUOUS_PAYMENT)

# Leftover / statement carry-over — not the amount paid on this receipt.
_REMAINING_PHRASES = _prep_phrases([
    "Balance Remaining", "Remaining Balance", "Bal Remaining", "Bal Due",
    "Amount Remaining", "Amt Remaining", "Amount Outstanding", "Amt Outstanding",
    "Outstanding Balance", "Outstanding Bal", "Outstanding Amt", "Outstanding Amount",
    "Arrears", "Total Arrears", "Unpaid Balance", "Unpaid Bal", "Unpaid Amt",
    "Unpaid Amount", "Carried Forward", "Balance C/F", "Bal C/F", "B/F",
    "Balance B/F", "Pending Balance", "Pending Bal", "Pending Amount", "Pending Amt",
    "Account Balance", "Acct Bal", "Statement Balance", "Closing Balance",
    "Closing Bal", "Amount Left", "Amt Left", "Remaining Charge", "Remaining Cost",
    "Residual Balance", "Residual Bal", "Deferred Balance", "Carryover",
], drop={"due", "bal", "balance", "remainder", "owed"})

_SAVINGS_PHRASES = _prep_phrases([
    "promotional savings", "price reduction", "price cut", "mark down", "markdown",
    "total savings", "you saved", "member savings", "store credit", "savings",
    "discount", "allowance", "deduction", "rebate", "concession", "waiver", "coupon",
])

_META_PHRASES = _prep_phrases([
    "Address", "Addr", "Location", "Store Location", "Store Address", "Venue",
    "Site", "Branch Address", "Shop Address", "Outlet Address", "Business Address",
    "Corporate Address", "Registered Office", "Reg Office", "Office Address",
    "Head Office", "Premise", "Premises", "Postal Address", "Mailing Address",
    "ZIP", "Zip Code", "Postal Code", "Postcode", "PIN Code",
    "Phone", "Phone No", "Phone No.", "Telephone", "Tel No", "Tel No.",
    "Contact No", "Contact No.", "Mobile", "Mob No", "Mob No.", "Cell No",
    "Cell No.", "Hotline", "Customer Care", "Cust Care", "Support No",
    "Support Number", "Helpline", "Office Phone", "Branch Phone", "Store Phone",
    "Shop Phone", "Landline", "Land Line", "Toll Free", "Toll-Free", "Call Us",
    "Email", "E-Mail", "Email Address", "E-Mail Address", "Email Addr",
    "Mail Address", "Electronic Mail", "Contact Email", "Support Email",
    "Customer Email", "Feedback Email", "Office Email", "Store Email",
    "Branch Email", "Corporate Email", "Info Email", "Email Us", "Help Email",
    "Sales Email", "Billing Email", "Accounts Email", "Email ID", "E-Mail ID",
])

_TOTAL_PHRASES = _prep_phrases([
    "amount before rounding", "Total", "Grand Total", "Gross Total",
    "Total Amount", "Amt Due", "Amount Due", "Total Due", "Balance Due",
    "Payable", "Total Payable", "Net Payable", "Final Total", "Final Amount",
    "Invoice Total", "Receipt Total", "Order Total", "Sale Total", "Total Sale",
    "Total Charged", "Amnt Due", "Grand Tot",
    "Net Tot", "Tot Amt", "Tot Amount", "G. Total", "G.Total", "Summary Total",
    "Check Total", "Bill Total", "Statement Total", "Amount To Pay", "Total To Pay",
    "Total Price", "Net Price", "Total Charge", "Combined Total", "Aggregate Total",
    "End Total", "Total Cost", "Total Value", "Transaction Total", "To Pay",
    "sum total", "gross amount", "due balance", "amount paid", "pay this",
    "aggregate", "tally", "Net Due", "Left to Pay", "Amount Owed", "Amount Owed",
    "Rent", "Rental", "Rent Payment", "Rent Amt", "Rent Amount", "House Rent",
    "Room Rent", "Office Rent", "Shop Rent", "Monthly Rent", "Lease", "Account Balance",
    "Lease Payment", "Lease Amt", "Lease Amount", "Lease Rent", "Tenancy",
    "Tenancy Fee", "Licence Fee", "License Fee", "Occupancy Cost", "HRA",
    "House Rent Allowance", "Base Rent", "Basic Rent", "Net Rent", "Gross Rent",
    "Flat Rent", "Apartment Rent", "Studio Rent", "Space Rental", "Venue Rental",
    "Equipment Rental", "Car Rental", "Vehicle Rent", "Hire Charges", "Hire Fee",
    "Rental Fee", "Rental Charge", "Rental Cost", "Property Rent",
    "Real Estate Rent", "Contract Rent", "Agreed Rent", "Current Rent",
    "Advance Rent", "Prepaid Rent", "Rent Due", "Rent Paid", "Rent Collected",
    "Rental Pymt",
], drop=_AMBIGUOUS_TOTALS)


def _phrase_in_line(norm: str, phrase: str) -> bool:
    compact_line = norm.replace(" ", "")
    compact_phrase = phrase.replace(" ", "").replace("-", "")
    if len(compact_phrase) >= 6 and compact_phrase in compact_line:
        return True
    pat = r"(?<![a-z0-9])" + re.escape(phrase).replace(r"\ ", r"[\s\-]*") + r"(?![a-z0-9])"
    return bool(re.search(pat, norm))


def _line_has_any_phrase(norm: str, phrases: list[str]) -> bool:
    return any(_phrase_in_line(norm, p) for p in phrases)


_IGNORE_KINDS = ("savings", "tax", "subtotal", "change", "payment", "remaining", "meta")


def _classify_amount_line(line: str) -> str | None:
    """savings | tax | subtotal | change | payment | remaining | meta | total | None"""
    norm = _norm_label_text(line)
    if not norm:
        return None
    if _line_has_any_phrase(norm, _SAVINGS_PHRASES):
        return "savings"
    if _line_has_any_phrase(norm, _TAX_PHRASES):
        return "tax"
    if _line_has_any_phrase(norm, _SUBTOTAL_PHRASES):
        return "subtotal"
    if _line_has_any_phrase(norm, _CHANGE_PHRASES):
        return "change"
    if _line_has_any_phrase(norm, _PAYMENT_PHRASES):
        return "payment"
    if _line_has_any_phrase(norm, _REMAINING_PHRASES):
        return "remaining"
    if _line_has_any_phrase(norm, _META_PHRASES):
        return "meta"
        return "meta"
    if _line_has_any_phrase(norm, _TOTAL_PHRASES):
        if re.search(r"points?\s+balance|reward\s+balance|card\s+balance", norm):
            return None
        return "total"
    return None


def _parse_amounts(text: str) -> list[float]:
    amounts = []
    for raw in _AMOUNT_RE.findall(text):
        try:
            amounts.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return amounts


def _amount_near_line(lines: list[str], idx: int) -> float | None:
    for j in (idx, idx + 1, idx - 1):
        if not (0 <= j < len(lines)):
            continue
        kind = _classify_amount_line(lines[j])
        if kind in _IGNORE_KINDS and j != idx:
            continue
        amounts = _parse_amounts(lines[j])
        if amounts:
            return amounts[-1]
    return None


def _find_total(lines: list[str]) -> tuple[float | None, bool]:
    for idx in range(len(lines) - 1, -1, -1):
        if _classify_amount_line(lines[idx]) != "total":
            continue
        amount = _amount_near_line(lines, idx)
        if amount is not None and amount > 0:
            return amount, True

    start = max(0, int(len(lines) * 0.5))
    fallback: list[float] = []
    for line in lines[start:]:
        if _classify_amount_line(line) in _IGNORE_KINDS:
            continue
        fallback.extend(a for a in _parse_amounts(line) if a > 0)
    if fallback:
        return max(fallback), False
    return None, False


_DATE_PATTERNS = [
    (r"\b(\d{4}-\d{2}-\d{2})\b", "%Y-%m-%d"),
    (r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b", None),  # ambiguous order, handled below
    (r"\b(\d{1,2}-\d{1,2}-\d{2,4})\b", None),
    (r"\b([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})\b", None),
    (r"\b(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})\b", None),
]

_MONTH_NAMES = (
    "jan feb mar apr may jun jul aug sep oct nov dec "
    "january february march april june july august september october november december"
).split()


def _try_parse_date(raw: str) -> datetime | None:
    raw = raw.strip().rstrip(",")
    candidates = []

    if "-" in raw and raw[:4].isdigit():
        candidates.append("%Y-%m-%d")
    elif "/" in raw or "-" in raw:
        sep = "/" if "/" in raw else "-"
        parts = raw.split(sep)
        if len(parts[-1]) == 2:
            candidates += [f"%m{sep}%d{sep}%y", f"%d{sep}%m{sep}%y"]
        else:
            candidates += [f"%m{sep}%d{sep}%Y", f"%d{sep}%m{sep}%Y"]
    else:
        candidates += ["%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y"]

    for fmt in candidates:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _find_date(text: str) -> tuple[str | None, bool]:
    now = datetime.now()
    earliest_sane = now - timedelta(days=365 * 5)
    latest_sane = now + timedelta(days=1)  # allow for timezone edge cases

    for pattern, _ in _DATE_PATTERNS:
        for raw in re.findall(pattern, text, flags=re.IGNORECASE):
            parsed = _try_parse_date(raw)
            if parsed and earliest_sane <= parsed <= latest_sane:
                return parsed.strftime("%Y-%m-%d"), True

    return None, False


def _find_store_name(lines: list[str]) -> tuple[str | None, bool]:
    # Store names print at the very top, before the address/items. Skip
    # lines that are mostly digits/punctuation (phone numbers, addresses,
    # separators) or too short/long to plausibly be a merchant name.
    for line in lines[:6]:
        cleaned = line.strip()
        if not (2 <= len(cleaned) <= 40):
            continue
        letters = sum(c.isalpha() for c in cleaned)
        if letters / max(len(cleaned), 1) < 0.5:
            continue
        if re.search(r"\d{3,}", cleaned):  # phone numbers, addresses, receipt #s
            continue
        return cleaned, True

    return None, False


def _find_currency(text: str) -> str | None:
    if "₹" in text or "Rs." in text or "Rs " in text or "INR" in text.upper() or "RUPEE" in text.upper():
        return "INR"
    upper = text.upper()
    codes = (
        ("CAD", "CAD"),
        ("C$", "CAD"),
        ("AUD", "AUD"),
        ("A$", "AUD"),
        ("GBP", "GBP"),
        ("EUR", "EUR"),
        ("AED", "AED"),
        ("SGD", "SGD"),
        ("CHF", "CHF"),
        ("JPY", "JPY"),
        ("CNY", "CNY"),
        ("USD", "USD"),
    )
    for needle, code in codes:
        if needle in upper or needle in text:
            return code
    if "£" in text:
        return "GBP"
    if "€" in text:
        return "EUR"
    if "¥" in text:
        return "JPY"
    if "$" in text:
        return "USD"
    return None


_CATEGORY_HINTS = (
    ("Food & Dining", ("restaurant", "cafe", "coffee", "pizza", "burger", "diner", "bar")),
    ("Groceries", ("grocery", "supermarket", "market", "trader joe", "whole foods", "walmart", "costco")),
    ("Transit", ("uber", "lyft", "metro", "transit", "parking", "gas station", "shell", "exxon")),
    ("Travel", ("airline", "airways", "hotel", "airbnb", "booking.com", "expedia")),
    ("Healthcare", ("pharmacy", "clinic", "hospital", "dental", "cvs", "walgreens")),
    ("Utilities", ("electric", "internet", "wifi", "verizon", "at&t", "comcast")),
    ("Entertainment", ("netflix", "spotify", "cinema", "theater", "amc")),
    ("Shopping", ("amazon", "target", "best buy", "ikea", "apple store")),
)


def _guess_category(text: str, store_name: str | None) -> str:
    haystack = f"{store_name or ''} {text}".lower()
    for category, needles in _CATEGORY_HINTS:
        if any(n in haystack for n in needles):
            return category
    return "General"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract_receipt_data_ocr(image_bytes: bytes) -> tuple[ReceiptExtraction, bool]:
    """
    Returns (extraction, all_fields_confident).

    all_fields_confident=False means at least one field is a low-confidence
    guess (or missing) — the caller can decide to escalate to a VLM if one
    is configured, or just hand the guess to the user to confirm/edit.
    """
    text, mean_conf = _run_ocr(image_bytes)
    lines = [l for l in text.splitlines() if l.strip()]

    store_name, store_confident = _find_store_name(lines)
    bill_date, date_confident = _find_date(text)
    total_amount, total_confident = _find_total(lines)
    currency = _find_currency(text)

    extraction = ReceiptExtraction(
        store_name=store_name or "Unknown store",
        bill_date=bill_date or datetime.now().strftime("%Y-%m-%d"),
        total_amount=total_amount or 0.0,
        currency=currency or "USD",
        category=_guess_category(text, store_name),
    )
    all_confident = (
        store_confident
        and date_confident
        and total_confident
        and bool(text.strip())
        and mean_conf >= _MIN_TESSERACT_CONF
        and not _looks_implausible(extraction)
    )
    return extraction, all_confident
