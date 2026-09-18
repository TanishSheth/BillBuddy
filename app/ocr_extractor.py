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
from difflib import SequenceMatcher

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

# PSM 6: one uniform text block. PSM 4 merges receipt columns (TOTAL ..... 22.25)
# across the dotted leader into footer strings.
_TESSERACT_PSM = "--oem 3 --psm 6"
_MIN_TESSERACT_CONF = 55.0
_MAX_PLAUSIBLE_TOTAL = 200000.0
_UNLABELED_TOTAL_CAP = 10000.0
_MONEY_TOL = 0.06


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


def _soften_thermal_ink(gray):
    """Light speckle removal only. Unsharp + bilateral split thermal pin-dots (3→8, I→|)."""
    return cv2.medianBlur(gray, 3)


def _bridge_thermal_dots(img):
    """Horizontal close so 58mm/80mm heated-pin gaps join into strokes."""
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 1))
    if float(np.mean(img)) > 127:
        return cv2.bitwise_not(cv2.morphologyEx(cv2.bitwise_not(img), cv2.MORPH_CLOSE, kernel))
    return cv2.morphologyEx(img, cv2.MORPH_CLOSE, kernel)


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


def _looks_like_digital_capture(bgr) -> bool:
    """Phone/portal screenshots: dark UI, sharp contrast, no paper quad."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    mean = float(np.mean(gray))
    h, w = gray.shape[:2]
    if h < 40 or w < 40:
        return False
    border = np.concatenate([gray[:6, :].ravel(), gray[-6:, :].ravel(), gray[:, :6].ravel(), gray[:, -6:].ravel()])
    interior = gray[h // 10: h - h // 10, w // 10: w - w // 10]
    if interior.size == 0:
        return False
    if mean < 110:
        return True
    if float(np.mean(border)) < 95 and float(np.std(interior)) > 35:
        return True
    return False


def _prepare_screenshot_variants(bgr):
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    if float(np.mean(gray)) < 127:
        gray = cv2.bitwise_not(gray)
    gray = _ensure_min_width(gray)
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return [("screen_gray", gray), ("screen_bin", otsu)]


def _prepare_paper_variants(bgr):
    quad = _find_document_quad(bgr)
    if quad is not None:
        bgr = _warp_perspective(bgr, quad)
    else:
        bgr = _crop_to_document(bgr)
    bgr = _deskew(bgr)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    gray = _flatten_illumination(gray)
    gray = _soften_thermal_ink(gray)
    gray = _bridge_thermal_dots(gray)
    gray = _ensure_min_width(gray)
    binary = _bridge_thermal_dots(_sauvola(gray))
    return [("gray", gray), ("binary", binary)]


def _prepare_variants(image_bytes: bytes):
    bgr = _pil_to_bgr(image_bytes)
    if _looks_like_digital_capture(bgr):
        logger.info("OCR treating image as a digital screenshot (skip paper crop / lighting flatten)")
        return _prepare_screenshot_variants(bgr)
    return _prepare_paper_variants(bgr)


def _group_tokens_into_lines(data: dict) -> tuple[list[str], int, float]:
    """Join Tesseract tokens that share a horizontal band so labels stay with prices."""
    n = len(data.get("text") or [])
    tokens = []
    confs: list[float] = []
    for i in range(n):
        word = str(data["text"][i] or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if conf >= 0:
            confs.append(conf)
        if not word or conf < 0:
            continue
        try:
            tokens.append({
                "text": word,
                "left": int(data["left"][i]),
                "top": int(data["top"][i]),
                "w": int(data["width"][i]),
                "h": max(int(data["height"][i]), 1),
            })
        except (TypeError, ValueError, KeyError):
            continue
    tokens.sort(key=lambda t: (t["top"], t["left"]))
    rows: list[dict] = []
    lefts: list[int] = []
    for tok in tokens:
        cy = tok["top"] + tok["h"] / 2.0
        matched = None
        for row in rows:
            if abs(cy - row["cy"]) <= max(row["h"], tok["h"]) * 0.55:
                matched = row
                break
        if matched is None:
            rows.append({"tokens": [tok], "cy": cy, "h": float(tok["h"])})
        else:
            matched["tokens"].append(tok)
            count = len(matched["tokens"])
            matched["cy"] = (matched["cy"] * (count - 1) + cy) / count
            matched["h"] = max(matched["h"], float(tok["h"]))
    rows.sort(key=lambda r: r["cy"])
    lines = []
    for row in rows:
        row["tokens"].sort(key=lambda t: t["left"])
        line = " ".join(t["text"] for t in row["tokens"]).strip()
        if line:
            lines.append(line)
            lefts.append(row["tokens"][0]["left"])
    mean = sum(confs) / len(confs) if confs else 0.0
    return lines, len(tokens), mean, lefts


def _tesseract_pass(image) -> tuple[str, float, list[str], int, list[int]]:
    empty = ("", 0.0, [], 0, [])
    if not TESSERACT_AVAILABLE:
        return empty
    try:
        data = pytesseract.image_to_data(image, config=_TESSERACT_PSM, output_type=pytesseract.Output.DICT)
        lines, token_count, mean, lefts = _group_tokens_into_lines(data)
        text = "\n".join(lines)
        return text, mean, lines, token_count, lefts
    except Exception as exc:
        logger.warning("Tesseract failed: %s", exc)
        return empty


_ANCHOR_RE = re.compile(
    r"\b(?:total|subtotal|sub-tot|tax|gst|hst|pst|vat|change|cash|qty|item|amount due|balance)\b|[$₹€£]|\busd\b|\bcad\b|\binr\b",
    re.I,
)
_DATE_HINT_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-](?:\d{4}|\d{2})\b")
_MONEY_HINT_RE = re.compile(r"\d+\.\d{2}")


def _variant_score(text: str, mean_conf: float, token_count: int) -> float:
    """Prefer a full receipt (TOTAL/TAX/dates/money) over a sparse high-confidence hallucination."""
    if not (text or "").strip():
        return -1.0
    anchors = len(_ANCHOR_RE.findall(text))
    dates = len(_DATE_HINT_RE.findall(text))
    money = len(_MONEY_HINT_RE.findall(text))
    return (
        anchors * 14.0
        + dates * 10.0
        + min(money, 15) * 5.0
        + min(len(text), 2500) / 180.0
        + min(token_count, 120) * 0.25
        + mean_conf * 0.12
    )


def _run_ocr(image_bytes: bytes) -> tuple[str, float, list[str], list[int]]:
    """Return (text, mean Tesseract word confidence 0-100, layout lines, line x-offsets)."""
    empty = ("", 0.0, [], [])
    if not TESSERACT_AVAILABLE:
        logger.warning("pytesseract not installed — local OCR extraction unavailable.")
        return empty

    if CV2_AVAILABLE:
        try:
            best = ("", -1.0, [], [], -1.0)
            for name, variant in _prepare_variants(image_bytes):
                text, conf, lines, ntok, lefts = _tesseract_pass(variant)
                score = _variant_score(text, conf, ntok)
                logger.info("OCR variant %s mean confidence %.1f score %.1f", name, conf, score)
                if score > best[4]:
                    best = (text, conf, lines, lefts, score)
            if best[4] >= 0:
                return best[0], max(best[1], 0.0), best[2], best[3]
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
        text, conf, lines, _ntok, lefts = _tesseract_pass(img)
        return text, conf, lines, lefts
    except Exception as exc:
        logger.warning("Local OCR failed: %s", exc)
        return empty


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
    "Visa Purchase", "Mastercard Purchase", "Debit Purchase", "Credit Purchase",
    "Card Purchase",
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
    "Acct Bal", "Closing Bal", "Amount Left", "Amt Left", "Remaining Charge", "Remaining Cost",
    "Residual Balance", "Residual Bal", "Deferred Balance", "Carryover",
], drop={"due", "bal", "balance", "remainder", "owed"})

_SAVINGS_PHRASES = _prep_phrases([
    "promotional savings", "price reduction", "price cut", "mark down", "markdown",
    "total savings", "you saved", "you save", "member savings", "store credit", "savings",
    "savings:", "bogo", "buy one get one",
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
    "Current Charges", "Current Charge", "Statement Balance", "Closing Balance",
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
    if re.search(r"\*\s*\d{3,4}\b", line) and _parse_amounts(line):
        return "payment"
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


_REGISTER_LINE_RE = re.compile(
    r"(?:\*\s*\d{3,})"
    r"|(?:\b(?:FS|STR|REG|TRN|CSHR|TRANS|CASHIER|OP#)\d*\b)"
    r"|(?:\d{3,}/\d{2,}/\d{2,})",
    re.I,
)


def _is_register_line(line: str) -> bool:
    """Register / store-system footers like *1935 FS67/003/006 — not a bill total."""
    return bool(_REGISTER_LINE_RE.search(line or ""))


def _amounts_close(a: float, b: float, tol: float = _MONEY_TOL) -> bool:
    return abs(a - b) <= max(tol, 0.01)


def _plausible_receipt_total(amount: float, labeled: bool) -> bool:
    if amount <= 0 or amount > _MAX_PLAUSIBLE_TOTAL:
        return False
    if not labeled and amount >= _UNLABELED_TOTAL_CAP:
        return False
    return True


def _amount_near_line(lines: list[str], idx: int) -> float | None:
    for j in (idx, idx + 1, idx - 1):
        if not (0 <= j < len(lines)):
            continue
        if _is_register_line(lines[j]) and j != idx:
            continue
        kind = _classify_amount_line(lines[j])
        if kind in _IGNORE_KINDS and j != idx:
            continue
        amounts = _parse_amounts(lines[j])
        if amounts:
            return amounts[-1]
    return None


def _kind_amounts(lines: list[str]) -> dict[str, list[float]]:
    buckets = {"subtotal": [], "tax": [], "total": [], "payment": [], "change": []}
    for i, line in enumerate(lines):
        if _is_register_line(line):
            continue
        kind = _classify_amount_line(line)
        if kind not in buckets:
            continue
        amount = _amount_near_line(lines, i)
        if amount is not None and amount > 0:
            buckets[kind].append(amount)
    return buckets


def _tax_candidates(sub: float, taxes: list[float]) -> list[float]:
    out = [0.0]
    if not taxes:
        return out
    out.append(taxes[-1])
    if len(taxes) > 1:
        small = [t for t in taxes if sub <= 0 or t < sub]
        if small:
            out.append(round(sum(small), 2))
    # unique preserve order
    seen: set[float] = set()
    uniq: list[float] = []
    for t in out:
        key = round(t, 2)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(t)
    return uniq


def _find_total(lines: list[str]) -> tuple[float | None, bool]:
    buckets = _kind_amounts(lines)

    for tot in reversed(buckets["total"]):
        if not _plausible_receipt_total(tot, True):
            continue
        for sub in buckets["subtotal"]:
            for tax in _tax_candidates(sub, buckets["tax"]):
                if _amounts_close(sub + tax, tot):
                    return tot, True
        for cash in buckets["payment"]:
            for chg in buckets["change"]:
                inferred = round(cash - chg, 2)
                if inferred > 0 and _amounts_close(inferred, tot):
                    return tot, True

    if buckets["subtotal"]:
        for sub in reversed(buckets["subtotal"]):
            for tax in _tax_candidates(sub, buckets["tax"]):
                inferred = round(sub + tax, 2)
                if inferred <= 0:
                    continue
                for tot in reversed(buckets["total"]):
                    if _amounts_close(inferred, tot):
                        return tot, True
                if tax > 0 and _plausible_receipt_total(inferred, True):
                    return inferred, True

    for tot in reversed(buckets["total"]):
        if _plausible_receipt_total(tot, True):
            return tot, True

    start = max(0, int(len(lines) * 0.5))
    fallback: list[float] = []
    for line in lines[start:]:
        if _is_register_line(line):
            continue
        if _classify_amount_line(line) in _IGNORE_KINDS:
            continue
        fallback.extend(
            a for a in _parse_amounts(line)
            if _plausible_receipt_total(a, False)
        )
    if fallback:
        return max(fallback), False
    return None, False


_DATE_PATTERNS = [
    (r"\b(\d{4}-\d{2}-\d{2})\b", "%Y-%m-%d"),
    # Year is exactly 2 or 4 digits so a timestamp (06:49PM) cannot glue onto 26.
    (r"\b(\d{1,2}/\d{1,2}/\d{4})\b", None),
    (r"\b(\d{1,2}/\d{1,2}/\d{2})(?!\d)", None),
    (r"\b(\d{1,2}-\d{1,2}-\d{4})\b", None),
    (r"\b(\d{1,2}-\d{1,2}-\d{2})(?!\d)", None),
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


def _drop_child_promo_lines(lines: list[str], lefts: list[int] | None) -> list[str]:
    """Skip indented BOGO / You saved children and $0.00 promo rows when picking a total."""
    if not lines:
        return lines
    lefts = lefts or [0] * len(lines)
    span = max(max(lefts) if lefts else 1, 1)
    out = []
    for line, left in zip(lines, lefts):
        if left > span * 0.22 and re.search(r"you saved|saved:|bogo|\$0\.00|\b0\.00\b", line, re.I):
            continue
        if re.search(r"you saved", line, re.I):
            continue
        amounts = _parse_amounts(line)
        if amounts and all(abs(a) < 0.005 for a in amounts) and _classify_amount_line(line) not in ("total", "subtotal", "tax"):
            continue
        out.append(line)
    return out


def _partial_ratio(needle: str, haystack: str) -> int:
    needle = (needle or "").lower().strip()
    haystack = (haystack or "").lower()
    if not needle or not haystack:
        return 0
    if needle in haystack:
        return 100
    n = len(needle)
    if len(haystack) < n:
        return int(SequenceMatcher(None, needle, haystack).ratio() * 100)
    best = 0.0
    for i in range(len(haystack) - n + 1):
        best = max(best, SequenceMatcher(None, needle, haystack[i:i + n]).ratio())
        if best >= 0.99:
            return 100
    return int(best * 100)


_KNOWN_VENDORS = [
    {"name": "Publix", "phones": {"5613625305"}, "stores": {"0553", "1708", "553"}, "aliases": ["publix"]},
    {"name": "Walmart", "phones": set(), "stores": set(), "aliases": ["walmart", "wal-mart"], "labels": ["great value"]},
    {"name": "Costco", "phones": set(), "stores": set(), "aliases": ["costco"], "labels": ["kirkland"]},
    {"name": "Trader Joe's", "phones": set(), "stores": set(), "aliases": ["trader joe", "trader joes"], "labels": ["trader joe"]},
    {"name": "Target", "phones": set(), "stores": set(), "aliases": ["target"], "labels": ["good & gather", "good and gather"]},
    {"name": "Kroger", "phones": set(), "stores": set(), "aliases": ["kroger"], "labels": []},
    {"name": "Whole Foods", "phones": set(), "stores": set(), "aliases": ["whole foods"], "labels": ["365 whole"]},
    {"name": "Aldi", "phones": set(), "stores": set(), "aliases": ["aldi"], "labels": []},
    {"name": "Safeway", "phones": set(), "stores": set(), "aliases": ["safeway"], "labels": []},
    {"name": "CVS", "phones": set(), "stores": set(), "aliases": ["cvs"], "labels": []},
    {"name": "Walgreens", "phones": set(), "stores": set(), "aliases": ["walgreens"], "labels": []},
    {"name": "Amazon", "phones": set(), "stores": set(), "aliases": ["amazon"], "labels": []},
]

_PHONE_RE = re.compile(r"\(?\d{3}\)?[\s.\-]*\d{3}[\s.\-]*\d{4}")
_STORE_NUM_RE = re.compile(r"\bstore\s*#?\s*(\d{3,5})\b", re.I)
_ADDRESSY_RE = re.compile(r"\b(rd|road|st|street|ave|avenue|blvd|square|hwy|dr|drive|ln|lane|pkwy|parkway)\b", re.I)


def _phones_in_text(text: str) -> set[str]:
    return {re.sub(r"\D", "", m.group(0)) for m in _PHONE_RE.finditer(text or "")}


def _store_ids_in_text(text: str) -> set[str]:
    found = set()
    for m in _STORE_NUM_RE.finditer(text or ""):
        found.add(m.group(1).lstrip("0") or m.group(1))
        found.add(m.group(1))
    return found


def _vendor_from_directory(text: str) -> str | None:
    phones = _phones_in_text(text)
    store_ids = _store_ids_in_text(text)
    blob = text.lower()
    for vendor in _KNOWN_VENDORS:
        if phones & vendor.get("phones", set()):
            return vendor["name"]
        if store_ids & vendor.get("stores", set()):
            return vendor["name"]
        for alias in vendor.get("aliases", []):
            if _partial_ratio(alias, blob) >= 75:
                return vendor["name"]
        for label in vendor.get("labels", []):
            if label in blob:
                return vendor["name"]
    return None


def _header_store_candidate(lines: list[str]) -> str | None:
    for line in lines[:8]:
        cleaned = line.strip()
        if not (2 <= len(cleaned) <= 48):
            continue
        letters = sum(c.isalpha() for c in cleaned)
        if letters / max(len(cleaned), 1) < 0.5:
            continue
        if re.search(r"\d{3,}", cleaned):
            continue
        if _ADDRESSY_RE.search(cleaned):
            continue
        return cleaned
    return None


def _find_store_name(lines: list[str], text: str | None = None) -> tuple[str | None, bool]:
    blob = text or "\n".join(lines)
    known = _vendor_from_directory(blob)
    header = _header_store_candidate(lines)
    if known:
        return known, True
    if header:
        return header, True
    return None, False


_US_STATE_ZIP = re.compile(
    r"\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\s+\d{5}(?:-\d{4})?\b"
)
_CA_POSTAL = re.compile(r"\b[ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z]\s?\d[ABCEGHJ-NPRSTV-Z]\d\b", re.I)
_UK_POSTAL = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", re.I)
_CA_PROV = re.compile(r"\b(ON|BC|QC|AB|MB|SK|NS|NB|NL|PE|YT|NT|NU)\b")
_INR_MARK = re.compile(r"\b(GSTIN|CGST|SGST|IGST|HSN|Rs\.?|INR|RUPEE)\b", re.I)
_INR_LAKH = re.compile(r"\b\d{1,2},\d{2},\d{3}(?:\.\d{2})?\b")
_INR_SLASH = re.compile(r"\d+\s*/-")
_HAS_CENTS = re.compile(r"\.\d{2}\b")


def _find_currency(text: str, amount: float | None = None) -> tuple[str | None, float]:
    """Return (ISO code, confidence 0-100). Below ~85 the UI should keep the group default."""
    blob = text or ""
    upper = blob.upper()
    amount_str = f"{amount:.2f}" if amount is not None else ""

    if _US_STATE_ZIP.search(blob) or _vendor_from_directory(blob) in {"Publix", "Walmart", "Target", "Kroger", "Safeway"}:
        return "USD", 100.0
    if _PHONE_RE.search(blob) and re.search(r"\bsales tax\b|\bsubtotal\b", blob, re.I):
        return "USD", 92.0
    if _INR_MARK.search(blob) or _INR_LAKH.search(blob) or _INR_SLASH.search(blob):
        return "INR", 96.0
    if _CA_POSTAL.search(blob) or (_CA_PROV.search(upper) and re.search(r"\b(GST|HST|PST)\b", upper)):
        return "CAD", 94.0
    if re.search(r"\b(VAT\s*REG|VAT REG)\b", upper) or (_UK_POSTAL.search(upper) and "VAT" in upper):
        return "GBP", 92.0
    if re.search(r"\b(MWST|TVA|IVA)\b", upper):
        return "EUR", 90.0
    if re.search(r"\b(GSTIN|CGST|SGST)\b", upper):
        return "INR", 100.0
    if re.search(r"\b(TRN|AED|DHS|DIRHAM)\b", upper):
        return "AED", 90.0
    if re.search(r"\bABN\b", upper) and re.search(r"\bGST\b", upper):
        return "AUD", 88.0

    if "₹" in blob or "RS." in upper or "RS " in upper or "RUPEE" in upper:
        return "INR", 90.0
    if "CAD" in upper or "C$" in blob:
        return "CAD", 88.0
    if "AUD" in upper or "A$" in blob:
        return "AUD", 88.0
    if "GBP" in upper:
        return "GBP", 90.0
    if "EUR" in upper:
        return "EUR", 90.0
    if "USD" in upper or "US$" in blob:
        return "USD", 90.0

    has_cents = bool(_HAS_CENTS.search(amount_str or blob))
    if "¥" in blob or re.search(r"\bJPY\b|\bYEN\b", upper):
        if has_cents:
            return "USD", 55.0
        return "JPY", 80.0
    if "£" in blob:
        return "GBP", 70.0
    if "€" in blob:
        return "EUR", 70.0
    if "$" in blob:
        return "USD", 58.0
    if re.search(r"\b[YV]\s*\d+\.\d{2}\b", blob):
        return "USD", 52.0
    return None, 0.0


_CATEGORY_HINTS = (
    ("Food & Dining", ("restaurant", "cafe", "coffee", "pizza", "burger", "diner", "bar")),
    ("Groceries", ("grocery", "supermarket", "market", "trader joe", "whole foods", "walmart", "costco", "publix")),
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
    text, mean_conf, boxed_lines, line_lefts = _run_ocr(image_bytes)
    lines = boxed_lines or [l for l in text.splitlines() if l.strip()]
    total_lines = _drop_child_promo_lines(lines, line_lefts)

    store_name, store_confident = _find_store_name(lines, text)
    bill_date, date_confident = _find_date(text)
    total_amount, total_confident = _find_total(total_lines)
    currency, currency_confidence = _find_currency(text, total_amount)

    extraction = ReceiptExtraction(
        store_name=store_name or "Unknown store",
        bill_date=bill_date or datetime.now().strftime("%Y-%m-%d"),
        total_amount=total_amount or 0.0,
        currency=currency or "USD",
        category=_guess_category(text, store_name),
        currency_confidence=float(currency_confidence or 0),
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
