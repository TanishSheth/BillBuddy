import os
import uuid
import shutil
import logging
import secrets
import time
import hashlib
import asyncio
import threading
import io
import json
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal
from calendar import monthrange
from typing import Dict, List, Optional

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Request, Query, status
from fastapi.responses import FileResponse, Response, StreamingResponse
from PIL import Image, ImageOps
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from apscheduler.schedulers.background import BackgroundScheduler

from . import models, schemas
from .config import (
    APP_VERSION,
    BASE_URL,
    DATA_DIR,
    DISABLE_SCHEDULER,
    IS_DEVELOPMENT,
    REPORTS_DIR,
    SECRET_KEY,
    STATIC_DIR,
    TMP_UPLOADS_DIR,
    UPLOADS_DIR,
)
from .database import engine, get_db, Base
from .auth import (
    get_current_account,
    hash_password,
    verify_password,
    validate_password_strength,
    create_access_token,
    process_pending_invites,
)
from .vlm import extract_receipt_data
from .vlm_providers import provider_catalog
from .settings_service import (
    account_settings_out,
    get_account_settings,
    get_group_member_settings,
    group_settings_out,
    resolve_vlm_config,
    update_account_settings,
    update_group_settings,
)
from .split_logic import calculate_splits, compute_balances, graph_constrained_settlements
from .fx_service import get_exchange_rates, get_conversion_rate, convert_amount, SUPPORTED_CURRENCIES
from .pdf_gen import generate_monthly_pdf, generate_personal_pdf, pdf_reports_available
from .emailer import (
    is_email_configured,
    send_invite_email,
    send_friend_invite_email,
    send_quick_split_email,
    send_monthly_report_email,
    send_password_reset_email,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BLOCKED_INVITE_DOMAINS = {
    "example.com", "example.org", "example.net", "test.com", "localhost", "invalid",
}

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/jpg"}

# Simple in-memory rate limiter for auth endpoints: { "ip:endpoint": [timestamps] }
_RATE_LIMIT_BUCKET: Dict[str, List[float]] = {}

# Per-process live update subscribers. The existing version polling remains as
# a fallback for disconnected clients and multi-process deployments.
_GROUP_EVENT_SUBSCRIBERS: Dict[int, List[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = {}
_GROUP_EVENT_LOCK = threading.Lock()


def notify_group_change(group_id: int, kind: str = "updated") -> None:
    """Tell open group pages to refresh after a committed change."""
    with _GROUP_EVENT_LOCK:
        subscribers = list(_GROUP_EVENT_SUBSCRIBERS.get(group_id, []))
    for loop, queue in subscribers:
        if loop.is_closed():
            continue
        try:
            loop.call_soon_threadsafe(queue.put_nowait, kind)
        except RuntimeError:
            pass


def _record_activity(
    db: Session, group_id: int, actor: Optional[models.Account], event_type: str,
    summary: str, entity_type: Optional[str] = None, entity_id: Optional[int] = None,
) -> None:
    db.add(models.GroupActivity(
        group_id=group_id, actor_account_id=actor.id if actor else None,
        event_type=event_type, summary=summary[:300], entity_type=entity_type, entity_id=entity_id,
    ))


def _notify_group_members(db: Session, group_id: int, message: str, link: str = "", *, exclude_account_id: Optional[int] = None) -> None:
    members = db.query(models.GroupMember).filter(
        models.GroupMember.group_id == group_id,
        or_(models.GroupMember.is_active.is_(True), models.GroupMember.is_active.is_(None)),
    ).all()
    for member in members:
        if member.account_id != exclude_account_id:
            db.add(models.Notification(
                account_id=member.account_id, group_id=group_id,
                message=message[:300], link=link or None,
            ))


def _period_has_paid_transfer(db: Session, period_id: int) -> bool:
    return db.query(models.GroupPayment.id).filter(
        models.GroupPayment.period_id == period_id,
        models.GroupPayment.status.in_(("PAID", "CONFIRMED")),
    ).first() is not None


def _period_history_is_protected(db: Session, period: models.MonthlyPeriod) -> bool:
    """Whether removing a member must retain this period's original ledger."""
    return period.status == "SETTLED" or _period_has_paid_transfer(db, period.id)


def _require_expense_editable(
    db: Session, period: models.MonthlyPeriod, account: models.Account, group_id: int,
    owner_override: bool = False,
) -> bool:
    if period.status == "SETTLED":
        raise HTTPException(400, "Cannot edit an expense from a settled period")
    if not _period_has_paid_transfer(db, period.id):
        return False
    member = require_group_member(group_id, account, db)
    if member.role != "owner" or not owner_override:
        raise HTTPException(
            409,
            "A payment has been recorded for this period. Only the owner can correct expenses; balances will be recalculated and members notified.",
        )
    return True


def check_rate_limit(request: Request, limit: int = 15, window_seconds: int = 60) -> None:
    client_ip = request.client.host if request.client else "unknown"
    path = request.url.path
    key = f"{client_ip}:{path}"
    now = time.time()
    
    bucket = _RATE_LIMIT_BUCKET.setdefault(key, [])
    bucket[:] = [t for t in bucket if now - t < window_seconds]
    if len(bucket) >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts. Please wait a minute before trying again."
        )
    bucket.append(now)


def _public_base_url(request: Request) -> str:
    """Use the request host when BASE_URL is localhost so LAN invite links work."""
    configured = BASE_URL.rstrip("/")
    if "localhost" not in configured and "127.0.0.1" not in configured:
        return configured
    host = request.headers.get("host") or request.url.netloc
    return f"{request.url.scheme}://{host}".rstrip("/")


def _invite_url(request: Request, invite_code: str) -> str:
    return f"{_public_base_url(request)}/join.html?code={invite_code}"


def _quick_split_url(request: Request, invite_code: str) -> str:
    return f"{_public_base_url(request)}/quick-join.html?code={invite_code}"


def _validate_invite_email(email: str, account: models.Account, group_id: int, db: Session) -> None:
    if email == account.email.lower():
        raise HTTPException(400, "You are already in this group")
    domain = email.rsplit("@", 1)[-1]
    if domain in BLOCKED_INVITE_DOMAINS:
        raise HTTPException(400, f"Cannot send invites to @{domain} — use a real email address")
    already = (
        db.query(models.GroupMember)
        .join(models.Account)
        .filter(models.GroupMember.group_id == group_id, models.Account.email == email, models.GroupMember.is_active.is_(True))
        .first()
    )
    if already:
        raise HTTPException(400, "This person is already in the group")


for d in (UPLOADS_DIR, TMP_UPLOADS_DIR, REPORTS_DIR):
    os.makedirs(d, exist_ok=True)

Base.metadata.create_all(bind=engine)


def _ensure_schema():
    """Add columns introduced after the first production database was created.

    Each change runs in its own transaction so one failure cannot roll back
    the others (Postgres aborts the whole transaction after the first error).
    """
    from sqlalchemy import text

    dialect = engine.dialect.name

    def _query(sql: str, params: Optional[dict] = None):
        with engine.connect() as conn:
            return conn.execute(text(sql), params or {}).fetchall()

    def _ddl(sql: str) -> None:
        with engine.begin() as conn:
            conn.execute(text(sql))

    def table_exists(table: str) -> bool:
        try:
            if dialect == "sqlite":
                rows = _query(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=:t",
                    {"t": table},
                )
            else:
                rows = _query(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = current_schema() AND table_name = :t",
                    {"t": table},
                )
            return bool(rows)
        except Exception as err:
            logger.warning("Could not inspect table %s: %s", table, err)
            return False

    def has_column(table: str, column: str) -> bool:
        try:
            if dialect == "sqlite":
                rows = _query(f"PRAGMA table_info({table})")
                return column in [r[1] for r in rows]
            rows = _query(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column",
                {"table": table, "column": column},
            )
            return bool(rows)
        except Exception as err:
            logger.warning("Could not inspect %s.%s: %s", table, column, err)
            return False

    statements = [
        ("groups", "currency", "VARCHAR(10) DEFAULT 'USD'"),
        ("expenses", "currency", "VARCHAR(10) DEFAULT 'USD'"),
        ("expenses", "category", "VARCHAR(50) DEFAULT 'General'"),
        ("expenses", "split_type", "VARCHAR(20) DEFAULT 'EQUAL'"),
        ("expenses", "fx_rate_to_group", "NUMERIC(12,6) DEFAULT 1.0"),
        ("expense_splits", "split_value", "NUMERIC(10,2)"),
        ("monthly_periods", "target_currency", "VARCHAR(10)"),
        ("monthly_periods", "settlement_fx_rate", "NUMERIC(12,6)"),
        ("account_settings", "preferred_currency", "VARCHAR(10) DEFAULT 'USD'"),
        ("pending_uploads", "account_id", "INTEGER"),
        ("accounts", "avatar_path", "VARCHAR"),
        ("groups", "cover_image_path", "VARCHAR"),
        ("group_members", "is_active", "BOOLEAN DEFAULT TRUE"),
        ("pending_uploads", "ocr_store_name", "VARCHAR(150)"),
        ("pending_uploads", "ocr_phones", "VARCHAR(200)"),
        ("pending_uploads", "ocr_store_ids", "VARCHAR(80)"),
        ("pending_uploads", "ocr_header_key", "VARCHAR(200)"),
        ("account_vendors", "name_key", "VARCHAR(80) DEFAULT ''"),
        ("account_vendors", "category", "VARCHAR(50) DEFAULT 'General'"),
        ("quick_splits", "split_type", "VARCHAR(20) DEFAULT 'EQUAL'"),
        ("quick_splits", "payer_share_amount", "NUMERIC(10,2) DEFAULT 0"),
        ("quick_splits", "created_by_account_id", "INTEGER"),
        ("quick_split_participants", "split_value", "NUMERIC(10,2)"),
        ("quick_split_participants", "invite_token", "VARCHAR(64)"),
        ("group_payments", "expense_cutoff_id", "INTEGER"),
    ]

    for table, column, spec in statements:
        try:
            if not table_exists(table) or has_column(table, column):
                continue
            _ddl(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")
            logger.info("Added missing column %s.%s", table, column)
        except Exception as err:
            logger.warning("Could not add %s.%s: %s", table, column, err)

    try:
        if table_exists("pending_uploads"):
            if dialect == "postgresql":
                row = _query(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'pending_uploads' AND column_name = 'group_id'"
                )
                if row and row[0][0] == "NO":
                    _ddl("ALTER TABLE pending_uploads ALTER COLUMN group_id DROP NOT NULL")
                    logger.info("Made pending_uploads.group_id nullable")
            elif dialect == "sqlite":
                info = _query("PRAGMA table_info(pending_uploads)")
                group_col = next((r for r in info if r[1] == "group_id"), None)
                if group_col is not None and int(group_col[3] or 0) == 1:
                    with engine.begin() as conn:
                        conn.execute(text("PRAGMA foreign_keys=OFF"))
                        conn.execute(
                            text(
                                "CREATE TABLE pending_uploads_new ("
                                "id VARCHAR(64) NOT NULL PRIMARY KEY, "
                                "group_id INTEGER, "
                                "account_id INTEGER, "
                                "file_path VARCHAR NOT NULL, "
                                "ocr_store_name VARCHAR(150), "
                                "ocr_phones VARCHAR(200), "
                                "ocr_store_ids VARCHAR(80), "
                                "ocr_header_key VARCHAR(200), "
                                "created_at DATETIME, "
                                "FOREIGN KEY(group_id) REFERENCES groups (id) ON DELETE CASCADE, "
                                "FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE"
                                ")"
                            )
                        )
                        conn.execute(
                            text(
                                "INSERT INTO pending_uploads_new "
                                "(id, group_id, account_id, file_path, created_at) "
                                "SELECT id, group_id, account_id, file_path, created_at "
                                "FROM pending_uploads"
                            )
                        )
                        conn.execute(text("DROP TABLE pending_uploads"))
                        conn.execute(text("ALTER TABLE pending_uploads_new RENAME TO pending_uploads"))
                    logger.info("Rebuilt pending_uploads so group_id can be empty")
    except Exception as err:
        logger.warning("Could not make pending_uploads.group_id nullable: %s", err)

    # Development builds before Quick Split was finalized could have created
    # participant columns intended for personal expenses. Rebuild only that
    # malformed local table; released databases create the correct shape.
    try:
        if table_exists("quick_split_participants") and has_column("quick_split_participants", "currency"):
            if dialect == "sqlite":
                with engine.begin() as conn:
                    conn.execute(text("PRAGMA foreign_keys=OFF"))
                    conn.execute(text(
                        "CREATE TABLE quick_split_participants_new ("
                        "id INTEGER NOT NULL PRIMARY KEY, quick_split_id INTEGER NOT NULL, "
                        "email VARCHAR(255) NOT NULL, account_id INTEGER, display_name VARCHAR(100), "
                        "share_amount NUMERIC(10,2) NOT NULL, payment_status VARCHAR(20) NOT NULL DEFAULT 'PENDING', "
                        "payment_marked_at DATETIME, "
                        "FOREIGN KEY(quick_split_id) REFERENCES quick_splits (id) ON DELETE CASCADE, "
                        "FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE SET NULL, "
                        "CONSTRAINT uq_quick_split_email UNIQUE (quick_split_id, email))"
                    ))
                    conn.execute(text(
                        "INSERT INTO quick_split_participants_new "
                        "(id, quick_split_id, email, account_id, display_name, share_amount, payment_status, payment_marked_at) "
                        "SELECT id, quick_split_id, email, account_id, display_name, share_amount, payment_status, payment_marked_at "
                        "FROM quick_split_participants"
                    ))
                    conn.execute(text("DROP TABLE quick_split_participants"))
                    conn.execute(text("ALTER TABLE quick_split_participants_new RENAME TO quick_split_participants"))
                logger.info("Rebuilt malformed quick_split_participants table")
            elif dialect == "postgresql":
                for column in ("currency", "category", "notes", "image_path", "created_at"):
                    if has_column("quick_split_participants", column):
                        _ddl(f"ALTER TABLE quick_split_participants DROP COLUMN {column}")
    except Exception as err:
        logger.warning("Could not repair quick_split_participants schema: %s", err)


_ensure_schema()

app = FastAPI(title="ShareSquad — Multi-User Expense Splitter")


@app.middleware("http")
async def disable_static_cache(request: Request, call_next):
    """Avoid stale HTML/JS/CSS after deploys (browsers otherwise keep old UI)."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".js", ".css")):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "app": "ShareSquad",
        "version": APP_VERSION,
        "environment": "development" if IS_DEVELOPMENT else "production",
        "pdf_reports": pdf_reports_available(),
    }


@app.get("/", include_in_schema=False)
def public_landing_page():
    """Public, indexable ShareSquad introduction; the signed-in app is /index.html."""
    return FileResponse(os.path.join(STATIC_DIR, "landing.html"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _period_key_for(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _group_reports_dir(group_id: int) -> str:
    path = os.path.join(REPORTS_DIR, str(group_id))
    os.makedirs(path, exist_ok=True)
    return path


def _personal_reports_dir(account_id: int) -> str:
    path = os.path.join(REPORTS_DIR, f"personal_{account_id}")
    os.makedirs(path, exist_ok=True)
    return path


def _group_uploads_dir(group_id: int) -> str:
    path = os.path.join(UPLOADS_DIR, str(group_id))
    os.makedirs(path, exist_ok=True)
    return path


def _personal_uploads_dir(account_id: int) -> str:
    path = os.path.join(UPLOADS_DIR, f"personal_{account_id}")
    os.makedirs(path, exist_ok=True)
    return path


def _member_is_active(member: models.GroupMember) -> bool:
    return getattr(member, "is_active", True) is not False


def _require_shareable_group(db: Session, group_id: int) -> None:
    """A shared expense needs an owner plus at least one active other member."""
    active_members = db.query(models.GroupMember.id).filter(
        models.GroupMember.group_id == group_id,
        or_(models.GroupMember.is_active.is_(True), models.GroupMember.is_active.is_(None)),
    ).count()
    if active_members < 2:
        raise HTTPException(400, "Invite at least one member before adding shared expenses")


def _save_profile_image(upload: UploadFile, dest_path: str) -> None:
    contents = upload.file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(400, f"File exceeds maximum size of {MAX_FILE_SIZE // (1024 * 1024)}MB")
    mime = (upload.content_type or "").lower()
    if mime not in ALLOWED_MIME_TYPES and not (upload.filename or "").lower().endswith(
        (".jpg", ".jpeg", ".png", ".webp")
    ):
        raise HTTPException(400, "Use a JPEG, PNG, or WEBP image")
    # Keep only a display-sized JPEG, never the original upload. This reduces
    # accidental sharing of high-resolution originals and strips EXIF metadata.
    try:
        image = Image.open(io.BytesIO(contents))
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        image.save(dest_path, format="JPEG", quality=86, optimize=True)
    except Exception as exc:
        raise HTTPException(400, "Use a readable JPEG, PNG, or WEBP image") from exc


def _public_scan_message(message: Optional[str]) -> Optional[str]:
    if not message:
        return None
    lowered = message.lower()
    if any(
        token in lowered
        for token in (
            "pydantic",
            "validation error",
            "type_unspecified",
            "traceback",
            "response_schema",
            "anyof",
            "http://",
            "https://",
            "exception",
        )
    ):
        return None
    if len(message) > 180:
        return None
    return message


def require_group_member(
    group_id: int, account: models.Account, db: Session
) -> models.GroupMember:
    member = (
        db.query(models.GroupMember)
        .filter_by(group_id=group_id, account_id=account.id)
        .first()
    )
    if not member:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not a member of this group")
    if not _member_is_active(member):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You are no longer a member of this group")
    return member


def require_group_owner(
    group_id: int, account: models.Account, db: Session
) -> models.GroupMember:
    member = require_group_member(group_id, account, db)
    if member.role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the group owner can perform this action")
    return member


def get_or_create_period(db: Session, group_id: int, period_key: str) -> models.MonthlyPeriod:
    period = (
        db.query(models.MonthlyPeriod)
        .filter_by(group_id=group_id, period_key=period_key)
        .first()
    )
    if not period:
        period = models.MonthlyPeriod(group_id=group_id, period_key=period_key, status="ACTIVE")
        db.add(period)
        db.commit()
        db.refresh(period)
    return period


def _normalize_currency(code: Optional[str], default: str = "USD") -> str:
    curr = (code or default or "USD").strip().upper()
    if curr not in SUPPORTED_CURRENCIES:
        raise HTTPException(400, f"Unsupported currency {curr}")
    return curr


def _members_as_dicts(db: Session, group_id: int, include_inactive: bool = False) -> list[dict]:
    query = db.query(models.GroupMember).filter(models.GroupMember.group_id == group_id)
    if not include_inactive:
        query = query.filter(or_(models.GroupMember.is_active.is_(True), models.GroupMember.is_active.is_(None)))
    members = query.all()
    return [{"id": m.id, "name": m.display_name} for m in members]


def _rate_to_dest(
    src: str,
    dest: str,
    base: str,
    custom_base_to_dest: Optional[float] = None,
) -> float:
    src, dest, base = src.upper(), dest.upper(), (base or dest).upper()
    if src == dest:
        return 1.0
    if custom_base_to_dest and custom_base_to_dest > 0:
        src_to_base = 1.0 if src == base else get_conversion_rate(src, base)
        return src_to_base * float(custom_base_to_dest)
    return get_conversion_rate(src, dest)


def _expenses_as_dicts(
    expenses: list[models.Expense],
    dest_currency: str = "USD",
    base_currency: Optional[str] = None,
    custom_base_to_dest: Optional[float] = None,
) -> list[dict]:
    dest = (dest_currency or "USD").upper()
    base = (base_currency or dest).upper()
    rows = []
    for e in expenses:
        src = (e.currency or base or "USD").upper()
        rate_f = _rate_to_dest(src, dest, base, custom_base_to_dest)
        converted_total, _ = convert_amount(e.total_amount, src, dest, custom_rate=rate_f)
        splits = []
        for s in e.splits:
            converted_share, _ = convert_amount(s.share_amount, src, dest, custom_rate=rate_f)
            splits.append({"user_id": s.member_id, "share_amount": float(converted_share)})
        rows.append(
            {
                "payer_id": e.payer_member_id,
                "expense_id": e.id,
                "total_amount": float(converted_total),
                "splits": splits,
                "created_at": e.created_at,
            }
        )
    return rows


def _apply_confirmed_payments(
    db: Session,
    period: models.MonthlyPeriod,
    balances: dict,
    expense_dicts: list[dict],
    destination_currency: str,
    base_currency: str,
    custom_base_to_destination: Optional[float] = None,
) -> None:
    """Apply confirmed transfers without allowing them to create a reverse debt."""
    for balance in balances.values():
        balance.setdefault("settlement_paid", 0.0)
        balance.setdefault("settlement_received", 0.0)
    payments = db.query(models.GroupPayment).filter_by(period_id=period.id, status="CONFIRMED").all()
    members = [{"id": member_id, "name": balance["name"]} for member_id, balance in balances.items()]
    applied_history: list[tuple[int, int, float]] = []

    def event_time(value):
        if value is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    payments.sort(key=lambda payment: event_time(payment.marked_paid_at or payment.created_at))
    for payment in payments:
        if payment.payer_member_id not in balances or payment.recipient_member_id not in balances:
            continue
        amount = Decimal(str(payment.amount))
        source_currency = (payment.currency or base_currency).upper()
        target_currency = destination_currency.upper()
        if source_currency != target_currency:
            amount, _ = convert_amount(
                amount, source_currency, target_currency,
                custom_rate=_rate_to_dest(source_currency, target_currency, base_currency, custom_base_to_destination),
            )
        amount_f = float(amount)
        # A payment only belongs to expenses that already existed when it was
        # made. Later bills in a reopened period are new debt, not something an
        # earlier settlement can silently clear.
        cutoff = event_time(payment.marked_paid_at or payment.created_at)
        historical_expenses = [
            expense for expense in expense_dicts
            if (payment.expense_cutoff_id is not None and expense.get("expense_id", 0) <= payment.expense_cutoff_id)
            or (payment.expense_cutoff_id is None and event_time(expense.get("created_at")) <= cutoff)
        ]
        historical_balances = compute_balances(members, historical_expenses)
        for payer_id, recipient_id, applied_amount in applied_history:
            historical_balances[payer_id]["net_balance"] = round(historical_balances[payer_id]["net_balance"] + applied_amount, 2)
            historical_balances[recipient_id]["net_balance"] = round(historical_balances[recipient_id]["net_balance"] - applied_amount, 2)

        # A transfer can only settle the payer's historical debt to this
        # recipient. This also protects legacy records with no retained bill.
        payer_owed = max(-float(historical_balances[payment.payer_member_id]["net_balance"]), 0.0)
        recipient_due = max(float(historical_balances[payment.recipient_member_id]["net_balance"]), 0.0)
        amount_f = round(min(amount_f, payer_owed, recipient_due), 2)
        if amount_f <= 0:
            continue
        applied_history.append((payment.payer_member_id, payment.recipient_member_id, amount_f))
        balances[payment.payer_member_id]["net_balance"] = round(
            balances[payment.payer_member_id]["net_balance"] + amount_f, 2
        )
        balances[payment.recipient_member_id]["net_balance"] = round(
            balances[payment.recipient_member_id]["net_balance"] - amount_f, 2
        )
        balances[payment.payer_member_id]["settlement_paid"] = round(
            balances[payment.payer_member_id]["settlement_paid"] + amount_f, 2
        )
        balances[payment.recipient_member_id]["settlement_received"] = round(
            balances[payment.recipient_member_id]["settlement_received"] + amount_f, 2
        )


def build_balances_and_settlements(
    db: Session, group_id: int, period: models.MonthlyPeriod
):
    group = db.query(models.Group).get(group_id)
    base_currency = group.currency if group and group.currency else "USD"

    # Once a payment has been recorded, the original participants and their
    # expenses are the accounting record behind that payment. Reopening the
    # period only permits an owner correction; it must not make a later member
    # removal rewrite the paid ledger.
    preserve_history = _period_history_is_protected(db, period)
    members = _members_as_dicts(db, group_id, include_inactive=preserve_history)
    active_member_ids = {member["id"] for member in members}
    expenses = (
        db.query(models.Expense)
        .filter_by(period_id=period.id)
        .order_by(models.Expense.bill_date.asc(), models.Expense.id.asc())
        .all()
    )
    # Legacy soft-removed memberships can remain in an upgraded database.
    # Treat any expense involving one as removed, matching the delete action.
    if not preserve_history:
        expenses = [
            expense for expense in expenses
            if expense.payer_member_id in active_member_ids
            and all(split.member_id in active_member_ids for split in expense.splits)
        ]
    expense_dicts = _expenses_as_dicts(expenses, base_currency, base_currency)
    raw_balances = compute_balances(members, expense_dicts)
    _apply_confirmed_payments(db, period, raw_balances, expense_dicts, base_currency, base_currency)
    raw_settlements = graph_constrained_settlements(members, expense_dicts, raw_balances)

    target_currency = (period.target_currency or "").upper() or None
    fx_rate = float(period.settlement_fx_rate) if period.settlement_fx_rate else None
    converted_balances = {}
    converted_settlements = {}

    if target_currency and target_currency != base_currency:
        if not fx_rate:
            fx_rate = get_conversion_rate(base_currency, target_currency)
        tgt_expense_dicts = _expenses_as_dicts(
            expenses,
            dest_currency=target_currency,
            base_currency=base_currency,
            custom_base_to_dest=fx_rate,
        )
        tgt_balances = compute_balances(members, tgt_expense_dicts)
        _apply_confirmed_payments(db, period, tgt_balances, tgt_expense_dicts, target_currency, base_currency, fx_rate)
        converted_balances = tgt_balances
        for s in graph_constrained_settlements(members, tgt_expense_dicts, tgt_balances):
            converted_settlements[(s["from_member_id"], s["to_member_id"])] = s["amount"]

    balances = []
    for mid, b in raw_balances.items():
        tb = converted_balances.get(mid) if converted_balances else None
        balances.append({
            **b,
            "currency": base_currency,
            "converted_total_paid": tb["total_paid"] if tb else None,
            "converted_total_share_owed": tb["total_share_owed"] if tb else None,
            "converted_net_balance": tb["net_balance"] if tb else None,
            "converted_currency": target_currency if tb else None,
        })

    settlements = []
    for s in raw_settlements:
        pair = (s["from_member_id"], s["to_member_id"])
        converted_amt = converted_settlements.get(pair)
        if converted_amt is None and converted_balances and fx_rate and target_currency:
            converted_amt = round(s["amount"] * fx_rate, 2)
        settlements.append({
            **s,
            "currency": base_currency,
            "converted_amount": converted_amt,
            "converted_currency": target_currency if converted_balances else None,
            "exchange_rate": fx_rate,
        })

    return balances, settlements, expenses


# ---------------------------------------------------------------------------
# Auth Endpoints
# ---------------------------------------------------------------------------
@app.post("/api/auth/register", response_model=schemas.TokenResponse)
def register(payload: schemas.RegisterRequest, request: Request, db: Session = Depends(get_db)):
    check_rate_limit(request, limit=10, window_seconds=60)
    email_clean = payload.email.lower().strip()
    weak = validate_password_strength(payload.password)
    if weak:
        raise HTTPException(400, weak)
    
    existing = db.query(models.Account).filter_by(email=email_clean).first()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists")

    account = models.Account(
        email=email_clean,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name.strip(),
    )
    db.add(account)
    db.flush()

    # Create default settings
    settings = models.AccountSettings(account_id=account.id)
    db.add(settings)

    process_pending_invites(db, account)

    db.commit()
    db.refresh(account)
    token = create_access_token(account.id)
    return schemas.TokenResponse(access_token=token)


@app.post("/api/auth/login", response_model=schemas.TokenResponse)
def login(payload: schemas.LoginRequest, request: Request, db: Session = Depends(get_db)):
    check_rate_limit(request, limit=20, window_seconds=60)
    email_clean = payload.email.lower().strip()
    account = db.query(models.Account).filter_by(email=email_clean).first()
    if not account or not verify_password(payload.password, account.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    process_pending_invites(db, account)
    token = create_access_token(account.id)
    return schemas.TokenResponse(access_token=token)


@app.get("/api/auth/me", response_model=schemas.MeOut)
def me(account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    memberships = (
        db.query(models.GroupMember)
        .filter_by(account_id=account.id)
        .all()
    )
    groups_out = []
    for m in memberships:
        g = m.group
        groups_out.append(
            schemas.GroupOut(
                id=g.id,
                name=g.name,
                currency=g.currency or "USD",
                invite_code=g.invite_code,
                created_at=g.created_at,
                has_cover=bool(getattr(g, "cover_image_path", None) and os.path.exists(g.cover_image_path or "")),
            )
        )
    return schemas.MeOut(
        account=schemas.AccountOut(
            id=account.id,
            email=account.email,
            display_name=account.display_name,
            has_avatar=bool(getattr(account, "avatar_path", None) and os.path.exists(account.avatar_path or "")),
        ),
        groups=groups_out,
    )


@app.patch("/api/auth/me", response_model=schemas.AccountOut)
def update_profile(
    payload: schemas.AccountProfileUpdate,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    if payload.display_name:
        name = payload.display_name.strip()
        account.display_name = name
        for membership in db.query(models.GroupMember).filter_by(account_id=account.id).all():
            membership.display_name = name

    if payload.email:
        email_clean = str(payload.email).lower().strip()
        if email_clean != account.email.lower():
            if not payload.current_password or not verify_password(payload.current_password, account.password_hash):
                raise HTTPException(400, "Current password is required to change email")
            taken = db.query(models.Account).filter(models.Account.email == email_clean, models.Account.id != account.id).first()
            if taken:
                raise HTTPException(409, "That email is already in use")
            account.email = email_clean

    if payload.new_password:
        if not payload.current_password or not verify_password(payload.current_password, account.password_hash):
            raise HTTPException(400, "Current password is required to change password")
        weak = validate_password_strength(payload.new_password)
        if weak:
            raise HTTPException(400, weak)
        account.password_hash = hash_password(payload.new_password.strip())

    db.commit()
    db.refresh(account)
    return schemas.AccountOut(
        id=account.id,
        email=account.email,
        display_name=account.display_name,
        has_avatar=bool(getattr(account, "avatar_path", None) and os.path.exists(account.avatar_path or "")),
    )


@app.post("/api/auth/me/avatar", response_model=schemas.AccountOut)
def upload_avatar(
    file: UploadFile = File(...),
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    dest_dir = os.path.join(DATA_DIR, "avatars")
    dest = os.path.join(dest_dir, f"account_{account.id}.jpg")
    _save_profile_image(file, dest)
    if account.avatar_path and account.avatar_path != dest and os.path.exists(account.avatar_path):
        try:
            os.remove(account.avatar_path)
        except OSError:
            pass
    account.avatar_path = dest
    db.commit()
    db.refresh(account)
    return schemas.AccountOut(
        id=account.id,
        email=account.email,
        display_name=account.display_name,
        has_avatar=True,
    )


@app.get("/api/auth/me/avatar")
def get_my_avatar(account: models.Account = Depends(get_current_account)):
    path = getattr(account, "avatar_path", None)
    if not path or not os.path.exists(path):
        raise HTTPException(404, "No profile photo")
    return FileResponse(path)


@app.delete("/api/auth/me/avatar", response_model=schemas.AccountOut)
def remove_my_avatar(account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    path = getattr(account, "avatar_path", None)
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
    account.avatar_path = None
    db.commit()
    return schemas.AccountOut(id=account.id, email=account.email, display_name=account.display_name, has_avatar=False)


@app.post("/api/auth/forgot-password", response_model=schemas.MessageResponse)
def forgot_password(
    payload: schemas.ForgotPasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    check_rate_limit(request, limit=5, window_seconds=60)
    email_clean = payload.email.lower().strip()
    account = db.query(models.Account).filter_by(email=email_clean).first()
    if not account:
        return schemas.MessageResponse(message="If an account exists with that email, a password reset link has been sent.")

    raw_token = secrets.token_urlsafe(32)
    expires = datetime.now() + timedelta(hours=1)

    reset_token = models.PasswordResetToken(
        account_id=account.id,
        token=raw_token,
        expires_at=expires,
    )
    db.add(reset_token)
    db.commit()

    reset_url = f"{_public_base_url(request)}/reset-password.html?token={raw_token}"
    dev_url = reset_url if IS_DEVELOPMENT else None

    if is_email_configured():
        send_password_reset_email(
            to_email=account.email,
            reset_url=reset_url,
            display_name=account.display_name,
        )

    return schemas.MessageResponse(
        message="If an account exists with that email, a password reset link has been sent.",
        reset_url=dev_url,
    )


@app.post("/api/auth/reset-password", response_model=schemas.MessageResponse)
def reset_password(payload: schemas.ResetPasswordRequest, request: Request, db: Session = Depends(get_db)):
    check_rate_limit(request, limit=10, window_seconds=60)
    token_rec = (
        db.query(models.PasswordResetToken)
        .filter_by(token=payload.token, used_at=None)
        .first()
    )
    if not token_rec or token_rec.expires_at < datetime.now():
        raise HTTPException(400, "Reset token is invalid or has expired")

    weak = validate_password_strength(payload.password)
    if weak:
        raise HTTPException(400, weak)

    account = token_rec.account
    account.password_hash = hash_password(payload.password)
    token_rec.used_at = datetime.now()
    db.commit()

    return schemas.MessageResponse(message="Your password has been reset successfully. You can now sign in.")


# ---------------------------------------------------------------------------
# Currency & Exchange Rates
# ---------------------------------------------------------------------------
@app.get("/api/currencies/rates", response_model=schemas.ExchangeRatesOut)
def get_rates(base: str = "USD"):
    rates = get_exchange_rates(base)
    return schemas.ExchangeRatesOut(base_currency=base.upper(), rates=rates)


@app.post("/api/groups/{group_id}/periods/{period_key}/convert-currency", response_model=schemas.PeriodOut)
def set_period_currency_conversion(
    group_id: int,
    period_key: str,
    payload: schemas.ConvertCurrencyRequest,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    period = get_or_create_period(db, group_id, period_key)

    if payload.clear:
        period.target_currency = None
        period.settlement_fx_rate = None
        db.commit()
        db.refresh(period)
        return period

    group = db.query(models.Group).get(group_id)
    base_currency = group.currency if group and group.currency else "USD"
    target_currency = _normalize_currency(payload.target_currency)

    if target_currency == base_currency.upper():
        period.target_currency = None
        period.settlement_fx_rate = None
        db.commit()
        db.refresh(period)
        return period

    if payload.custom_exchange_rate and payload.custom_exchange_rate > 0:
        fx_rate = Decimal(str(payload.custom_exchange_rate))
    else:
        fx_rate = Decimal(str(get_conversion_rate(base_currency, target_currency)))

    period.target_currency = target_currency
    period.settlement_fx_rate = fx_rate
    db.commit()
    db.refresh(period)
    return period


# ---------------------------------------------------------------------------
# Quick Split Endpoints
# ---------------------------------------------------------------------------
def _quick_split_out(split: models.QuickSplit, account: models.Account) -> schemas.QuickSplitOut:
    is_payer = split.payer_account_id == account.id
    is_owner = (split.created_by_account_id or split.payer_account_id) == account.id
    return schemas.QuickSplitOut(
        id=split.id,
        title=split.title,
        total_amount=float(split.total_amount),
        currency=split.currency or "USD",
        bill_date=split.bill_date,
        category=split.category or "General",
        split_type=split.split_type or "EQUAL",
        payer_share_amount=float(split.payer_share_amount or 0),
        payer_name=split.payer.display_name if split.payer else "Someone",
        payer_account_id=split.payer_account_id,
        payer_email=split.payer.email if split.payer else None,
        payer_has_avatar=bool(split.payer and getattr(split.payer, "avatar_path", None) and os.path.exists(split.payer.avatar_path or "")),
        invite_code=split.invite_code,
        status=split.status,
        created_at=split.created_at,
        is_payer=is_payer,
        is_owner=is_owner,
        participants=[
            schemas.QuickSplitParticipantOut(
                id=p.id,
                account_id=p.account_id,
                email=None if p.email.endswith("@invite.local") else p.email,
                display_name=p.display_name,
                share_amount=float(p.share_amount),
                split_value=float(p.split_value) if p.split_value is not None else None,
                payment_status=p.payment_status,
                payment_marked_at=p.payment_marked_at,
                is_me=(p.account_id == account.id or p.email.lower() == account.email.lower()),
                invite_token=p.invite_token,
                has_avatar=bool(p.account and getattr(p.account, "avatar_path", None) and os.path.exists(p.account.avatar_path or "")),
            )
            for p in split.participants
        ],
    )


def _apply_quick_split_values(split: models.QuickSplit, payer_value, participant_values: dict[int, object]) -> None:
    """Apply normal-group split maths to the payer plus every invitee."""
    split_type = (split.split_type or "EQUAL").upper()
    if split_type not in {"EQUAL", "PERCENTAGE", "EXACT", "SHARES"}:
        raise HTTPException(400, "Choose Equal, Percentage, Exact, or Shares")
    inputs = {0: 1 if split_type == "EQUAL" else payer_value}
    inputs.update(participant_values)
    if split_type != "EQUAL" and any(value is None or float(value) < 0 for value in inputs.values()):
        raise HTTPException(400, "Enter a value for every person in this split")
    try:
        shares = calculate_splits(split_type, Decimal(str(split.total_amount)), inputs)
    except ValueError as err:
        raise HTTPException(400, str(err)) from err
    split.payer_share_amount = shares[0]
    for participant in split.participants:
        participant.share_amount = shares[participant.id]
        participant.split_value = None if split_type == "EQUAL" else Decimal(str(participant_values[participant.id]))


def _require_quick_split_access(split_id: int, account: models.Account, db: Session) -> models.QuickSplit:
    split = db.query(models.QuickSplit).get(split_id)
    if not split:
        raise HTTPException(404, "Quick split not found")
    if split.payer_account_id == account.id:
        return split
    allowed = db.query(models.QuickSplitParticipant).filter(
        models.QuickSplitParticipant.quick_split_id == split.id,
        or_(
            models.QuickSplitParticipant.account_id == account.id,
            models.QuickSplitParticipant.email == account.email.lower(),
        ),
    ).first()
    if not allowed:
        raise HTTPException(403, "You do not have access to this quick split")
    if allowed.account_id is None:
        allowed.account_id = account.id
        allowed.display_name = account.display_name
        db.commit()
        db.refresh(split)
    return split


@app.get("/api/quick-splits", response_model=list[schemas.QuickSplitOut])
def list_quick_splits(account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    rows = (
        db.query(models.QuickSplit)
        .outerjoin(models.QuickSplitParticipant)
        .filter(
            or_(
                models.QuickSplit.payer_account_id == account.id,
                models.QuickSplitParticipant.account_id == account.id,
                models.QuickSplitParticipant.email == account.email.lower(),
            )
        )
        .order_by(models.QuickSplit.created_at.desc())
        .distinct()
        .all()
    )
    return [_quick_split_out(item, account) for item in rows]


@app.post("/api/quick-splits", response_model=schemas.QuickSplitOut)
def create_quick_split(
    payload: schemas.QuickSplitCreate,
    request: Request,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    if len(payload.participants) != 1:
        raise HTTPException(400, "A one-to-one split needs exactly one other person. Create a group for 3 or more people.")
    if payload.bill_date > date.today():
        raise HTTPException(400, "A bill date cannot be in the future")
    invitees = []
    for index, raw in enumerate(payload.participants, start=1):
        token = secrets.token_urlsafe(16)
        selected_account = db.query(models.Account).get(raw.account_id) if raw.account_id else None
        if raw.account_id and not selected_account:
            raise HTTPException(404, "Friend account not found")
        email = selected_account.email if selected_account else (str(raw.email).strip().lower() if raw.email else f"slot-{token}@invite.local")
        if email and email not in [item[0] for item in invitees]:
            invitees.append((email, raw.value, raw.label or f"Person {index}", token))
    if not invitees:
        raise HTTPException(400, "Add at least one person to split with")
    if account.email.lower() in [item[0] for item in invitees] and (not payload.payer_account_id or payload.payer_account_id == account.id):
        raise HTTPException(400, "Do not add your own email — your share is included automatically")

    total = Decimal(str(payload.total_amount)).quantize(Decimal("0.01"))
    payer_account = account
    if payload.payer_account_id and payload.payer_account_id != account.id:
        payer_account = db.query(models.Account).get(payload.payer_account_id)
        if not payer_account:
            raise HTTPException(404, "Person who paid was not found")
        if not any(raw.account_id == account.id or (raw.email and str(raw.email).lower() == account.email.lower()) for raw in payload.participants):
            raise HTTPException(400, "When your friend paid, your own account must be the other person in the split")
    # A direct split confirms an existing-account connection as a reusable
    # Friend, so later bills never need another email invitation.
    for raw in payload.participants:
        if raw.account_id and raw.account_id != account.id:
            _activate_friendship(db, account.id, raw.account_id)
    split = models.QuickSplit(
        title=payload.title.strip(), total_amount=total,
        currency=_normalize_currency(payload.currency, "USD"), bill_date=payload.bill_date,
        category=payload.category or "General", created_by_account_id=account.id,
        payer_account_id=payer_account.id,
        split_type=(payload.split_type or "EQUAL").upper(),
    )
    db.add(split)
    db.flush()
    invite_url = _quick_split_url(request, split.invite_code)
    participant_values = {}
    for email, value, label, token in invitees:
        existing_account = selected_account or db.query(models.Account).filter_by(email=email).first()
        db.add(models.QuickSplitParticipant(
            quick_split_id=split.id, email=email,
            account_id=existing_account.id if existing_account else None,
            display_name=existing_account.display_name if existing_account else label,
            share_amount=Decimal("0.00"), split_value=value, invite_token=token,
        ))
    db.flush()
    for participant in split.participants:
        participant_values[participant.id] = next(value for email, value, _label, _token in invitees if email == participant.email)
    _apply_quick_split_values(split, payload.payer_value, participant_values)
    # A settlement closes the balance that existed at that moment. A later
    # bill in the same month must reopen it rather than being hidden behind
    # the already-settled month record.
    for participant in split.participants:
        if participant.account_id and participant.account_id != split.payer_account_id:
            _reopen_friend_settlement(db, split.payer_account_id, participant.account_id, split.bill_date)
    db.commit()
    db.refresh(split)

    money = f"{payload.currency.upper()} {total:.2f}"
    for email, _value, _label, _token in invitees:
        if email.endswith("@invite.local"):
            continue
        if not payload.silent_invite and is_email_configured():
            try:
                send_quick_split_email(email, split.title, money, invite_url, account.display_name)
            except Exception:
                logger.exception("Could not send quick split invite to %s", email)
    return _quick_split_out(split, account)


@app.put("/api/quick-splits/{split_id}", response_model=schemas.QuickSplitOut)
def update_quick_split(split_id: int, payload: schemas.QuickSplitUpdate, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    split = _require_quick_split_access(split_id, account, db)
    if (split.created_by_account_id or split.payer_account_id) != account.id:
        raise HTTPException(403, "Only the person who paid can edit this split")
    if any(p.payment_status != "PENDING" for p in split.participants):
        raise HTTPException(400, "This split cannot be edited after someone has marked payment")
    if len(payload.participants) != 1:
        raise HTTPException(400, "A one-to-one split needs exactly one other person. Create a group for 3 or more people.")
    if payload.bill_date > date.today():
        raise HTTPException(400, "A bill date cannot be in the future")
    invitees = []
    for index, raw in enumerate(payload.participants, start=1):
        token = secrets.token_urlsafe(16)
        selected_account = db.query(models.Account).get(raw.account_id) if raw.account_id else None
        if raw.account_id and not selected_account:
            raise HTTPException(404, "Friend account not found")
        email = selected_account.email if selected_account else (str(raw.email).strip().lower() if raw.email else f"slot-{token}@invite.local")
        if email and email not in [item[0] for item in invitees]:
            invitees.append((email, raw.value, raw.label or f"Person {index}", token))
    if not invitees or (account.email.lower() in [item[0] for item in invitees] and split.payer_account_id == account.id):
        raise HTTPException(400, "Add at least one other person's email")
    split.title = payload.title.strip()
    split.total_amount = Decimal(str(payload.total_amount)).quantize(Decimal("0.01"))
    split.currency = _normalize_currency(payload.currency, "USD")
    split.bill_date = payload.bill_date
    split.category = payload.category or "General"
    split.split_type = (payload.split_type or "EQUAL").upper()
    existing_by_email = {p.email: p for p in split.participants}
    kept = set()
    for email, value, label, token in invitees:
        participant = existing_by_email.get(email)
        if participant is None:
            existing_account = selected_account or db.query(models.Account).filter_by(email=email).first()
            participant = models.QuickSplitParticipant(quick_split_id=split.id, email=email, account_id=existing_account.id if existing_account else None, display_name=existing_account.display_name if existing_account else label, share_amount=0, invite_token=token)
            db.add(participant)
        participant.split_value = value
        kept.add(email)
    for participant in list(split.participants):
        if participant.email not in kept:
            db.delete(participant)
    db.flush()
    # The relationship may already have been loaded above.  Refresh it after
    # adding/removing invitees so deleted slots are not included in the share
    # calculation and newly-created slots receive their database ids.
    db.expire(split, ["participants"])
    participant_values = {p.id: p.split_value for p in split.participants if p.email in kept}
    _apply_quick_split_values(split, payload.payer_value, participant_values)
    db.commit(); db.refresh(split)
    return _quick_split_out(split, account)


@app.delete("/api/quick-splits/{split_id}")
def delete_quick_split(split_id: int, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    """Permanently remove a one-to-one split. Only its payer may do this."""
    split = db.query(models.QuickSplit).get(split_id)
    if not split:
        raise HTTPException(404, "One-to-one split not found")
    if (split.created_by_account_id or split.payer_account_id) != account.id:
        raise HTTPException(403, "Only the person who created this split can delete it")
    db.delete(split)
    db.commit()
    return {"ok": True, "message": "Split deleted"}


def _friend_month_balance(db: Session, account_id: int, friend_id: int, month_key: str):
    try:
        start = date.fromisoformat(f"{month_key}-01")
    except ValueError as err:
        raise HTTPException(400, "Use a month in YYYY-MM format") from err
    end = date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1)
    one, two = _friend_pair(account_id, friend_id)
    settlement = db.query(models.FriendMonthlySettlement).filter_by(
        account_one_id=one, account_two_id=two, month_key=month_key,
    ).first()
    query = (
        db.query(models.QuickSplit, models.QuickSplitParticipant)
        .join(models.QuickSplitParticipant)
        .filter(
            models.QuickSplit.bill_date >= start,
            models.QuickSplit.bill_date < end,
            or_(
                (models.QuickSplit.payer_account_id == account_id) & (models.QuickSplitParticipant.account_id == friend_id),
                (models.QuickSplit.payer_account_id == friend_id) & (models.QuickSplitParticipant.account_id == account_id),
            ),
        )
    )
    # A settlement applies only to bills that existed when it was confirmed.
    # Keep its timestamp as the cutoff when later activity reopens the month.
    if settlement and settlement.settled_at:
        query = query.filter(models.QuickSplit.created_at > settlement.settled_at)
    rows = query.all()
    balance = Decimal("0.00")
    currency = "USD"
    for split, participant in rows:
        currency = split.currency or currency
        balance += Decimal(str(participant.share_amount)) if split.payer_account_id == account_id else -Decimal(str(participant.share_amount))
    return balance.quantize(Decimal("0.01")), currency


def _friend_pair(first_id: int, second_id: int) -> tuple[int, int]:
    if first_id == second_id:
        raise HTTPException(400, "You cannot add yourself as a friend")
    return tuple(sorted((first_id, second_id)))


def _activate_friendship(db: Session, first_id: int, second_id: int) -> models.FriendConnection:
    one, two = _friend_pair(first_id, second_id)
    row = db.query(models.FriendConnection).filter_by(account_one_id=one, account_two_id=two).first()
    if row is None:
        row = models.FriendConnection(account_one_id=one, account_two_id=two, status="ACTIVE")
        db.add(row)
    elif row.status != "REMOVED":
        row.status = "ACTIVE"
    return row


def _reopen_friend_settlement(db: Session, first_id: int, second_id: int, bill_date: date) -> None:
    """New activity after a monthly settlement starts a fresh open balance."""
    one, two = _friend_pair(first_id, second_id)
    month_key = bill_date.strftime("%Y-%m")
    settlement = db.query(models.FriendMonthlySettlement).filter_by(
        account_one_id=one, account_two_id=two, month_key=month_key,
    ).first()
    # A timestamp means this row has already closed an earlier set of bills.
    # Recalculate the still-open portion every time another bill is added,
    # including while the other person is part-way through confirming it.
    if settlement and settlement.settled_at:
        balance, currency = _friend_month_balance(db, first_id, second_id, month_key)
        settlement.status = "PENDING"
        settlement.payer_confirmed = False
        settlement.recipient_confirmed = False
        settlement.amount = abs(balance)
        settlement.currency = currency
        settlement.payer_account_id = first_id if balance < 0 else second_id
        settlement.recipient_account_id = second_id if balance < 0 else first_id


def _reconcile_friend_settlements(db: Session, first_id: int, second_id: int) -> None:
    """Repair a settled month if a bill was added after its settlement time."""
    one, two = _friend_pair(first_id, second_id)
    rows = db.query(models.FriendMonthlySettlement).filter_by(
        account_one_id=one, account_two_id=two, status="SETTLED",
    ).all()
    changed = False
    for settlement in rows:
        if not settlement.settled_at:
            continue
        try:
            start = date.fromisoformat(f"{settlement.month_key}-01")
        except ValueError:
            continue
        end = date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1)
        has_new_bill = db.query(models.QuickSplit.id).join(models.QuickSplitParticipant).filter(
            models.QuickSplit.bill_date >= start,
            models.QuickSplit.bill_date < end,
            models.QuickSplit.created_at > settlement.settled_at,
            or_(
                (models.QuickSplit.payer_account_id == first_id) & (models.QuickSplitParticipant.account_id == second_id),
                (models.QuickSplit.payer_account_id == second_id) & (models.QuickSplitParticipant.account_id == first_id),
            ),
        ).first()
        if has_new_bill:
            _reopen_friend_settlement(db, first_id, second_id, start)
            changed = True
    if changed:
        db.commit()


def _consolidated_friend_sources(db: Session, account_id: int, friend_id: int, currency: str) -> list[dict]:
    """Return open pair debts, never mixing currencies or unpaid payment claims."""
    currency = _normalize_currency(currency, "USD")
    sources: list[dict] = []
    one, two = _friend_pair(account_id, friend_id)
    monthly = {
        row.month_key: row for row in db.query(models.FriendMonthlySettlement).filter_by(
            account_one_id=one, account_two_id=two,
        ).all()
    }
    direct_by_month: dict[str, Decimal] = {}
    direct_rows = db.query(models.QuickSplit, models.QuickSplitParticipant).join(models.QuickSplitParticipant).filter(
        models.QuickSplit.currency == currency,
        or_(
            (models.QuickSplit.payer_account_id == account_id) & (models.QuickSplitParticipant.account_id == friend_id),
            (models.QuickSplit.payer_account_id == friend_id) & (models.QuickSplitParticipant.account_id == account_id),
        ),
    ).all()
    for split, participant in direct_rows:
        month_key = split.bill_date.strftime("%Y-%m")
        settlement = monthly.get(month_key)
        if settlement and settlement.status == "SETTLED":
            continue
        if settlement and settlement.settled_at and split.created_at and split.created_at <= settlement.settled_at:
            continue
        value = Decimal(str(participant.share_amount))
        direct_by_month[month_key] = direct_by_month.get(month_key, Decimal("0.00")) + (value if split.payer_account_id == account_id else -value)

    # Confirmed pair payments allocated to a direct month reduce only that
    # month's open amount. A pending claim never changes either person's debt.
    payments = db.query(models.PairPayment).filter_by(
        account_one_id=one, account_two_id=two, status="CONFIRMED", currency=currency,
    ).all()
    for payment in payments:
        for allocation in json.loads(payment.allocations_json or "[]"):
            if allocation.get("kind") != "DIRECT":
                continue
            month_key = allocation.get("month_key")
            if month_key not in direct_by_month:
                continue
            amount = Decimal(str(allocation.get("amount", 0)))
            direct_by_month[month_key] += amount if payment.payer_account_id == account_id else -amount

    for month_key, value in direct_by_month.items():
        if value:
            sources.append({"kind": "DIRECT", "label": f"Direct splits · {month_key}", "amount": float(value), "currency": currency, "period_key": month_key, "group_id": None})

    # Group balances become available for consolidated settlement only after
    # that month's group period has been finalized. This preserves the audit
    # trail and prevents a later edited bill being paid twice.
    group_member_rows = db.query(models.GroupMember).filter(
        models.GroupMember.account_id.in_((account_id, friend_id)),
    ).all()
    members_by_group: dict[int, dict[int, models.GroupMember]] = {}
    for member in group_member_rows:
        members_by_group.setdefault(member.group_id, {})[member.account_id] = member
    for group_id, members in members_by_group.items():
        if account_id not in members or friend_id not in members:
            continue
        group = db.query(models.Group).get(group_id)
        if not group or _normalize_currency(group.currency, "USD") != currency:
            continue
        for period in db.query(models.MonthlyPeriod).filter_by(group_id=group_id, status="SETTLED").order_by(models.MonthlyPeriod.period_key.asc()).all():
            _balances, settlements, _expenses = build_balances_and_settlements(db, group_id, period)
            mine, theirs = members[account_id].id, members[friend_id].id
            for settlement in settlements:
                if {settlement["from_member_id"], settlement["to_member_id"]} != {mine, theirs}:
                    continue
                amount = Decimal(str(settlement["amount"]))
                signed = amount if settlement["from_member_id"] == theirs else -amount
                sources.append({
                    "kind": "GROUP", "label": f"{group.name} · {period.period_key}", "amount": float(signed), "currency": currency,
                    "period_key": period.period_key, "group_id": group_id,
                    "period_id": period.id, "payer_member_id": settlement["from_member_id"], "recipient_member_id": settlement["to_member_id"],
                })
    return sources


def _consolidated_friend_balance(db: Session, account_id: int, friend_id: int, currency: str) -> tuple[Decimal, list[dict]]:
    sources = _consolidated_friend_sources(db, account_id, friend_id, currency)
    balance = sum((Decimal(str(source["amount"])) for source in sources), Decimal("0.00"))
    return balance.quantize(Decimal("0.01")), sources


def _sync_group_friends(db: Session, account_id: int) -> None:
    """Members become durable friends unless either side explicitly removed them."""
    group_ids = [row[0] for row in db.query(models.GroupMember.group_id).filter_by(account_id=account_id, is_active=True).all()]
    if not group_ids:
        return
    others = db.query(models.GroupMember.account_id).filter(
        models.GroupMember.group_id.in_(group_ids), models.GroupMember.account_id != account_id,
        models.GroupMember.is_active.is_(True),
    ).distinct().all()
    for (other_id,) in others:
        _activate_friendship(db, account_id, other_id)
    db.commit()


def _sync_quick_split_friends(db: Session, account_id: int) -> None:
    """Bring existing one-to-one split relationships into the durable list once."""
    partner_ids = set()
    payer_rows = db.query(models.QuickSplitParticipant.account_id).join(models.QuickSplit).filter(
        models.QuickSplit.payer_account_id == account_id,
        models.QuickSplitParticipant.account_id.isnot(None),
    ).all()
    partner_ids.update(row[0] for row in payer_rows if row[0] != account_id)
    participant_rows = db.query(models.QuickSplit.payer_account_id).join(models.QuickSplitParticipant).filter(
        models.QuickSplitParticipant.account_id == account_id,
        models.QuickSplit.payer_account_id.isnot(None),
    ).all()
    partner_ids.update(row[0] for row in participant_rows if row[0] != account_id)
    for partner_id in partner_ids:
        _activate_friendship(db, account_id, partner_id)
    if partner_ids:
        db.commit()


@app.get("/api/friends", response_model=list[schemas.FriendOut])
def list_friends(account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    _sync_group_friends(db, account.id)
    _sync_quick_split_friends(db, account.id)
    rows = db.query(models.FriendConnection).filter(
        or_(models.FriendConnection.account_one_id == account.id, models.FriendConnection.account_two_id == account.id),
        models.FriendConnection.status == "ACTIVE",
    ).all()
    friend_ids = [row.account_two_id if row.account_one_id == account.id else row.account_one_id for row in rows]
    people = db.query(models.Account).filter(models.Account.id.in_(friend_ids)).all() if friend_ids else []
    return [schemas.FriendOut(
        id=person.id, display_name=person.display_name, email=person.email,
        has_avatar=bool(person.avatar_path and os.path.exists(person.avatar_path)), source="friend",
    ) for person in sorted(people, key=lambda person: person.display_name.lower())]


@app.delete("/api/friends/{friend_id}", response_model=schemas.MessageResponse)
def remove_friend(friend_id: int, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    one, two = _friend_pair(account.id, friend_id)
    row = db.query(models.FriendConnection).filter_by(account_one_id=one, account_two_id=two).first()
    if not row:
        raise HTTPException(404, "Friend not found")
    row.status = "REMOVED"
    db.commit()
    return schemas.MessageResponse(message="Friend removed")


@app.post("/api/friends/invites", response_model=schemas.FriendInviteOut)
def invite_friend(
    payload: schemas.FriendInviteRequest,
    request: Request,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    email = str(payload.email).lower().strip() if payload.email else None
    if email == account.email.lower():
        raise HTTPException(400, "You cannot add yourself as a friend")
    existing = db.query(models.Account).filter_by(email=email).first() if email else None
    if existing:
        _activate_friendship(db, account.id, existing.id)
        db.commit()
        return schemas.FriendInviteOut(status="ACTIVE", email=email, message=f"{existing.display_name} was added as a friend")
    invite = models.FriendInvite(inviter_account_id=account.id, email=email)
    db.add(invite)
    db.commit(); db.refresh(invite)
    url = f"{str(request.base_url).rstrip('/')}/friend-join.html?code={invite.invite_code}"
    email_sent = False
    if email and is_email_configured():
        try:
            email_sent = bool(send_friend_invite_email(email, url, account.display_name))
        except Exception:
            logger.exception("Failed to send friend invite email to %s", email)
    message = "Friend invite emailed." if email_sent else "Friend invite created. Copy the link or send it by email."
    return schemas.FriendInviteOut(status="PENDING", email=email, invite_url=url, email_sent=email_sent, message=message)


@app.post("/api/friends/invites/{invite_code}/accept", response_model=schemas.MessageResponse)
def accept_friend_invite(invite_code: str, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    invite = db.query(models.FriendInvite).filter_by(invite_code=invite_code, status="PENDING").first()
    if not invite:
        raise HTTPException(404, "Friend invite not found or already used")
    if invite.email and invite.email.lower() != account.email.lower():
        raise HTTPException(403, "This friend invite was sent to another email address")
    _activate_friendship(db, invite.inviter_account_id, account.id)
    invite.status = "ACCEPTED"
    db.commit()
    return schemas.MessageResponse(message="You are now friends")


def _friend_settlement_out(row: models.FriendMonthlySettlement) -> schemas.FriendSettlementOut:
    return schemas.FriendSettlementOut(
        month_key=row.month_key, payer_account_id=row.payer_account_id, recipient_account_id=row.recipient_account_id,
        amount=float(row.amount), currency=row.currency, payer_confirmed=row.payer_confirmed,
        recipient_confirmed=row.recipient_confirmed, status=row.status, settled_at=row.settled_at,
    )


@app.get("/api/friends/{friend_id}/settlements", response_model=list[schemas.FriendSettlementOut])
def list_friend_settlements(friend_id: int, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    _reconcile_friend_settlements(db, account.id, friend_id)
    one, two = sorted((account.id, friend_id))
    rows = db.query(models.FriendMonthlySettlement).filter_by(account_one_id=one, account_two_id=two).order_by(models.FriendMonthlySettlement.month_key.desc()).all()
    return [_friend_settlement_out(row) for row in rows]


def _pair_payment_out(payment: models.PairPayment) -> schemas.PairPaymentOut:
    return schemas.PairPaymentOut(
        id=payment.id, payer_account_id=payment.payer_account_id, recipient_account_id=payment.recipient_account_id,
        amount=float(payment.amount), currency=payment.currency, status=payment.status,
        reported_by_account_id=payment.reported_by_account_id, created_at=payment.created_at, confirmed_at=payment.confirmed_at,
    )


def _apply_confirmed_pair_payment(db: Session, payment: models.PairPayment) -> None:
    """Apply a recipient-confirmed pair payment exactly once to group sources."""
    if payment.status == "CONFIRMED":
        return
    for allocation in json.loads(payment.allocations_json or "[]"):
        if allocation.get("kind") != "GROUP":
            continue
        period = db.query(models.MonthlyPeriod).get(allocation.get("period_id"))
        if not period or period.status != "SETTLED":
            raise HTTPException(409, "A group source is no longer ready for this payment. Refresh and record it again.")
        cutoff_id = db.query(func.max(models.Expense.id)).filter_by(period_id=period.id).scalar()
        db.add(models.GroupPayment(
            period_id=period.id, payer_member_id=allocation["payer_member_id"], recipient_member_id=allocation["recipient_member_id"],
            amount=Decimal(str(allocation["amount"])), currency=payment.currency, status="CONFIRMED",
            expense_cutoff_id=cutoff_id, marked_paid_at=datetime.now(timezone.utc), confirmed_at=datetime.now(timezone.utc),
        ))
    payment.status = "CONFIRMED"
    payment.confirmed_at = datetime.now(timezone.utc)


def _reconcile_recipient_reported_pair_payments(db: Session, account_one_id: int, account_two_id: int, currency: str) -> None:
    """A lender's own received-payment record is authoritative by design."""
    payments = db.query(models.PairPayment).filter_by(
        account_one_id=account_one_id, account_two_id=account_two_id, currency=currency, status="PENDING",
    ).all()
    changed = False
    for payment in payments:
        if payment.reported_by_account_id == payment.recipient_account_id:
            _apply_confirmed_pair_payment(db, payment)
            changed = True
    if changed:
        db.commit()


@app.get("/api/friends/{friend_id}/consolidated-balance", response_model=schemas.ConsolidatedFriendBalanceOut)
def get_consolidated_friend_balance(
    friend_id: int, currency: str = "USD", account: models.Account = Depends(get_current_account), db: Session = Depends(get_db),
):
    if friend_id == account.id or not db.query(models.Account).get(friend_id):
        raise HTTPException(404, "Friend not found")
    one, two = _friend_pair(account.id, friend_id)
    connection = db.query(models.FriendConnection).filter_by(account_one_id=one, account_two_id=two, status="ACTIVE").first()
    if not connection:
        raise HTTPException(403, "Add this person as a friend before viewing a consolidated balance")
    normalized_currency = _normalize_currency(currency, "USD")
    _reconcile_recipient_reported_pair_payments(db, one, two, normalized_currency)
    balance, sources = _consolidated_friend_balance(db, account.id, friend_id, normalized_currency)
    payments = db.query(models.PairPayment).filter_by(account_one_id=one, account_two_id=two, currency=normalized_currency).order_by(models.PairPayment.created_at.desc()).all()
    payer_id = account.id if balance < 0 else friend_id if balance > 0 else None
    recipient_id = friend_id if balance < 0 else account.id if balance > 0 else None
    return schemas.ConsolidatedFriendBalanceOut(
        friend_id=friend_id, balance=float(balance), currency=normalized_currency,
        payer_account_id=payer_id, recipient_account_id=recipient_id,
        sources=[schemas.ConsolidatedSourceOut(**{key: value for key, value in source.items() if key in {"kind", "label", "amount", "currency", "period_key", "group_id"}}) for source in sources],
        payments=[_pair_payment_out(payment) for payment in payments],
    )


@app.post("/api/friends/{friend_id}/consolidated-payments", response_model=schemas.PairPaymentOut)
def record_consolidated_friend_payment(
    friend_id: int, payload: schemas.PairPaymentCreate, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db),
):
    normalized_currency = _normalize_currency(payload.currency, "USD")
    balance, sources = _consolidated_friend_balance(db, account.id, friend_id, normalized_currency)
    if not balance:
        raise HTTPException(400, "There is no open balance to pay")
    payer_id = account.id if balance < 0 else friend_id
    recipient_id = friend_id if balance < 0 else account.id
    amount = Decimal(str(payload.amount)).quantize(Decimal("0.01"))
    if account.id not in (payer_id, recipient_id):
        raise HTTPException(403, "Only the two people in this balance can record payment")
    if amount > abs(balance):
        raise HTTPException(400, f"Payment cannot exceed the open balance of {normalized_currency} {abs(balance):.2f}")

    remaining = amount
    allocations = []
    # Oldest first makes partial payments predictable and gives every source
    # a permanent, inspectable allocation.
    for source in sorted(sources, key=lambda item: (item["period_key"] or "", item["kind"])):
        if remaining <= 0 or Decimal(str(source["amount"])) * balance <= 0:
            continue
        applied = min(remaining, abs(Decimal(str(source["amount"]))))
        allocation = {"kind": source["kind"], "amount": float(applied), "month_key": source.get("period_key")}
        if source["kind"] == "GROUP":
            allocation.update({
                "period_id": source["period_id"], "payer_member_id": source["payer_member_id"],
                "recipient_member_id": source["recipient_member_id"], "group_id": source["group_id"],
            })
        allocations.append(allocation)
        remaining -= applied
    if remaining > Decimal("0.001"):
        raise HTTPException(409, "This balance changed. Refresh and try the payment again.")

    one, two = _friend_pair(account.id, friend_id)
    payment = models.PairPayment(
        account_one_id=one, account_two_id=two, payer_account_id=payer_id, recipient_account_id=recipient_id,
        amount=amount, currency=normalized_currency, status="PENDING", reported_by_account_id=account.id,
        allocations_json=json.dumps(allocations),
    )
    db.add(payment)
    # A lender recording money received does not need to request their own
    # confirmation. A borrower's "I paid" entry remains pending for lender approval.
    if account.id == recipient_id:
        _apply_confirmed_pair_payment(db, payment)
    db.commit(); db.refresh(payment)
    return _pair_payment_out(payment)


@app.post("/api/friends/{friend_id}/consolidated-payments/{payment_id}/confirm", response_model=schemas.PairPaymentOut)
def confirm_consolidated_friend_payment(
    friend_id: int, payment_id: int, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db),
):
    one, two = _friend_pair(account.id, friend_id)
    payment = db.query(models.PairPayment).filter_by(id=payment_id, account_one_id=one, account_two_id=two).first()
    if not payment:
        raise HTTPException(404, "Payment not found")
    if payment.status != "PENDING":
        raise HTTPException(400, "This payment has already been handled")
    if account.id == payment.reported_by_account_id:
        raise HTTPException(403, "The other person must confirm this payment")
    if account.id not in (payment.payer_account_id, payment.recipient_account_id):
        raise HTTPException(403, "Only the other person in this payment can confirm it")
    _apply_confirmed_pair_payment(db, payment)
    db.commit(); db.refresh(payment)
    return _pair_payment_out(payment)


@app.get("/api/friends/{friend_id}/avatar")
def get_friend_avatar(friend_id: int, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    if friend_id == account.id:
        target = account
    else:
        one, two = _friend_pair(account.id, friend_id)
        related = db.query(models.FriendConnection).filter_by(account_one_id=one, account_two_id=two, status="ACTIVE").first()
        if not related:
            raise HTTPException(403, "You do not have access to this friend's profile photo")
        target = db.query(models.Account).get(friend_id)
    path = getattr(target, "avatar_path", None)
    if not path or not os.path.exists(path):
        raise HTTPException(404, "No profile photo")
    return FileResponse(path)


@app.post("/api/friends/{friend_id}/settlements/confirm", response_model=schemas.FriendSettlementOut)
def confirm_friend_settlement(friend_id: int, payload: schemas.FriendSettlementConfirm, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    if friend_id == account.id or not db.query(models.Account).get(friend_id):
        raise HTTPException(404, "Friend not found")
    balance, currency = _friend_month_balance(db, account.id, friend_id, payload.month_key)
    if not balance:
        raise HTTPException(400, "There is no balance to settle for this month")
    payer_id = account.id if balance < 0 else friend_id
    recipient_id = friend_id if balance < 0 else account.id
    one, two = sorted((account.id, friend_id))
    settlement = db.query(models.FriendMonthlySettlement).filter_by(account_one_id=one, account_two_id=two, month_key=payload.month_key).first()
    if settlement is None:
        settlement = models.FriendMonthlySettlement(
            account_one_id=one, account_two_id=two, month_key=payload.month_key,
            payer_account_id=payer_id, recipient_account_id=recipient_id,
            amount=abs(balance), currency=currency,
        )
        db.add(settlement)
    if payload.action == "paid":
        if account.id != settlement.payer_account_id:
            raise HTTPException(403, "Only the person who owes can confirm payment")
        settlement.payer_confirmed = True
    else:
        if account.id != settlement.recipient_account_id:
            raise HTTPException(403, "Only the person receiving payment can confirm receipt")
        settlement.recipient_confirmed = True
    # Receipt is the final authority: the recipient may settle immediately,
    # while a payer's confirmation alone remains pending for their approval.
    if settlement.recipient_confirmed:
        settlement.status = "SETTLED"
        settlement.settled_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(settlement)
    return _friend_settlement_out(settlement)


@app.get("/api/quick-splits/{split_id}", response_model=schemas.QuickSplitOut)
def get_quick_split(split_id: int, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    return _quick_split_out(_require_quick_split_access(split_id, account, db), account)


@app.post("/api/quick-splits/join", response_model=schemas.QuickSplitOut)
def join_quick_split(payload: schemas.QuickSplitJoinRequest, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    split = db.query(models.QuickSplit).filter_by(invite_code=payload.invite_code.strip()).first()
    if not split:
        raise HTTPException(404, "Invalid quick split link")
    participant = None
    if payload.participant_token:
        participant = db.query(models.QuickSplitParticipant).filter_by(
            quick_split_id=split.id, invite_token=payload.participant_token
        ).first()
    if not participant:
        participant = db.query(models.QuickSplitParticipant).filter_by(
            quick_split_id=split.id, email=account.email.lower()
        ).first()
    if not participant:
        participant = db.query(models.QuickSplitParticipant).filter(
            models.QuickSplitParticipant.quick_split_id == split.id,
            models.QuickSplitParticipant.account_id.is_(None),
            models.QuickSplitParticipant.email.like("slot-%@invite.local"),
        ).first()
    if not participant:
        raise HTTPException(403, "There are no available invitation slots in this quick split")
    participant.account_id = account.id
    participant.display_name = account.display_name
    db.commit()
    db.refresh(split)
    return _quick_split_out(split, account)


@app.post("/api/quick-splits/{split_id}/participants/{participant_id}/mark-paid", response_model=schemas.QuickSplitOut)
def mark_quick_split_paid(split_id: int, participant_id: int, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    split = _require_quick_split_access(split_id, account, db)
    participant = db.query(models.QuickSplitParticipant).filter_by(id=participant_id, quick_split_id=split.id).first()
    if not participant:
        raise HTTPException(404, "Participant not found")
    if participant.account_id != account.id and participant.email.lower() != account.email.lower():
        raise HTTPException(403, "Only this person can mark their share paid")
    participant.account_id = account.id
    participant.display_name = account.display_name
    participant.payment_status = "PAID"
    participant.payment_marked_at = datetime.utcnow()
    db.commit()
    db.refresh(split)
    return _quick_split_out(split, account)


@app.post("/api/quick-splits/{split_id}/participants/{participant_id}/confirm", response_model=schemas.QuickSplitOut)
def confirm_quick_split_payment(split_id: int, participant_id: int, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    split = _require_quick_split_access(split_id, account, db)
    if split.payer_account_id != account.id:
        raise HTTPException(403, "Only the person who paid can confirm payment")
    participant = db.query(models.QuickSplitParticipant).filter_by(id=participant_id, quick_split_id=split.id).first()
    if not participant or participant.payment_status != "PAID":
        raise HTTPException(400, "This payment has not been marked paid yet")
    participant.payment_status = "CONFIRMED"
    if all(p.payment_status == "CONFIRMED" for p in split.participants):
        split.status = "SETTLED"
    db.commit()
    db.refresh(split)
    return _quick_split_out(split, account)


# ---------------------------------------------------------------------------
# Groups Endpoints
# ---------------------------------------------------------------------------
@app.get("/api/groups", response_model=list[schemas.GroupListOut])
def list_groups(account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    memberships = db.query(models.GroupMember).filter_by(account_id=account.id).all()
    results = []
    for m in memberships:
        if not _member_is_active(m):
            continue
        g = m.group
        all_members = db.query(models.GroupMember).filter_by(group_id=g.id).all()
        member_count = sum(1 for x in all_members if _member_is_active(x))
        results.append(
            schemas.GroupListOut(
                id=g.id,
                name=g.name,
                currency=g.currency or "USD",
                invite_code=g.invite_code,
                created_at=g.created_at,
                has_cover=bool(getattr(g, "cover_image_path", None) and os.path.exists(g.cover_image_path or "")),
                role=m.role,
                member_count=member_count,
            )
        )
    return results


@app.post("/api/groups", response_model=schemas.GroupOut)
def create_group(
    payload: schemas.GroupCreate,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    curr = _normalize_currency(payload.currency, "USD")
    last_error = None
    for attempt in range(2):
        group = models.Group(
            name=payload.name.strip(),
            currency=curr,
            created_by_id=account.id,
        )
        db.add(group)
        try:
            db.flush()
            member = models.GroupMember(
                group_id=group.id,
                account_id=account.id,
                display_name=account.display_name,
                role="owner",
            )
            db.add(member)
            db.commit()
            db.refresh(group)
            return group
        except Exception as err:
            last_error = err
            db.rollback()
            logger.exception("Failed to create group (attempt %s)", attempt + 1)
            _ensure_schema()
    raise HTTPException(
        status_code=500,
        detail="Could not create the group. Try again in a moment.",
    ) from last_error


@app.get("/api/groups/{group_id}/sync", response_model=schemas.GroupSyncOut)
def get_group_sync(
    group_id: int,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    exp = (
        db.query(
            func.coalesce(func.max(models.Expense.id), 0),
            func.count(models.Expense.id),
        )
        .select_from(models.Expense)
        .join(models.MonthlyPeriod, models.Expense.period_id == models.MonthlyPeriod.id)
        .filter(models.MonthlyPeriod.group_id == group_id)
        .first()
    )
    mem = (
        db.query(
            func.coalesce(func.max(models.GroupMember.id), 0),
            func.count(models.GroupMember.id),
        )
        .filter(models.GroupMember.group_id == group_id)
        .first()
    )
    periods = (
        db.query(models.MonthlyPeriod)
        .filter_by(group_id=group_id)
        .order_by(models.MonthlyPeriod.period_key.asc())
        .all()
    )
    period_sig = "|".join(
        f"{p.period_key}:{p.status}:{p.target_currency}:{p.settlement_fx_rate}:{p.pdf_filename}"
        for p in periods
    )
    raw = f"{exp[0]}:{exp[1]}:{mem[0]}:{mem[1]}:{period_sig}"
    version = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
    return schemas.GroupSyncOut(version=version)


@app.get("/api/groups/{group_id}/events")
async def stream_group_events(
    group_id: int,
    request: Request,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    """Authenticated server-sent events for an open group's live updates."""
    require_group_member(group_id, account, db)
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=20)
    loop = asyncio.get_running_loop()
    subscription = (loop, queue)
    with _GROUP_EVENT_LOCK:
        _GROUP_EVENT_SUBSCRIBERS.setdefault(group_id, []).append(subscription)

    async def events():
        try:
            yield "event: connected\ndata: ready\n\n"
            while not await request.is_disconnected():
                try:
                    kind = await asyncio.wait_for(queue.get(), timeout=25)
                    yield f"event: group-change\ndata: {kind}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            with _GROUP_EVENT_LOCK:
                listeners = _GROUP_EVENT_SUBSCRIBERS.get(group_id, [])
                if subscription in listeners:
                    listeners.remove(subscription)
                if not listeners:
                    _GROUP_EVENT_SUBSCRIBERS.pop(group_id, None)

    return StreamingResponse(
        events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/groups/{group_id}", response_model=schemas.GroupDetailOut)
def get_group(
    group_id: int,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    group = db.query(models.Group).get(group_id)
    if not group:
        raise HTTPException(404, "Group not found")

    members = (
        db.query(models.GroupMember)
        .join(models.Account)
        .filter(
            models.GroupMember.group_id == group_id,
            or_(models.GroupMember.is_active.is_(True), models.GroupMember.is_active.is_(None)),
        )
        .all()
    )
    members_out = [
        schemas.MemberOut(
            id=m.id,
            account_id=m.account_id,
            display_name=m.display_name,
            email=m.account.email,
            role=m.role,
            is_active=True,
            has_avatar=bool(getattr(m.account, "avatar_path", None) and os.path.exists(m.account.avatar_path or "")),
        )
        for m in members
    ]

    return schemas.GroupDetailOut(
        id=group.id,
        name=group.name,
        currency=group.currency or "USD",
        invite_code=group.invite_code,
        created_at=group.created_at,
        has_cover=bool(getattr(group, "cover_image_path", None) and os.path.exists(group.cover_image_path or "")),
        members=members_out,
    )


@app.patch("/api/groups/{group_id}", response_model=schemas.GroupDetailOut)
def update_group(
    group_id: int,
    payload: schemas.GroupUpdate,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_owner(group_id, account, db)
    group = db.query(models.Group).get(group_id)
    if not group:
        raise HTTPException(404, "Group not found")
    if payload.name:
        group.name = payload.name.strip()
    db.commit()
    return get_group(group_id, account, db)


@app.post("/api/groups/{group_id}/cover", response_model=schemas.GroupDetailOut)
def upload_group_cover(
    group_id: int,
    file: UploadFile = File(...),
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_owner(group_id, account, db)
    group = db.query(models.Group).get(group_id)
    if not group:
        raise HTTPException(404, "Group not found")
    dest = os.path.join(_group_uploads_dir(group_id), "cover.jpg")
    _save_profile_image(file, dest)
    group.cover_image_path = dest
    db.commit()
    return get_group(group_id, account, db)


@app.get("/api/groups/{group_id}/cover")
def get_group_cover(
    group_id: int,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    group = db.query(models.Group).get(group_id)
    path = getattr(group, "cover_image_path", None) if group else None
    if not path or not os.path.exists(path):
        raise HTTPException(404, "No group photo")
    return FileResponse(path)


@app.delete("/api/groups/{group_id}/cover", response_model=schemas.GroupDetailOut)
def remove_group_cover(group_id: int, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    require_group_owner(group_id, account, db)
    group = db.query(models.Group).get(group_id)
    path = getattr(group, "cover_image_path", None) if group else None
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
    if group:
        group.cover_image_path = None
        db.commit()
    return get_group(group_id, account, db)


@app.get("/api/groups/{group_id}/members/{member_id}/avatar")
def get_group_member_avatar(group_id: int, member_id: int, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    require_group_member(group_id, account, db)
    member = db.query(models.GroupMember).filter_by(id=member_id, group_id=group_id).first()
    path = getattr(getattr(member, "account", None), "avatar_path", None)
    if not path or not os.path.exists(path):
        raise HTTPException(404, "No profile photo")
    return FileResponse(path)


def _purge_member_group_history(db: Session, group_id: int, member: models.GroupMember) -> int:
    """Remove only unlocked activity; retain any ledger linked to a payment."""
    candidate_periods = db.query(models.MonthlyPeriod).filter_by(group_id=group_id).all()
    period_ids = [
        period.id for period in candidate_periods
        if not _period_history_is_protected(db, period)
    ]
    expense_ids: set[int] = set()
    if period_ids:
        expense_ids.update(
            eid
            for (eid,) in db.query(models.Expense.id)
            .filter(
                models.Expense.period_id.in_(period_ids),
                models.Expense.payer_member_id == member.id,
            )
            .all()
        )
        expense_ids.update(
            eid
            for (eid,) in db.query(models.Expense.id)
            .join(models.ExpenseSplit)
            .filter(
                models.Expense.period_id.in_(period_ids),
                models.ExpenseSplit.member_id == member.id,
            )
            .all()
        )

    deleted = 0
    if expense_ids:
        expenses = db.query(models.Expense).filter(models.Expense.id.in_(expense_ids)).all()
        for expense in expenses:
            if expense.image_path and os.path.exists(expense.image_path):
                try:
                    os.remove(expense.image_path)
                except OSError:
                    pass
            db.delete(expense)
            deleted += 1

    # Keep the membership row as an archival reference. Protected periods load
    # it into their historical balance view; new, unlocked group activity does
    # not include it.
    member.is_active = False
    return deleted


@app.delete("/api/groups/{group_id}/members/{member_id}")
def remove_group_member(
    group_id: int,
    member_id: int,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_owner(group_id, account, db)
    target = (
        db.query(models.GroupMember)
        .filter_by(id=member_id, group_id=group_id)
        .first()
    )
    if not target:
        raise HTTPException(404, "Member not found")
    if target.account_id == account.id:
        raise HTTPException(400, "You cannot remove yourself. Transfer ownership or delete the group.")
    if target.role == "owner":
        owner_count = (
            db.query(models.GroupMember)
            .filter_by(group_id=group_id, role="owner")
            .count()
        )
        if owner_count <= 1:
            raise HTTPException(400, "Cannot remove the only owner")
    name = target.display_name
    removed_expenses = _purge_member_group_history(db, group_id, target)
    _record_activity(db, group_id, account, "member_removed", f"{name} was removed from active group activity", "member", member_id)
    _notify_group_members(db, group_id, f"{name} was removed from active group activity. Settled history is preserved.", f"/group.html?group={group_id}", exclude_account_id=account.id)
    db.commit()
    notify_group_change(group_id, "member-removed")
    msg = f"{name} was removed from active group activity"
    if removed_expenses:
        msg += f" and {removed_expenses} active expense{'s' if removed_expenses != 1 else ''} were removed"
    return {"ok": True, "message": msg}


@app.delete("/api/groups/{group_id}")
def delete_group(
    group_id: int,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_owner(group_id, account, db)
    group = db.query(models.Group).get(group_id)
    if not group:
        raise HTTPException(404, "Group not found")
    
    # Remove files
    group_uploads = _group_uploads_dir(group_id)
    if os.path.exists(group_uploads):
        shutil.rmtree(group_uploads, ignore_errors=True)
    group_reports = _group_reports_dir(group_id)
    if os.path.exists(group_reports):
        shutil.rmtree(group_reports, ignore_errors=True)

    db.delete(group)
    db.commit()
    return {"ok": True, "message": "Group deleted successfully"}


@app.post("/api/groups/{group_id}/leave")
def leave_group(
    group_id: int,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    member = require_group_member(group_id, account, db)
    if member.role == "owner":
        owner_count = (
            db.query(models.GroupMember)
            .filter_by(group_id=group_id, role="owner")
            .count()
        )
        if owner_count <= 1:
            raise HTTPException(400, "You are the sole owner of this group. Delete the group instead or transfer ownership first.")

    _purge_member_group_history(db, group_id, member)
    db.commit()
    notify_group_change(group_id, "member-left")
    return {"ok": True, "message": "Left group successfully"}


@app.post("/api/groups/{group_id}/invites", response_model=schemas.InviteCreateOut)
def create_group_invite(
    group_id: int,
    payload: schemas.InviteByEmailRequest,
    request: Request,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    check_rate_limit(request, limit=10, window_seconds=60)
    require_group_member(group_id, account, db)
    group = db.query(models.Group).get(group_id)
    if not group:
        raise HTTPException(404, "Group not found")

    email = str(payload.email).strip().lower()
    _validate_invite_email(email, account, group_id, db)
    invite_url = _invite_url(request, group.invite_code)

    already_member = False
    existing_account = db.query(models.Account).filter_by(email=email).first()
    if existing_account:
        membership = db.query(models.GroupMember).filter_by(group_id=group.id, account_id=existing_account.id).first()
        if membership:
            membership.is_active = True
            membership.display_name = existing_account.display_name
        else:
            db.add(models.GroupMember(group_id=group.id, account_id=existing_account.id, display_name=existing_account.display_name, role="member"))
        pending = (
            db.query(models.GroupInvite)
            .filter_by(group_id=group.id, email=email)
            .first()
        )
        if pending: pending.status = "accepted"
        already_member = True
    else:
        invite = (
            db.query(models.GroupInvite)
            .filter_by(group_id=group.id, email=email)
            .first()
        )
        if invite:
            invite.status = "pending"
        else:
            db.add(models.GroupInvite(group_id=group.id, email=email, status="pending"))

    db.commit()

    email_sent = False
    if is_email_configured():
        try:
            email_sent = bool(
                send_invite_email(
                    to_email=email,
                    group_name=group.name,
                    invite_url=invite_url,
                    inviter_name=account.display_name,
                )
            )
        except Exception:
            logger.exception("Failed to send invite email to %s", email)

    if already_member and email_sent:
        message = f"{email} was added to the group and emailed a join link."
    elif already_member:
        message = f"{email} was added to the group. Email is not configured — share the invite link if they need it."
    elif email_sent:
        message = f"Invite emailed to {email}."
    else:
        message = (
            f"Invite saved for {email}. Email is not configured on this server — "
            "copy the invite link below and send it yourself."
        )

    return schemas.InviteCreateOut(
        email=email,
        email_sent=email_sent,
        already_member=already_member,
        invite_url=invite_url,
        message=message,
    )


@app.get("/api/group-invites")
def list_group_invites(account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    rows = db.query(models.GroupInvite).filter_by(email=account.email.lower(), status="pending").all()
    return [{"id": row.id, "group_id": row.group_id, "group_name": row.group.name} for row in rows if row.group]


@app.post("/api/group-invites/{invite_id}/accept")
def accept_group_invite(invite_id: int, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    invite = db.query(models.GroupInvite).filter_by(id=invite_id, email=account.email.lower(), status="pending").first()
    if not invite: raise HTTPException(404, "Group invitation not found")
    if not db.query(models.GroupMember).filter_by(group_id=invite.group_id, account_id=account.id).first():
        db.add(models.GroupMember(group_id=invite.group_id, account_id=account.id, display_name=account.display_name, role="member"))
    invite.status = "accepted"; db.commit(); notify_group_change(invite.group_id, "member-joined")
    return {"ok": True}


@app.post("/api/group-invites/{invite_id}/decline")
def decline_group_invite(invite_id: int, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    invite = db.query(models.GroupInvite).filter_by(id=invite_id, email=account.email.lower(), status="pending").first()
    if not invite: raise HTTPException(404, "Group invitation not found")
    invite.status = "declined"; db.commit()
    return {"ok": True}


@app.post("/api/groups/join", response_model=schemas.GroupOut)
def join_group(
    payload: schemas.JoinGroupRequest,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    group = db.query(models.Group).filter_by(invite_code=payload.invite_code.strip()).first()
    if not group:
        raise HTTPException(404, "Invalid invite code")

    existing = (
        db.query(models.GroupMember)
        .filter_by(group_id=group.id, account_id=account.id)
        .first()
    )
    if not existing:
        member = models.GroupMember(
            group_id=group.id,
            account_id=account.id,
            display_name=account.display_name,
            role="member",
        )
        db.add(member)
        db.commit()
    return group


def _learned_vendors_for(db: Session, account_id: int) -> list[dict]:
    """Personal gazetteer: saved unique stores for this account, plus expense history."""
    from .ocr_extractor import vendor_name_key

    merged: dict[str, dict] = {}

    def _add(name: str, phones=None, store_ids=None, header_key: str = "", category: str = ""):
        label = (name or "").strip()
        if not label or label.lower().startswith("unknown"):
            return
        key = vendor_name_key(label)
        if len(key) < 3:
            return
        row = merged.get(key)
        if row is None:
            row = {
                "name": label,
                "phones": set(),
                "store_ids": set(),
                "header_key": "",
                "category": "",
            }
            merged[key] = row
        row["phones"].update(p for p in (phones or set()) if p)
        row["store_ids"].update(s for s in (store_ids or set()) if s)
        if header_key and not row["header_key"]:
            row["header_key"] = header_key
        cat = (category or "").strip()
        if cat and cat != "General":
            row["category"] = cat
        elif not row["category"] and cat:
            row["category"] = cat

    for row in db.query(models.AccountVendor).filter_by(account_id=account_id).all():
        phones = {row.phone} if row.phone else set()
        stores = {row.store_id} if row.store_id else set()
        _add(row.name, phones, stores, row.header_key or "", getattr(row, "category", "") or "")

    for store_name, category in (
        db.query(models.PersonalExpense.store_name, models.PersonalExpense.category)
        .filter_by(account_id=account_id)
        .distinct()
        .all()
    ):
        _add(store_name, category=category or "")

    member_ids = [
        mid for (mid,) in db.query(models.GroupMember.id).filter_by(account_id=account_id).all()
    ]
    if member_ids:
        for store_name, category in (
            db.query(models.Expense.store_name, models.Expense.category)
            .filter(models.Expense.payer_member_id.in_(member_ids))
            .distinct()
            .all()
        ):
            _add(store_name, category=category or "")

    return list(merged.values())[:500]


def _apply_scan_fingerprint(pending: models.PendingUpload, fingerprint: dict, store_name: str) -> None:
    pending.ocr_store_name = (store_name or "")[:150]
    pending.ocr_phones = ",".join((fingerprint or {}).get("phones") or [])[:200]
    pending.ocr_store_ids = ",".join((fingerprint or {}).get("store_ids") or [])[:80]
    pending.ocr_header_key = ((fingerprint or {}).get("header_key") or "")[:200]


def _upsert_account_vendor(
    db: Session,
    account_id: int,
    name: str,
    *,
    phone: str = "",
    store_id: str = "",
    header_key: str = "",
    category: str = "",
) -> None:
    from .ocr_extractor import vendor_name_key

    name_key = vendor_name_key(name)
    if len(name_key) < 3:
        return
    rows = db.query(models.AccountVendor).filter_by(account_id=account_id).all()
    row = None
    if phone:
        row = next((r for r in rows if r.phone == phone), None)
    if row is None and store_id:
        row = next((r for r in rows if r.store_id == store_id), None)
    if row is None:
        row = next((r for r in rows if (getattr(r, "name_key", None) or vendor_name_key(r.name)) == name_key), None)
    if row is None and header_key:
        row = next((r for r in rows if r.header_key == header_key), None)

    cat = (category or "").strip() or "General"
    if row:
        row.name = name
        if hasattr(row, "name_key"):
            row.name_key = name_key
        if phone:
            row.phone = phone
        if store_id:
            row.store_id = store_id
        if header_key:
            row.header_key = header_key
        if hasattr(row, "category"):
            row.category = cat or row.category or "General"
        return
    db.add(
        models.AccountVendor(
            account_id=account_id,
            name=name,
            name_key=name_key,
            phone=phone or "",
            store_id=store_id or "",
            header_key=header_key or "",
            category=cat,
        )
    )


def _remember_account_vendor(
    db: Session,
    account_id: int,
    chosen_name: str,
    pending: models.PendingUpload | None = None,
    category: str | None = None,
) -> None:
    """Write the personal gazetteer from a saved bill (user-confirmed name)."""
    if not account_id:
        return
    chosen = (chosen_name or "").strip()[:150]
    if not chosen or chosen.lower().startswith("unknown"):
        return
    phones = [p for p in ((pending.ocr_phones if pending else None) or "").split(",") if len(p) >= 10]
    store_ids = [s for s in ((pending.ocr_store_ids if pending else None) or "").split(",") if s]
    header_key = ((pending.ocr_header_key if pending else None) or "").strip()
    _upsert_account_vendor(
        db,
        account_id,
        chosen,
        phone=phones[0] if phones else "",
        store_id=store_ids[0] if store_ids else "",
        header_key=header_key,
        category=(category or "").strip(),
    )
    for sid in store_ids[1:]:
        _upsert_account_vendor(db, account_id, chosen, store_id=sid, category=(category or "").strip())


def _remember_vendor_correction(
    db: Session,
    account_id: int,
    pending: models.PendingUpload | None,
    chosen_name: str,
    category: str | None = None,
) -> None:
    _remember_account_vendor(db, account_id, chosen_name, pending=pending, category=category)


# ---------------------------------------------------------------------------
# Receipt Upload & VLM Extraction
# ---------------------------------------------------------------------------
@app.post("/api/groups/{group_id}/receipts/upload", response_model=schemas.ExtractionResponse)
async def upload_receipt(
    group_id: int,
    file: UploadFile = File(...),
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    _require_shareable_group(db, group_id)

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in [".jpg", ".jpeg", ".png", ".webp", ".heic"]:
        raise HTTPException(400, "Unsupported image format (JPEG, PNG, WEBP allowed)")

    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(400, f"File exceeds maximum size of {MAX_FILE_SIZE // (1024*1024)}MB")

    upload_id = uuid.uuid4().hex
    tmp_filename = f"{group_id}_{upload_id}{ext}"
    tmp_path = os.path.join(TMP_UPLOADS_DIR, tmp_filename)

    with open(tmp_path, "wb") as f:
        f.write(contents)

    pending = models.PendingUpload(id=upload_id, group_id=group_id, account_id=account.id, file_path=tmp_path)
    db.add(pending)
    db.commit()

    mime = file.content_type or "image/jpeg"
    vlm_config = resolve_vlm_config(db, account.id, group_id)
    extraction, source, message, fingerprint = extract_receipt_data(
        contents, mime, vlm_config, learned_vendors=_learned_vendors_for(db, account.id),
    )
    message = _public_scan_message(message)
    _apply_scan_fingerprint(pending, fingerprint, extraction.store_name)
    db.add(pending)
    db.commit()

    return schemas.ExtractionResponse(
        upload_id=upload_id,
        image_url=f"/api/groups/{group_id}/receipts/tmp/{tmp_filename}",
        extraction=extraction,
        source=source,
        message=message,
    )


@app.get("/api/groups/{group_id}/receipts/tmp/{filename}")
def get_tmp_receipt(
    group_id: int,
    filename: str,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    # Strictly enforce access control
    if not filename.startswith(f"{group_id}_"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied to receipt")

    path = os.path.join(TMP_UPLOADS_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(404, "Receipt not found")
    return FileResponse(path)


# ---------------------------------------------------------------------------
# Expenses CRUD
# ---------------------------------------------------------------------------
def _validate_expense_members(db: Session, group_id: int, payer_id: int, participant_ids: list[int]) -> None:
    if not participant_ids:
        raise HTTPException(400, "Split must include at least one member")
    member_ids = set(participant_ids + [payer_id])
    existing = {
        m.id
        for m in db.query(models.GroupMember.id)
        .filter(
            models.GroupMember.group_id == group_id,
            models.GroupMember.id.in_(member_ids),
            or_(models.GroupMember.is_active.is_(True), models.GroupMember.is_active.is_(None)),
        )
        .all()
    }
    for mid in member_ids:
        if mid not in existing:
            raise HTTPException(400, f"Member id {mid} is not in this group")


def _apply_expense_splits_custom(
    db: Session,
    expense: models.Expense,
    total_amount: float,
    split_type: str,
    split_with: Optional[list[int]],
    split_details: Optional[list[schemas.SplitDetailInput]],
) -> None:
    for split in list(expense.splits):
        db.delete(split)
    db.flush()

    split_type = (split_type or "EQUAL").upper()
    
    # Build inputs for calculate_splits
    if split_details and len(split_details) > 0:
        inputs = {item.member_id: (item.value if item.value is not None else 1.0) for item in split_details}
    elif split_with and len(split_with) > 0:
        inputs = split_with
    else:
        raise HTTPException(400, "Must specify members to split with")

    try:
        remainder_offset = 0
        if split_type == "EQUAL":
            prior = (
                db.query(models.Expense)
                .filter(
                    models.Expense.period_id == expense.period_id,
                    models.Expense.split_type == "EQUAL",
                    models.Expense.id < expense.id,
                )
                .count()
            )
            remainder_offset = prior
        shares = calculate_splits(split_type, total_amount, inputs, remainder_offset=remainder_offset)
    except ValueError as e:
        raise HTTPException(400, str(e))

    for mid, amount in shares.items():
        val = None
        if isinstance(inputs, dict) and mid in inputs:
            val = Decimal(str(inputs[mid]))
        db.add(
            models.ExpenseSplit(
                expense_id=expense.id,
                member_id=mid,
                share_amount=amount,
                split_value=val,
            )
        )


@app.post("/api/groups/{group_id}/expenses", response_model=schemas.ExpenseOut)
def create_expense(
    group_id: int,
    payload: schemas.ExpenseCreate,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    _require_shareable_group(db, group_id)
    group = db.query(models.Group).get(group_id)
    if not group:
        raise HTTPException(404, "Group not found")

    final_path = None
    pending = None
    if payload.upload_id:
        pending = (
            db.query(models.PendingUpload)
            .filter_by(id=payload.upload_id, group_id=group_id)
            .first()
        )
        if not pending or not os.path.exists(pending.file_path):
            raise HTTPException(400, "Upload not found or already used. Re-upload the receipt.")
        ext = os.path.splitext(pending.file_path)[1]
        final_filename = f"{uuid.uuid4().hex}{ext}"
        group_uploads = _group_uploads_dir(group_id)
        final_path = os.path.join(group_uploads, final_filename)
        shutil.move(pending.file_path, final_path)

    participant_ids = []
    if payload.split_details:
        participant_ids = [d.member_id for d in payload.split_details]
    elif payload.split_with:
        participant_ids = payload.split_with
    else:
        raise HTTPException(400, "Must specify members to split with")

    _validate_expense_members(db, group_id, payload.payer_member_id, participant_ids)

    period_key = payload.period_key or _period_key_for(payload.bill_date)
    period = get_or_create_period(db, group_id, period_key)
    if period.status == "SETTLED":
        raise HTTPException(400, f"Period {period_key} is already settled; cannot add expenses")

    expense = models.Expense(
        period_id=period.id,
        store_name=payload.store_name.strip(),
        bill_date=payload.bill_date,
        total_amount=Decimal(str(payload.total_amount)),
        currency=_normalize_currency(payload.currency, group.currency or "USD"),
        category=payload.category or "General",
        split_type=payload.split_type or "EQUAL",
        payer_member_id=payload.payer_member_id,
        image_path=final_path,
    )
    db.add(expense)
    db.flush()

    _apply_expense_splits_custom(
        db,
        expense,
        payload.total_amount,
        payload.split_type or "EQUAL",
        payload.split_with,
        payload.split_details,
    )

    _remember_account_vendor(
        db, account.id, payload.store_name, pending=pending, category=payload.category,
    )
    if pending:
        db.delete(pending)

    db.commit()
    db.refresh(expense)
    _record_activity(db, group_id, account, "expense_created", f"{account.display_name} added {expense.store_name}", "expense", expense.id)
    _notify_group_members(db, group_id, f"{account.display_name} added {expense.store_name} to this group.", f"/group.html?group={group_id}", exclude_account_id=account.id)
    db.commit()
    notify_group_change(group_id, "expense-created")
    return expense


@app.put("/api/groups/{group_id}/expenses/{expense_id}", response_model=schemas.ExpenseOut)
def update_expense(
    group_id: int,
    expense_id: int,
    payload: schemas.ExpenseUpdate,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
    owner_override: bool = Query(False),
):
    require_group_member(group_id, account, db)
    _require_shareable_group(db, group_id)
    expense = db.query(models.Expense).get(expense_id)
    if not expense or expense.period.group_id != group_id:
        raise HTTPException(404, "Expense not found")
    was_locked = _require_expense_editable(db, expense.period, account, group_id, owner_override)

    participant_ids = []
    if payload.split_details:
        participant_ids = [d.member_id for d in payload.split_details]
    elif payload.split_with:
        participant_ids = payload.split_with

    _validate_expense_members(db, group_id, payload.payer_member_id, participant_ids)

    period_key = payload.period_key or _period_key_for(payload.bill_date)
    period = get_or_create_period(db, group_id, period_key)

    expense.period_id = period.id
    expense.store_name = payload.store_name.strip()
    expense.bill_date = payload.bill_date
    expense.total_amount = Decimal(str(payload.total_amount))
    expense.currency = _normalize_currency(payload.currency, expense.currency or "USD")
    expense.category = payload.category or expense.category or "General"
    expense.split_type = payload.split_type or expense.split_type or "EQUAL"
    expense.payer_member_id = payload.payer_member_id

    _apply_expense_splits_custom(
        db,
        expense,
        payload.total_amount,
        expense.split_type,
        payload.split_with,
        payload.split_details,
    )

    _remember_account_vendor(db, account.id, expense.store_name, category=expense.category)

    db.commit()
    db.refresh(expense)
    _record_activity(db, group_id, account, "expense_updated", f"{account.display_name} updated {expense.store_name}", "expense", expense.id)
    if was_locked:
        _notify_group_members(db, group_id, f"{account.display_name} corrected {expense.store_name}. Recorded payments remain applied; check your recalculated balance.", f"/group.html?group={group_id}", exclude_account_id=account.id)
    else:
        _notify_group_members(db, group_id, f"{account.display_name} updated {expense.store_name}.", f"/group.html?group={group_id}", exclude_account_id=account.id)
    db.commit()
    notify_group_change(group_id, "expense-updated")
    return expense


@app.delete("/api/groups/{group_id}/expenses/{expense_id}")
def delete_expense(
    group_id: int,
    expense_id: int,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
    owner_override: bool = Query(False),
):
    require_group_member(group_id, account, db)
    expense = db.query(models.Expense).get(expense_id)
    if not expense or expense.period.group_id != group_id:
        raise HTTPException(404, "Expense not found")
    was_locked = _require_expense_editable(db, expense.period, account, group_id, owner_override)

    if expense.image_path and os.path.exists(expense.image_path):
        try:
            os.remove(expense.image_path)
        except OSError:
            pass

    store_name = expense.store_name
    db.delete(expense)
    _record_activity(db, group_id, account, "expense_deleted", f"{account.display_name} deleted {store_name}", "expense", expense_id)
    text = f"{account.display_name} {'corrected the period by deleting' if was_locked else 'deleted'} {store_name}."
    if was_locked:
        text += " Recorded payments remain applied; check your recalculated balance."
    _notify_group_members(db, group_id, text, f"/group.html?group={group_id}", exclude_account_id=account.id)
    db.commit()
    notify_group_change(group_id, "expense-deleted")
    return {"ok": True, "message": "Expense deleted"}


@app.get("/api/groups/{group_id}/expenses", response_model=list[schemas.ExpenseOut])
def list_expenses(
    group_id: int,
    period_key: Optional[str] = None,
    category: Optional[str] = None,
    search: Optional[str] = Query(None, max_length=100),
    member_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    min_amount: Optional[float] = Query(None, ge=0),
    max_amount: Optional[float] = Query(None, ge=0),
    involved_me: bool = False,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    current_member = require_group_member(group_id, account, db)
    q = (
        db.query(models.Expense)
        .join(models.MonthlyPeriod)
        .filter(models.MonthlyPeriod.group_id == group_id)
    )
    if period_key:
        q = q.filter(models.MonthlyPeriod.period_key == period_key)
    if category and category != "All":
        q = q.filter(models.Expense.category == category)
    if search:
        term = f"%{search.strip()}%"
        q = q.filter(or_(models.Expense.store_name.ilike(term), models.Expense.category.ilike(term)))
    if member_id:
        q = q.filter(or_(models.Expense.payer_member_id == member_id, models.Expense.splits.any(models.ExpenseSplit.member_id == member_id)))
    if involved_me:
        q = q.filter(or_(models.Expense.payer_member_id == current_member.id, models.Expense.splits.any(models.ExpenseSplit.member_id == current_member.id)))
    if date_from:
        q = q.filter(models.Expense.bill_date >= date_from)
    if date_to:
        q = q.filter(models.Expense.bill_date <= date_to)
    if min_amount is not None:
        q = q.filter(models.Expense.total_amount >= Decimal(str(min_amount)))
    if max_amount is not None:
        q = q.filter(models.Expense.total_amount <= Decimal(str(max_amount)))

    return q.order_by(models.Expense.bill_date.desc(), models.Expense.id.desc()).all()


def _group_expense_or_404(db: Session, group_id: int, expense_id: int) -> models.Expense:
    expense = db.query(models.Expense).get(expense_id)
    if not expense or expense.period.group_id != group_id:
        raise HTTPException(404, "Expense not found")
    return expense


@app.get("/api/groups/{group_id}/expenses/{expense_id}/comments", response_model=list[schemas.ExpenseCommentOut])
def list_expense_comments(group_id: int, expense_id: int, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    require_group_member(group_id, account, db)
    _group_expense_or_404(db, group_id, expense_id)
    rows = db.query(models.ExpenseComment).filter_by(expense_id=expense_id).order_by(models.ExpenseComment.created_at.asc()).all()
    return [schemas.ExpenseCommentOut(id=row.id, body=row.body, author_name=row.account.display_name, created_at=row.created_at, updated_at=row.updated_at) for row in rows]


@app.post("/api/groups/{group_id}/expenses/{expense_id}/comments", response_model=schemas.ExpenseCommentOut)
def create_expense_comment(group_id: int, expense_id: int, payload: schemas.ExpenseCommentCreate, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    require_group_member(group_id, account, db)
    expense = _group_expense_or_404(db, group_id, expense_id)
    body = payload.body.strip()
    if not body:
        raise HTTPException(400, "A comment cannot be empty")
    comment = models.ExpenseComment(expense_id=expense.id, account_id=account.id, body=body)
    db.add(comment)
    _record_activity(db, group_id, account, "comment_added", f"{account.display_name} commented on {expense.store_name}", "expense", expense.id)
    _notify_group_members(db, group_id, f"{account.display_name} commented on {expense.store_name}.", f"/group.html?group={group_id}", exclude_account_id=account.id)
    db.commit()
    db.refresh(comment)
    notify_group_change(group_id, "comment-added")
    return schemas.ExpenseCommentOut(id=comment.id, body=comment.body, author_name=account.display_name, created_at=comment.created_at, updated_at=comment.updated_at)


@app.get("/api/groups/{group_id}/activity", response_model=list[schemas.GroupActivityOut])
def list_group_activity(group_id: int, limit: int = Query(30, ge=1, le=100), account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    require_group_member(group_id, account, db)
    rows = db.query(models.GroupActivity).filter_by(group_id=group_id).order_by(models.GroupActivity.created_at.desc(), models.GroupActivity.id.desc()).limit(limit).all()
    return [schemas.GroupActivityOut(id=row.id, event_type=row.event_type, summary=row.summary, actor_name=row.actor.display_name if row.actor else None, entity_type=row.entity_type, entity_id=row.entity_id, created_at=row.created_at) for row in rows]


@app.get("/api/notifications", response_model=list[schemas.NotificationOut])
def list_notifications(unread_only: bool = False, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    query = db.query(models.Notification).filter_by(account_id=account.id)
    if unread_only:
        query = query.filter(models.Notification.read_at.is_(None))
    return query.order_by(models.Notification.created_at.desc(), models.Notification.id.desc()).limit(50).all()


@app.post("/api/notifications/{notification_id}/read", response_model=schemas.NotificationOut)
def mark_notification_read(notification_id: int, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    row = db.query(models.Notification).filter_by(id=notification_id, account_id=account.id).first()
    if not row:
        raise HTTPException(404, "Notification not found")
    row.read_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Periods, Balances, Settlements, Reports
# ---------------------------------------------------------------------------
@app.get("/api/groups/{group_id}/periods/{period_key}/payments", response_model=list[schemas.GroupPaymentOut])
def list_group_payments(group_id: int, period_key: str, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    require_group_member(group_id, account, db)
    period = db.query(models.MonthlyPeriod).filter_by(group_id=group_id, period_key=period_key).first()
    if not period:
        return []
    return db.query(models.GroupPayment).filter_by(period_id=period.id).order_by(models.GroupPayment.created_at.desc()).all()


@app.post("/api/groups/{group_id}/periods/{period_key}/payments", response_model=schemas.GroupPaymentOut)
def mark_group_payment_paid(group_id: int, period_key: str, payload: schemas.GroupPaymentCreate, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    payer = require_group_member(group_id, account, db)
    period = db.query(models.MonthlyPeriod).filter_by(group_id=group_id, period_key=period_key).first()
    if not period:
        raise HTTPException(404, "Period not found")
    if period.status != "SETTLED":
        raise HTTPException(400, "Settle this period before recording payments")
    if payer.id != payload.payer_member_id:
        raise HTTPException(403, "Only the payer can mark this payment as paid")
    _validate_expense_members(db, group_id, payload.payer_member_id, [payload.recipient_member_id])
    if payload.payer_member_id == payload.recipient_member_id:
        raise HTTPException(400, "A member cannot pay themselves")
    currency = _normalize_currency(payload.currency, "USD")
    expense_cutoff_id = db.query(func.max(models.Expense.id)).filter_by(period_id=period.id).scalar()
    payment = models.GroupPayment(
        period_id=period.id, payer_member_id=payload.payer_member_id,
        recipient_member_id=payload.recipient_member_id,
        amount=Decimal(str(payload.amount)), currency=currency,
        status="PAID", expense_cutoff_id=expense_cutoff_id, marked_paid_at=datetime.now(timezone.utc),
    )
    db.add(payment)
    recipient = db.query(models.GroupMember).get(payload.recipient_member_id)
    _record_activity(db, group_id, account, "payment_marked_paid", f"{payer.display_name} marked a payment of {currency} {payload.amount:.2f} as paid to {recipient.display_name}", "payment", None)
    _notify_group_members(db, group_id, f"{payer.display_name} marked a {currency} {payload.amount:.2f} payment as paid to {recipient.display_name}.", f"/group.html?group={group_id}", exclude_account_id=account.id)
    db.commit()
    db.refresh(payment)
    notify_group_change(group_id, "payment-paid")
    return payment


@app.post("/api/groups/{group_id}/periods/{period_key}/payments/received", response_model=schemas.GroupPaymentOut)
def mark_group_payment_received(group_id: int, period_key: str, payload: schemas.GroupPaymentCreate, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    """Recipient shortcut: record a transfer as received and confirmed at once."""
    recipient = require_group_member(group_id, account, db)
    period = db.query(models.MonthlyPeriod).filter_by(group_id=group_id, period_key=period_key).first()
    if not period:
        raise HTTPException(404, "Period not found")
    if period.status != "SETTLED":
        raise HTTPException(400, "Settle this period before recording payments")
    if recipient.id != payload.recipient_member_id:
        raise HTTPException(403, "Only the recipient can mark this payment as received")
    _validate_expense_members(db, group_id, payload.payer_member_id, [payload.recipient_member_id])
    if payload.payer_member_id == payload.recipient_member_id:
        raise HTTPException(400, "A member cannot pay themselves")
    currency = _normalize_currency(payload.currency, "USD")
    now = datetime.now(timezone.utc)
    expense_cutoff_id = db.query(func.max(models.Expense.id)).filter_by(period_id=period.id).scalar()
    payment = models.GroupPayment(
        period_id=period.id, payer_member_id=payload.payer_member_id,
        recipient_member_id=payload.recipient_member_id,
        amount=Decimal(str(payload.amount)), currency=currency,
        status="CONFIRMED", expense_cutoff_id=expense_cutoff_id, marked_paid_at=now, confirmed_at=now,
    )
    db.add(payment)
    payer = db.query(models.GroupMember).get(payload.payer_member_id)
    _record_activity(db, group_id, account, "payment_received", f"{recipient.display_name} recorded {currency} {payload.amount:.2f} received from {payer.display_name}", "payment", None)
    _notify_group_members(db, group_id, f"{recipient.display_name} recorded a {currency} {payload.amount:.2f} payment received from {payer.display_name}. Balances were recalculated.", f"/group.html?group={group_id}", exclude_account_id=account.id)
    db.commit()
    db.refresh(payment)
    notify_group_change(group_id, "payment-received")
    return payment


@app.post("/api/groups/{group_id}/payments/{payment_id}/confirm", response_model=schemas.GroupPaymentOut)
def confirm_group_payment(group_id: int, payment_id: int, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    recipient = require_group_member(group_id, account, db)
    payment = db.query(models.GroupPayment).join(models.MonthlyPeriod).filter(
        models.GroupPayment.id == payment_id, models.MonthlyPeriod.group_id == group_id,
    ).first()
    if not payment:
        raise HTTPException(404, "Payment not found")
    if payment.period.status != "SETTLED":
        raise HTTPException(400, "Settle this period before confirming payments")
    if payment.recipient_member_id != recipient.id:
        raise HTTPException(403, "Only the recipient can confirm this payment")
    if payment.status != "PAID":
        raise HTTPException(400, "This payment cannot be confirmed")
    payment.status = "CONFIRMED"
    payment.confirmed_at = datetime.now(timezone.utc)
    payer = db.query(models.GroupMember).get(payment.payer_member_id)
    _record_activity(db, group_id, account, "payment_confirmed", f"{recipient.display_name} confirmed payment from {payer.display_name}", "payment", payment.id)
    _notify_group_members(db, group_id, f"{recipient.display_name} confirmed a {payment.currency} {float(payment.amount):.2f} payment from {payer.display_name}. Balances were recalculated.", f"/group.html?group={group_id}", exclude_account_id=account.id)
    db.commit()
    db.refresh(payment)
    notify_group_change(group_id, "payment-confirmed")
    return payment


@app.get("/api/groups/{group_id}/dashboard", response_model=schemas.GroupDashboardOut)
def get_group_dashboard(group_id: int, period_key: str, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    require_group_member(group_id, account, db)
    group = db.query(models.Group).get(group_id)
    period = db.query(models.MonthlyPeriod).filter_by(group_id=group_id, period_key=period_key).first()
    if not period:
        return schemas.GroupDashboardOut(total_spent=0, currency=(group.currency if group else "USD"), category_totals={}, member_totals=[], largest_expenses=[])
    balances, _, expenses = build_balances_and_settlements(db, group_id, period)
    category_totals: dict[str, float] = {}
    for expense in expenses:
        category = expense.category or "General"
        category_totals[category] = round(category_totals.get(category, 0) + float(expense.total_amount), 2)
    return schemas.GroupDashboardOut(
        total_spent=round(sum(float(expense.total_amount) for expense in expenses), 2),
        currency=group.currency or "USD", category_totals=category_totals,
        member_totals=balances,
        largest_expenses=sorted(expenses, key=lambda expense: float(expense.total_amount), reverse=True)[:5],
    )

@app.get("/api/groups/{group_id}/periods", response_model=list[schemas.PeriodOut])
def list_periods(
    group_id: int,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    periods = (
        db.query(models.MonthlyPeriod)
        .filter_by(group_id=group_id)
        .order_by(models.MonthlyPeriod.period_key.desc())
        .all()
    )
    return periods


@app.get("/api/groups/{group_id}/periods/{period_key}/balances", response_model=list[schemas.BalanceOut])
def get_balances(
    group_id: int,
    period_key: str,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    period = db.query(models.MonthlyPeriod).filter_by(group_id=group_id, period_key=period_key).first()
    if not period:
        members = _members_as_dicts(db, group_id)
        raw = compute_balances(members, [])
        group = db.query(models.Group).get(group_id)
        curr = (group.currency if group else "USD") or "USD"
        return [{"currency": curr, **v} for v in raw.values()]
    balances, _, _ = build_balances_and_settlements(db, group_id, period)
    return balances


@app.get("/api/groups/{group_id}/periods/{period_key}/settlements", response_model=list[schemas.SettlementOut])
def get_settlements(
    group_id: int,
    period_key: str,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    period = db.query(models.MonthlyPeriod).filter_by(group_id=group_id, period_key=period_key).first()
    if not period:
        return []
    _, settlements, _ = build_balances_and_settlements(db, group_id, period)
    return settlements


@app.post("/api/groups/{group_id}/periods/{period_key}/reopen", response_model=schemas.PeriodOut)
def reopen_period(
    group_id: int,
    period_key: str,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    period = db.query(models.MonthlyPeriod).filter_by(group_id=group_id, period_key=period_key).first()
    if not period:
        raise HTTPException(404, "Period not found")
    if period.status != "SETTLED":
        raise HTTPException(400, "Period is not settled")

    period.status = "ACTIVE"
    period.pdf_filename = None
    period.pdf_path = None
    period.settled_at = None
    _record_activity(db, group_id, account, "period_reopened", f"{account.display_name} reopened {period_key}", "period", period.id)
    _notify_group_members(db, group_id, f"{account.display_name} reopened the {period_key} period. Balances may have changed.", f"/group.html?group={group_id}", exclude_account_id=account.id)
    db.commit()
    db.refresh(period)
    notify_group_change(group_id, "period-reopened")
    return period


@app.post("/api/groups/{group_id}/periods/{period_key}/generate-report")
def generate_report(
    group_id: int,
    period_key: str,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    period = get_or_create_period(db, group_id, period_key)
    # Re-exporting is read-only: allow a settled period's report to be
    # regenerated when payment confirmations have been recorded afterwards.

    group = db.query(models.Group).get(group_id)
    balances, settlements, expenses = build_balances_and_settlements(db, group_id, period)

    members_by_id = {m.id: m.display_name for m in db.query(models.GroupMember).filter_by(group_id=group_id).all()}
    expense_dicts = []
    for e in expenses:
        participant_names = [members_by_id.get(s.member_id, "?") for s in e.splits]
        expense_dicts.append({
            "bill_date": e.bill_date.isoformat(),
            "store_name": e.store_name,
            "category": e.category or "General",
            "payer_name": members_by_id.get(e.payer_member_id, "?"),
            "participant_names": participant_names,
            "total_amount": float(e.total_amount),
            "currency": e.currency or group.currency or "USD",
            "image_path": e.image_path,
        })

    reports_dir = _group_reports_dir(group_id)
    try:
        pdf_path, filename = generate_monthly_pdf(
            period.period_key,
            balances,
            settlements,
            expense_dicts,
            reports_dir,
            group_name=group.name,
            base_currency=group.currency or "USD",
            target_currency=period.target_currency,
            fx_rate=float(period.settlement_fx_rate) if period.settlement_fx_rate else None,
        )
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc

    period.pdf_filename = filename
    period.pdf_path = pdf_path
    period.status = "SETTLED"
    period.settled_at = datetime.now()
    _record_activity(db, group_id, account, "period_settled", f"{account.display_name} settled {period_key}", "period", period.id)
    _notify_group_members(db, group_id, f"{account.display_name} settled the {period_key} period. Review your consolidated Friend balance.", f"/group.html?group={group_id}", exclude_account_id=account.id)
    db.commit()
    notify_group_change(group_id, "period-settled")

    return {"ok": True, "filename": filename, "pdf_url": f"/api/groups/{group_id}/reports/{filename}"}


@app.get("/api/groups/{group_id}/reports/{filename}")
def download_group_report(
    group_id: int,
    filename: str,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    reports_dir = _group_reports_dir(group_id)
    safe_name = os.path.basename(filename)
    path = os.path.join(reports_dir, safe_name)
    if not os.path.isfile(path):
        raise HTTPException(404, "Report not found")
    return FileResponse(path, media_type="application/pdf", filename=safe_name)


# ---------------------------------------------------------------------------
# Personal Expenses Endpoints
# ---------------------------------------------------------------------------
def _claim_personal_upload(
    db: Session, account_id: int, upload_id: str, chosen_name: str | None = None,
) -> str:
    pending = (
        db.query(models.PendingUpload)
        .filter_by(id=upload_id, account_id=account_id)
        .first()
    )
    src = pending.file_path if pending and os.path.exists(pending.file_path) else None
    if not src:
        prefix = f"personal_{account_id}_{upload_id}"
        try:
            for name in os.listdir(TMP_UPLOADS_DIR):
                if name.startswith(prefix):
                    src = os.path.join(TMP_UPLOADS_DIR, name)
                    break
        except FileNotFoundError:
            src = None
    if pending and chosen_name:
        _remember_account_vendor(db, account_id, chosen_name, pending=pending)
    if pending:
        db.delete(pending)
    if not src or not os.path.exists(src):
        raise HTTPException(400, "Upload not found or already used. Re-upload the receipt.")
    ext = os.path.splitext(src)[1]
    dest = os.path.join(_personal_uploads_dir(account_id), f"{uuid.uuid4().hex}{ext}")
    shutil.move(src, dest)
    return dest


@app.get("/api/personal/summary", response_model=schemas.PersonalMonthlySummaryOut)
def get_personal_summary(
    period_key: Optional[str] = None,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    if not period_key:
        period_key = _period_key_for(date.today())

    year, month = map(int, period_key.split("-"))
    _, last_day = monthrange(year, month)
    start_d = date(year, month, 1)
    end_d = date(year, month, last_day)

    expenses = (
        db.query(models.PersonalExpense)
        .filter(
            models.PersonalExpense.account_id == account.id,
            models.PersonalExpense.bill_date >= start_d,
            models.PersonalExpense.bill_date <= end_d,
        )
        .order_by(models.PersonalExpense.bill_date.desc(), models.PersonalExpense.id.desc())
        .all()
    )

    settings = get_account_settings(db, account.id)
    curr = (settings.preferred_currency or "USD").upper()

    total_spent = 0.0
    category_breakdown: Dict[str, float] = {}
    for e in expenses:
        src = (e.currency or "USD").upper()
        converted, _rate = convert_amount(e.total_amount, src, curr)
        amt = float(converted)
        total_spent += amt
        cat = e.category or "General"
        category_breakdown[cat] = round(category_breakdown.get(cat, 0.0) + amt, 2)

    return schemas.PersonalMonthlySummaryOut(
        period_key=period_key,
        total_spent=round(total_spent, 2),
        currency=curr,
        category_breakdown=category_breakdown,
        expense_count=len(expenses),
        expenses=expenses,
    )


@app.post("/api/personal/expenses", response_model=schemas.PersonalExpenseOut)
def create_personal_expense(
    payload: schemas.PersonalExpenseCreate,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    final_path = None
    if payload.upload_id:
        final_path = _claim_personal_upload(db, account.id, payload.upload_id, payload.store_name)

    expense = models.PersonalExpense(
        account_id=account.id,
        store_name=payload.store_name.strip(),
        bill_date=payload.bill_date,
        total_amount=Decimal(str(payload.total_amount)),
        currency=_normalize_currency(payload.currency, "USD"),
        category=payload.category or "General",
        notes=payload.notes,
        image_path=final_path,
    )
    db.add(expense)
    _remember_account_vendor(db, account.id, expense.store_name, category=expense.category)
    db.commit()
    db.refresh(expense)
    return expense


@app.put("/api/personal/expenses/{expense_id}", response_model=schemas.PersonalExpenseOut)
def update_personal_expense(
    expense_id: int,
    payload: schemas.PersonalExpenseUpdate,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    expense = db.query(models.PersonalExpense).filter_by(id=expense_id, account_id=account.id).first()
    if not expense:
        raise HTTPException(404, "Personal expense not found")

    expense.store_name = payload.store_name.strip()
    expense.bill_date = payload.bill_date
    expense.total_amount = Decimal(str(payload.total_amount))
    expense.currency = _normalize_currency(payload.currency, expense.currency or "USD")
    expense.category = payload.category or expense.category or "General"
    expense.notes = payload.notes

    _remember_account_vendor(db, account.id, expense.store_name, category=expense.category)

    db.commit()
    db.refresh(expense)
    return expense


@app.delete("/api/personal/expenses/{expense_id}")
def delete_personal_expense(
    expense_id: int,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    expense = db.query(models.PersonalExpense).filter_by(id=expense_id, account_id=account.id).first()
    if not expense:
        raise HTTPException(404, "Personal expense not found")

    if expense.image_path and os.path.exists(expense.image_path):
        try:
            os.remove(expense.image_path)
        except OSError:
            pass

    db.delete(expense)
    db.commit()
    return {"ok": True, "message": "Personal expense deleted"}


@app.post("/api/personal/receipts/upload", response_model=schemas.ExtractionResponse)
async def upload_personal_receipt(
    file: UploadFile = File(...),
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in [".jpg", ".jpeg", ".png", ".webp", ".heic"]:
        raise HTTPException(400, "Unsupported image format")

    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(400, f"File exceeds maximum size of {MAX_FILE_SIZE // (1024*1024)}MB")

    upload_id = uuid.uuid4().hex
    tmp_filename = f"personal_{account.id}_{upload_id}{ext}"
    tmp_path = os.path.join(TMP_UPLOADS_DIR, tmp_filename)

    with open(tmp_path, "wb") as f:
        f.write(contents)

    pending = models.PendingUpload(id=upload_id, account_id=account.id, file_path=tmp_path)
    db.add(pending)
    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not record personal upload in the database; keeping the temp file")
        _ensure_schema()

    mime = file.content_type or "image/jpeg"
    vlm_config = resolve_vlm_config(db, account.id)
    extraction, source, message, fingerprint = extract_receipt_data(
        contents, mime, vlm_config, learned_vendors=_learned_vendors_for(db, account.id),
    )
    message = _public_scan_message(message)
    _apply_scan_fingerprint(pending, fingerprint, extraction.store_name)
    try:
        db.add(pending)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not store OCR fingerprint on personal upload")

    return schemas.ExtractionResponse(
        upload_id=upload_id,
        image_url=f"/api/personal/receipts/tmp/{tmp_filename}",
        extraction=extraction,
        source=source,
        message=message,
    )


@app.get("/api/personal/receipts/tmp/{filename}")
def get_personal_tmp_receipt(
    filename: str,
    account: models.Account = Depends(get_current_account),
):
    if not filename.startswith(f"personal_{account.id}_"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")

    path = os.path.join(TMP_UPLOADS_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(404, "Receipt not found")
    return FileResponse(path)


@app.get("/api/personal/reports/{period_key}/pdf")
def generate_personal_report_pdf(
    period_key: str,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    year, month = map(int, period_key.split("-"))
    _, last_day = monthrange(year, month)
    start_d = date(year, month, 1)
    end_d = date(year, month, last_day)

    expenses = (
        db.query(models.PersonalExpense)
        .filter(
            models.PersonalExpense.account_id == account.id,
            models.PersonalExpense.bill_date >= start_d,
            models.PersonalExpense.bill_date <= end_d,
        )
        .order_by(models.PersonalExpense.bill_date.asc())
        .all()
    )

    if not pdf_reports_available():
        raise HTTPException(
            503,
            "PDF export needs WeasyPrint. Use the venv from run.ps1 (pip install -r requirements.txt).",
        )

    settings = get_account_settings(db, account.id)
    dest_currency = (settings.preferred_currency or "USD").upper()

    expense_dicts = []
    for e in expenses:
        src = (e.currency or "USD").upper()
        converted, _rate = convert_amount(e.total_amount, src, dest_currency)
        expense_dicts.append(
            {
                "bill_date": e.bill_date.isoformat(),
                "store_name": e.store_name,
                "category": e.category or "General",
                "total_amount": float(e.total_amount),
                "converted_amount": float(converted),
                "currency": src,
                "notes": e.notes,
                "image_path": e.image_path,
            }
        )

    reports_dir = _personal_reports_dir(account.id)
    try:
        pdf_path, filename = generate_personal_pdf(
            period_key=period_key,
            user_name=account.display_name,
            expenses=expense_dicts,
            output_dir=reports_dir,
            currency=dest_currency,
        )
    except Exception as exc:
        logging.getLogger(__name__).exception("Personal PDF failed")
        raise HTTPException(500, f"Could not generate PDF: {exc}") from exc

    return FileResponse(pdf_path, media_type="application/pdf", filename=filename)


# ---------------------------------------------------------------------------
# Settings Endpoints
# ---------------------------------------------------------------------------
@app.get("/api/settings/account", response_model=schemas.AccountSettingsOut)
def get_user_settings(account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    settings = get_account_settings(db, account.id)
    return account_settings_out(settings)


@app.put("/api/settings/account", response_model=schemas.AccountSettingsOut)
def update_user_settings(
    payload: schemas.AccountSettingsUpdate,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    settings = update_account_settings(db, account.id, payload)
    return account_settings_out(settings)


@app.get("/api/settings/groups/{group_id}", response_model=schemas.GroupSettingsOut)
def get_group_settings(
    group_id: int,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    settings = get_group_member_settings(db, group_id, account.id)
    return group_settings_out(settings)


@app.put("/api/settings/groups/{group_id}", response_model=schemas.GroupSettingsOut)
def update_group_settings_endpoint(
    group_id: int,
    payload: schemas.GroupSettingsUpdate,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    settings = update_group_settings(db, group_id, account.id, payload)
    return group_settings_out(settings)


@app.get("/api/vlm/providers", response_model=list[schemas.VlmProviderOption])
def list_vlm_providers():
    return provider_catalog()


# ---------------------------------------------------------------------------
# Static Files & Mounts
# ---------------------------------------------------------------------------
if os.path.exists(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
