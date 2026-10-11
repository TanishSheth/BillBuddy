"""
Receipt data extraction via user-selected vision-language models.
"""
import base64
import json
import logging
import re
from datetime import date

import httpx
from pydantic import BaseModel, Field

from .schemas import ReceiptExtraction
from .settings_service import ResolvedVlmConfig

logger = logging.getLogger(__name__)


def _missing_scan_fields(extraction: ReceiptExtraction) -> list[str]:
    from .ocr_extractor import looks_garbled_merchant

    missing: list[str] = []
    store = (extraction.store_name or "").strip().lower()
    if not store or store.startswith("unknown") or looks_garbled_merchant(extraction.store_name or ""):
        missing.append("merchant")
    if not (extraction.bill_date or "").strip():
        missing.append("date")
    try:
        if float(extraction.total_amount or 0) <= 0:
            missing.append("total")
    except (TypeError, ValueError):
        missing.append("total")
    return missing


def _message_for_extraction(extraction: ReceiptExtraction) -> str | None:
    from .ocr_extractor import tesseract_status

    tess = tesseract_status()
    if not tess.get("available"):
        return (
            "Receipt scanning needs Tesseract OCR on this server "
            f"({tess.get('error') or 'not installed'}). Enter the details manually, or ask your host to install tesseract-ocr."
        )
    missing = _missing_scan_fields(extraction)
    if not missing:
        return None
    if len(missing) == 1:
        return f"Could not read the {missing[0]}. Please fill it in before saving."
    if len(missing) == 2:
        return f"Could not read {' and '.join(missing)}. Please fill those in before saving."
    return "Could not read merchant, date, or total. Please enter them before saving."

EXTRACTION_PROMPT = (
    "You are a receipt-parsing assistant. Look at the receipt image and "
    "extract: store_name (merchant name as printed), bill_date "
    "(transaction date as YYYY-MM-DD), total_amount (the amount the customer "
    "paid / grand total due — never savings, discounts, tax-only, or subtotal), "
    "currency (ISO code like USD or INR), and category (short label like "
    "Groceries or Food & Dining). "
    "For Costco receipts, return store_name exactly as Costco even if the "
    "large logo is stylised, creased, partly faint, or reads imperfectly. "
    "For any receipt, prefer the final row labelled TOTAL (including forms "
    "such as **** TOTAL) and ignore SUBTOTAL, TAX, savings, and duplicated "
    "card-approval amounts. "
    "Respond with JSON only."
)


class _VlmReceiptSchema(BaseModel):
    """Gemini rejects Optional/anyOf fields; keep every property a concrete type."""
    store_name: str
    bill_date: str
    total_amount: float
    currency: str = Field(default="USD")
    category: str = Field(default="General")


def _to_extraction(data: dict) -> ReceiptExtraction:
    amount = data.get("total_amount")
    if isinstance(amount, str):
        cleaned = re.sub(r"[^\d.]", "", amount.replace(",", ""))
        amount = float(cleaned) if cleaned else 0.0
    bill = str(data.get("bill_date") or date.today().isoformat())[:10]
    return ReceiptExtraction(
        store_name=str(data.get("store_name") or "Unknown store").strip() or "Unknown store",
        bill_date=bill,
        total_amount=float(amount or 0.0),
        currency=str(data.get("currency") or "USD").strip().upper()[:10] or "USD",
        category=str(data.get("category") or "General").strip() or "General",
        currency_confidence=0,
    )


def extract_receipt_data(
    image_bytes: bytes,
    mime_type: str,
    config: ResolvedVlmConfig,
    learned_vendors: list | None = None,
) -> tuple[ReceiptExtraction, str, str | None, dict]:
    """
    Two-tier extraction:
      1. Always run local OCR + regex (fingerprints + offline fallback).
      2. When an API key is configured, prefer the vision model so Windows
         and Linux/ARM Tesseract quirks do not produce different saved fields.
      3. Without a key, keep OCR-only (escalate path unused).
    """
    from .ocr_extractor import extract_receipt_data_ocr

    ocr_result, ocr_confident, fingerprint = extract_receipt_data_ocr(
        image_bytes, learned_vendors=learned_vendors,
    )

    missing = _missing_scan_fields(ocr_result)

    if not config.api_key:
        if not ocr_confident or missing:
            logger.warning(
                "OCR weak (missing=%s, confident=%s) and no API key resolved (source=%s)",
                missing,
                ocr_confident,
                getattr(config, "source", "?"),
            )
        return ocr_result, "ocr", _message_for_extraction(ocr_result), fingerprint

    try:
        logger.info(
            "Using VLM for receipt scan (%s/%s, source=%s, ocr_confident=%s, missing=%s)",
            config.provider,
            config.model,
            getattr(config, "source", "?"),
            ocr_confident,
            missing,
        )
        if config.provider == "google":
            data = _extract_google(image_bytes, mime_type, config.api_key, config.model)
        elif config.provider == "openai":
            data = _extract_openai_compatible(
                image_bytes, mime_type, config.api_key, config.model,
                "https://api.openai.com/v1/chat/completions",
            )
        elif config.provider == "anthropic":
            data = _extract_anthropic(image_bytes, mime_type, config.api_key, config.model)
        elif config.provider == "mistral":
            data = _extract_openai_compatible(
                image_bytes, mime_type, config.api_key, config.model,
                "https://api.mistral.ai/v1/chat/completions",
            )
        elif config.provider == "groq":
            data = _extract_openai_compatible(
                image_bytes, mime_type, config.api_key, config.model,
                "https://api.groq.com/openai/v1/chat/completions",
            )
        else:
            return ocr_result, "ocr", _message_for_extraction(ocr_result), fingerprint

        result = _to_extraction(data)
        return result, "vlm", _message_for_extraction(result), fingerprint
    except Exception as exc:
        logger.exception("VLM extraction failed (%s/%s): %s", config.provider, config.model, exc)
        msg = _message_for_extraction(ocr_result)
        fail = f"AI scan failed ({config.provider}). Using local OCR — check the fields."
        return ocr_result, "ocr", f"{fail} {msg}".strip() if msg else fail, fingerprint


def _parse_json_response(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return json.loads(text)


def _extract_google(image_bytes: bytes, mime_type: str, api_key: str, model: str) -> dict:
    from google.genai import types
    from google import genai

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            EXTRACTION_PROMPT,
        ],
        config={
            "response_mime_type": "application/json",
            "response_schema": _VlmReceiptSchema,
        },
    )
    return json.loads(response.text)


def _extract_openai_compatible(
    image_bytes: bytes, mime_type: str, api_key: str, model: str, endpoint: str
) -> dict:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": EXTRACTION_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{b64}"},
                    },
                ],
            }
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 500,
    }
    with httpx.Client(timeout=60.0) as client:
        res = client.post(
            endpoint,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
        res.raise_for_status()
        content = res.json()["choices"][0]["message"]["content"]
    return _parse_json_response(content)


def _extract_anthropic(image_bytes: bytes, mime_type: str, api_key: str, model: str) -> dict:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": model,
        "max_tokens": 500,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": EXTRACTION_PROMPT},
                ],
            }
        ],
    }
    with httpx.Client(timeout=60.0) as client:
        res = client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        res.raise_for_status()
        content = res.json()["content"][0]["text"]
    return _parse_json_response(content)
