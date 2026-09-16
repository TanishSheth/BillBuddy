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


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------

def _run_ocr(image_bytes: bytes) -> str:
    """Preprocess the image a bit and return raw OCR text. Empty string on failure."""
    if not TESSERACT_AVAILABLE:
        logger.warning("pytesseract not installed — local OCR extraction unavailable.")
        return ""

    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)  # respect phone-camera rotation
        img = img.convert("L")  # grayscale
        img = ImageOps.autocontrast(img)

        # Upscale small photos — Tesseract does much better above ~1500px wide.
        if img.width < 1500:
            scale = 1500 / img.width
            img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)

        return pytesseract.image_to_string(img)
    except Exception as exc:
        logger.warning("Local OCR failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Field extraction
# ---------------------------------------------------------------------------

_AMOUNT_RE = re.compile(r"(?<!\d)(\d{1,3}(?:,\d{3})*(?:\.\d{2})|\d+\.\d{2})(?!\d)")

# Longer phrases first so "total savings" wins over "total".
_TOTAL_PHRASES = [
    "amount before rounding",
    "total amount due",
    "total amount",
    "grand total",
    "sum total",
    "net payable",
    "gross amount",
    "amount due",
    "balance due",
    "due balance",
    "amount paid",
    "total due",
    "to pay",
    "pay this",
    "aggregate",
    "tally",
    "total",
    "balance",
]

_TAX_PHRASES = [
    "sales tax",
    "service tax",
    "excise duty",
    "custom duty",
    "customs duty",
    "surcharge",
    "taxation",
    "assessment",
    "tariff",
    "levy",
    "cgst",
    "sgst",
    "igst",
    "hst",
    "gst",
    "vat",
    "tax",
]

_SAVINGS_PHRASES = [
    "promotional savings",
    "price reduction",
    "price cut",
    "mark down",
    "markdown",
    "total savings",
    "you saved",
    "member savings",
    "store credit",
    "savings",
    "discount",
    "allowance",
    "deduction",
    "rebate",
    "concession",
    "waiver",
    "coupon",
]

_SUBTOTAL_PHRASES = [
    "amount before tax",
    "pre-tax total",
    "pre tax total",
    "running total",
    "section total",
    "interim total",
    "item total",
    "line total",
    "base amount",
    "base value",
    "gross total",
    "net total",
    "sub total",
    "subtotal",
]


def _norm_label_text(line: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", line.lower()).split())


def _phrase_in_line(norm: str, phrase: str) -> bool:
    compact_line = norm.replace(" ", "")
    compact_phrase = phrase.replace(" ", "").replace("-", "")
    if len(compact_phrase) >= 6 and compact_phrase in compact_line:
        return True
    pat = r"(?<![a-z])" + re.escape(phrase).replace(r"\ ", r"[\s\-]*") + r"(?![a-z])"
    return bool(re.search(pat, norm))


def _line_has_any_phrase(norm: str, phrases: list[str]) -> bool:
    return any(_phrase_in_line(norm, p) for p in phrases)


def _classify_amount_line(line: str) -> str | None:
    """savings | tax | subtotal | total | None"""
    norm = _norm_label_text(line)
    if not norm:
        return None
    if _line_has_any_phrase(norm, _SAVINGS_PHRASES):
        return "savings"
    if _line_has_any_phrase(norm, _TAX_PHRASES):
        return "tax"
    if _line_has_any_phrase(norm, _SUBTOTAL_PHRASES):
        return "subtotal"
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
        if kind in ("savings", "tax", "subtotal") and j != idx:
            continue
        amounts = _parse_amounts(lines[j])
        if amounts:
            return amounts[-1]
    return None


def _find_total(lines: list[str]) -> tuple[float | None, bool]:
    for idx in range(len(lines) - 1, -1, -1):
        if _classify_amount_line(lines[idx]) == "total":
            amount = _amount_near_line(lines, idx)
            if amount is not None:
                return amount, True

    start = max(0, int(len(lines) * 0.6))
    for line in reversed(lines[start:]):
        kind = _classify_amount_line(line)
        if kind in ("savings", "tax", "subtotal"):
            continue
        amounts = _parse_amounts(line)
        if amounts:
            return amounts[-1], False
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
    text = _run_ocr(image_bytes)
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
    all_confident = store_confident and date_confident and total_confident and bool(text)
    return extraction, all_confident
