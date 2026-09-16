"""Resolve and persist user / group VLM settings."""
import os

from sqlalchemy.orm import Session

from . import models
from .crypto_utils import decrypt_secret, encrypt_secret
from .vlm_providers import DEFAULT_MODEL, DEFAULT_PROVIDER, is_valid_provider_model


def _defaults_account_settings(db: Session, account_id: int) -> models.AccountSettings:
    row = db.query(models.AccountSettings).filter_by(account_id=account_id).first()
    if not row:
        row = models.AccountSettings(account_id=account_id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _defaults_group_settings(
    db: Session, account_id: int, group_id: int
) -> models.GroupMemberSettings:
    row = (
        db.query(models.GroupMemberSettings)
        .filter_by(account_id=account_id, group_id=group_id)
        .first()
    )
    if not row:
        row = models.GroupMemberSettings(account_id=account_id, group_id=group_id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def account_settings_out(row: models.AccountSettings) -> dict:
    return {
        "use_own_vlm": row.use_own_vlm,
        "vlm_provider": row.vlm_provider or DEFAULT_PROVIDER,
        "vlm_model": row.vlm_model or DEFAULT_MODEL,
        "has_api_key": bool(row.vlm_api_key_encrypted),
        "email_monthly_reports": row.email_monthly_reports,
        "compact_list_view": row.compact_list_view,
    }


def group_settings_out(row: models.GroupMemberSettings) -> dict:
    return {
        "override_global_vlm": row.override_global_vlm,
        "vlm_provider": row.vlm_provider or DEFAULT_PROVIDER,
        "vlm_model": row.vlm_model or DEFAULT_MODEL,
        "has_api_key": bool(row.vlm_api_key_encrypted),
        "equal_split_default": row.equal_split_default,
    }


def get_account_settings(db: Session, account_id: int) -> models.AccountSettings:
    return _defaults_account_settings(db, account_id)


def get_group_member_settings(
    db: Session, account_id: int, group_id: int
) -> models.GroupMemberSettings:
    return _defaults_group_settings(db, account_id, group_id)


def update_account_settings(db: Session, account_id: int, payload: dict) -> models.AccountSettings:
    row = _defaults_account_settings(db, account_id)

    if "use_own_vlm" in payload:
        row.use_own_vlm = bool(payload["use_own_vlm"])
    if "email_monthly_reports" in payload:
        row.email_monthly_reports = bool(payload["email_monthly_reports"])
    if "compact_list_view" in payload:
        row.compact_list_view = bool(payload["compact_list_view"])

    provider = payload.get("vlm_provider", row.vlm_provider or DEFAULT_PROVIDER)
    model = payload.get("vlm_model", row.vlm_model or DEFAULT_MODEL)
    if not is_valid_provider_model(provider, model):
        raise ValueError("Invalid provider or model")
    row.vlm_provider = provider
    row.vlm_model = model

    api_key = payload.get("vlm_api_key")
    if api_key is not None:
        api_key = api_key.strip()
        if api_key:
            row.vlm_api_key_encrypted = encrypt_secret(api_key)
        elif payload.get("clear_api_key"):
            row.vlm_api_key_encrypted = None

    db.commit()
    db.refresh(row)
    return row


def update_group_settings(
    db: Session, account_id: int, group_id: int, payload: dict
) -> models.GroupMemberSettings:
    row = _defaults_group_settings(db, account_id, group_id)

    if "override_global_vlm" in payload:
        row.override_global_vlm = bool(payload["override_global_vlm"])
    if "equal_split_default" in payload:
        row.equal_split_default = bool(payload["equal_split_default"])

    provider = payload.get("vlm_provider", row.vlm_provider or DEFAULT_PROVIDER)
    model = payload.get("vlm_model", row.vlm_model or DEFAULT_MODEL)
    if not is_valid_provider_model(provider, model):
        raise ValueError("Invalid provider or model")
    row.vlm_provider = provider
    row.vlm_model = model

    api_key = payload.get("vlm_api_key")
    if api_key is not None:
        api_key = api_key.strip()
        if api_key:
            row.vlm_api_key_encrypted = encrypt_secret(api_key)
        elif payload.get("clear_api_key"):
            row.vlm_api_key_encrypted = None

    db.commit()
    db.refresh(row)
    return row


class ResolvedVlmConfig:
    def __init__(self, provider: str, model: str, api_key: str | None, source: str):
        self.provider = provider
        self.model = model
        self.api_key = api_key
        self.source = source  # group | account | server | none


def resolve_vlm_config(
    db: Session, account_id: int, group_id: int | None = None
) -> ResolvedVlmConfig:
    account_row = get_account_settings(db, account_id)
    if group_id:
        group_row = get_group_member_settings(db, account_id, group_id)

        if group_row.override_global_vlm:
            api_key = decrypt_secret(group_row.vlm_api_key_encrypted)
            if api_key:
                return ResolvedVlmConfig(
                    group_row.vlm_provider or DEFAULT_PROVIDER,
                    group_row.vlm_model or DEFAULT_MODEL,
                    api_key,
                    "group",
                )

    if account_row.use_own_vlm:
        api_key = decrypt_secret(account_row.vlm_api_key_encrypted)
        if api_key:
            return ResolvedVlmConfig(
                account_row.vlm_provider or DEFAULT_PROVIDER,
                account_row.vlm_model or DEFAULT_MODEL,
                api_key,
                "account",
            )

    server_key = (
        os.environ.get("SERVER_VLM_API_KEY", "").strip()
        or os.environ.get("GEMINI_API_KEY", "").strip()
    )
    if server_key:
        server_provider = os.environ.get("SERVER_VLM_PROVIDER", "google")
        server_model = os.environ.get(
            "SERVER_VLM_MODEL",
            os.environ.get("GEMINI_MODEL", DEFAULT_MODEL),
        )
        return ResolvedVlmConfig(server_provider, server_model, server_key, "server")

    return ResolvedVlmConfig(DEFAULT_PROVIDER, DEFAULT_MODEL, None, "none")
