"""Catalog of vision-language providers and models for receipt extraction.

Keep this list aligned with each provider's current multimodal / vision APIs.
Deprecated IDs are remapped via MODEL_ALIASES so older saved settings still work.
"""

VLM_PROVIDERS: dict[str, dict] = {
    "google": {
        "label": "Google",
        "models": [
            # Current multimodal generateContent models (Oct 2026).
            # 2.5 Flash/Pro return 404 for new API keys — do not list them.
            {"id": "gemini-3.8-flash", "label": "Gemini 3.8 Flash"},
            {"id": "gemini-3.6-flash", "label": "Gemini 3.6 Flash"},
            {"id": "gemini-3.5-flash-lite", "label": "Gemini 3.5 Flash-Lite"},
        ],
    },
    "openai": {
        "label": "OpenAI",
        "models": [
            {"id": "gpt-5", "label": "GPT-5"},
            {"id": "gpt-5-mini", "label": "GPT-5 Mini"},
            {"id": "gpt-5-nano", "label": "GPT-5 Nano"},
            {"id": "gpt-4o", "label": "GPT-4o"},
            {"id": "gpt-4o-mini", "label": "GPT-4o Mini"},
        ],
    },
    "anthropic": {
        "label": "Anthropic",
        "models": [
            {"id": "claude-sonnet-5", "label": "Claude Sonnet 5"},
            {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5"},
            {"id": "claude-opus-5", "label": "Claude Opus 5"},
            {"id": "claude-sonnet-4-20250514", "label": "Claude Sonnet 4"},
        ],
    },
    "mistral": {
        "label": "Mistral",
        "models": [
            {"id": "mistral-large-2512", "label": "Mistral Large 3"},
            {"id": "mistral-medium-2508", "label": "Mistral Medium 3.1"},
            {"id": "mistral-small-2506", "label": "Mistral Small 3.2"},
            {"id": "pixtral-large-latest", "label": "Pixtral Large (legacy)"},
        ],
    },
    "groq": {
        "label": "Groq",
        "models": [
            {"id": "qwen/qwen3.8-27b", "label": "Qwen 3.8 27B Vision"},
        ],
    },
}

DEFAULT_PROVIDER = "google"
DEFAULT_MODEL = "gemini-3.8-flash"

# Old saved settings → current multimodal model that still accepts images.
MODEL_ALIASES: dict[str, dict[str, str]] = {
    "google": {
        "gemini-1.5-flash": "gemini-3.8-flash",
        "gemini-1.5-flash-latest": "gemini-3.8-flash",
        "gemini-1.5-pro": "gemini-3.8-flash",
        "gemini-1.5-pro-latest": "gemini-3.8-flash",
        "gemini-2.0-flash": "gemini-3.8-flash",
        "gemini-2.0-flash-001": "gemini-3.8-flash",
        "gemini-2.0-flash-lite": "gemini-3.5-flash-lite",
        "gemini-2.0-flash-lite-001": "gemini-3.5-flash-lite",
        # 2.5 is legacy-only; new keys get 404 — always remap.
        "gemini-2.5-flash": "gemini-3.8-flash",
        "gemini-2.5-pro": "gemini-3.8-flash",
        "gemini-2.5-flash-lite": "gemini-3.5-flash-lite",
        "gemini-pro-vision": "gemini-3.8-flash",
        "gemini-flash-latest": "gemini-3.8-flash",
    },
    "openai": {
        "gpt-4-vision-preview": "gpt-4o",
        "gpt-4-turbo": "gpt-4o",
        "gpt-4.1": "gpt-5",
        "gpt-4.1-mini": "gpt-5-mini",
        "gpt-4.1-nano": "gpt-5-nano",
    },
    "anthropic": {
        "claude-3-7-sonnet-20250219": "claude-sonnet-5",
        "claude-3-5-sonnet-20241022": "claude-sonnet-5",
        "claude-3-5-haiku-20241022": "claude-haiku-4-5",
        "claude-3-opus-20240229": "claude-opus-5",
        "claude-3-sonnet-20240229": "claude-sonnet-5",
        "claude-3-haiku-20240307": "claude-haiku-4-5",
    },
    "mistral": {
        "pixtral-12b-2409": "mistral-small-2506",
        "pixtral-large-2411": "mistral-large-2512",
    },
    "groq": {
        "llama-3.2-90b-vision-preview": "qwen/qwen3.8-27b",
        "llama-3.2-11b-vision-preview": "qwen/qwen3.8-27b",
        "llava-v1.5-7b-4096-preview": "qwen/qwen3.8-27b",
    },
}


def normalize_model_id(provider: str, model: str | None) -> str:
    """Map deprecated / retired IDs onto a current catalog model."""
    provider = (provider or DEFAULT_PROVIDER).strip().lower()
    model = (model or "").strip() or DEFAULT_MODEL
    aliases = MODEL_ALIASES.get(provider) or {}
    mapped = aliases.get(model, model)
    if is_valid_provider_model(provider, mapped):
        return mapped
    # Unknown leftover string → provider default.
    pdata = VLM_PROVIDERS.get(provider) or VLM_PROVIDERS[DEFAULT_PROVIDER]
    return pdata["models"][0]["id"]


def provider_catalog() -> list[dict]:
    return [
        {
            "id": pid,
            "label": pdata["label"],
            "models": pdata["models"],
        }
        for pid, pdata in VLM_PROVIDERS.items()
    ]


def is_valid_provider_model(provider: str, model: str) -> bool:
    pdata = VLM_PROVIDERS.get(provider)
    if not pdata:
        return False
    return any(m["id"] == model for m in pdata["models"])


def google_model_failover_chain(preferred: str | None) -> list[str]:
    """Preferred model first, then other Google catalog IDs (capacity failover)."""
    preferred = normalize_model_id("google", preferred)
    catalog = [m["id"] for m in VLM_PROVIDERS["google"]["models"]]
    ordered = [preferred] + [m for m in catalog if m != preferred]
    # De-dupe while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for mid in ordered:
        if mid not in seen:
            seen.add(mid)
            out.append(mid)
    return out
