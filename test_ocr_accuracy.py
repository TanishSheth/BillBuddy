"""OCR accuracy regressions for MAJOR-013/019."""
import os
from pathlib import Path

os.environ.setdefault("BILLBUDDY_ENV", "development")

from app.ocr_extractor import (
    _detect_currency,
    _drop_child_promo_lines,
    _find_store_name,
    _find_total,
    _find_total_bottom_fallback,
    extract_receipt_data_ocr,
)


ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "fixtures" / "sample-receipt.png"


def test_share_squad_cafe_text_parsing():
    lines = [
        "SHARE SQUAD CAFE",
        "123 Demo Street",
        "Date: 10/07/2026",
        "Latte 5.50",
        "Muffin 3.75",
        "Tax 0.75",
        "SUBTOTAL 10.00",
        "TOTAL 13.25",
        "CARD 13.25",
    ]
    store, _ok = _find_store_name(lines, "\n".join(lines))
    assert store and "share squad" in store.lower()
    total, confident = _find_total(lines)
    assert total == 13.25
    assert confident is True


def test_ocr_misread_total_label():
    lines = ["Coffee 4.00", "T0TAL 9.50"]
    total, confident = _find_total(lines)
    assert total == 9.50
    assert confident is True


def test_bottom_fallback_when_no_total_label():
    lines = ["Item A 2.00", "Item B 3.00", "11.50"]
    total, confident = _find_total_bottom_fallback(lines)
    assert total == 11.50
    assert confident is False


def test_promo_lines_dropped_before_total():
    lines = ["Milk 3.00", "  You Saved $0.00", "TOTAL 3.00"]
    lefts = [10, 80, 10]
    cleaned, out_lefts, _boxes = _drop_child_promo_lines(lines, lefts, None)
    assert "You Saved" not in "\n".join(cleaned)
    assert out_lefts is not None
    total, ok = _find_total(cleaned)
    assert total == 3.00 and ok


def test_currency_detection_inr_and_usd():
    assert _detect_currency("GSTIN 22AAAAA0000A1Z5  Total Rs. 120.00")[0] == "INR"
    assert _detect_currency("TOTAL $13.25\nFL 33401")[0] == "USD"


def test_sample_receipt_image_ocr():
    if not FIXTURE.exists():
        from scripts.make_sample_receipt import main as make_fixture
        make_fixture()
    assert FIXTURE.exists()
    extraction, _confident, _fp = extract_receipt_data_ocr(FIXTURE.read_bytes())
    assert "share squad" in (extraction.store_name or "").lower()
    assert abs(float(extraction.total_amount) - 13.25) < 0.01
    assert extraction.bill_date == "2026-10-07"
