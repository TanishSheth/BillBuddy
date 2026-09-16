"""
Receipt data extraction via user-selected vision-language models.
"""
import base64
import json
import logging
from datetime import date

import httpx

from .schemas import ReceiptExtraction
from .settings_service import ResolvedVlmConfig

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = (
    "You are a receipt-parsing assistant. Look at the receipt image and "
    "extract exactly: store_name (merchant name as printed), bill_date "
    "(the transaction/purchase date, formatted strictly as YYYY-MM-DD), "
    "and total_amount (the final total charged, as a plain number with 2 "
    "decimal places, no currency symbols). If a field is unreadable, make "
    "your best reasonable guess rather than leaving it blank. "
    "Respond with JSON only: {\"store_name\": \"...\", \"bill_date\": \"YYYY-MM-DD\", \"total_amount\": 0.00}"
)

JSON_SCHEMA_HINT = {
    "type": "object",
    "properties": {
        "store_name": {"type": "string"},
        "bill_date": {"type": "string"},
        "total_amount": {"type": "number"},
    },
    "required": ["store_name", "bill_date", "total_amount"],
}


def extract_receipt_data(
    image_bytes: bytes,
    mime_type: str,
    config: ResolvedVlmConfig,
) -> tuple[ReceiptExtraction, str, str | None]:
    """
    Two-tier extraction:
      1. Always try local OCR + regex first — free, offline, instant.
      2. Only escalate to a paid VLM call if OCR came back low-confidence
         on any field AND the user/host has an API key configured.
    This means receipt scanning works even with zero API key set up, and
    when a key *is* configured it's only spent on the receipts that
    actually need the extra accuracy.
    """
    from .ocr_extractor import extract_receipt_data_ocr

    ocr_result, ocr_confident = extract_receipt_data_ocr(image_bytes)

    if ocr_confident:
        return ocr_result, "ocr", None

    if not config.api_key:
        # No key configured — hand back the OCR guess rather than a blank
        # stub. It may be partially wrong; the user can correct it in the
        # form before saving.
        return (
            ocr_result,
            "ocr",
            "Some fields could not be read confidently by local OCR. "
            "Please double check them, or add an API key in Settings for higher-accuracy extraction.",
        )

    try:
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
            return ocr_result, "ocr", f"Unsupported provider: {config.provider}. Falling back to local OCR guess."

        return ReceiptExtraction(**data), "vlm", None
    except Exception as exc:
        logger.exception("VLM extraction failed (%s/%s): %s", config.provider, config.model, exc)
        # Don't throw the receipt away — fall back to whatever OCR found.
        return (
            ocr_result,
            "ocr",
            f"VLM extraction failed ({exc}); showing local OCR guess instead. Please double check the fields.",
        )


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
            "response_schema": ReceiptExtraction,
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


def _stub() -> ReceiptExtraction:
    return ReceiptExtraction(
        store_name="Unknown Store (edit me)",
        bill_date=date.today().isoformat(),
        total_amount=0.00,
    )
