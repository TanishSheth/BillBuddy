"""Catalog of vision-language providers and models for receipt extraction."""

VLM_PROVIDERS: dict[str, dict] = {
    "google": {
        "label": "Google",
        "models": [
            {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash"},
            {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"},
            {"id": "gemini-2.0-flash", "label": "Gemini 2.0 Flash"},
            {"id": "gemini-1.5-flash", "label": "Gemini 1.5 Flash"},
            {"id": "gemini-1.5-pro", "label": "Gemini 1.5 Pro"},
        ],
    },
    "openai": {
        "label": "OpenAI",
        "models": [
            {"id": "gpt-4o", "label": "GPT-4o"},
            {"id": "gpt-4o-mini", "label": "GPT-4o Mini"},
            {"id": "gpt-4.1", "label": "GPT-4.1"},
            {"id": "gpt-4.1-mini", "label": "GPT-4.1 Mini"},
        ],
    },
    "anthropic": {
        "label": "Anthropic",
        "models": [
            {"id": "claude-sonnet-4-20250514", "label": "Claude Sonnet 4"},
            {"id": "claude-3-7-sonnet-20250219", "label": "Claude 3.7 Sonnet"},
            {"id": "claude-3-5-sonnet-20241022", "label": "Claude 3.5 Sonnet"},
            {"id": "claude-3-5-haiku-20241022", "label": "Claude 3.5 Haiku"},
        ],
    },
    "mistral": {
        "label": "Mistral",
        "models": [
            {"id": "pixtral-large-latest", "label": "Pixtral Large"},
            {"id": "pixtral-12b-2409", "label": "Pixtral 12B"},
        ],
    },
    "groq": {
        "label": "Groq",
        "models": [
            {"id": "llama-3.2-90b-vision-preview", "label": "Llama 3.2 90B Vision"},
            {"id": "llama-3.2-11b-vision-preview", "label": "Llama 3.2 11B Vision"},
        ],
    },
}

DEFAULT_PROVIDER = "google"
DEFAULT_MODEL = "gemini-2.5-flash"


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
