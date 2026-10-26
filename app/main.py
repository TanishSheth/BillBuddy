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
from contextvars import ContextVar
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal
from calendar import monthrange
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Request, Query, status
from fastapi.responses import FileResponse, Response, StreamingResponse, RedirectResponse
from PIL import Image, ImageOps
from fastapi.staticfiles import StaticFiles
from sqlalchemy import case, func, or_, update, text
from sqlalchemy.orm import Session, selectinload
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import models, schemas
from .config import (
    APP_VERSION,
    ASYNC_OCR,
    BASE_URL,
    DATA_DIR,
    DISABLE_SCHEDULER,
    IS_DEVELOPMENT,
    REPORTS_DIR,
    RUN_SCHEDULER,
    SECRET_KEY,
    STATIC_DIR,
    TMP_UPLOADS_DIR,
    UPLOADS_DIR,
)
from .database import engine, get_db, Base, SessionLocal
from .auth import (
    get_current_account,
    hash_password,
    verify_password,
    validate_password_strength,
    create_access_token_for_account,
    bump_session_version,
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
from .split_logic import calculate_splits, compute_balances, graph_constrained_settlements, simplify_debts
from .fx_service import get_exchange_rates, get_conversion_rate, convert_amount, SUPPORTED_CURRENCIES
from .pdf_gen import (
    ensure_fontconfig,
    generate_monthly_pdf,
    generate_personal_pdf,
    generate_monthly_digest_pdf,
    pdf_reports_available,
)
from .emailer import (
    is_email_configured,
    send_invite_email,
    send_quick_split_email,
    send_monthly_digest_email,
    send_password_reset_email,
    send_payment_reminder_email,
)
from .demo_service import create_sandbox, purge_expired_sandboxes, reset_sandbox, sandbox_for_account
from . import redis_support
from .storage import get_storage
from .scan_worker import (
    content_fingerprint,
    enqueue_scan_job,
    kick_scan_worker,
    process_scan_job,
    purge_old_tmp_uploads,
    start_background_poller,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
# PDF generation subsets fonts; keep that chatter out of normal app logs.
logging.getLogger("fontTools").setLevel(logging.WARNING)
logging.getLogger("fontTools.subset").setLevel(logging.WARNING)
logging.getLogger("weasyprint").setLevel(logging.WARNING)
# Must run before any WeasyPrint import so Fontconfig finds fonts.conf.
ensure_fontconfig()
BUSINESS_TIMEZONE = ZoneInfo(os.environ.get("APP_TIMEZONE", "America/New_York"))

# Block disposable/placeholder domains in production only — QA and local demos
# commonly use @example.com fixtures.
BLOCKED_INVITE_DOMAINS = {
    "example.com", "example.org", "example.net", "test.com", "localhost", "invalid",
} if not IS_DEVELOPMENT else set()

# Category filter aliases: UI labels vs legacy/slug storage (any case).
_CATEGORY_ALIASES = {
    "FOOD & DINING": {"FOOD & DINING", "Food & Dining", "Food", "Dining", "FOOD", "DINING"},
    "Food & Dining": {"FOOD & DINING", "Food & Dining", "Food", "Dining", "FOOD", "DINING"},
    "Food": {"FOOD & DINING", "Food & Dining", "Food", "Dining", "FOOD", "DINING"},
    "GENERAL": {"GENERAL", "General", "General / Other", "Other", "GENERAL / OTHER", "OTHER"},
    "General": {"GENERAL", "General", "General / Other", "Other", "GENERAL / OTHER", "OTHER"},
    "General / Other": {"GENERAL", "General", "General / Other", "Other", "GENERAL / OTHER", "OTHER"},
}

# Built-in + custom category tags are stored UPPERCASE so case variants never
# split charts (Rent/rent, Food & Dining/food & dining).
_KNOWN_CATEGORIES = (
    "FOOD & DINING",
    "GROCERIES",
    "TRAVEL",
    "TRANSIT",
    "HOUSING",
    "UTILITIES",
    "ENTERTAINMENT",
    "SHOPPING",
    "HEALTHCARE",
    "GENERAL",
)
_KNOWN_CATEGORY_BY_KEY = {name.casefold(): name for name in _KNOWN_CATEGORIES}
_KNOWN_CATEGORY_BY_KEY.update({
    "food": "FOOD & DINING",
    "dining": "FOOD & DINING",
    "general / other": "GENERAL",
    "other": "GENERAL",
})


def _normalize_category(value: str | None) -> str:
    """Canonicalize every category tag to UPPERCASE (known presets + custom)."""
    text = (value or "").strip() or "GENERAL"
    known = _KNOWN_CATEGORY_BY_KEY.get(text.casefold())
    if known:
        return known
    return text.upper()[:50]

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/jpg"}

# Per-process live update subscribers. Redis pub/sub fans out across workers;
# version polling remains as a fallback for disconnected clients.
_GROUP_EVENT_SUBSCRIBERS: Dict[int, List[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = {}
_GROUP_EVENT_LOCK = threading.Lock()
_BALANCE_MEMO: ContextVar[Optional[dict]] = ContextVar("balance_memo", default=None)
_TIMED_PATH_PREFIXES = (
    "/api/friends/",
    "/api/groups/",
    "/api/personal/receipts/",
    "/api/scans",
)


def _fanout_group_event(group_id: int, kind: str) -> None:
    with _GROUP_EVENT_LOCK:
        subscribers = list(_GROUP_EVENT_SUBSCRIBERS.get(group_id, []))
    for loop, queue in subscribers:
        if loop.is_closed():
            continue
        try:
            loop.call_soon_threadsafe(queue.put_nowait, kind)
        except RuntimeError:
            pass


def _bump_group_revision(db: Session, group_id: int) -> int:
    """Advance the durable sync revision for a group (call inside the write txn)."""
    group = db.query(models.Group).get(group_id)
    if not group:
        return 0
    group.revision = int(getattr(group, "revision", 1) or 1) + 1
    db.add(group)
    return group.revision


def notify_group_change(group_id: int, kind: str = "updated") -> None:
    """Tell open group pages to refresh after a committed change."""
    payload = {"group_id": group_id, "kind": kind}
    if redis_support.get_redis() is not None:
        # Fan-out happens in each worker via the Redis subscription handler.
        redis_support.publish(f"group-events:{group_id}", payload)
    else:
        _fanout_group_event(group_id, kind)


def _on_redis_group_event(body: str) -> None:
    try:
        data = json.loads(body)
        group_id = int(data.get("group_id"))
        kind = str(data.get("kind") or "updated")
    except Exception:
        return
    _fanout_group_event(group_id, kind)


def _record_activity(
    db: Session, group_id: int, actor: Optional[models.Account], event_type: str,
    summary: str, entity_type: Optional[str] = None, entity_id: Optional[int] = None,
) -> None:
    db.add(models.GroupActivity(
        group_id=group_id, actor_account_id=actor.id if actor else None,
        event_type=event_type, summary=summary[:300], entity_type=entity_type, entity_id=entity_id,
    ))


def _audit_json(value) -> str:
    """Stable JSON is essential: it is the exact material protected by the hash chain."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _expense_snapshot(db: Session, expense: models.Expense) -> dict:
    """A complete, human-readable financial snapshot; never expose only a vague update."""
    payer_rows = db.query(models.ExpensePayer).filter_by(expense_id=expense.id).all()
    split_rows = db.query(models.ExpenseSplit).filter_by(expense_id=expense.id).all()
    payers = sorted(
        [{"member_id": int(row.member_id), "amount": str(Decimal(str(row.amount_paid)).quantize(Decimal("0.01")))} for row in payer_rows],
        key=lambda row: row["member_id"],
    )
    if not payers:
        payers = [{"member_id": int(expense.payer_member_id), "amount": str(Decimal(str(expense.total_amount)).quantize(Decimal("0.01")))}]
    splits = sorted(
        [{"member_id": int(row.member_id), "share_amount": str(Decimal(str(row.share_amount)).quantize(Decimal("0.01"))), "split_value": str(row.split_value) if row.split_value is not None else None} for row in split_rows],
        key=lambda row: row["member_id"],
    )
    return {
        "store_name": expense.store_name,
        "bill_date": expense.bill_date.isoformat() if expense.bill_date else None,
        "total_amount": str(Decimal(str(expense.total_amount)).quantize(Decimal("0.01"))),
        "currency": _normalize_currency(expense.currency, "USD"),
        "category": expense.category,
        "split_type": expense.split_type,
        "payers": payers,
        "splits": splits,
    }


def _changed_snapshot_fields(before: Optional[dict], after: Optional[dict]) -> list[str]:
    keys = sorted(set((before or {}).keys()) | set((after or {}).keys()))
    return [key for key in keys if (before or {}).get(key) != (after or {}).get(key)]


def _record_expense_audit(
    db: Session, *, group_id: int, expense_id: Optional[int], actor: Optional[models.Account],
    event_type: str, before: Optional[dict], after: Optional[dict],
) -> models.ExpenseAuditEvent:
    """Append one tamper-evident audit event.  No code path updates old audit rows."""
    prior = (
        db.query(models.ExpenseAuditEvent)
        .filter_by(group_id=group_id)
        .order_by(models.ExpenseAuditEvent.id.desc())
        .first()
    )
    revision = (
        db.query(func.max(models.ExpenseAuditEvent.revision))
        .filter_by(group_id=group_id, expense_id=expense_id)
        .scalar()
        or 0
    ) + 1
    created_at = datetime.now(timezone.utc)
    changed = _changed_snapshot_fields(before, after)
    material = {
        "group_id": group_id, "expense_id": expense_id, "event_type": event_type,
        "revision": revision, "before": before, "after": after, "changed_fields": changed,
        "previous_hash": prior.event_hash if prior else None, "created_at": created_at.isoformat(),
    }
    event = models.ExpenseAuditEvent(
        group_id=group_id, expense_id=expense_id, actor_account_id=actor.id if actor else None,
        event_type=event_type, revision=revision, before_json=_audit_json(before) if before is not None else None,
        after_json=_audit_json(after) if after is not None else None,
        changed_fields_json=_audit_json(changed), previous_hash=prior.event_hash if prior else None,
        event_hash=hashlib.sha256(_audit_json(material).encode("utf-8")).hexdigest(), created_at=created_at,
    )
    db.add(event)
    return event


def _backfill_expense_audit_baselines() -> int:
    """Give pre-audit bills a starting snapshot once, without inventing history."""
    db = SessionLocal()
    try:
        rows = (
            db.query(models.Expense)
            .join(models.MonthlyPeriod)
            .outerjoin(models.ExpenseAuditEvent, models.ExpenseAuditEvent.expense_id == models.Expense.id)
            .filter(models.ExpenseAuditEvent.id.is_(None))
            .all()
        )
        for expense in rows:
            _record_expense_audit(
                db, group_id=expense.period.group_id, expense_id=expense.id, actor=None,
                event_type="BASELINE", before=None, after=_expense_snapshot(db, expense),
            )
        if rows:
            db.commit()
        return len(rows)
    except Exception:
        db.rollback()
        logger.exception("Expense audit baseline backfill failed")
        return 0
    finally:
        db.close()


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


def _group_focus_link(group_id: int, tab: str, focus: str = "") -> str:
    """A notification link that opens the exact relevant group workspace."""
    link = f"/group.html?group={group_id}&tab={tab}"
    return f"{link}&focus={focus}" if focus else link


def _period_has_paid_transfer(db: Session, period_id: int) -> bool:
    return db.query(models.GroupPayment.id).filter(
        models.GroupPayment.period_id == period_id,
        models.GroupPayment.status.in_(("PAID", "CONFIRMED")),
    ).first() is not None


def _period_history_is_protected(db: Session, period: models.MonthlyPeriod) -> bool:
    """Whether removing a member must retain this period's original ledger."""
    return period.status == "SETTLED" or _period_has_paid_transfer(db, period.id)


def _unlock_group_period(period: models.MonthlyPeriod) -> bool:
    """Clear legacy settled locks so months stay editable after month-end email."""
    if period.status != "SETTLED":
        return False
    period.status = "ACTIVE"
    period.settled_at = None
    return True


def _require_expense_editable(
    db: Session, period: models.MonthlyPeriod, account: models.Account, group_id: int,
    owner_override: bool = False,
) -> bool:
    """Paid periods need an owner correction; legacy settled locks just reopen."""
    member = require_group_member(group_id, account, db)
    paid = _period_has_paid_transfer(db, period.id)
    _unlock_group_period(period)
    if not paid:
        return False
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
    if redis_support.rate_limit_hit(key, limit, window_seconds):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts. Please wait a minute before trying again."
        )


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

    # PostgreSQL has no DATETIME type. SQLite accepts both names.
    timestamp_type = "DATETIME" if dialect == "sqlite" else "TIMESTAMP"
    statements = [
        ("groups", "currency", "VARCHAR(10) DEFAULT 'USD'"),
        ("expenses", "currency", "VARCHAR(10) DEFAULT 'USD'"),
        ("expenses", "category", "VARCHAR(50) DEFAULT 'General'"),
        ("expenses", "split_type", "VARCHAR(20) DEFAULT 'EQUAL'"),
        ("expenses", "fx_rate_to_group", "NUMERIC(12,6) DEFAULT 1.0"),
        ("expenses", "recurring_template_id", "INTEGER"),
        ("expense_splits", "split_value", "NUMERIC(10,2)"),
        ("monthly_periods", "target_currency", "VARCHAR(10)"),
        ("monthly_periods", "settlement_fx_rate", "NUMERIC(12,6)"),
        ("account_settings", "preferred_currency", "VARCHAR(10) DEFAULT 'USD'"),
        ("pending_uploads", "account_id", "INTEGER"),
        ("accounts", "avatar_path", "VARCHAR"),
        ("accounts", "session_version", "INTEGER NOT NULL DEFAULT 1"),
        ("accounts", "deletion_requested_at", timestamp_type),
        ("accounts", "deletion_purge_after", timestamp_type),
        ("accounts", "deletion_completed_at", timestamp_type),
        ("groups", "cover_image_path", "VARCHAR"),
        ("groups", "revision", "INTEGER NOT NULL DEFAULT 1"),
        ("group_members", "is_active", "BOOLEAN DEFAULT TRUE"),
        ("group_members", "joined_at", timestamp_type),
        ("group_members", "left_at", timestamp_type),
        ("monthly_periods", "report_revision", "INTEGER NOT NULL DEFAULT 1"),
        ("account_monthly_digests", "delivery_claimed_at", timestamp_type),
        ("account_monthly_digests", "delivery_error", "VARCHAR(300)"),
        ("account_monthly_digests", "revision", "INTEGER NOT NULL DEFAULT 1"),
        ("account_monthly_digests", "revised_at", timestamp_type),
        ("account_monthly_digests", "source_fingerprint", "VARCHAR(64)"),
        ("pending_uploads", "ocr_store_name", "VARCHAR(150)"),
        ("pending_uploads", "ocr_phones", "VARCHAR(200)"),
        ("pending_uploads", "ocr_store_ids", "VARCHAR(80)"),
        ("pending_uploads", "ocr_header_key", "VARCHAR(200)"),
        ("account_vendors", "name_key", "VARCHAR(80) DEFAULT ''"),
        ("account_vendors", "category", "VARCHAR(50) DEFAULT 'General'"),
        ("quick_splits", "split_type", "VARCHAR(20) DEFAULT 'EQUAL'"),
        ("quick_splits", "payer_share_amount", "NUMERIC(10,2) DEFAULT 0"),
        ("quick_splits", "payer_paid_amount", "NUMERIC(10,2)"),
        ("quick_splits", "created_by_account_id", "INTEGER"),
        ("quick_splits", "edited_after_payment", "BOOLEAN DEFAULT FALSE"),
        ("quick_splits", "edit_history_json", "TEXT"),
        ("quick_splits", "adjustment_of_split_id", "INTEGER"),
        ("quick_split_participants", "split_value", "NUMERIC(10,2)"),
        ("quick_split_participants", "paid_amount", "NUMERIC(10,2) NOT NULL DEFAULT 0"),
        ("quick_split_participants", "invite_token", "VARCHAR(64)"),
        ("group_payments", "expense_cutoff_id", "INTEGER"),
        ("group_payments", "cleared_amount", "NUMERIC(10,2)"),
    ]

    for table, column, spec in statements:
        try:
            if not table_exists(table) or has_column(table, column):
                continue
            _ddl(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")
            logger.info("Added missing column %s.%s", table, column)
        except Exception as err:
            logger.warning("Could not add %s.%s: %s", table, column, err)

    # APP_VERSION-based demo keys outgrew the original VARCHAR(40).
    try:
        if dialect == "postgresql" and table_exists("demo_templates"):
            row = _query(
                "SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = 'demo_templates' AND column_name = 'version'"
            )
            if row and row[0][0] is not None and int(row[0][0]) < 80:
                _ddl("ALTER TABLE demo_templates ALTER COLUMN version TYPE VARCHAR(80)")
                logger.info("Widened demo_templates.version to VARCHAR(80)")
    except Exception as err:
        logger.warning("Could not widen demo_templates.version: %s", err)

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

    # Scale indexes (safe to re-run; IF NOT EXISTS on Postgres / ignore on SQLite).
    index_statements = [
        "CREATE INDEX IF NOT EXISTS ix_expenses_period_id ON expenses (period_id)",
        "CREATE INDEX IF NOT EXISTS ix_expense_splits_expense_id ON expense_splits (expense_id)",
        "CREATE INDEX IF NOT EXISTS ix_expense_splits_member_id ON expense_splits (member_id)",
        "CREATE INDEX IF NOT EXISTS ix_monthly_periods_group_id ON monthly_periods (group_id)",
        "CREATE INDEX IF NOT EXISTS ix_group_members_group_id ON group_members (group_id)",
        "CREATE INDEX IF NOT EXISTS ix_group_members_account_id ON group_members (account_id)",
        "CREATE INDEX IF NOT EXISTS ix_quick_split_participants_quick_split_id ON quick_split_participants (quick_split_id)",
        "CREATE INDEX IF NOT EXISTS ix_notifications_account_read_created ON notifications (account_id, read_at, created_at)",
    ]
    for sql in index_statements:
        try:
            _ddl(sql)
        except Exception as err:
            logger.warning("Could not ensure index (%s): %s", sql, err)

    _backfill_expense_fx_rates()
    _backfill_category_case()


def _backfill_category_case() -> None:
    """Uppercase custom category tags and fold case variants (Rent/rent → RENT)."""
    tables = (
        models.PersonalExpense,
        models.Expense,
        models.QuickSplit,
        models.AccountVendor,
        models.RecurringExpenseTemplate,
    )
    try:
        db = SessionLocal()
    except Exception:
        return
    try:
        updated = 0
        for model in tables:
            if not hasattr(model, "category"):
                continue
            try:
                rows = db.query(model).all()
            except Exception as err:
                logger.warning("Category backfill skipped for %s: %s", model.__tablename__, err)
                try:
                    db.rollback()
                except Exception:
                    pass
                continue
            for row in rows:
                raw = getattr(row, "category", None)
                normalized = _normalize_category(raw)
                if (raw or "") != normalized:
                    row.category = normalized
                    updated += 1
        if updated:
            db.commit()
            logger.info("Normalized category case on %s rows", updated)
    except Exception as err:
        logger.warning("Category case backfill skipped: %s", err)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def _fx_rate_between(from_curr: str, to_curr: str) -> Decimal:
    """Units of to_curr per 1 from_curr for any supported pair. Same currency → 1."""
    src = (from_curr or "USD").strip().upper() or "USD"
    dest = (to_curr or "USD").strip().upper() or "USD"
    if src == dest:
        return Decimal("1.0")
    try:
        rate = float(get_conversion_rate(src, dest))
    except Exception:
        logger.exception("FX lookup failed for %s→%s", src, dest)
        return Decimal("1.0")
    if rate <= 0:
        return Decimal("1.0")
    return Decimal(str(rate))


def _stored_fx_looks_unset(rate: float, expense_currency: str, group_currency: str) -> bool:
    """True when a cross-currency row still has the pre-D7 default of 1.0."""
    src = (expense_currency or "USD").strip().upper() or "USD"
    dest = (group_currency or "USD").strip().upper() or "USD"
    if src == dest:
        return False
    return rate <= 0 or abs(rate - 1.0) < 0.000001


def _backfill_expense_fx_rates() -> None:
    """Fix pre-D7 rows that kept the default 1.0 rate for any cross-currency pair."""
    try:
        db = SessionLocal()
    except Exception:
        return
    try:
        rows = (
            db.query(models.Expense, models.Group)
            .join(models.MonthlyPeriod, models.Expense.period_id == models.MonthlyPeriod.id)
            .join(models.Group, models.MonthlyPeriod.group_id == models.Group.id)
            .all()
        )
        updated = 0
        for expense, group in rows:
            exp_ccy = (expense.currency or "USD").strip().upper() or "USD"
            grp_ccy = (group.currency or "USD").strip().upper() or "USD"
            rate = float(expense.fx_rate_to_group or 0) or 1.0
            if exp_ccy == grp_ccy:
                if abs(rate - 1.0) > 0.000001:
                    expense.fx_rate_to_group = Decimal("1.0")
                    updated += 1
                continue
            if _stored_fx_looks_unset(rate, exp_ccy, grp_ccy):
                expense.fx_rate_to_group = _fx_rate_between(exp_ccy, grp_ccy)
                updated += 1
        if updated:
            db.commit()
            logger.info("Backfilled fx_rate_to_group on %s expenses", updated)
    except Exception as err:
        logger.warning("Expense FX backfill skipped: %s", err)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


_ensure_schema()

app = FastAPI(
    title="ShareSquad",
    # Disable the auto-generated API docs in production.
    # They expose every endpoint, schema, and parameter to anyone who visits /docs.
    docs_url="/docs" if IS_DEVELOPMENT else None,
    redoc_url="/redoc" if IS_DEVELOPMENT else None,
    openapi_url="/openapi.json" if IS_DEVELOPMENT else None,
)


@app.middleware("http")
async def disable_static_cache(request: Request, call_next):
    """Avoid stale HTML/JS/CSS after deploys and strip server identification headers."""
    started = time.perf_counter()
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".js", ".css")):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    # Absolute JWT lifetime — tokens are not refreshed on each request.
    # Hashed Vite assets may be cached; this app serves classic static files.
    if any(path.startswith(prefix) for prefix in _TIMED_PATH_PREFIXES):
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Response-Time-ms"] = f"{elapsed_ms:.1f}"
        if elapsed_ms >= 500:
            logger.info("slow_request path=%s method=%s ms=%.1f status=%s", path, request.method, elapsed_ms, response.status_code)
    # Remove headers that reveal the server tech stack.
    if "server" in response.headers:
        del response.headers["server"]
    if "x-powered-by" in response.headers:
        del response.headers["x-powered-by"]
    return response


@app.get("/api/health")
def health():
    from .ocr_extractor import tesseract_status

    tess = tesseract_status()
    return {
        "ok": True,
        "app": "ShareSquad",
        "version": APP_VERSION,
        "environment": "development" if IS_DEVELOPMENT else "production",
        "pdf_reports": pdf_reports_available(),
        "async_ocr": ASYNC_OCR,
        "redis": bool(redis_support.get_redis()),
        "storage": get_storage().__class__.__name__,
        "tesseract": tess,
        "ocr_ready": bool(tess.get("available")),
    }


@app.get("/healthz")
def healthz():
    """Liveness: process is up."""
    return {"ok": True}


@app.get("/readyz")
def readyz():
    """Readiness: database accepts connections."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True}
    except Exception as err:
        raise HTTPException(503, f"Database not ready: {err}") from err


@app.get("/", include_in_schema=False)
def public_landing_page():
    """Public, indexable ShareSquad introduction; the signed-in app is /index.html."""
    return FileResponse(os.path.join(STATIC_DIR, "landing.html"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _period_key_for(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _business_today() -> date:
    """Calendar date used for monthly close and the default current period."""
    return datetime.now(BUSINESS_TIMEZONE).date()


def _group_reports_dir(group_id: int) -> str:
    path = os.path.join(REPORTS_DIR, str(group_id))
    os.makedirs(path, exist_ok=True)
    return path


def _personal_reports_dir(account_id: int) -> str:
    path = os.path.join(REPORTS_DIR, f"personal_{account_id}")
    os.makedirs(path, exist_ok=True)
    return path


def _digest_reports_dir(account_id: int) -> str:
    path = os.path.join(REPORTS_DIR, f"digest_{account_id}")
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
        payments = []
        for payer in e.payers:
            converted_paid, _ = convert_amount(payer.amount_paid, src, dest, custom_rate=rate_f)
            payments.append({"payer_id": payer.member_id, "amount": float(converted_paid)})
        rows.append(
            {
                "payer_id": e.payer_member_id,
                "payments": payments,
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
    """Apply confirmed transfers; cleared cash after a bill shrinks/deletes becomes credit.

    Each payment stores how much debt it cleared when confirmed. That snapshot is
    always replayed in full, so a later edit/delete that reduces historical debt
    turns the unused cash into credit. Payments recorded above the debt at
    confirm time stay capped at that snapshot, so a later bill in a reopened
    period remains outstanding.
    """
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

    def apply_transfer(payer_id: int, recipient_id: int, amount_f: float) -> None:
        if amount_f <= 0:
            return
        balances[payer_id]["net_balance"] = round(balances[payer_id]["net_balance"] + amount_f, 2)
        balances[recipient_id]["net_balance"] = round(balances[recipient_id]["net_balance"] - amount_f, 2)
        balances[payer_id]["settlement_paid"] = round(balances[payer_id]["settlement_paid"] + amount_f, 2)
        balances[recipient_id]["settlement_received"] = round(balances[recipient_id]["settlement_received"] + amount_f, 2)

    def historical_owe(payment: models.GroupPayment) -> tuple[float, float]:
        cutoff = event_time(payment.marked_paid_at or payment.created_at)
        historical_expenses = [
            expense for expense in expense_dicts
            if (payment.expense_cutoff_id is not None and expense.get("expense_id", 0) <= payment.expense_cutoff_id)
            or (payment.expense_cutoff_id is None and event_time(expense.get("created_at")) <= cutoff)
        ]
        historical_balances = compute_balances(members, historical_expenses)
        for payer_id, recipient_id, applied_amount in applied_history:
            historical_balances[payer_id]["net_balance"] = round(
                historical_balances[payer_id]["net_balance"] + applied_amount, 2
            )
            historical_balances[recipient_id]["net_balance"] = round(
                historical_balances[recipient_id]["net_balance"] - applied_amount, 2
            )
        payer_id = payment.payer_member_id
        recipient_id = payment.recipient_member_id
        payer_owed = max(-float(historical_balances[payer_id]["net_balance"]), 0.0)
        recipient_due = max(float(historical_balances[recipient_id]["net_balance"]), 0.0)
        return payer_owed, recipient_due

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
        payment_amount = round(float(amount), 2)
        if payment_amount <= 0:
            continue
        payer_id = payment.payer_member_id
        recipient_id = payment.recipient_member_id
        if payment.cleared_amount is not None:
            # Snapshot from confirm time — apply in full so bill shrink/delete becomes credit.
            cleared = round(float(payment.cleared_amount), 2)
            if source_currency != target_currency:
                cleared_dec, _ = convert_amount(
                    Decimal(str(payment.cleared_amount)), source_currency, target_currency,
                    custom_rate=_rate_to_dest(source_currency, target_currency, base_currency, custom_base_to_destination),
                )
                cleared = round(float(cleared_dec), 2)
            apply_transfer(payer_id, recipient_id, cleared)
            applied_history.append((payer_id, recipient_id, cleared))
            continue
        # Legacy rows without a snapshot: cap at current historical debt (no invented credit).
        payer_owed, recipient_due = historical_owe(payment)
        settle_amount = round(min(payment_amount, payer_owed, recipient_due), 2)
        apply_transfer(payer_id, recipient_id, settle_amount)
        applied_history.append((payer_id, recipient_id, settle_amount))


def _cleared_amount_for_group_payment(
    db: Session, period: models.MonthlyPeriod, payment: models.GroupPayment,
) -> Decimal:
    """Debt this payment can clear right now (capped at the recorded cash)."""
    expense_rows = (
        db.query(models.Expense)
        .filter_by(period_id=period.id)
        .order_by(models.Expense.id.asc())
        .all()
    )
    members = _members_as_dicts(db, period.group_id, include_inactive=True)
    member_ids = {m["id"] for m in members}
    pay_map = _payments_by_expense(db, expense_rows)
    expense_dicts = []
    for expense in expense_rows:
        if payment.expense_cutoff_id is not None and expense.id > payment.expense_cutoff_id:
            continue
        splits = [
            {"user_id": s.member_id, "share_amount": float(s.share_amount)}
            for s in expense.splits if s.member_id in member_ids
        ]
        payments_map = pay_map.get(expense.id, {})
        expense_dicts.append({
            "expense_id": expense.id,
            "payer_id": expense.payer_member_id,
            "total_amount": float(expense.total_amount),
            "splits": splits,
            "payments": [{"payer_id": mid, "amount": float(amt)} for mid, amt in payments_map.items()],
            "created_at": expense.created_at,
        })
    hist = compute_balances(members, expense_dicts)
    prior_q = (
        db.query(models.GroupPayment)
        .filter_by(period_id=period.id, status="CONFIRMED")
        .filter(models.GroupPayment.id != payment.id)
    )
    prior = prior_q.order_by(models.GroupPayment.id.asc()).all()
    prior.sort(key=lambda row: (row.marked_paid_at or row.created_at or datetime.min.replace(tzinfo=timezone.utc), row.id))
    for prior_payment in prior:
        cleared = prior_payment.cleared_amount if prior_payment.cleared_amount is not None else prior_payment.amount
        amt = float(cleared)
        if prior_payment.payer_member_id not in hist or prior_payment.recipient_member_id not in hist:
            continue
        hist[prior_payment.payer_member_id]["net_balance"] = round(
            hist[prior_payment.payer_member_id]["net_balance"] + amt, 2
        )
        hist[prior_payment.recipient_member_id]["net_balance"] = round(
            hist[prior_payment.recipient_member_id]["net_balance"] - amt, 2
        )
    if payment.payer_member_id not in hist or payment.recipient_member_id not in hist:
        return Decimal("0.00")
    payer_owed = max(-float(hist[payment.payer_member_id]["net_balance"]), 0.0)
    recipient_due = max(float(hist[payment.recipient_member_id]["net_balance"]), 0.0)
    return Decimal(str(round(min(float(payment.amount), payer_owed, recipient_due), 2)))


def _repair_group_payment_snapshots(db: Session, period: models.MonthlyPeriod) -> None:
    """Backfill zero/legacy payment snapshots using the debt at each payment time.

    A snapshot is the amount a transfer actually cleared, not merely the cash
    recorded.  Older rows without one must never be replayed at face value:
    doing so can make a later valid payment appear to clear nothing.
    """
    payments = db.query(models.GroupPayment).filter_by(period_id=period.id, status="CONFIRMED").all()
    if not payments:
        return
    expenses = db.query(models.Expense).filter_by(period_id=period.id).order_by(models.Expense.id.asc()).all()
    members = _members_as_dicts(db, period.group_id, include_inactive=True)
    member_ids = {member["id"] for member in members}
    pay_map = _payments_by_expense(db, expenses)
    expense_dicts = [{
        "expense_id": expense.id,
        "payer_id": expense.payer_member_id,
        "total_amount": float(expense.total_amount),
        "splits": [{"user_id": split.member_id, "share_amount": float(split.share_amount)} for split in expense.splits if split.member_id in member_ids],
        "payments": [{"payer_id": member_id, "amount": float(amount)} for member_id, amount in pay_map.get(expense.id, {}).items()],
        "created_at": expense.created_at,
    } for expense in expenses]

    def event_time(value):
        if value is None:
            return datetime.min.replace(tzinfo=timezone.utc)
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    payments.sort(key=lambda row: (event_time(row.marked_paid_at or row.created_at), row.id))
    applied: list[tuple[int, int, float]] = []
    for payment in payments:
        if payment.payer_member_id not in member_ids or payment.recipient_member_id not in member_ids:
            continue
        cutoff = event_time(payment.marked_paid_at or payment.created_at)
        historical = [
            expense for expense in expense_dicts
            if (payment.expense_cutoff_id is not None and expense.get("expense_id", 0) <= payment.expense_cutoff_id)
            or (payment.expense_cutoff_id is None and event_time(expense.get("created_at")) <= cutoff)
        ]
        balances = compute_balances(members, historical)
        for payer_id, recipient_id, amount in applied:
            balances[payer_id]["net_balance"] = round(balances[payer_id]["net_balance"] + amount, 2)
            balances[recipient_id]["net_balance"] = round(balances[recipient_id]["net_balance"] - amount, 2)
        payer_owed = max(-float(balances[payment.payer_member_id]["net_balance"]), 0.0)
        recipient_due = max(float(balances[payment.recipient_member_id]["net_balance"]), 0.0)
        existing = Decimal(str(payment.cleared_amount or "0"))
        cleared = existing if existing > 0 else Decimal(str(round(min(float(payment.amount), payer_owed, recipient_due), 2)))
        if existing <= 0:
            payment.cleared_amount = cleared
        applied.append((payment.payer_member_id, payment.recipient_member_id, float(cleared)))


def _repair_all_group_payment_snapshots(db: Session) -> None:
    """One safe startup repair for historical payments created before snapshots."""
    period_ids = [row[0] for row in db.query(models.GroupPayment.period_id).filter(
        models.GroupPayment.status == "CONFIRMED",
        or_(models.GroupPayment.cleared_amount.is_(None), models.GroupPayment.cleared_amount == 0),
    ).distinct().all()]
    for period_id in period_ids:
        period = db.query(models.MonthlyPeriod).get(period_id)
        if period:
            _repair_group_payment_snapshots(db, period)
    if period_ids:
        db.commit()


def build_balances_and_settlements(
    db: Session, group_id: int, period: models.MonthlyPeriod, settlement_mode: str = "smart"
):
    mode = (settlement_mode or "smart").strip().lower()
    if mode not in ("smart", "simplified"):
        mode = "smart"
    memo = _BALANCE_MEMO.get()
    memo_key = (group_id, period.id, mode)
    if memo is not None and memo_key in memo:
        return memo[memo_key]

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
        .options(
            selectinload(models.Expense.payers),
            selectinload(models.Expense.splits),
        )
        .filter_by(period_id=period.id)
        .order_by(models.Expense.bill_date.asc(), models.Expense.id.asc())
        .all()
    )
    # Legacy soft-removed memberships can remain in an upgraded database.
    # Treat any expense involving one as removed, matching the delete action.
    if not preserve_history:
        def _payers_are_active(expense: models.Expense) -> bool:
            payer_ids = [payer.member_id for payer in expense.payers] or [expense.payer_member_id]
            return all(payer_id in active_member_ids for payer_id in payer_ids)

        expenses = [
            expense for expense in expenses
            if _payers_are_active(expense)
            and all(split.member_id in active_member_ids for split in expense.splits)
        ]
    expense_dicts = _expenses_as_dicts(expenses, base_currency, base_currency)
    raw_balances = compute_balances(members, expense_dicts)
    _apply_confirmed_payments(db, period, raw_balances, expense_dicts, base_currency, base_currency)
    if mode == "simplified":
        raw_settlements = simplify_debts(raw_balances)
    else:
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
        if mode == "simplified":
            converted_list = simplify_debts(tgt_balances)
        else:
            converted_list = graph_constrained_settlements(members, tgt_expense_dicts, tgt_balances)
        for s in converted_list:
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

    result = (balances, settlements, expenses)
    if memo is not None:
        memo[memo_key] = result
    return result


def build_group_balances_all_time(
    db: Session, group_id: int, settlement_mode: str = "smart",
) -> tuple[list[dict], list[dict]]:
    """Overall member nets + Who pays whom across every period (any duration).

    Each period's confirmed GroupPayments are applied first via the existing
    period builder, then nets are summed and settlements are recomputed once.
    """
    mode = (settlement_mode or "smart").strip().lower()
    if mode not in ("smart", "simplified"):
        mode = "smart"
    group = db.query(models.Group).get(group_id)
    base_currency = (group.currency if group and group.currency else "USD") or "USD"
    periods = (
        db.query(models.MonthlyPeriod)
        .filter_by(group_id=group_id)
        .order_by(models.MonthlyPeriod.period_key.asc())
        .all()
    )
    if not periods:
        members = _members_as_dicts(db, group_id)
        raw = compute_balances(members, [])
        balances = [{
            **value,
            "currency": base_currency,
            "converted_total_paid": None,
            "converted_total_share_owed": None,
            "converted_net_balance": None,
            "converted_currency": None,
        } for value in raw.values()]
        return balances, []

    nets: dict[int, Decimal] = {}
    paid: dict[int, Decimal] = {}
    owed: dict[int, Decimal] = {}
    settlement_paid: dict[int, Decimal] = {}
    settlement_received: dict[int, Decimal] = {}
    names: dict[int, str] = {}
    all_expense_dicts: list[dict] = []

    for period in periods:
        balances, _settlements, expenses = build_balances_and_settlements(
            db, group_id, period, settlement_mode=mode,
        )
        all_expense_dicts.extend(_expenses_as_dicts(expenses, base_currency, base_currency))
        for row in balances:
            mid = int(row["member_id"])
            names[mid] = row.get("name") or names.get(mid) or f"Member {mid}"
            nets[mid] = nets.get(mid, Decimal("0.00")) + Decimal(str(row.get("net_balance") or 0))
            paid[mid] = paid.get(mid, Decimal("0.00")) + Decimal(str(row.get("total_paid") or 0))
            owed[mid] = owed.get(mid, Decimal("0.00")) + Decimal(str(row.get("total_share_owed") or 0))
            settlement_paid[mid] = settlement_paid.get(mid, Decimal("0.00")) + Decimal(str(row.get("settlement_paid") or 0))
            settlement_received[mid] = settlement_received.get(mid, Decimal("0.00")) + Decimal(
                str(row.get("settlement_received") or 0)
            )

    raw_balances: dict[int, dict] = {}
    for mid, net in nets.items():
        raw_balances[mid] = {
            "member_id": mid,
            "name": names.get(mid) or f"Member {mid}",
            "total_paid": float(paid.get(mid, Decimal("0.00")).quantize(Decimal("0.01"))),
            "total_share_owed": float(owed.get(mid, Decimal("0.00")).quantize(Decimal("0.01"))),
            "net_balance": float(net.quantize(Decimal("0.01"))),
            "settlement_paid": float(settlement_paid.get(mid, Decimal("0.00")).quantize(Decimal("0.01"))),
            "settlement_received": float(settlement_received.get(mid, Decimal("0.00")).quantize(Decimal("0.01"))),
            "currency": base_currency,
            "converted_total_paid": None,
            "converted_total_share_owed": None,
            "converted_net_balance": None,
            "converted_currency": None,
        }

    members_for_graph = [{"id": mid, "name": raw_balances[mid]["name"]} for mid in raw_balances]
    if mode == "simplified":
        raw_settlements = simplify_debts(raw_balances)
    else:
        raw_settlements = graph_constrained_settlements(members_for_graph, all_expense_dicts, raw_balances)

    settlements = [{
        **row,
        "currency": base_currency,
        "converted_amount": None,
        "converted_currency": None,
        "exchange_rate": None,
    } for row in raw_settlements]
    return list(raw_balances.values()), settlements


def _period_report_inputs(db: Session, group: models.Group, period: models.MonthlyPeriod):
    balances, settlements, expenses = build_balances_and_settlements(db, group.id, period)
    members_by_id = {member.id: member.display_name for member in db.query(models.GroupMember).filter_by(group_id=group.id).all()}
    expense_dicts = [{
        "bill_date": expense.bill_date.isoformat(),
        "store_name": expense.store_name,
        "category": expense.category or "General",
        "payer_name": ", ".join(f"{members_by_id.get(payer.member_id, '?')} {payer.amount_paid}" for payer in expense.payers) if expense.payers else members_by_id.get(expense.payer_member_id, "?"),
        "participant_names": [members_by_id.get(split.member_id, "?") for split in expense.splits],
        "total_amount": float(expense.total_amount),
        "currency": expense.currency or group.currency or "USD",
        "image_path": expense.image_path,
    } for expense in expenses]
    return balances, settlements, expense_dicts


def _snapshot_group_period_pdf(db: Session, group: models.Group, period: models.MonthlyPeriod) -> tuple[Optional[str], Optional[str]]:
    """Write/refresh a group PDF snapshot without locking the month as settled."""
    balances, settlements, expense_dicts = _period_report_inputs(db, group, period)
    try:
        pdf_path, pdf_filename = generate_monthly_pdf(
            period.period_key, balances, settlements, expense_dicts, _group_reports_dir(group.id),
            group_name=group.name, base_currency=group.currency or "USD",
            target_currency=period.target_currency,
            fx_rate=float(period.settlement_fx_rate) if period.settlement_fx_rate else None,
        )
        period.pdf_filename = pdf_filename
        period.pdf_path = pdf_path
        return pdf_path, pdf_filename
    except Exception:
        logger.exception("Could not generate report snapshot for group %s period %s", group.id, period.period_key)
        return None, None


def _period_dates(period_key: str) -> tuple[date, date]:
    try:
        year, month = (int(part) for part in period_key.split("-"))
        return date(year, month, 1), date(year, month, monthrange(year, month)[1])
    except (ValueError, TypeError):
        raise HTTPException(400, "Period must use YYYY-MM")


def _membership_belongs_in_period(membership: models.GroupMember, period_end: date) -> bool:
    """Do not retroactively email a person who joined after a month closed."""
    cutoff = datetime.combine(period_end + timedelta(days=1), datetime.min.time())
    if membership.joined_at is not None:
        joined = membership.joined_at.replace(tzinfo=None) if membership.joined_at.tzinfo else membership.joined_at
        if joined >= cutoff:
            return False
    if membership.left_at is not None:
        left = membership.left_at.replace(tzinfo=None) if membership.left_at.tzinfo else membership.left_at
        if left < cutoff:
            return False
    return True


def _build_account_monthly_digest(db: Session, account: models.Account, period_key: str) -> models.AccountMonthlyDigest:
    """Create one account-owned report from every ledger visible to that account."""
    start_d, end_d = _period_dates(period_key)
    group_sections = []
    memberships = db.query(models.GroupMember).filter_by(account_id=account.id).all()
    for membership in memberships:
        if not _membership_belongs_in_period(membership, end_d):
            continue
        group = db.query(models.Group).get(membership.group_id)
        # Month-end email is a data snapshot — do not require the period to be "settled".
        period = db.query(models.MonthlyPeriod).filter_by(group_id=membership.group_id, period_key=period_key).first()
        if not group or not period:
            continue
        _, settlements, expenses = build_balances_and_settlements(db, group.id, period)
        member_names = {m.id: m.display_name for m in db.query(models.GroupMember).filter_by(group_id=group.id).all()}
        mine = []
        for settlement in settlements:
            if settlement["from_member_id"] == membership.id:
                mine.append({"description": f"You pay {member_names.get(settlement['to_member_id'], 'member')}", "amount": -float(settlement["amount"])})
            elif settlement["to_member_id"] == membership.id:
                mine.append({"description": f"{member_names.get(settlement['from_member_id'], 'Member')} pays you", "amount": float(settlement["amount"])})
        expenses_this_month = []
        expenses_added_late = []
        for expense in expenses:
            row = {
                "bill_date": expense.bill_date.isoformat(),
                "store_name": expense.store_name,
                "category": expense.category or "General",
                "total_amount": float(expense.total_amount),
                "currency": expense.currency or group.currency or "USD",
                "payer_name": member_names.get(expense.payer_member_id, "Member"),
            }
            if expense.bill_date < start_d:
                expenses_added_late.append(row)
            else:
                expenses_this_month.append(row)
        group_sections.append({
            "name": group.name,
            "currency": group.currency or "USD",
            "expense_count": len(expenses),
            "report_revision": int(period.report_revision or 1),
            "settlements": mine,
            "expenses_this_month": expenses_this_month,
            "expenses_added_late": expenses_added_late,
        })

    personal_expenses = [{
        "bill_date": item.bill_date.isoformat(), "store_name": item.store_name,
        "category": item.category or "General", "total_amount": float(item.total_amount), "currency": item.currency or "USD",
    } for item in db.query(models.PersonalExpense).filter(
        models.PersonalExpense.account_id == account.id,
        models.PersonalExpense.bill_date >= start_d, models.PersonalExpense.bill_date <= end_d,
    ).order_by(models.PersonalExpense.bill_date.asc()).all()]

    split_rows = db.query(models.QuickSplit).outerjoin(models.QuickSplitParticipant).filter(
        models.QuickSplit.bill_date >= start_d, models.QuickSplit.bill_date <= end_d,
        or_(models.QuickSplit.payer_account_id == account.id, models.QuickSplitParticipant.account_id == account.id),
    ).distinct().all()
    private_splits = []
    for split in split_rows:
        other = next((p for p in split.participants if p.account_id != account.id), None)
        participant = next((p for p in split.participants if p.account_id == account.id), None)
        if split.payer_account_id == account.id:
            balance = sum((Decimal(str(p.share_amount)) for p in split.participants), Decimal("0"))
        elif participant:
            balance = -Decimal(str(participant.share_amount))
        else:
            continue
        private_splits.append({
            "bill_date": split.bill_date.isoformat(), "title": split.title,
            "other_name": (other.display_name if other else "Private split"),
            "balance_label": ("You are owed " if balance >= 0 else "You owe ") + f"{abs(float(balance)):,.2f} {split.currency or 'USD'}",
        })

    digest = db.query(models.AccountMonthlyDigest).filter_by(account_id=account.id, period_key=period_key).first()
    if not digest:
        digest = models.AccountMonthlyDigest(account_id=account.id, period_key=period_key)
        db.add(digest)
        db.flush()
    fingerprint_payload = {"groups": group_sections, "personal": personal_expenses, "private": private_splits}
    source_fingerprint = hashlib.sha256(json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    is_revision = bool(digest.source_fingerprint and digest.source_fingerprint != source_fingerprint)
    if is_revision:
        digest.revision = int(digest.revision or 1) + 1
        digest.revised_at = datetime.now(timezone.utc)
    path, filename = generate_monthly_digest_pdf(
        period_key, account.display_name, group_sections, personal_expenses, private_splits,
        _digest_reports_dir(account.id), revision=int(digest.revision or 1),
    )
    digest.pdf_path, digest.pdf_filename, digest.generated_at = path, filename, datetime.now(timezone.utc)
    digest.source_fingerprint = source_fingerprint
    return digest


def _deliver_monthly_digests(period_key: str) -> None:
    """Generate and send at most one opted-in digest per account for the month."""
    db = SessionLocal()
    try:
        account_ids = {
            row[0] for row in db.query(models.GroupMember.account_id).join(models.MonthlyPeriod, models.MonthlyPeriod.group_id == models.GroupMember.group_id).filter(
                models.MonthlyPeriod.period_key == period_key,
                or_(models.GroupMember.joined_at.is_(None), models.GroupMember.joined_at < datetime.combine(_period_dates(period_key)[1] + timedelta(days=1), datetime.min.time())),
            ).all()
        }
        start_d, end_d = _period_dates(period_key)
        account_ids.update(row[0] for row in db.query(models.PersonalExpense.account_id).filter(models.PersonalExpense.bill_date >= start_d, models.PersonalExpense.bill_date <= end_d).all())
        account_ids.update(row[0] for row in db.query(models.QuickSplit.payer_account_id).filter(models.QuickSplit.bill_date >= start_d, models.QuickSplit.bill_date <= end_d).all())
        account_ids.update(row[0] for row in db.query(models.QuickSplitParticipant.account_id).join(models.QuickSplit).filter(models.QuickSplitParticipant.account_id.isnot(None), models.QuickSplit.bill_date >= start_d, models.QuickSplit.bill_date <= end_d).all())
        for account_id in account_ids:
            account = db.query(models.Account).get(account_id)
            if account:
                _build_account_monthly_digest(db, account, period_key)
        db.commit()
        if is_email_configured():
            for digest in db.query(models.AccountMonthlyDigest).filter_by(period_key=period_key, emailed_at=None).all():
                settings = db.query(models.AccountSettings).filter_by(account_id=digest.account_id).first()
                if digest.account.deletion_completed_at or (settings is not None and not settings.email_monthly_reports):
                    continue
                try:
                    # Atomically claim this recipient. Concurrent workers and
                    # deploy restarts cannot both pass this update.
                    claimed = db.execute(update(models.AccountMonthlyDigest).where(
                        models.AccountMonthlyDigest.id == digest.id,
                        models.AccountMonthlyDigest.emailed_at.is_(None),
                        models.AccountMonthlyDigest.delivery_claimed_at.is_(None),
                    ).values(delivery_claimed_at=datetime.now(timezone.utc), delivery_error=None))
                    db.commit()
                    if claimed.rowcount != 1:
                        continue
                    send_monthly_digest_email(digest.account.email, digest.account.display_name, period_key, digest.pdf_path, digest.pdf_filename)
                    db.execute(update(models.AccountMonthlyDigest).where(models.AccountMonthlyDigest.id == digest.id).values(
                        emailed_at=datetime.now(timezone.utc), delivery_claimed_at=None, delivery_error=None,
                    ))
                    db.commit()
                except Exception:
                    db.rollback()
                    db.execute(update(models.AccountMonthlyDigest).where(models.AccountMonthlyDigest.id == digest.id).values(
                        delivery_claimed_at=None, delivery_error="Delivery failed; the next scheduled close check may retry.",
                    ))
                    db.commit()
                    logger.exception("Could not email monthly digest to account %s", digest.account_id)
    except Exception:
        db.rollback()
        logger.exception("Monthly digest delivery failed for %s", period_key)
    finally:
        db.close()


def _rollover_monthly_periods() -> None:
    """Open the current calendar month for every group.

    Past months stay editable. Month-end email/digest delivery is a separate
    scheduled job and must not mark periods as settled.
    """
    db = SessionLocal()
    try:
        # Older builds auto-settled months at rollover. Clear those locks so
        # members can still add or edit expenses in past months.
        db.query(models.MonthlyPeriod).filter(models.MonthlyPeriod.status == "SETTLED").update(
            {"status": "ACTIVE", "settled_at": None},
            synchronize_session=False,
        )
        current_key = _period_key_for(_business_today())
        for group in db.query(models.Group).all():
            if not db.query(models.MonthlyPeriod.id).filter_by(group_id=group.id, period_key=current_key).first():
                db.add(models.MonthlyPeriod(group_id=group.id, period_key=current_key, status="ACTIVE"))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Monthly period rollover failed")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Auth Endpoints
# ---------------------------------------------------------------------------
def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _finalize_account_deletion(db: Session, account: models.Account) -> None:
    """Irreversibly erase sign-in and personal data while preserving shared ledgers."""
    if account.deletion_completed_at:
        return
    for expense in db.query(models.PersonalExpense).filter_by(account_id=account.id).all():
        if expense.image_path and os.path.exists(expense.image_path):
            try:
                os.remove(expense.image_path)
            except OSError:
                pass
        db.delete(expense)
    if account.avatar_path and os.path.exists(account.avatar_path):
        try:
            os.remove(account.avatar_path)
        except OSError:
            pass
    for upload in db.query(models.PendingUpload).filter_by(account_id=account.id).all():
        if upload.file_path and os.path.exists(upload.file_path):
            try:
                os.remove(upload.file_path)
            except OSError:
                pass
        db.delete(upload)
    for membership in db.query(models.GroupMember).filter_by(account_id=account.id).all():
        if membership.role == "owner":
            replacement = db.query(models.GroupMember).filter(
                models.GroupMember.group_id == membership.group_id,
                models.GroupMember.account_id != account.id,
                models.GroupMember.is_active.is_(True),
            ).first()
            if replacement:
                replacement.role = "owner"
        membership.is_active = False
        membership.left_at = datetime.now(timezone.utc)
        membership.role = "member"
        membership.display_name = "Deleted member"
    for settings in db.query(models.GroupMemberSettings).filter_by(account_id=account.id).all():
        settings.vlm_api_key_encrypted = None
    settings = db.query(models.AccountSettings).filter_by(account_id=account.id).first()
    if settings:
        settings.vlm_api_key_encrypted = None
        settings.use_own_vlm = False
        settings.email_monthly_reports = False
    db.query(models.FriendConnection).filter(
        or_(models.FriendConnection.account_one_id == account.id, models.FriendConnection.account_two_id == account.id)
    ).update({models.FriendConnection.status: "REMOVED"}, synchronize_session=False)
    db.query(models.AccountVendor).filter_by(account_id=account.id).delete(synchronize_session=False)
    account.avatar_path = None
    account.display_name = "Deleted member"
    account.email = f"deleted-{account.id}-{secrets.token_hex(8)}@deleted.invalid"
    account.password_hash = hash_password(secrets.token_urlsafe(32))
    account.deletion_completed_at = datetime.now(timezone.utc)
    account.deletion_purge_after = None


def _purge_expired_account_deletions() -> None:
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        accounts = db.query(models.Account).filter(
            models.Account.deletion_purge_after.isnot(None),
            models.Account.deletion_completed_at.is_(None),
        ).all()
        for account in accounts:
            due = _as_utc(account.deletion_purge_after)
            if due and due <= now:
                _finalize_account_deletion(db, account)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Could not finalize scheduled account deletions")
    finally:
        db.close()


READ_NOTIFICATION_RETENTION = timedelta(days=2)


def _purge_stale_read_notifications(db: Optional[Session] = None) -> int:
    """Delete notifications that have been read for more than 2 days."""
    owns_session = db is None
    session = db or SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - READ_NOTIFICATION_RETENTION
        stale_ids = [
            row.id
            for row in session.query(models.Notification.id, models.Notification.read_at)
            .filter(models.Notification.read_at.isnot(None))
            .all()
            if (read_at := _as_utc(row.read_at)) is not None and read_at <= cutoff
        ]
        if not stale_ids:
            return 0
        deleted = (
            session.query(models.Notification)
            .filter(models.Notification.id.in_(stale_ids))
            .delete(synchronize_session=False)
        )
        session.commit()
        if deleted:
            logger.info("Purged %s read notifications older than 2 days", deleted)
        return int(deleted or 0)
    except Exception:
        session.rollback()
        logger.exception("Could not purge stale read notifications")
        return 0
    finally:
        if owns_session:
            session.close()


account_cleanup_scheduler = BackgroundScheduler()
_SCHEDULER_LOCK_FH = None


def _acquire_scheduler_lock() -> bool:
    """Return True if this process should own APScheduler (one per machine/volume)."""
    global _SCHEDULER_LOCK_FH
    lock_path = os.path.join(DATA_DIR, "scheduler.lock")
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        fh = open(lock_path, "a+")
        if os.name == "nt":
            import msvcrt

            try:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                fh.close()
                return False
        else:
            import fcntl

            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                return False
        fh.write(str(os.getpid()))
        fh.flush()
        _SCHEDULER_LOCK_FH = fh
        return True
    except Exception as err:
        logger.warning("Could not acquire scheduler lock: %s", err)
        return False


@app.on_event("startup")
def start_account_deletion_cleanup() -> None:
    redis_support.subscribe("group-events:*", _on_redis_group_event)
    start_background_poller(resolve_vlm_config)

    # Lightweight always-on repair/cleanup that is safe on every worker.
    _purge_expired_account_deletions()
    db = SessionLocal()
    try:
        purge_expired_sandboxes(db)
        _repair_all_group_payment_snapshots(db)
        _repair_all_weekly_locks_to_smart(db)
    except Exception:
        db.rollback()
        logger.exception("Startup balance repair failed")
    finally:
        db.close()
    _backfill_expense_audit_baselines()

    # Exactly one process owns scheduled digests/rollover/email (RUN_SCHEDULER=1).
    # A filesystem lock prevents duplicate schedulers when WEB_CONCURRENCY>1.
    if RUN_SCHEDULER and not DISABLE_SCHEDULER and _acquire_scheduler_lock():
        _purge_stale_read_notifications()
        _rollover_monthly_periods()
        _lock_due_weekly_settlements()
        # Digest catch-up only from the scheduler owner — never on every worker.
        if not IS_DEVELOPMENT:
            today = _business_today()
            previous_month = date(today.year - (today.month == 1), 12 if today.month == 1 else today.month - 1, 1)
            _deliver_monthly_digests(_period_key_for(previous_month))
        if not account_cleanup_scheduler.running:
            account_cleanup_scheduler.add_job(_purge_expired_account_deletions, "interval", hours=24, id="account-deletion-cleanup", replace_existing=True)
            account_cleanup_scheduler.add_job(_purge_stale_read_notifications, "interval", hours=6, id="read-notification-cleanup", replace_existing=True)
            account_cleanup_scheduler.add_job(
                lambda: _purge_demo_sandboxes_job(), "interval", minutes=30,
                id="demo-sandbox-cleanup", replace_existing=True,
            )
            account_cleanup_scheduler.add_job(
                purge_old_tmp_uploads, "interval", hours=6,
                id="tmp-upload-cleanup", replace_existing=True,
            )
            account_cleanup_scheduler.add_job(
                _rollover_monthly_periods,
                CronTrigger(hour=0, minute=0, timezone=BUSINESS_TIMEZONE),
                id="monthly-period-rollover", replace_existing=True,
            )
            account_cleanup_scheduler.add_job(
                _lock_due_weekly_settlements,
                CronTrigger(hour=0, minute=5, timezone=BUSINESS_TIMEZONE),
                id="weekly-settlement-lock", replace_existing=True,
            )
            account_cleanup_scheduler.add_job(
                lambda: _deliver_monthly_digests(_period_key_for(date(_business_today().year - (_business_today().month == 1), 12 if _business_today().month == 1 else _business_today().month - 1, 1))),
                CronTrigger(hour=0, minute=15, timezone=BUSINESS_TIMEZONE),
                id="monthly-digest-delivery", replace_existing=True,
            )
            account_cleanup_scheduler.add_job(
                _process_due_recurring_expenses,
                CronTrigger(hour=0, minute=30, timezone=BUSINESS_TIMEZONE),
                id="recurring-expense-post", replace_existing=True,
            )
            account_cleanup_scheduler.start()
            logger.info("Scheduler owner started (RUN_SCHEDULER=1)")
            # Catch anything due immediately after deploy/restart.
            try:
                _process_due_recurring_expenses()
            except Exception:
                logger.exception("Initial recurring expense pass failed")
    else:
        logger.info("Scheduler not started on this process (RUN_SCHEDULER=%s DISABLE=%s)", RUN_SCHEDULER, DISABLE_SCHEDULER)


@app.on_event("shutdown")
def stop_account_deletion_cleanup() -> None:
    if account_cleanup_scheduler.running:
        account_cleanup_scheduler.shutdown(wait=False)


def _purge_demo_sandboxes_job() -> None:
    db = SessionLocal()
    try:
        purge_expired_sandboxes(db)
    except Exception:
        db.rollback()
        logger.exception("Could not purge expired demo sandboxes")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Public demo sandbox
# ---------------------------------------------------------------------------
@app.post("/api/demo/sandbox", response_model=schemas.DemoSandboxOut)
def start_demo_sandbox(request: Request, db: Session = Depends(get_db)):
    """Create an isolated, expiring copy of the private demo template."""
    check_rate_limit(request, limit=5, window_seconds=60)
    try:
        return create_sandbox(db)
    except Exception as exc:
        db.rollback()
        logger.exception("Could not create demo sandbox")
        raise HTTPException(503, "Could not start a demo sandbox. Please try again.") from exc


@app.get("/api/demo/sandbox", response_model=schemas.DemoSandboxOut)
def demo_sandbox_status(
    account: models.Account = Depends(get_current_account), db: Session = Depends(get_db),
):
    sandbox = sandbox_for_account(db, account.id)
    if not sandbox:
        raise HTTPException(404, "This account is not a demo sandbox")
    return schemas.DemoSandboxOut(
        sandbox_id=sandbox.id,
        group_id=sandbox.group_id,
        expires_at=sandbox.expires_at,
        access_token=create_access_token_for_account(account),
    )


@app.post("/api/demo/sandbox/reset", response_model=schemas.DemoSandboxOut)
def reset_demo_sandbox(
    account: models.Account = Depends(get_current_account), db: Session = Depends(get_db),
):
    try:
        return reset_sandbox(db, account.id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        db.rollback()
        logger.exception("Could not reset demo sandbox")
        raise HTTPException(503, "Could not reset this demo sandbox. Please try again.") from exc


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
    token = create_access_token_for_account(account)
    return schemas.TokenResponse(access_token=token)


@app.post("/api/auth/login", response_model=schemas.TokenResponse)
def login(payload: schemas.LoginRequest, request: Request, db: Session = Depends(get_db)):
    check_rate_limit(request, limit=20, window_seconds=60)
    email_clean = payload.email.lower().strip()
    account = db.query(models.Account).filter_by(email=email_clean).first()
    if not account or not verify_password(payload.password, account.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if account.deletion_completed_at:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    deletion_deadline = _as_utc(account.deletion_purge_after)
    if deletion_deadline:
        if deletion_deadline <= datetime.now(timezone.utc):
            _finalize_account_deletion(db, account)
            db.commit()
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account is scheduled for deletion. Restore it within 30 days from the login page.")

    process_pending_invites(db, account)
    token = create_access_token_for_account(account)
    return schemas.TokenResponse(access_token=token)


@app.post("/api/auth/restore-account", response_model=schemas.TokenResponse)
def restore_account(payload: schemas.AccountRestoreRequest, request: Request, db: Session = Depends(get_db)):
    check_rate_limit(request, limit=10, window_seconds=60)
    account = db.query(models.Account).filter_by(email=str(payload.email).lower().strip()).first()
    if not account or not verify_password(payload.password, account.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    deadline = _as_utc(account.deletion_purge_after)
    if not deadline or account.deletion_completed_at:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No restorable account was found")
    if deadline <= datetime.now(timezone.utc):
        _finalize_account_deletion(db, account)
        db.commit()
        raise HTTPException(status.HTTP_410_GONE, "The 30-day recovery period has ended")
    account.deletion_requested_at = None
    account.deletion_purge_after = None
    bump_session_version(account)
    db.commit()
    process_pending_invites(db, account)
    return schemas.TokenResponse(access_token=create_access_token_for_account(account))


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
        bump_session_version(account)

    db.commit()
    db.refresh(account)
    return schemas.AccountOut(
        id=account.id,
        email=account.email,
        display_name=account.display_name,
        has_avatar=bool(getattr(account, "avatar_path", None) and os.path.exists(account.avatar_path or "")),
    )


@app.post("/api/auth/me/delete-account", response_model=schemas.MessageResponse)
def request_account_deletion(
    payload: schemas.AccountDeletionRequest,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.current_password, account.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect")
    now = datetime.now(timezone.utc)
    account.deletion_requested_at = now
    account.deletion_purge_after = now + timedelta(days=30)
    bump_session_version(account)
    db.commit()
    return schemas.MessageResponse(message="Your account is scheduled for permanent deletion in 30 days. You can restore it by signing in from the login page before then.")


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
        try:
            send_password_reset_email(
                to_email=account.email,
                reset_url=reset_url,
                display_name=account.display_name,
            )
        except Exception:
            logger.exception("Password reset email failed for %s", account.email)

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
    bump_session_version(account)
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
def _can_manage_quick_split(split: models.QuickSplit, account: models.Account) -> bool:
    """Either party on a one-to-one bill can edit or delete it."""
    if (split.created_by_account_id or split.payer_account_id) == account.id:
        return True
    if split.payer_account_id == account.id:
        return True
    return any(
        (p.account_id == account.id) or (p.email and p.email.lower() == account.email.lower())
        for p in split.participants
    )


def _quick_split_out(split: models.QuickSplit, account: models.Account) -> schemas.QuickSplitOut:
    is_payer = split.payer_account_id == account.id
    is_owner = _can_manage_quick_split(split, account)
    history = []
    try:
        history = json.loads(split.edit_history_json or "[]")
        if not isinstance(history, list):
            history = []
    except Exception:
        history = []
    last_adj = None
    if history:
        try:
            last_adj = float(history[-1].get("delta"))
        except Exception:
            last_adj = None
    return schemas.QuickSplitOut(
        id=split.id,
        title=split.title,
        total_amount=float(split.total_amount),
        currency=split.currency or "USD",
        bill_date=split.bill_date,
        category=split.category or "General",
        split_type=split.split_type or "EQUAL",
        payer_share_amount=float(split.payer_share_amount or 0),
        payer_paid_amount=float(split.payer_paid_amount) if split.payer_paid_amount is not None else None,
        payer_name=split.payer.display_name if split.payer else "Someone",
        payer_account_id=split.payer_account_id,
        payer_email=split.payer.email if split.payer else None,
        payer_has_avatar=bool(split.payer and getattr(split.payer, "avatar_path", None) and os.path.exists(split.payer.avatar_path or "")),
        invite_code=split.invite_code,
        status=split.status,
        created_at=split.created_at,
        is_payer=is_payer,
        is_owner=is_owner,
        edited_after_payment=bool(getattr(split, "edited_after_payment", False)),
        edit_history=history,
        adjustment_of_split_id=getattr(split, "adjustment_of_split_id", None),
        last_adjustment_amount=last_adj,
        participants=[
            schemas.QuickSplitParticipantOut(
                id=p.id,
                account_id=p.account_id,
                email=None if p.email.endswith("@invite.local") else p.email,
                display_name=p.display_name,
                share_amount=float(p.share_amount),
                paid_amount=float(p.paid_amount or 0),
                remaining_amount=float(_private_participant_remaining(split, p)),
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


def _assign_shared_payment(split: models.QuickSplit, payer_paid_amount: Optional[float]) -> None:
    """Record cash when both people paid the same bill. Empty means the payer paid all of it."""
    if payer_paid_amount is None:
        split.payer_paid_amount = None
        return
    total = Decimal(str(split.total_amount)).quantize(Decimal("0.01"))
    paid = Decimal(str(payer_paid_amount)).quantize(Decimal("0.01"))
    if paid <= 0 or paid >= total:
        raise HTTPException(400, "When both people pay, each amount must be greater than zero and less than the bill total")
    split.payer_paid_amount = paid


def _viewer_direct_effect(split: models.QuickSplit, participant: models.QuickSplitParticipant, account_id: int) -> Decimal:
    """Positive means the other person owes this account for one private bill."""
    total = Decimal(str(split.total_amount))
    payer_paid = Decimal(str(split.payer_paid_amount)) if split.payer_paid_amount is not None else total
    if split.payer_account_id == account_id:
        my_paid = payer_paid
        my_share = Decimal(str(split.payer_share_amount or 0))
    else:
        my_paid = total - payer_paid
        my_share = Decimal(str(participant.share_amount))
    return (my_paid - my_share).quantize(Decimal("0.01"))


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
        raise HTTPException(400, "Invalid split values — check the amounts entered") from err
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


def _quick_split_is_payment_locked(db: Session, split: models.QuickSplit) -> bool:
    """Legacy helper: month was fully settled (kept for callers; edits now use adjustments)."""
    if not split.created_at:
        return False
    for participant in split.participants:
        partner_id = participant.account_id
        if not partner_id or partner_id == split.payer_account_id:
            continue
        one, two = _friend_pair(split.payer_account_id, partner_id)
        settlement = db.query(models.FriendMonthlySettlement).filter_by(
            account_one_id=one, account_two_id=two, month_key=split.bill_date.strftime("%Y-%m"),
        ).first()
        if settlement and (settlement.status == "SETTLED" or (settlement.settled_at and split.created_at <= settlement.settled_at)):
            return True
    return False


def _quick_split_was_paid(db: Session, split: models.QuickSplit) -> bool:
    """True when cash already moved for this private bill (pair payment or monthly settle)."""
    if any(p.payment_status == "CONFIRMED" for p in (split.participants or [])):
        return True
    if _quick_split_is_payment_locked(db, split):
        return True
    month_key = split.bill_date.strftime("%Y-%m")
    currency = _normalize_currency(split.currency, "USD")
    for participant in split.participants or []:
        partner_id = participant.account_id
        if not partner_id or partner_id == split.payer_account_id:
            continue
        one, two = _friend_pair(split.payer_account_id, partner_id)
        if _pair_has_direct_split_payment(db, one, two, month_key, split.payer_account_id, currency, split.id):
            return True
        if _pair_has_direct_split_payment(db, one, two, month_key, partner_id, currency, split.id):
            return True
    return False


def _notify_accounts(db: Session, account_ids: list[int], message: str, link: str = "") -> None:
    seen = set()
    for account_id in account_ids:
        if not account_id or account_id in seen:
            continue
        seen.add(account_id)
        db.add(models.Notification(account_id=account_id, group_id=None, message=message[:300], link=link or None))


def _friend_share_amount(split: models.QuickSplit, friend_account_id: int) -> Decimal:
    for participant in split.participants or []:
        if participant.account_id == friend_account_id:
            return Decimal(str(participant.share_amount or 0)).quantize(Decimal("0.01"))
    return Decimal("0.00")


def _append_quick_split_edit_history(split: models.QuickSplit, entry: dict) -> None:
    try:
        history = json.loads(split.edit_history_json or "[]")
        if not isinstance(history, list):
            history = []
    except Exception:
        history = []
    history.append(entry)
    split.edit_history_json = json.dumps(history[-20:])
    split.edited_after_payment = True


def _create_paid_bill_adjustment(
    db: Session,
    *,
    source: models.QuickSplit,
    friend_id: int,
    delta: Decimal,
    editor: models.Account,
) -> models.QuickSplit:
    """Put only the difference into the current month; leave settled months closed."""
    amount = abs(delta).quantize(Decimal("0.01"))
    today = _business_today()
    if delta > 0:
        # Friend owes more → original payer "paid" the extra on their behalf.
        payer_id = source.payer_account_id
        other_id = friend_id
    else:
        # Friend was overcharged → credit them by reversing the delta.
        payer_id = friend_id
        other_id = source.payer_account_id
    other = db.query(models.Account).get(other_id)
    adj = models.QuickSplit(
        title=f"Adjustment · {source.title}"[:150],
        total_amount=amount,
        currency=_normalize_currency(source.currency, "USD"),
        bill_date=today,
        category=source.category or "General",
        split_type="EXACT",
        payer_share_amount=Decimal("0.00"),
        created_by_account_id=editor.id,
        payer_account_id=payer_id,
        invite_code=secrets.token_urlsafe(12),
        status="OPEN",
        edited_after_payment=False,
        edit_history_json=None,
        adjustment_of_split_id=source.id,
    )
    db.add(adj)
    db.flush()
    db.add(
        models.QuickSplitParticipant(
            quick_split_id=adj.id,
            email=(other.email if other else f"adj-{adj.id}@invite.local"),
            account_id=other_id,
            display_name=other.display_name if other else "Friend",
            share_amount=amount,
            split_value=amount,
            payment_status="PENDING",
            invite_token=secrets.token_urlsafe(12),
        )
    )
    return adj


def _offset_paid_edit_in_past_month(
    db: Session,
    *,
    source: models.QuickSplit,
    friend_id: int,
    delta: Decimal,
    editor_id: int,
) -> None:
    """Cancel an in-place share change in a past month so only the adjustment counts."""
    amount = abs(delta).quantize(Decimal("0.01"))
    if amount <= 0 or not source.payer_account_id or not friend_id:
        return
    month_key = source.bill_date.strftime("%Y-%m")
    currency = _normalize_currency(source.currency, "USD")
    if delta > 0:
        # Friend share rose → neutralize with a same-month payment from friend → payer.
        payer_id, recipient_id = friend_id, source.payer_account_id
    else:
        payer_id, recipient_id = source.payer_account_id, friend_id
    _record_direct_pair_payment(
        db, payer_id, recipient_id, amount, currency, month_key, editor_id,
    )


def _scrub_quick_split_payment_refs(db: Session, split: models.QuickSplit) -> None:
    """Detach a deleted bill's id from pair payments so reused SQLite ids stay clean."""
    if not split.id or not split.payer_account_id:
        return
    partner_ids = [
        p.account_id for p in (split.participants or [])
        if p.account_id and p.account_id != split.payer_account_id
    ]
    for partner_id in partner_ids:
        one, two = _friend_pair(split.payer_account_id, partner_id)
        for payment in db.query(models.PairPayment).filter_by(
            account_one_id=one, account_two_id=two,
        ).all():
            try:
                allocations = json.loads(payment.allocations_json or "[]")
            except Exception:
                continue
            if not isinstance(allocations, list):
                continue
            changed = False
            rewritten = []
            for allocation in allocations:
                if not isinstance(allocation, dict):
                    rewritten.append(allocation)
                    continue
                if allocation.get("quick_split_id") == split.id:
                    cleaned = {
                        key: value for key, value in allocation.items()
                        if key not in {"quick_split_id", "participant_id"}
                    }
                    cleaned["deleted_quick_split"] = True
                    rewritten.append(cleaned)
                    changed = True
                else:
                    rewritten.append(allocation)
            if changed:
                payment.allocations_json = json.dumps(rewritten)


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
        category=_normalize_category(payload.category), created_by_account_id=account.id,
        payer_account_id=payer_account.id,
        split_type=(payload.split_type or "EQUAL").upper(),
        edited_after_payment=False,
        edit_history_json=None,
        adjustment_of_split_id=None,
    )
    _assign_shared_payment(split, payload.payer_paid_amount)
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
    if not _can_manage_quick_split(split, account):
        raise HTTPException(403, "You cannot edit this split")
    if len(payload.participants) != 1:
        raise HTTPException(400, "A one-to-one split needs exactly one other person. Create a group for 3 or more people.")
    if payload.bill_date > date.today():
        raise HTTPException(400, "A bill date cannot be in the future")

    was_paid = _quick_split_was_paid(db, split)
    month_was_settled = _quick_split_is_payment_locked(db, split)
    bill_month = split.bill_date.strftime("%Y-%m")
    current_month = _business_today().strftime("%Y-%m")
    # Past-month paid bills (combined payment or monthly settle) move only the
    # delta into the current month via an adjustment row.
    use_adjustment = was_paid and (month_was_settled or bill_month < current_month)
    old_total = Decimal(str(split.total_amount or 0)).quantize(Decimal("0.01"))
    old_title = split.title
    friend_id = next(
        (p.account_id for p in (split.participants or []) if p.account_id and p.account_id != split.payer_account_id),
        None,
    )
    old_friend_share = _friend_share_amount(split, friend_id) if friend_id else Decimal("0.00")

    payer_account = account
    if payload.payer_account_id and payload.payer_account_id != account.id:
        payer_account = db.query(models.Account).get(payload.payer_account_id)
        if not payer_account:
            raise HTTPException(404, "Person who paid was not found")
        if not any(raw.account_id == account.id or (raw.email and str(raw.email).lower() == account.email.lower()) for raw in payload.participants):
            raise HTTPException(400, "When your friend paid, your own account must be the other person in the split")
    elif payload.payer_account_id:
        payer_account = db.query(models.Account).get(payload.payer_account_id) or account

    invitees = []
    selected_accounts = {}
    for index, raw in enumerate(payload.participants, start=1):
        token = secrets.token_urlsafe(16)
        selected_account = db.query(models.Account).get(raw.account_id) if raw.account_id else None
        if raw.account_id and not selected_account:
            raise HTTPException(404, "Friend account not found")
        email = selected_account.email if selected_account else (str(raw.email).strip().lower() if raw.email else f"slot-{token}@invite.local")
        if email and email not in [item[0] for item in invitees]:
            invitees.append((email, raw.value, raw.label or f"Person {index}", token))
            selected_accounts[email] = selected_account
    if not invitees:
        raise HTTPException(400, "Add at least one other person")
    if account.email.lower() in [item[0] for item in invitees] and payer_account.id == account.id:
        raise HTTPException(400, "Add at least one other person's email")

    for raw in payload.participants:
        if raw.account_id and raw.account_id != account.id:
            _activate_friendship(db, account.id, raw.account_id)

    split.title = payload.title.strip()
    split.total_amount = Decimal(str(payload.total_amount)).quantize(Decimal("0.01"))
    split.currency = _normalize_currency(payload.currency, "USD")
    # Paid/settled or past-month paid bills keep their original month; delta lands today.
    if not use_adjustment:
        split.bill_date = payload.bill_date
    split.category = _normalize_category(payload.category)
    split.split_type = (payload.split_type or "EQUAL").upper()
    split.payer_account_id = payer_account.id
    _assign_shared_payment(split, payload.payer_paid_amount)
    existing_by_email = {p.email: p for p in split.participants}
    kept = set()
    for email, value, label, token in invitees:
        participant = existing_by_email.get(email)
        if participant is None:
            existing_account = selected_accounts.get(email) or db.query(models.Account).filter_by(email=email).first()
            participant = models.QuickSplitParticipant(quick_split_id=split.id, email=email, account_id=existing_account.id if existing_account else None, display_name=existing_account.display_name if existing_account else label, share_amount=0, invite_token=token)
            db.add(participant)
        else:
            existing_account = selected_accounts.get(email) or db.query(models.Account).filter_by(email=email).first()
            if existing_account:
                participant.account_id = existing_account.id
                participant.display_name = existing_account.display_name
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

    friend_id = next(
        (p.account_id for p in (split.participants or []) if p.account_id and p.account_id != split.payer_account_id),
        friend_id,
    )
    new_friend_share = _friend_share_amount(split, friend_id) if friend_id else Decimal("0.00")
    delta = (new_friend_share - old_friend_share).quantize(Decimal("0.01"))

    if was_paid and friend_id and abs(delta) >= Decimal("0.005"):
        if use_adjustment:
            _create_paid_bill_adjustment(db, source=split, friend_id=friend_id, delta=delta, editor=account)
            # Past month still counts in consolidated sources (unlike a SETTLED
            # monthly settlement). Neutralize the in-place share change there so
            # only the current-month adjustment moves money.
            if not month_was_settled and bill_month < current_month:
                _offset_paid_edit_in_past_month(
                    db, source=split, friend_id=friend_id, delta=delta, editor_id=account.id,
                )
        # Current-month paid (not settled): in-place share change moves balance by delta only.
        _append_quick_split_edit_history(split, {
            "at": datetime.now(timezone.utc).isoformat(),
            "editor_id": account.id,
            "editor_name": account.display_name,
            "old_total": float(old_total),
            "new_total": float(Decimal(str(payload.total_amount)).quantize(Decimal("0.01"))),
            "old_friend_share": float(old_friend_share),
            "new_friend_share": float(new_friend_share),
            "delta": float(delta),
            "title": old_title,
        })
        direction = "more" if delta > 0 else "less"
        msg = (
            f"{account.display_name} edited “{old_title}” after it was paid: "
            f"{split.currency} {float(old_total):.2f} → {float(Decimal(str(payload.total_amount))):.2f}. "
            f"Balance moves by {split.currency} {abs(float(delta)):.2f} ({direction})."
        )
        _notify_accounts(db, [split.payer_account_id, friend_id], msg, "/index.html?view=friends")
    elif not was_paid:
        for participant in split.participants:
            if participant.account_id and participant.account_id != split.payer_account_id:
                _reopen_friend_settlement(db, split.payer_account_id, participant.account_id, split.bill_date)
                _reopen_friend_settlement_for_bill_change(
                    db, split.payer_account_id, participant.account_id, split.bill_date, split.created_at,
                )

    db.commit(); db.refresh(split)
    return _quick_split_out(split, account)


def _pair_direct_month_paid_total(
    db: Session, one: int, two: int, month_key: str, payer_id: int, currency: str,
) -> Decimal:
    currency = _normalize_currency(currency, "USD")
    total = Decimal("0.00")
    for payment in db.query(models.PairPayment).filter_by(
        account_one_id=one, account_two_id=two, status="CONFIRMED",
        payer_account_id=payer_id, currency=currency,
    ).all():
        for allocation in json.loads(payment.allocations_json or "[]"):
            if allocation.get("kind") == "DIRECT" and allocation.get("month_key") == month_key:
                total += Decimal(str(allocation.get("amount", 0)))
    return total.quantize(Decimal("0.01"))


def _pair_has_direct_split_payment(
    db: Session, one: int, two: int, month_key: str, payer_id: int, currency: str, quick_split_id: int,
) -> bool:
    currency = _normalize_currency(currency, "USD")
    for payment in db.query(models.PairPayment).filter_by(
        account_one_id=one, account_two_id=two, status="CONFIRMED",
        payer_account_id=payer_id, currency=currency,
    ).all():
        for allocation in json.loads(payment.allocations_json or "[]"):
            if (
                allocation.get("kind") == "DIRECT"
                and allocation.get("month_key") == month_key
                and allocation.get("quick_split_id") == quick_split_id
            ):
                return True
    return False


def _record_direct_pair_payment(
    db: Session,
    payer_id: int,
    recipient_id: int,
    amount: Decimal,
    currency: str,
    month_key: str,
    reported_by_account_id: int,
    quick_split_id: Optional[int] = None,
) -> Optional[models.PairPayment]:
    """Persist confirmed private-bill cash so later edit/delete can reverse into credit."""
    amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    if amount <= 0 or payer_id == recipient_id:
        return None
    currency = _normalize_currency(currency, "USD")
    one, two = _friend_pair(payer_id, recipient_id)
    if quick_split_id is not None and _pair_has_direct_split_payment(
        db, one, two, month_key, payer_id, currency, quick_split_id,
    ):
        return None
    allocation = {
        "kind": "DIRECT",
        "month_key": month_key,
        "amount": float(amount),
    }
    if quick_split_id is not None:
        allocation["quick_split_id"] = quick_split_id
        # The caller has already applied this exact bill payment locally.
        allocation["private_applied"] = True
    payment = models.PairPayment(
        account_one_id=one,
        account_two_id=two,
        payer_account_id=payer_id,
        recipient_account_id=recipient_id,
        amount=amount,
        currency=currency,
        status="CONFIRMED",
        reported_by_account_id=reported_by_account_id,
        allocations_json=json.dumps([allocation]),
        confirmed_at=datetime.now(timezone.utc),
    )
    db.add(payment)
    return payment


def _reopen_friend_settlement_for_bill_change(
    db: Session, first_id: int, second_id: int, bill_date: date, bill_created_at: Optional[datetime] = None,
) -> None:
    """If a settled month's bill is edited/deleted, reopen that month so balances can move."""
    one, two = _friend_pair(first_id, second_id)
    month_key = bill_date.strftime("%Y-%m")
    settlement = db.query(models.FriendMonthlySettlement).filter_by(
        account_one_id=one, account_two_id=two, month_key=month_key,
    ).first()
    if not settlement or not settlement.settled_at:
        return
    created = bill_created_at
    if created and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    settled_at = settlement.settled_at if settlement.settled_at.tzinfo else settlement.settled_at.replace(tzinfo=timezone.utc)
    if created and created > settled_at:
        return
    # Cash that closed the month must remain as credit/debt after the bill change.
    if settlement.payer_account_id and settlement.recipient_account_id and settlement.amount:
        already_paid = _pair_direct_month_paid_total(
            db, one, two, month_key, settlement.payer_account_id, settlement.currency or "USD",
        )
        gap = (Decimal(str(settlement.amount)) - already_paid).quantize(Decimal("0.01"))
        if gap > 0:
            _record_direct_pair_payment(
                db,
                settlement.payer_account_id,
                settlement.recipient_account_id,
                gap,
                settlement.currency or "USD",
                month_key,
                settlement.recipient_account_id,
            )
    settlement.status = "PENDING"
    settlement.payer_confirmed = False
    settlement.recipient_confirmed = False
    settlement.settled_at = None
    balance, currency = _friend_month_balance(db, first_id, second_id, month_key)
    settlement.amount = abs(balance)
    settlement.currency = currency
    settlement.payer_account_id = first_id if balance < 0 else second_id
    settlement.recipient_account_id = second_id if balance < 0 else first_id


def _preserve_friend_payment_credit_before_delete(db: Session, split: models.QuickSplit) -> None:
    """When a paid private bill is deleted, keep confirmed cash as an open Friend credit."""
    confirmed = [p for p in split.participants if p.payment_status == "CONFIRMED" and p.account_id]
    if not confirmed or not split.payer_account_id:
        return
    month_key = split.bill_date.strftime("%Y-%m")
    currency = _normalize_currency(split.currency, "USD")
    for participant in confirmed:
        if participant.account_id == split.payer_account_id:
            continue
        amount = Decimal(str(participant.share_amount or 0)).quantize(Decimal("0.01"))
        if amount <= 0:
            continue
        # Debtor already paid the lender; deleting the bill means the lender owes that cash back.
        # Skip when confirm already wrote a PairPayment for this month.
        _record_direct_pair_payment(
            db,
            participant.account_id,
            split.payer_account_id,
            amount,
            currency,
            month_key,
            split.payer_account_id,
            quick_split_id=split.id,
        )


@app.delete("/api/quick-splits/{split_id}")
def delete_quick_split(split_id: int, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    """Permanently remove a one-to-one split. Either party may do this."""
    split = _require_quick_split_access(split_id, account, db)
    if not _can_manage_quick_split(split, account):
        raise HTTPException(403, "You cannot delete this split")
    was_paid = _quick_split_was_paid(db, split)
    month_was_settled = _quick_split_is_payment_locked(db, split)
    partners = [
        participant.account_id for participant in split.participants
        if participant.account_id and participant.account_id != split.payer_account_id
    ]
    bill_date = split.bill_date
    created_at = split.created_at
    payer_id = split.payer_account_id
    title = split.title
    currency = _normalize_currency(split.currency, "USD")
    # Monthly-settled deletes credit via a current-month adjustment only.
    # Pair-payment deletes keep cash via preserve. Never both — that double-credits.
    if was_paid and partners and month_was_settled:
        for partner_id in partners:
            share = _friend_share_amount(split, partner_id)
            if share > 0:
                _create_paid_bill_adjustment(
                    db, source=split, friend_id=partner_id, delta=-share, editor=account,
                )
            _notify_accounts(
                db,
                [payer_id, partner_id],
                f"{account.display_name} deleted “{title}” after it was paid. "
                f"The paid amount stays as a {currency} credit on your balance.",
                "/index.html?view=friends",
            )
    else:
        _preserve_friend_payment_credit_before_delete(db, split)
        if was_paid and partners:
            for partner_id in partners:
                _notify_accounts(
                    db,
                    [payer_id, partner_id],
                    f"{account.display_name} deleted “{title}” after it was paid. "
                    f"The paid amount stays as a {currency} credit on your balance.",
                    "/index.html?view=friends",
                )
    # Drop bill-id links before delete so SQLite autoincrement reuse cannot
    # revive "paid" / "edited after payment" state on a brand-new bill.
    _scrub_quick_split_payment_refs(db, split)
    db.delete(split)
    db.flush()
    if not was_paid:
        for partner_id in partners:
            if payer_id:
                _reopen_friend_settlement_for_bill_change(db, payer_id, partner_id, bill_date, created_at)
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
    # Prefer the currency of this month's private bills — never let a stray
    # pair-payment currency (or account preferred currency) relabel CAD as GBP.
    counts: dict[str, int] = {}
    balance_by_ccy: dict[str, Decimal] = {}
    for split, participant in rows:
        ccy = _normalize_currency(split.currency, "USD")
        counts[ccy] = counts.get(ccy, 0) + 1
        balance_by_ccy[ccy] = balance_by_ccy.get(ccy, Decimal("0.00")) + _viewer_direct_effect(
            split, participant, account_id,
        )
    if counts:
        currency = max(counts.items(), key=lambda item: item[1])[0]
    elif settlement and settlement.currency:
        currency = _normalize_currency(settlement.currency, "USD")
    else:
        currency = "USD"
    balance = balance_by_ccy.get(currency, Decimal("0.00"))
    # Confirmed private payments in the month's bill currency only.
    for payment in db.query(models.PairPayment).filter_by(
        account_one_id=one, account_two_id=two, status="CONFIRMED",
        currency=currency,
    ).all():
        for allocation in json.loads(payment.allocations_json or "[]"):
            if allocation.get("kind") != "DIRECT" or allocation.get("month_key") != month_key:
                continue
            amount = Decimal(str(allocation.get("amount", 0)))
            balance += amount if payment.payer_account_id == account_id else -amount
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


def _pair_settlement_source_for_period(
    db: Session,
    group: models.Group,
    period: models.MonthlyPeriod,
    my_member_id: int,
    friend_member_id: int,
    currency: str,
    *,
    settlement_ready: bool,
) -> Optional[dict]:
    """Friend↔Friend amount from group Who pays whom (expense-aware). Counts in Friend total.

    Uses group base currency when it matches the Friends currency. When the period
    was settled (or previewed) into a different target currency, uses the converted
    Who-pays-whom amount so AUD groups settled into USD still appear on USD Friends.
    """
    currency = _normalize_currency(currency, "USD")
    base_currency = _normalize_currency(group.currency, "USD")
    target_raw = (period.target_currency or "").strip().upper()
    target_currency = _normalize_currency(target_raw) if target_raw else ""
    _balances, settlements, _expenses = build_balances_and_settlements(
        db, group.id, period, settlement_mode="smart",
    )
    for settlement in settlements:
        if {settlement["from_member_id"], settlement["to_member_id"]} != {my_member_id, friend_member_id}:
            continue

        converted_raw = (
            str(settlement.get("converted_currency") or target_currency or "").strip().upper()
        )
        converted_currency = _normalize_currency(converted_raw) if converted_raw else ""
        converted_amount = settlement.get("converted_amount")
        if currency == base_currency:
            amount = Decimal(str(settlement["amount"]))
            amount_currency = base_currency
        elif converted_currency and currency == converted_currency and converted_amount is not None:
            amount = Decimal(str(converted_amount))
            amount_currency = converted_currency
        else:
            return None

        # `from` pays `to` → from owes to. Positive for the viewer means the friend owes them.
        signed = amount if settlement["from_member_id"] == friend_member_id else -amount
        if abs(signed) < Decimal("0.005"):
            return None
        suffix = "" if settlement_ready else " (current)"
        settle_note = ""
        if amount_currency != base_currency:
            settle_note = f" · {amount_currency}"
        return {
            "kind": "GROUP",
            "label": f"{group.name} · Who pays whom · {period.period_key}{settle_note}{suffix}",
            "amount": float(signed),
            "currency": amount_currency,
            "period_key": period.period_key,
            "group_id": group.id,
            "period_id": period.id,
            "payer_member_id": settlement["from_member_id"],
            "recipient_member_id": settlement["to_member_id"],
            "settlement_ready": settlement_ready,
        }
    return None


def _locked_weekly_net_by_member(
    db: Session, group_id: int, period_id: int, *, exclude_batch_id: Optional[int] = None,
) -> dict[int, Decimal]:
    """Net already promised by locked weekly batches (credit is positive)."""
    totals: dict[int, Decimal] = {}
    query = (
        db.query(models.WeeklySettlementLeg)
        .join(models.WeeklySettlementBatch, models.WeeklySettlementLeg.batch_id == models.WeeklySettlementBatch.id)
        .filter(
            models.WeeklySettlementBatch.group_id == group_id,
            models.WeeklySettlementBatch.period_id == period_id,
            models.WeeklySettlementBatch.status == "LOCKED",
        )
    )
    if exclude_batch_id is not None:
        query = query.filter(models.WeeklySettlementBatch.id != exclude_batch_id)
    for row in query.all():
        amount = Decimal(str(row.amount)).quantize(Decimal("0.01"))
        totals[row.payer_member_id] = totals.get(row.payer_member_id, Decimal("0.00")) - amount
        totals[row.recipient_member_id] = totals.get(row.recipient_member_id, Decimal("0.00")) + amount
    return totals


def _applied_pair_amounts_for_batch(db: Session, batch_id: int) -> dict[tuple[int, int], Decimal]:
    """Confirmed Friends cash already allocated to legs in this locked batch."""
    applied: dict[tuple[int, int], Decimal] = {}
    for payment in db.query(models.PairPayment).filter_by(status="CONFIRMED").all():
        for allocation in json.loads(payment.allocations_json or "[]"):
            if allocation.get("kind") != "GROUP" or allocation.get("batch_id") != batch_id:
                continue
            payer_id = allocation.get("payer_member_id")
            recipient_id = allocation.get("recipient_member_id")
            if payer_id is None or recipient_id is None:
                continue
            key = (int(payer_id), int(recipient_id))
            applied[key] = applied.get(key, Decimal("0.00")) + Decimal(str(allocation.get("amount") or 0))
    return {key: amount.quantize(Decimal("0.01")) for key, amount in applied.items()}


def _compute_weekly_residual_legs(
    db: Session, group: models.Group, period: models.MonthlyPeriod, *, exclude_batch_id: Optional[int] = None,
) -> tuple[dict[int, dict], list[dict]]:
    """Live period net minus older locked nets → residual who-pays-whom legs.

    Uses the same expense-aware (smart) graph as the group Balances card so
    Friends never invents a transfer between two people who only co-owed
    someone else — that mismatch was showing wrong Bk / Extra pay lines.
    """
    balances, _live_settlements, expenses = build_balances_and_settlements(
        db, group.id, period, settlement_mode="smart",
    )
    locked_nets = _locked_weekly_net_by_member(db, group.id, period.id, exclude_batch_id=exclude_batch_id)
    residual: dict[int, dict] = {}
    for row in balances:
        member_id = int(row["member_id"])
        net = Decimal(str(row["net_balance"])) - locked_nets.get(member_id, Decimal("0.00"))
        residual[member_id] = {
            "member_id": member_id, "name": row["name"], "net_balance": float(net.quantize(Decimal("0.01"))),
        }
    members = [{"id": mid, "name": row["name"]} for mid, row in residual.items()]
    base_currency = _normalize_currency(group.currency, "USD")
    expense_dicts = _expenses_as_dicts(expenses, base_currency, base_currency)
    legs = graph_constrained_settlements(members, expense_dicts, residual)
    clean_legs = [{
        "payer_member_id": int(row["from_member_id"]),
        "recipient_member_id": int(row["to_member_id"]),
        "amount": Decimal(str(row["amount"])).quantize(Decimal("0.01")),
        "currency": base_currency,
    } for row in legs if Decimal(str(row["amount"])) > 0]
    return residual, clean_legs


def _rewrite_weekly_batch_legs(
    db: Session,
    batch: models.WeeklySettlementBatch,
    group: models.Group,
    period: models.MonthlyPeriod,
    residual: dict[int, dict],
    clean_legs: list[dict],
) -> None:
    """Replace legs on an existing locked batch; never drop below already-applied cash."""
    applied = _applied_pair_amounts_for_batch(db, batch.id)
    floor_pairs = {key: amount for key, amount in applied.items() if amount > 0}
    final_legs: dict[tuple[int, int], dict] = {}
    for leg in clean_legs:
        key = (leg["payer_member_id"], leg["recipient_member_id"])
        amount = max(leg["amount"], floor_pairs.pop(key, Decimal("0.00")))
        final_legs[key] = {
            "payer_member_id": key[0],
            "recipient_member_id": key[1],
            "amount": amount,
            "currency": leg["currency"],
        }
    # Keep a zero-remaining floor leg when cash was already paid on a direction that
    # no longer appears in residual math, so history stays allocated to this week.
    for key, paid in floor_pairs.items():
        final_legs[key] = {
            "payer_member_id": key[0],
            "recipient_member_id": key[1],
            "amount": paid,
            "currency": _normalize_currency(group.currency, "USD"),
        }

    db.query(models.WeeklySettlementLeg).filter_by(batch_id=batch.id).delete(synchronize_session=False)
    serialized = [{
        "payer_member_id": leg["payer_member_id"],
        "recipient_member_id": leg["recipient_member_id"],
        "amount": str(Decimal(str(leg["amount"])).quantize(Decimal("0.01"))),
        "currency": leg["currency"],
    } for leg in final_legs.values()]
    for leg in serialized:
        db.add(models.WeeklySettlementLeg(
            batch_id=batch.id,
            payer_member_id=leg["payer_member_id"],
            recipient_member_id=leg["recipient_member_id"],
            amount=Decimal(leg["amount"]),
            currency=leg["currency"],
        ))
    snapshot = {
        "period_key": period.period_key,
        "window_starts_at": (_as_utc(batch.window_starts_at) or batch.window_starts_at).isoformat(),
        "window_ends_at": (_as_utc(batch.window_ends_at) or batch.window_ends_at).isoformat(),
        "residual_net_by_member": {
            str(mid): str(Decimal(str(row["net_balance"])).quantize(Decimal("0.01"))) for mid, row in residual.items()
        },
        "legs": serialized,
        "reconciled": True,
    }
    prior_chain = (
        db.query(models.WeeklySettlementBatch)
        .filter(
            models.WeeklySettlementBatch.group_id == group.id,
            models.WeeklySettlementBatch.id != batch.id,
        )
        .order_by(models.WeeklySettlementBatch.id.desc())
        .first()
    )
    material = {
        "group_id": group.id, "period_id": period.id, "snapshot": snapshot,
        "previous_hash": prior_chain.batch_hash if prior_chain else None,
        "batch_id": batch.id,
    }
    batch.snapshot_json = _audit_json(snapshot)
    batch.previous_hash = prior_chain.batch_hash if prior_chain else None
    batch.batch_hash = hashlib.sha256(_audit_json(material).encode("utf-8")).hexdigest()
    db.add(batch)


def _reconcile_latest_weekly_lock(
    db: Session, group: models.Group, period: models.MonthlyPeriod, *, actor: Optional[models.Account] = None,
) -> Optional[models.WeeklySettlementBatch]:
    """After a bill change, rewrite the latest locked week so Friends updates in-place."""
    latest = (
        db.query(models.WeeklySettlementBatch)
        .filter_by(group_id=group.id, period_id=period.id, status="LOCKED")
        .order_by(models.WeeklySettlementBatch.window_ends_at.desc(), models.WeeklySettlementBatch.id.desc())
        .first()
    )
    if not latest:
        return None
    residual, clean_legs = _compute_weekly_residual_legs(db, group, period, exclude_batch_id=latest.id)
    _rewrite_weekly_batch_legs(db, latest, group, period, residual, clean_legs)
    _record_activity(
        db, group.id, actor, "weekly_settlement_reconciled",
        f"Updated locked weekly instructions through {(_as_utc(latest.window_ends_at) or latest.window_ends_at).astimezone(BUSINESS_TIMEZONE).date().isoformat()} after a bill change.",
        "weekly_settlement_batch", latest.id,
    )
    return latest


def _repair_all_weekly_locks_to_smart(db: Session) -> int:
    """Rewrite every latest locked week onto expense-aware Who-pays-whom.

    Older locks used simplify_debts and invented friend↔friend transfers (e.g.
    co-debtors of Bk). Safe to run on every startup: floors already-paid cash.
    """
    latest_ids = [
        row[0]
        for row in (
            db.query(func.max(models.WeeklySettlementBatch.id))
            .filter(models.WeeklySettlementBatch.status == "LOCKED")
            .group_by(models.WeeklySettlementBatch.group_id, models.WeeklySettlementBatch.period_id)
            .all()
        )
    ]
    if not latest_ids:
        return 0
    repaired = 0
    batches = (
        db.query(models.WeeklySettlementBatch)
        .filter(models.WeeklySettlementBatch.id.in_(latest_ids))
        .all()
    )
    groups = {
        g.id: g
        for g in db.query(models.Group).filter(
            models.Group.id.in_({batch.group_id for batch in batches})
        ).all()
    }
    periods = {
        p.id: p
        for p in db.query(models.MonthlyPeriod).filter(
            models.MonthlyPeriod.id.in_({batch.period_id for batch in batches})
        ).all()
    }
    for batch in batches:
        group = groups.get(batch.group_id)
        period = periods.get(batch.period_id)
        if not group or not period:
            continue
        residual, clean_legs = _compute_weekly_residual_legs(
            db, group, period, exclude_batch_id=batch.id,
        )
        _rewrite_weekly_batch_legs(db, batch, group, period, residual, clean_legs)
        repaired += 1
    if repaired:
        db.commit()
        logger.info("Repaired %s weekly settlement lock(s) onto smart Who-pays-whom", repaired)
    return repaired


def _lock_weekly_settlement_batch(
    db: Session, group: models.Group, period: models.MonthlyPeriod, *,
    window_ends_at: datetime, created_by: Optional[models.Account] = None,
) -> models.WeeklySettlementBatch:
    """Freeze residual debt for Friends. Manual lock is allowed mid-week.

    Later expense edits reconcile the latest locked batch for this period so the
    correction appears on Friends in that same locked timeframe.
    """
    end = _as_utc(window_ends_at) or datetime.now(timezone.utc)
    existing = db.query(models.WeeklySettlementBatch).filter_by(
        group_id=group.id, period_id=period.id, window_ends_at=end,
    ).first()
    if existing:
        # Re-locking the same window refreshes legs from live balances.
        residual, clean_legs = _compute_weekly_residual_legs(db, group, period, exclude_batch_id=existing.id)
        _rewrite_weekly_batch_legs(db, existing, group, period, residual, clean_legs)
        return existing
    previous = (
        db.query(models.WeeklySettlementBatch)
        .filter_by(group_id=group.id, period_id=period.id, status="LOCKED")
        .order_by(models.WeeklySettlementBatch.window_ends_at.desc(), models.WeeklySettlementBatch.id.desc())
        .first()
    )
    start = previous.window_ends_at if previous else datetime.combine(
        date.fromisoformat(f"{period.period_key}-01"), datetime.min.time(), tzinfo=timezone.utc,
    )
    residual, clean_legs = _compute_weekly_residual_legs(db, group, period)
    serialized = [{
        "payer_member_id": leg["payer_member_id"],
        "recipient_member_id": leg["recipient_member_id"],
        "amount": str(leg["amount"]),
        "currency": leg["currency"],
    } for leg in clean_legs]
    snapshot = {
        "period_key": period.period_key, "window_starts_at": _as_utc(start).isoformat(),
        "window_ends_at": end.isoformat(), "residual_net_by_member": {
            str(mid): str(Decimal(str(row["net_balance"])).quantize(Decimal("0.01"))) for mid, row in residual.items()
        }, "legs": serialized,
    }
    prior_chain = (
        db.query(models.WeeklySettlementBatch)
        .filter_by(group_id=group.id).order_by(models.WeeklySettlementBatch.id.desc()).first()
    )
    material = {"group_id": group.id, "period_id": period.id, "snapshot": snapshot,
                "previous_hash": prior_chain.batch_hash if prior_chain else None}
    batch = models.WeeklySettlementBatch(
        group_id=group.id, period_id=period.id, window_starts_at=_as_utc(start), window_ends_at=end,
        status="LOCKED", snapshot_json=_audit_json(snapshot),
        previous_hash=prior_chain.batch_hash if prior_chain else None,
        batch_hash=hashlib.sha256(_audit_json(material).encode("utf-8")).hexdigest(),
        created_by_account_id=created_by.id if created_by else None,
    )
    db.add(batch)
    db.flush()
    for leg in clean_legs:
        db.add(models.WeeklySettlementLeg(
            batch_id=batch.id,
            payer_member_id=leg["payer_member_id"],
            recipient_member_id=leg["recipient_member_id"],
            amount=leg["amount"],
            currency=leg["currency"],
        ))
    _record_activity(
        db, group.id, created_by, "weekly_settlement_locked",
        f"Locked weekly settlement through {end.astimezone(BUSINESS_TIMEZONE).date().isoformat()} ({len(clean_legs)} payment instruction{'s' if len(clean_legs) != 1 else ''}).",
        "weekly_settlement_batch", batch.id,
    )
    return batch


def _lock_due_weekly_settlements() -> int:
    """Scheduler-safe catch-up. A restart may delay a lock, but can never duplicate one."""
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        count = 0
        periods = (
            db.query(models.MonthlyPeriod)
            .join(models.Group)
            .filter(models.MonthlyPeriod.period_key <= _period_key_for(_business_today()))
            .all()
        )
        for period in periods:
            group = db.query(models.Group).get(period.group_id)
            if not group or not db.query(models.Expense.id).filter_by(period_id=period.id).first():
                continue
            month_start = datetime.combine(date.fromisoformat(f"{period.period_key}-01"), datetime.min.time(), tzinfo=timezone.utc)
            next_month = date(month_start.year + (month_start.month == 12), 1 if month_start.month == 12 else month_start.month + 1, 1)
            month_end = datetime.combine(next_month, datetime.min.time(), tzinfo=timezone.utc)
            latest = (
                db.query(models.WeeklySettlementBatch)
                .filter_by(group_id=group.id, period_id=period.id, status="LOCKED")
                .order_by(models.WeeklySettlementBatch.window_ends_at.desc()).first()
            )
            cursor = _as_utc(latest.window_ends_at) if latest else month_start
            while cursor < month_end and now >= min(cursor + timedelta(days=7), month_end):
                end = min(cursor + timedelta(days=7), month_end)
                _lock_weekly_settlement_batch(db, group, period, window_ends_at=end)
                cursor = end
                count += 1
        if count:
            db.commit()
        return count
    except Exception:
        db.rollback()
        logger.exception("Weekly settlement locking failed")
        return 0
    finally:
        db.close()


def _weekly_pair_settlement_sources(
    db: Session, group: models.Group, period: models.MonthlyPeriod, my_member_id: int, friend_member_id: int, currency: str,
) -> list[dict]:
    """Only locked weekly instructions appear as payable on Friends.

    Draft group math stays on the group page until someone locks the week
    (manually or on schedule). Bill edits after a lock rewrite that week's
    latest locked batch so Friends updates in the same timeframe.

    Extra pay is only for true reverse overpay: cash already moved one way on
    this pair/period, then a reverse residual appears (bill shrink). Locked
    weekly remaining must NOT be reclassified just because live Who-pays-whom
    differs — multi-person netting often makes those graphs disagree.
    """
    if _normalize_currency(group.currency, "USD") != _normalize_currency(currency, "USD"):
        return []
    rows = (
        db.query(models.WeeklySettlementLeg, models.WeeklySettlementBatch)
        .join(models.WeeklySettlementBatch, models.WeeklySettlementLeg.batch_id == models.WeeklySettlementBatch.id)
        .filter(
            models.WeeklySettlementBatch.group_id == group.id,
            models.WeeklySettlementBatch.period_id == period.id,
            models.WeeklySettlementBatch.status == "LOCKED",
            or_(
                (models.WeeklySettlementLeg.payer_member_id == my_member_id) & (models.WeeklySettlementLeg.recipient_member_id == friend_member_id),
                (models.WeeklySettlementLeg.payer_member_id == friend_member_id) & (models.WeeklySettlementLeg.recipient_member_id == my_member_id),
            ),
        ).order_by(models.WeeklySettlementBatch.window_ends_at.asc()).all()
    )
    confirmed_payments = db.query(models.PairPayment).filter(
        models.PairPayment.status == "CONFIRMED",
        models.PairPayment.currency == _normalize_currency(currency, "USD"),
    ).all()
    # Confirmed group cash for this period (Friends or group UI). Older pair
    # payments may omit batch_id; consume this pool oldest-week-first so paid
    # legs clear and reverse overpay residuals become Extra pay.
    period_cash_left: dict[tuple[int, int], Decimal] = {}
    for payment in (
        db.query(models.GroupPayment)
        .filter(
            models.GroupPayment.period_id == period.id,
            models.GroupPayment.status.in_(("PAID", "CONFIRMED")),
            or_(
                (models.GroupPayment.payer_member_id == my_member_id) & (models.GroupPayment.recipient_member_id == friend_member_id),
                (models.GroupPayment.payer_member_id == friend_member_id) & (models.GroupPayment.recipient_member_id == my_member_id),
            ),
        )
        .all()
    ):
        cash = Decimal(str(payment.cleared_amount if payment.cleared_amount is not None else payment.amount))
        key = (int(payment.payer_member_id), int(payment.recipient_member_id))
        period_cash_left[key] = period_cash_left.get(key, Decimal("0.00")) + cash

    results: list[dict] = []
    for leg, batch in rows:
        # Pair payments are allocated to a precise frozen leg.  This is vital:
        # recording a payment must reduce that leg, not make a new graph pass
        # reroute somebody else's debt.
        applied = Decimal("0.00")
        opposite_from_alloc = Decimal("0.00")
        for payment in confirmed_payments:
            for allocation in json.loads(payment.allocations_json or "[]"):
                if allocation.get("kind") != "GROUP":
                    continue
                batch_match = allocation.get("batch_id") == batch.id
                period_match = (
                    allocation.get("batch_id") is None
                    and allocation.get("period_id") == period.id
                )
                if not batch_match and not period_match:
                    continue
                amount = Decimal(str(allocation.get("amount") or 0))
                if (allocation.get("payer_member_id") == leg.payer_member_id
                        and allocation.get("recipient_member_id") == leg.recipient_member_id):
                    applied += amount
                elif (allocation.get("payer_member_id") == leg.recipient_member_id
                        and allocation.get("recipient_member_id") == leg.payer_member_id):
                    opposite_from_alloc += amount
        same_key = (int(leg.payer_member_id), int(leg.recipient_member_id))
        opp_key = (int(leg.recipient_member_id), int(leg.payer_member_id))
        # Spend period GroupPayment cash once across weeks (oldest first).
        if applied <= Decimal("0.005") and period_cash_left.get(same_key, Decimal("0.00")) > 0:
            take = min(Decimal(str(leg.amount)), period_cash_left[same_key])
            applied += take
            period_cash_left[same_key] -= take
        opposite_pool = period_cash_left.get(opp_key, Decimal("0.00"))
        remaining = max(Decimal("0.00"), Decimal(str(leg.amount)) - applied).quantize(Decimal("0.01"))
        if remaining <= 0:
            continue
        signed = remaining if leg.payer_member_id == friend_member_id else -remaining
        # Extra pay only for true reverse overpay: allocation on this batch the
        # other way, or unused opposite GroupPayment while live Who-pays-whom
        # for this pair is already clear (bill shrink after cash moved).
        force_extra = opposite_from_alloc > Decimal("0.005")
        if not force_extra and opposite_pool > Decimal("0.005"):
            live_edge = _pair_settlement_source_for_period(
                db, group, period, my_member_id, friend_member_id, currency, settlement_ready=True,
            )
            live_amt = Decimal(str(live_edge["amount"])) if live_edge else Decimal("0.00")
            force_extra = abs(live_amt) < Decimal("0.005")
        if force_extra:
            results.append({
                "kind": "CREDIT", "label": "Extra pay",
                "amount": float(signed), "currency": _normalize_currency(leg.currency, "USD"),
                "period_key": period.period_key, "group_id": None, "period_id": period.id,
                "batch_id": batch.id, "payer_member_id": leg.payer_member_id,
                "recipient_member_id": leg.recipient_member_id, "settlement_ready": True,
            })
            continue
        results.append({
            "kind": "GROUP",
            "label": (
                f"{group.name} · week ending "
                f"{batch.window_ends_at.astimezone(BUSINESS_TIMEZONE).date().isoformat()}"
            ),
            "amount": float(signed), "currency": _normalize_currency(leg.currency, "USD"),
            "period_key": period.period_key, "group_id": group.id, "period_id": period.id,
            "batch_id": batch.id, "payer_member_id": leg.payer_member_id,
            "recipient_member_id": leg.recipient_member_id, "settlement_ready": True,
        })
    return results


def _consolidated_friend_sources(db: Session, account_id: int, friend_id: int, currency: str) -> list[dict]:
    """Friend total sources: Who pays whom (per shared group) + direct private bills.

    Individual shared group expenses are NOT listed here — those are view-only on
    the Friends ledger via group-contributions and must not be double-counted.
    """
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
        direct_by_month[month_key] = direct_by_month.get(month_key, Decimal("0.00")) + _viewer_direct_effect(split, participant, account_id)

    payments = db.query(models.PairPayment).filter_by(
        account_one_id=one, account_two_id=two, status="CONFIRMED", currency=currency,
    ).all()
    residual_by_month: dict[str, Decimal] = {}
    for payment in payments:
        for allocation in json.loads(payment.allocations_json or "[]"):
            if allocation.get("kind") != "DIRECT":
                continue
            month_key = allocation.get("month_key")
            if not month_key:
                continue
            amount = Decimal(str(allocation.get("amount", 0)))
            signed = amount if payment.payer_account_id == account_id else -amount
            if month_key in direct_by_month:
                direct_by_month[month_key] += signed
            else:
                # Bills for this month were deleted or closed out after payment —
                # keep the cash as an open credit/debit so overpay is visible.
                residual_by_month[month_key] = residual_by_month.get(month_key, Decimal("0.00")) + signed

    for month_key, value in direct_by_month.items():
        if value:
            sources.append({
                "kind": "DIRECT", "label": f"Direct splits · {month_key}", "amount": float(value),
                "currency": currency, "period_key": month_key, "group_id": None, "settlement_ready": True,
            })
    for month_key, value in residual_by_month.items():
        if value:
            sources.append({
                "kind": "DIRECT", "label": f"Extra pay · {month_key}", "amount": float(value),
                "currency": currency, "period_key": month_key, "group_id": None, "settlement_ready": True,
            })

    # Intentional overpayments are durable pair-level credits. They are not
    # silently discarded, and they offset the next bill in this currency.
    credit_total = Decimal("0.00")
    for payment in db.query(models.PairPayment).filter_by(
        account_one_id=one, account_two_id=two, status="CONFIRMED", currency=currency,
    ).all():
        for allocation in json.loads(payment.allocations_json or "[]"):
            if allocation.get("kind") != "CREDIT":
                continue
            amount = Decimal(str(allocation.get("amount") or 0)).quantize(Decimal("0.01"))
            credit_total += amount if payment.payer_account_id == account_id else -amount
    if abs(credit_total) >= Decimal("0.005"):
        sources.append({
            "kind": "CREDIT", "label": "Extra pay", "amount": float(credit_total),
            "currency": currency, "period_key": None, "group_id": None, "settlement_ready": True,
        })

    # One Who-pays-whom edge per shared group period (same plan as the group Balances card).
    # Include groups whose base currency matches, OR periods settled/previewed into this currency.
    # Batch membership → groups → periods → expense presence to avoid N+1 fan-out.
    group_member_rows = db.query(models.GroupMember).filter(
        models.GroupMember.account_id.in_((account_id, friend_id)),
    ).all()
    members_by_group: dict[int, dict[int, models.GroupMember]] = {}
    for member in group_member_rows:
        members_by_group.setdefault(member.group_id, {})[member.account_id] = member
    shared_group_ids = [
        group_id for group_id, members in members_by_group.items()
        if account_id in members and friend_id in members
    ]
    if not shared_group_ids:
        return sources
    groups_by_id = {
        group.id: group
        for group in db.query(models.Group).filter(models.Group.id.in_(shared_group_ids)).all()
    }
    periods = (
        db.query(models.MonthlyPeriod)
        .filter(models.MonthlyPeriod.group_id.in_(shared_group_ids))
        .order_by(models.MonthlyPeriod.period_key.asc())
        .all()
    )
    periods_by_group: dict[int, list] = {}
    for period in periods:
        periods_by_group.setdefault(period.group_id, []).append(period)
    open_period_ids = [p.id for p in periods if p.status != "SETTLED"]
    expense_period_ids: set[int] = set()
    if open_period_ids:
        expense_period_ids = {
            row[0]
            for row in db.query(models.Expense.period_id)
            .filter(models.Expense.period_id.in_(open_period_ids))
            .distinct()
            .all()
        }
    for group_id in shared_group_ids:
        members = members_by_group[group_id]
        group = groups_by_id.get(group_id)
        if not group:
            continue
        base_currency = _normalize_currency(group.currency, "USD")
        mine_id, theirs_id = members[account_id].id, members[friend_id].id
        for period in periods_by_group.get(group_id, []):
            target_raw = (period.target_currency or "").strip().upper()
            target_currency = _normalize_currency(target_raw) if target_raw else ""
            if base_currency != currency and target_currency != currency:
                continue
            if not members[account_id].is_active or not members[friend_id].is_active:
                continue
            if period.id not in expense_period_ids and period.status != "SETTLED":
                continue
            # Friends payable group amounts must match the group Balances
            # Who-pays-whom card. Stacked weekly lock leftovers were inventing
            # Extra pay lines and flipping signs (e.g. Kuku / Bk). Weekly locks
            # remain for audit + payment allocation targeting only.
            live = _pair_settlement_source_for_period(
                db, group, period, mine_id, theirs_id, currency, settlement_ready=True,
            )
            if live:
                sources.append(live)
    return sources


def _payments_by_expense(db: Session, expenses: list[models.Expense]) -> dict[int, dict[int, Decimal]]:
    """Cash each member put toward a bill. Older rows credit the single payer for the full total."""
    result: dict[int, dict[int, Decimal]] = {}
    expense_ids = [expense.id for expense in expenses]
    if expense_ids:
        for row in db.query(models.ExpensePayer).filter(models.ExpensePayer.expense_id.in_(expense_ids)).all():
            result.setdefault(row.expense_id, {})[row.member_id] = Decimal(str(row.amount_paid))
    for expense in expenses:
        result.setdefault(expense.id, {expense.payer_member_id: Decimal(str(expense.total_amount))})
    return result


def _pair_expense_amount(my_id: int, their_id: int, shares: dict[int, Decimal], payments: dict[int, Decimal]) -> Optional[Decimal]:
    """How much the other person owes this account on one group bill.

    A third person's payment is not turned into a debt between this pair.
    One payer covering the other person's share keeps the previous result.
    """
    if payments.get(my_id, Decimal("0")) == 0 and payments.get(their_id, Decimal("0")) == 0:
        return None
    people = set(shares) | set(payments)
    overs: dict[int, Decimal] = {}
    unders: dict[int, Decimal] = {}
    for member_id in people:
        net = payments.get(member_id, Decimal("0")) - shares.get(member_id, Decimal("0"))
        if net > 0:
            overs[member_id] = net
        elif net < 0:
            unders[member_id] = -net
    total_over = sum(overs.values(), Decimal("0"))
    if total_over <= 0:
        return None
    owed_to_me = unders.get(their_id, Decimal("0")) * overs.get(my_id, Decimal("0")) / total_over
    owed_by_me = unders.get(my_id, Decimal("0")) * overs.get(their_id, Decimal("0")) / total_over
    amount = (owed_to_me - owed_by_me).quantize(Decimal("0.01"))
    return amount or None


def _consolidated_friend_balance(db: Session, account_id: int, friend_id: int, currency: str) -> tuple[Decimal, list[dict]]:
    """Return live balance and atomic (per-month / per-period) sources for payment allocation."""
    sources = _consolidated_friend_sources(db, account_id, friend_id, currency)
    balance = sum((Decimal(str(source["amount"])) for source in sources), Decimal("0.00"))
    return balance.quantize(Decimal("0.01")), sources


def _source_out_fields(source: dict) -> dict:
    return {
        key: value
        for key, value in source.items()
        if key in {"kind", "label", "amount", "currency", "period_key", "group_id", "settlement_ready"}
    }


def _aggregate_friend_display_sources(atomic_sources: list[dict]) -> list[dict]:
    """Fold N months/periods into one Direct line + one line per shared group.

    Payment allocation keeps using atomic sources; this list is display-only.
    """
    direct_total = Decimal("0.00")
    direct_currency = None
    direct_ready = True
    group_totals: dict[int, dict] = {}
    credits: list[dict] = []

    for source in atomic_sources:
        amount = Decimal(str(source.get("amount") or 0))
        if abs(amount) < Decimal("0.005"):
            continue
        currency = _normalize_currency(source.get("currency"), "USD")
        if source.get("kind") == "DIRECT":
            direct_total += amount
            direct_currency = direct_currency or currency
            direct_ready = direct_ready and bool(source.get("settlement_ready", True))
            continue
        if source.get("kind") == "CREDIT":
            credits.append({
                "kind": "CREDIT", "label": str(source.get("label") or "Extra pay"),
                "amount": amount, "currency": currency, "period_key": None, "group_id": None,
                "settlement_ready": True,
            })
            continue
        if source.get("kind") != "GROUP":
            continue
        group_id = source.get("group_id")
        if group_id is None:
            continue
        entry = group_totals.get(group_id)
        if not entry:
            label = str(source.get("label") or "Group").split(" · ")[0].strip() or "Group"
            entry = {
                "kind": "GROUP",
                "label": label,
                "amount": Decimal("0.00"),
                "currency": currency,
                "period_key": None,
                "group_id": group_id,
                "settlement_ready": True,
            }
            group_totals[group_id] = entry
        entry["amount"] += amount
        entry["settlement_ready"] = bool(entry["settlement_ready"] and source.get("settlement_ready", True))

    display: list[dict] = []
    if abs(direct_total) >= Decimal("0.005"):
        display.append({
            "kind": "DIRECT",
            "label": "Direct splits",
            "amount": float(direct_total.quantize(Decimal("0.01"))),
            "currency": direct_currency or "USD",
            "period_key": None,
            "group_id": None,
            "settlement_ready": direct_ready,
        })
    for group_id in sorted(group_totals.keys()):
        entry = group_totals[group_id]
        amount = entry["amount"].quantize(Decimal("0.01"))
        if abs(amount) < Decimal("0.005"):
            continue
        display.append({
            **entry,
            "amount": float(amount),
        })
    credit_total = sum((entry["amount"] for entry in credits), Decimal("0.00"))
    if abs(credit_total) >= Decimal("0.005"):
        credit_currency = credits[0]["currency"] if credits else "USD"
        display.append({
            "kind": "CREDIT",
            "label": "Extra pay",
            "amount": float(credit_total.quantize(Decimal("0.01"))),
            "currency": credit_currency,
            "period_key": None,
            "group_id": None,
            "settlement_ready": True,
        })
    return display


def _friend_balance_currencies(db: Session, account_id: int, friend_id: int) -> list[str]:
    """Return currencies that can appear on a friend's dashboard card.

    A period settled into a target currency is shown only in that target currency;
    showing its base currency too would display the same group debt twice.
    """
    one, two = _friend_pair(account_id, friend_id)
    currencies: set[str] = set()
    direct_rows = db.query(models.QuickSplit.currency).join(models.QuickSplitParticipant).filter(
        or_(
            (models.QuickSplit.payer_account_id == account_id) & (models.QuickSplitParticipant.account_id == friend_id),
            (models.QuickSplit.payer_account_id == friend_id) & (models.QuickSplitParticipant.account_id == account_id),
        ),
    ).distinct().all()
    currencies.update(_normalize_currency(row[0], "USD") for row in direct_rows)
    payment_rows = db.query(models.PairPayment.currency).filter_by(
        account_one_id=one, account_two_id=two,
    ).distinct().all()
    currencies.update(_normalize_currency(row[0], "USD") for row in payment_rows)

    memberships = db.query(models.GroupMember).filter(
        models.GroupMember.account_id.in_((account_id, friend_id)),
    ).all()
    members_by_group: dict[int, set[int]] = {}
    for membership in memberships:
        members_by_group.setdefault(membership.group_id, set()).add(membership.account_id)
    for group_id, member_account_ids in members_by_group.items():
        if account_id not in member_account_ids or friend_id not in member_account_ids:
            continue
        group = db.query(models.Group).get(group_id)
        if not group:
            continue
        base_currency = _normalize_currency(group.currency, "USD")
        periods = db.query(models.MonthlyPeriod).filter_by(group_id=group_id).all()
        for period in periods:
            target_currency = (period.target_currency or "").strip().upper()
            currencies.add(_normalize_currency(target_currency) if target_currency else base_currency)
    return sorted(currencies)


def _consolidated_balance_out(
    db: Session, account: models.Account, friend_id: int, currency: str,
) -> schemas.ConsolidatedFriendBalanceOut:
    """Build one currency-specific Friend balance response."""
    one, two = _friend_pair(account.id, friend_id)
    normalized_currency = _normalize_currency(currency, "USD")
    _reconcile_recipient_reported_pair_payments(db, one, two, normalized_currency)
    # Paid Friend cash must also mark the covered private bills so the ledger
    # can move them into Paid private bills instead of leaving them open.
    _repair_paid_direct_bills_for_pair(db, one, two, normalized_currency)
    balance, atomic_sources = _consolidated_friend_balance(db, account.id, friend_id, normalized_currency)
    display_sources = _aggregate_friend_display_sources(atomic_sources)
    # Friend payments are allowed for the live Who-pays-whom result even while
    # its group period is open.  Recording one also creates the matching group
    # payment, so the group and Friend views stay in sync.
    settleable_balance = balance
    payments = db.query(models.PairPayment).filter_by(
        account_one_id=one, account_two_id=two, currency=normalized_currency,
    ).order_by(models.PairPayment.created_at.desc()).all()
    payer_id = account.id if balance < 0 else friend_id if balance > 0 else None
    recipient_id = friend_id if balance < 0 else account.id if balance > 0 else None
    return schemas.ConsolidatedFriendBalanceOut(
        friend_id=friend_id, balance=float(balance), settleable_balance=float(settleable_balance), currency=normalized_currency,
        payer_account_id=payer_id, recipient_account_id=recipient_id,
        sources=[schemas.ConsolidatedSourceOut(**_source_out_fields(source)) for source in display_sources],
        atomic_sources=[schemas.ConsolidatedSourceOut(**_source_out_fields(source)) for source in atomic_sources],
        payments=[_pair_payment_out(payment) for payment in payments],
    )


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


def _list_friend_accounts(db: Session, account_id: int) -> list[models.Account]:
    _sync_group_friends(db, account_id)
    _sync_quick_split_friends(db, account_id)
    rows = db.query(models.FriendConnection).filter(
        or_(models.FriendConnection.account_one_id == account_id, models.FriendConnection.account_two_id == account_id),
        models.FriendConnection.status == "ACTIVE",
    ).all()
    friend_ids = [row.account_two_id if row.account_one_id == account_id else row.account_one_id for row in rows]
    people = db.query(models.Account).filter(models.Account.id.in_(friend_ids)).all() if friend_ids else []
    return sorted(people, key=lambda person: person.display_name.lower())


@app.get("/api/friends", response_model=list[schemas.FriendOut])
def list_friends(account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    storage = get_storage()
    people = _list_friend_accounts(db, account.id)
    return [schemas.FriendOut(
        id=person.id, display_name=person.display_name, email=person.email,
        has_avatar=bool(person.avatar_path and storage.exists(person.avatar_path)), source="friend",
    ) for person in people]


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
    if not existing:
        raise HTTPException(404, "No ShareSquad account uses that email yet. Ask them to create an account first, then add them directly.")
    _activate_friendship(db, account.id, existing.id)
    db.commit()
    return schemas.FriendInviteOut(status="ACTIVE", email=email, message=f"{existing.display_name} was added as a friend")


def _friend_settlement_out(row: models.FriendMonthlySettlement) -> schemas.FriendSettlementOut:
    return schemas.FriendSettlementOut(
        month_key=row.month_key, payer_account_id=row.payer_account_id, recipient_account_id=row.recipient_account_id,
        amount=float(row.amount), currency=row.currency, payer_confirmed=row.payer_confirmed,
        recipient_confirmed=row.recipient_confirmed, status=row.status, settled_at=row.settled_at,
    )


def _repair_friend_settlement_currency(
    db: Session, account_id: int, friend_id: int, settlement: models.FriendMonthlySettlement,
) -> bool:
    """Relabel a settlement when every bill that month shares one currency."""
    try:
        start, end = _month_date_bounds(settlement.month_key)
    except ValueError:
        return False
    currencies = {
        _normalize_currency(row[0], "USD")
        for row in db.query(models.QuickSplit.currency).join(models.QuickSplitParticipant).filter(
            models.QuickSplit.bill_date >= start,
            models.QuickSplit.bill_date < end,
            or_(
                (models.QuickSplit.payer_account_id == account_id) & (models.QuickSplitParticipant.account_id == friend_id),
                (models.QuickSplit.payer_account_id == friend_id) & (models.QuickSplitParticipant.account_id == account_id),
            ),
        ).distinct().all()
    }
    if len(currencies) != 1:
        return False
    bill_currency = next(iter(currencies))
    if _normalize_currency(settlement.currency, "USD") == bill_currency:
        return False
    settlement.currency = bill_currency
    return True


@app.get("/api/friends/{friend_id}/settlements", response_model=list[schemas.FriendSettlementOut])
def list_friend_settlements(friend_id: int, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    _reconcile_friend_settlements(db, account.id, friend_id)
    one, two = sorted((account.id, friend_id))
    rows = db.query(models.FriendMonthlySettlement).filter_by(account_one_id=one, account_two_id=two).order_by(models.FriendMonthlySettlement.month_key.desc()).all()
    changed = False
    for row in rows:
        if _repair_friend_settlement_currency(db, account.id, friend_id, row):
            changed = True
    if changed:
        db.commit()
    return [_friend_settlement_out(row) for row in rows]


def _pair_payment_out(payment: models.PairPayment) -> schemas.PairPaymentOut:
    allocations = json.loads(payment.allocations_json or "[]")
    payment_kind = str(allocations[0].get("payment_kind") or "RECORDED") if allocations else "RECORDED"
    return schemas.PairPaymentOut(
        id=payment.id, payer_account_id=payment.payer_account_id, recipient_account_id=payment.recipient_account_id,
        amount=float(payment.amount), currency=payment.currency, status=payment.status,
        payment_kind=payment_kind,
        allocations=allocations,
        reported_by_account_id=payment.reported_by_account_id, created_at=payment.created_at, confirmed_at=payment.confirmed_at,
    )


def _apply_confirmed_pair_payment(db: Session, payment: models.PairPayment) -> None:
    """Apply a recipient-confirmed pair payment exactly once to group sources."""
    if payment.status == "CONFIRMED":
        # Still reconcile private-bill paid markers for older already-confirmed rows.
        _sync_direct_bills_covered_by_pair_payment(db, payment)
        return
    for allocation in json.loads(payment.allocations_json or "[]"):
        if allocation.get("kind") != "GROUP":
            continue
        period = db.query(models.MonthlyPeriod).get(allocation.get("period_id"))
        if not period:
            raise HTTPException(409, "A group source is no longer available. Refresh and try the payment again.")
        cutoff_id = db.query(func.max(models.Expense.id)).filter_by(period_id=period.id).scalar()
        group_payment = models.GroupPayment(
            period_id=period.id, payer_member_id=allocation["payer_member_id"], recipient_member_id=allocation["recipient_member_id"],
            amount=Decimal(str(allocation["amount"])), currency=payment.currency, status="CONFIRMED",
            expense_cutoff_id=cutoff_id, marked_paid_at=datetime.now(timezone.utc), confirmed_at=datetime.now(timezone.utc),
        )
        db.add(group_payment)
        db.flush()
        group = db.query(models.Group).get(period.group_id)
        actor = db.query(models.Account).get(payment.reported_by_account_id) if payment.reported_by_account_id else None
        _record_expense_audit(
            db, group_id=period.group_id, expense_id=None, actor=actor, event_type="PAYMENT_CONFIRMED",
            before=None, after={
                "group_payment_id": group_payment.id, "batch_id": allocation.get("batch_id"),
                "payer_member_id": group_payment.payer_member_id, "recipient_member_id": group_payment.recipient_member_id,
                "amount": str(Decimal(str(group_payment.amount)).quantize(Decimal("0.01"))),
                "currency": group_payment.currency,
            },
        )
        _record_activity(
            db, period.group_id, actor, "weekly_settlement_payment_recorded",
            f"Recorded payment for a locked weekly settlement ({group.currency if group else group_payment.currency} {group_payment.amount}).",
            "group_payment", group_payment.id,
        )
        _repair_group_payment_snapshots(db, period)
    payment.status = "CONFIRMED"
    payment.confirmed_at = datetime.now(timezone.utc)
    _sync_direct_bills_covered_by_pair_payment(db, payment)


def _month_date_bounds(month_key: str) -> tuple[date, date]:
    start = date.fromisoformat(f"{month_key}-01")
    end = date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1)
    return start, end


def _private_participant_remaining(split: models.QuickSplit, participant: models.QuickSplitParticipant) -> Decimal:
    """Unpaid cash for this participant, including legacy fully-paid rows."""
    raw_due = (
        max(Decimal("0.00"), -_viewer_direct_effect(split, participant, participant.account_id))
        if participant.account_id else Decimal(str(participant.share_amount or 0))
    )
    paid = Decimal(str(participant.paid_amount or 0))
    if participant.payment_status == "CONFIRMED" and paid <= 0:
        # Old rows used only a status; preserve their historical meaning.
        return Decimal("0.00")
    return max(Decimal("0.00"), raw_due - paid).quantize(Decimal("0.01"))


def _apply_private_participant_payment(
    split: models.QuickSplit, participant: models.QuickSplitParticipant, amount: Decimal,
) -> Decimal:
    """Apply only the requested portion; strike through only when fully covered."""
    remaining = _private_participant_remaining(split, participant)
    applied = min(max(Decimal("0.00"), amount), remaining).quantize(Decimal("0.01"))
    if applied <= 0:
        return Decimal("0.00")
    participant.paid_amount = (Decimal(str(participant.paid_amount or 0)) + applied).quantize(Decimal("0.01"))
    if _private_participant_remaining(split, participant) <= Decimal("0.00"):
        participant.payment_status = "CONFIRMED"
        participant.payment_marked_at = participant.payment_marked_at or datetime.now(timezone.utc)
    else:
        participant.payment_status = "PARTIAL"
    if all(_private_participant_remaining(split, p) <= Decimal("0.00") for p in split.participants):
        split.status = "SETTLED"
    return applied


def _mark_quick_split_debtor_paid(split: models.QuickSplit, debtor_id: int, amount: Optional[Decimal] = None) -> Decimal:
    """Apply a full or partial Friend payment to this debtor's private bill."""
    applied = Decimal("0.00")
    for participant in split.participants:
        if participant.account_id == debtor_id:
            applied += _apply_private_participant_payment(split, participant, amount if amount is not None else _private_participant_remaining(split, participant))
    return applied


def _debtor_owed_on_private_bill(
    split: models.QuickSplit, participant: models.QuickSplitParticipant, debtor_id: int,
) -> Decimal:
    """Positive amount this debtor still owes on one private bill."""
    effect = _viewer_direct_effect(split, participant, debtor_id)
    owed = (-effect).quantize(Decimal("0.01"))
    if owed <= 0:
        return Decimal("0.00")
    paid = Decimal(str(participant.paid_amount or 0))
    if participant.payment_status == "CONFIRMED" and paid <= 0:
        return Decimal("0.00")
    return max(Decimal("0.00"), owed - paid).quantize(Decimal("0.01"))


def _sync_direct_bills_covered_by_pair_payment(db: Session, payment: models.PairPayment) -> bool:
    """Mark private bills covered by a Friend payment as paid, and backfill bill IDs.

    Consolidated payments historically allocated only by month. That zeroed the
    Friend total while leaving QuickSplitParticipant.payment_status PENDING, so
    the Friends ledger kept showing the bill as open. Persist the covered bills.
    """
    if payment.status != "CONFIRMED" or not payment.payer_account_id:
        return False
    allocations = json.loads(payment.allocations_json or "[]")
    if not allocations:
        return False
    currency = _normalize_currency(payment.currency, "USD")
    changed = False
    rewritten: list[dict] = []
    payment_time = payment.confirmed_at or payment.created_at

    def bill_rows_for_month(month_key: str):
        try:
            start, end = _month_date_bounds(month_key)
        except ValueError:
            return []
        return (
            db.query(models.QuickSplit, models.QuickSplitParticipant)
            .join(models.QuickSplitParticipant)
            .filter(
                models.QuickSplit.currency == currency,
                models.QuickSplit.bill_date >= start,
                models.QuickSplit.bill_date < end,
                or_(
                    (models.QuickSplit.payer_account_id == payment.payer_account_id)
                    & (models.QuickSplitParticipant.account_id == payment.recipient_account_id),
                    (models.QuickSplit.payer_account_id == payment.recipient_account_id)
                    & (models.QuickSplitParticipant.account_id == payment.payer_account_id),
                ),
            )
            .all()
        )

    for allocation in allocations:
        if allocation.get("kind") != "DIRECT":
            rewritten.append(allocation)
            continue
        amount = Decimal(str(allocation.get("amount", 0))).quantize(Decimal("0.01"))
        if amount <= 0:
            rewritten.append(allocation)
            continue
        quick_split_id = allocation.get("quick_split_id")
        if quick_split_id:
            split = db.query(models.QuickSplit).get(int(quick_split_id))
            if allocation.get("private_applied"):
                rewritten.append(allocation)
                continue
            if split:
                participant = next((p for p in split.participants if p.account_id == payment.payer_account_id), None)
                if participant and participant.payment_status == "CONFIRMED" and Decimal(str(participant.paid_amount or 0)) <= 0:
                    # Legacy full-payment markers have no numeric total; keep
                    # their recorded allocation intact while making replays safe.
                    allocation = {**allocation, "private_applied": True}
                else:
                    applied = _mark_quick_split_debtor_paid(split, payment.payer_account_id, amount)
                    allocation = {**allocation, "private_applied": True, "amount": float(applied)}
                changed = True
            rewritten.append(allocation)
            continue

        month_key = allocation.get("month_key") or allocation.get("period_key")
        if not month_key:
            rewritten.append(allocation)
            continue
        remaining = amount
        candidates = []
        for split, participant in bill_rows_for_month(str(month_key)):
            if participant.account_id != payment.payer_account_id:
                # Debtor of this transfer is the payment payer; only their unpaid share counts.
                continue
            if payment_time and split.created_at and _as_utc(split.created_at) > _as_utc(payment_time):
                continue
            owed = _debtor_owed_on_private_bill(split, participant, payment.payer_account_id)
            if owed <= 0:
                continue
            candidates.append((split, participant, owed))
        candidates.sort(
            key=lambda item: (
                item[0].bill_date.isoformat() if item[0].bill_date else "",
                item[0].created_at.isoformat() if item[0].created_at else "",
                item[0].id or 0,
            ),
            reverse=True,
        )
        exact = next((item for item in candidates if abs(item[2] - remaining) < Decimal("0.005")), None)
        applied_rows: list[tuple] = []
        if exact:
            applied_rows.append((exact[0], exact[1], remaining))
            remaining = Decimal("0.00")
        else:
            for split, participant, owed in candidates:
                if remaining <= 0:
                    break
                take = min(remaining, owed)
                applied_rows.append((split, participant, take))
                remaining -= take
        if not applied_rows:
            rewritten.append(allocation)
            continue
        payment_kind = allocation.get("payment_kind")
        for split, participant, take in applied_rows:
            applied = _apply_private_participant_payment(split, participant, take)
            if applied <= 0:
                continue
            entry = {
                "kind": "DIRECT",
                "month_key": str(month_key),
                "amount": float(applied),
                "quick_split_id": split.id,
                "participant_id": participant.id,
                "private_applied": True,
            }
            if payment_kind:
                entry["payment_kind"] = payment_kind
            rewritten.append(entry)
            changed = True
        if remaining > Decimal("0.005"):
            leftover = {
                "kind": "DIRECT",
                "month_key": str(month_key),
                "amount": float(remaining),
            }
            if payment_kind:
                leftover["payment_kind"] = payment_kind
            rewritten.append(leftover)

    if rewritten != allocations:
        payment.allocations_json = json.dumps(rewritten)
        changed = True
    return changed


def _repair_paid_direct_bills_for_pair(db: Session, account_one_id: int, account_two_id: int, currency: str) -> None:
    """Repair existing Friend payments that cleared money but left private bills open."""
    currency = _normalize_currency(currency, "USD")
    payments = (
        db.query(models.PairPayment)
        .filter_by(
            account_one_id=account_one_id,
            account_two_id=account_two_id,
            currency=currency,
            status="CONFIRMED",
        )
        .order_by(models.PairPayment.created_at.asc(), models.PairPayment.id.asc())
        .all()
    )
    changed = False
    for payment in payments:
        if _sync_direct_bills_covered_by_pair_payment(db, payment):
            changed = True
    if changed:
        db.commit()


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
    return _consolidated_balance_out(db, account, friend_id, currency)


@app.get("/api/friends/consolidated-balances-summary", response_model=schemas.FriendsConsolidatedSummaryOut)
def get_friends_consolidated_balances_summary(
    account: models.Account = Depends(get_current_account), db: Session = Depends(get_db),
):
    """One dashboard round-trip for every Friend's open balances (per currency)."""
    friends = _list_friend_accounts(db, account.id)
    token = _BALANCE_MEMO.set({})
    try:
        results = []
        for friend in friends:
            balances = [
                _consolidated_balance_out(db, account, friend.id, currency)
                for currency in _friend_balance_currencies(db, account.id, friend.id)
            ]
            results.append(schemas.ConsolidatedFriendBalancesOut(friend_id=friend.id, balances=balances))
    finally:
        _BALANCE_MEMO.reset(token)
    return schemas.FriendsConsolidatedSummaryOut(friends=results)


@app.get("/api/friends/{friend_id}/consolidated-balances", response_model=schemas.ConsolidatedFriendBalancesOut)
def get_consolidated_friend_balances(
    friend_id: int, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db),
):
    """All visible Friend balances, separated by currency for the dashboard."""
    if friend_id == account.id or not db.query(models.Account).get(friend_id):
        raise HTTPException(404, "Friend not found")
    one, two = _friend_pair(account.id, friend_id)
    connection = db.query(models.FriendConnection).filter_by(account_one_id=one, account_two_id=two, status="ACTIVE").first()
    if not connection:
        raise HTTPException(403, "Add this person as a friend before viewing consolidated balances")
    token = _BALANCE_MEMO.set({})
    try:
        balances = [
            _consolidated_balance_out(db, account, friend_id, currency)
            for currency in _friend_balance_currencies(db, account.id, friend_id)
        ]
    finally:
        _BALANCE_MEMO.reset(token)
    return schemas.ConsolidatedFriendBalancesOut(friend_id=friend_id, balances=balances)


def _friend_owed_totals(
    db: Session, account: models.Account, friend_id: int,
) -> list[schemas.PaymentReminderAmountOut]:
    """Amounts the friend currently owes the viewer (positive consolidated balances)."""
    currencies = _friend_balance_currencies(db, account.id, friend_id)
    owed: list[schemas.PaymentReminderAmountOut] = []
    for currency in currencies:
        balance, _atomic_sources = _consolidated_friend_balance(db, account.id, friend_id, currency)
        if balance <= Decimal("0.005"):
            continue
        owed.append(schemas.PaymentReminderAmountOut(currency=currency, amount=float(balance)))
    return owed


@app.post("/api/friends/{friend_id}/payment-reminder", response_model=schemas.PaymentReminderOut)
def send_friend_payment_reminder(
    friend_id: int,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    """Manually email this friend only — polite reminder with totals they still owe you.

    Allowed whenever they owe you. Never emails anyone else; never runs automatically.
    """
    friend = db.query(models.Account).get(friend_id)
    if friend_id == account.id or not friend:
        raise HTTPException(404, "Friend not found")
    one, two = _friend_pair(account.id, friend_id)
    connection = db.query(models.FriendConnection).filter_by(
        account_one_id=one, account_two_id=two, status="ACTIVE",
    ).first()
    if not connection:
        raise HTTPException(403, "Add this person as a friend before sending a reminder")
    if not is_email_configured():
        raise HTTPException(503, "Email is not configured on this server yet.")

    token = _BALANCE_MEMO.set({})
    try:
        owed = _friend_owed_totals(db, account, friend_id)
    finally:
        _BALANCE_MEMO.reset(token)
    if not owed:
        raise HTTPException(400, "This friend does not currently owe you any money.")

    amount_lines = [f"{row.currency} {row.amount:,.2f}" for row in owed]
    try:
        send_payment_reminder_email(
            to_email=friend.email,
            friend_name=friend.display_name or "there",
            sender_name=account.display_name or "a friend",
            amount_lines=amount_lines,
        )
    except Exception:
        logger.exception("Payment reminder failed for friend_id=%s", friend_id)
        raise HTTPException(502, "Could not send the reminder email. Try again in a moment.")

    totals = ", ".join(amount_lines)
    return schemas.PaymentReminderOut(
        email_sent=True,
        message=f"Reminder sent to {friend.display_name} ({totals}).",
        amounts=owed,
    )


@app.get("/api/friends/{friend_id}/group-contributions", response_model=list[schemas.FriendGroupContributionOut])
def get_friend_group_contributions(
    friend_id: int, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db),
):
    """Read-only shared group bills for the Friends ledger (do not affect Friend balance).

    Lists expenses where both people participated. pair_amount is contextual only
    when one of them covered the other's share; it is never settled from Friends.
    """
    if friend_id == account.id or not db.query(models.Account).get(friend_id):
        raise HTTPException(404, "Friend not found")
    one, two = _friend_pair(account.id, friend_id)
    connection = db.query(models.FriendConnection).filter_by(account_one_id=one, account_two_id=two, status="ACTIVE").first()
    if not connection:
        raise HTTPException(403, "Add this person as a friend before viewing group contributions")

    mine = {member.group_id: member for member in db.query(models.GroupMember).filter_by(account_id=account.id, is_active=True).all()}
    theirs = {member.group_id: member for member in db.query(models.GroupMember).filter_by(account_id=friend_id, is_active=True).all()}
    shared_group_ids = sorted(set(mine).intersection(theirs))
    if not shared_group_ids:
        return []

    rows = db.query(models.Expense, models.MonthlyPeriod, models.Group).join(
        models.MonthlyPeriod, models.Expense.period_id == models.MonthlyPeriod.id
    ).join(models.Group, models.MonthlyPeriod.group_id == models.Group.id).filter(
        models.MonthlyPeriod.group_id.in_(shared_group_ids)
    ).order_by(models.Expense.bill_date.desc(), models.Expense.id.desc()).all()
    expense_ids = [expense.id for expense, _period, _group in rows]
    split_map: dict[int, dict[int, Decimal]] = {}
    if expense_ids:
        for split in db.query(models.ExpenseSplit).filter(models.ExpenseSplit.expense_id.in_(expense_ids)).all():
            split_map.setdefault(split.expense_id, {})[split.member_id] = Decimal(str(split.share_amount))

    payments = _payments_by_expense(db, [expense for expense, _period, _group in rows])
    result = []
    for expense, period, group in rows:
        my_member = mine[group.id]
        friend_member = theirs[group.id]
        shares = split_map.get(expense.id, {})
        expense_payments = payments.get(expense.id, {})
        # A payer is involved even when their own share is zero.  For example,
        # KP can pay a bill that is split only to Tanish; that bill belongs in
        # their shared Friends history just as much as a two-way split does.
        my_involved = my_member.id in shares or expense_payments.get(my_member.id, Decimal("0")) > 0
        friend_involved = friend_member.id in shares or expense_payments.get(friend_member.id, Decimal("0")) > 0
        if not my_involved or not friend_involved:
            continue
        amount = _pair_expense_amount(my_member.id, friend_member.id, shares, expense_payments)
        result.append(schemas.FriendGroupContributionOut(
            group_id=group.id, group_name=group.name, expense_id=expense.id,
            title=expense.store_name, bill_date=expense.bill_date, period_key=period.period_key,
            currency=expense.currency or group.currency or "USD",
            pair_amount=float(amount) if amount is not None else 0.0,
        ))
    return result


@app.post("/api/friends/{friend_id}/consolidated-payments", response_model=schemas.PairPaymentOut)
def record_consolidated_friend_payment(
    friend_id: int, payload: schemas.PairPaymentCreate, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db),
):
    normalized_currency = _normalize_currency(payload.currency, "USD")
    _live_balance, sources = _consolidated_friend_balance(db, account.id, friend_id, normalized_currency)
    # A Friend payment applies to the current pair balance, including an open
    # group period.  The paired GroupPayment created below keeps both views
    # consistent and retains the audit record.
    settleable_balance = _live_balance
    if not settleable_balance:
        raise HTTPException(400, "There is no balance to settle in this currency.")
    payer_id = account.id if settleable_balance < 0 else friend_id
    recipient_id = friend_id if settleable_balance < 0 else account.id
    amount = Decimal(str(payload.amount)).quantize(Decimal("0.01"))
    if account.id not in (payer_id, recipient_id):
        raise HTTPException(403, "Only the two people in this balance can record payment")
    payment_kind = "OVERPAYMENT" if amount > abs(settleable_balance) else "FULL" if amount == abs(settleable_balance) else "PARTIAL"

    remaining = amount
    allocations = []
    # Oldest first makes partial payments predictable and gives every source
    # a permanent, inspectable allocation.
    for source in sorted(sources, key=lambda item: (item["period_key"] or "", item["kind"])):
        if remaining <= 0 or Decimal(str(source["amount"])) * settleable_balance <= 0:
            continue
        applied = min(remaining, abs(Decimal(str(source["amount"]))))
        allocation = {"kind": source["kind"], "amount": float(applied), "month_key": source.get("period_key"), "payment_kind": payment_kind}
        if source["kind"] == "GROUP":
            allocation.update({
                "period_id": source["period_id"], "payer_member_id": source["payer_member_id"],
                "recipient_member_id": source["recipient_member_id"], "group_id": source["group_id"],
                "batch_id": source.get("batch_id"),
            })
        allocations.append(allocation)
        remaining -= applied
    if remaining > Decimal("0.001"):
        allocations.append({
            "kind": "CREDIT", "amount": float(remaining), "month_key": None,
            "payment_kind": "OVERPAYMENT",
        })

    one, two = _friend_pair(account.id, friend_id)
    payment = models.PairPayment(
        account_one_id=one, account_two_id=two, payer_account_id=payer_id, recipient_account_id=recipient_id,
        amount=amount, currency=normalized_currency, status="PENDING", reported_by_account_id=account.id,
        allocations_json=json.dumps(allocations),
    )
    db.add(payment)
    # Lender (recipient / person owed) recording received → auto-confirm.
    # Owing person marking payment → stays PENDING until the lender confirms.
    if account.id == recipient_id:
        _apply_confirmed_pair_payment(db, payment)
        for allocation in allocations:
            if allocation.get("kind") == "GROUP" and allocation.get("group_id"):
                _bump_group_revision(db, int(allocation["group_id"]))
    else:
        db.add(models.Notification(
            account_id=recipient_id,
            group_id=None,
            message=f"{account.display_name} marked a {normalized_currency} {float(amount):.2f} payment. Confirm to apply it.",
            link="/friends.html",
        ))
    db.commit(); db.refresh(payment)
    if payment.status == "CONFIRMED":
        for allocation in allocations:
            if allocation.get("kind") == "GROUP" and allocation.get("group_id"):
                notify_group_change(int(allocation["group_id"]), "payment-confirmed")
    return _pair_payment_out(payment)


@app.post("/api/friends/{friend_id}/consolidated-payments/{payment_id}/confirm", response_model=schemas.PairPaymentOut)
def confirm_consolidated_friend_payment(
    friend_id: int, payment_id: int, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db),
):
    """Only the lender (recipient) may confirm a debtor-reported payment."""
    one, two = _friend_pair(account.id, friend_id)
    payment = db.query(models.PairPayment).filter_by(id=payment_id, account_one_id=one, account_two_id=two).first()
    if not payment:
        raise HTTPException(404, "Payment not found")
    if payment.status == "CONFIRMED":
        return _pair_payment_out(payment)
    if payment.status != "PENDING":
        raise HTTPException(400, "This payment has already been handled")
    if account.id != payment.recipient_account_id:
        raise HTTPException(403, "Only the person who is owed can confirm this payment")
    _apply_confirmed_pair_payment(db, payment)
    allocations = json.loads(payment.allocations_json or "[]")
    for allocation in allocations:
        if allocation.get("kind") == "GROUP" and allocation.get("group_id"):
            _bump_group_revision(db, int(allocation["group_id"]))
    db.commit(); db.refresh(payment)
    for allocation in allocations:
        if allocation.get("kind") == "GROUP" and allocation.get("group_id"):
            notify_group_change(int(allocation["group_id"]), "payment-confirmed")
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
    else:
        # Always refresh from live bill currency/amount so a stale PENDING row
        # (e.g. wrong preferred-currency label) cannot save CAD as GBP.
        settlement.payer_account_id = payer_id
        settlement.recipient_account_id = recipient_id
        settlement.amount = abs(balance)
        settlement.currency = currency
    if payload.action == "paid":
        if account.id != settlement.payer_account_id:
            raise HTTPException(403, "Only the person who owes can record payment")
        settlement.payer_confirmed = True
    else:
        if account.id != settlement.recipient_account_id:
            raise HTTPException(403, "Only the person receiving payment can record receipt")
        settlement.recipient_confirmed = True

    # Lender (recipient) recording receipt settles immediately.
    # Debtor marking paid stays PENDING until the lender confirms.
    lender_settled = account.id == settlement.recipient_account_id
    if lender_settled:
        settlement.payer_confirmed = True
        settlement.recipient_confirmed = True
    if settlement.payer_confirmed and settlement.recipient_confirmed:
        settlement.status = "SETTLED"
        settlement.settled_at = datetime.now(timezone.utc)
        try:
            start, end = _month_date_bounds(payload.month_key)
        except ValueError:
            start = end = None
        if start and end:
            rows = (
                db.query(models.QuickSplit, models.QuickSplitParticipant)
                .join(models.QuickSplitParticipant)
                .filter(
                    models.QuickSplit.bill_date >= start,
                    models.QuickSplit.bill_date < end,
                    or_(
                        (models.QuickSplit.payer_account_id == account.id) & (models.QuickSplitParticipant.account_id == friend_id),
                        (models.QuickSplit.payer_account_id == friend_id) & (models.QuickSplitParticipant.account_id == account.id),
                    ),
                )
                .all()
            )
            for split, _participant in rows:
                _mark_quick_split_debtor_paid(split, settlement.payer_account_id)
    else:
        settlement.status = "PENDING"
        settlement.settled_at = None
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


def _finalize_quick_split_participant_payment(
    db: Session, split: models.QuickSplit, participant: models.QuickSplitParticipant,
    reported_by_account_id: Optional[int] = None,
) -> None:
    """Apply cash for a private-bill participant (caller enforces lender/debtor rules)."""
    remaining = _private_participant_remaining(split, participant)
    if remaining <= 0:
        return
    applied = _apply_private_participant_payment(split, participant, remaining)
    if participant.account_id and split.payer_account_id:
        _record_direct_pair_payment(
            db,
            participant.account_id,
            split.payer_account_id,
            applied,
            split.currency or "USD",
            split.bill_date.strftime("%Y-%m"),
            reported_by_account_id or split.payer_account_id,
            quick_split_id=split.id,
        )


@app.post("/api/quick-splits/{split_id}/participants/{participant_id}/mark-paid", response_model=schemas.QuickSplitOut)
def mark_quick_split_paid(split_id: int, participant_id: int, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    split = _require_quick_split_access(split_id, account, db)
    participant = db.query(models.QuickSplitParticipant).filter_by(id=participant_id, quick_split_id=split.id).first()
    if not participant:
        raise HTTPException(404, "Participant not found")
    is_debtor = (
        (participant.account_id and participant.account_id == account.id)
        or (participant.email and participant.email.lower() == account.email.lower())
    )
    is_payer = split.payer_account_id == account.id
    if not is_debtor and not is_payer:
        raise HTTPException(403, "Only the people on this bill can record payment")
    if is_debtor:
        participant.account_id = account.id
        participant.display_name = account.display_name
    # Bill payer (lender) recording received → apply now.
    # Debtor marking paid → MARKED until the lender confirms.
    if is_payer:
        _finalize_quick_split_participant_payment(db, split, participant, reported_by_account_id=account.id)
    else:
        if _private_participant_remaining(split, participant) <= 0:
            return _quick_split_out(split, account)
        participant.payment_status = "MARKED"
        participant.payment_marked_at = datetime.now(timezone.utc)
        if split.payer_account_id:
            db.add(models.Notification(
                account_id=split.payer_account_id,
                group_id=None,
                message=f"{account.display_name} marked their share of “{split.title}” as paid. Confirm to apply it.",
                link=f"/quick-split.html?id={split.id}",
            ))
    db.commit()
    db.refresh(split)
    return _quick_split_out(split, account)


@app.post("/api/quick-splits/{split_id}/participants/{participant_id}/confirm", response_model=schemas.QuickSplitOut)
def confirm_quick_split_payment(split_id: int, participant_id: int, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    """Only the bill payer (lender) may confirm a debtor-marked payment."""
    split = _require_quick_split_access(split_id, account, db)
    participant = db.query(models.QuickSplitParticipant).filter_by(id=participant_id, quick_split_id=split.id).first()
    if not participant:
        raise HTTPException(404, "Participant not found")
    if participant.payment_status == "CONFIRMED":
        return _quick_split_out(split, account)
    if split.payer_account_id != account.id:
        raise HTTPException(403, "Only the person who is owed can confirm this payment")
    _finalize_quick_split_participant_payment(db, split, participant, reported_by_account_id=account.id)
    db.commit()
    db.refresh(split)
    return _quick_split_out(split, account)


# ---------------------------------------------------------------------------
# Groups Endpoints
# ---------------------------------------------------------------------------
@app.get("/api/groups", response_model=list[schemas.GroupListOut])
def list_groups(account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    memberships = (
        db.query(models.GroupMember)
        .options(selectinload(models.GroupMember.group))
        .filter_by(account_id=account.id)
        .all()
    )
    active = [m for m in memberships if _member_is_active(m)]
    group_ids = [m.group_id for m in active]
    counts: Dict[int, int] = {}
    if group_ids:
        rows = (
            db.query(models.GroupMember.group_id, func.count(models.GroupMember.id))
            .filter(
                models.GroupMember.group_id.in_(group_ids),
                or_(models.GroupMember.is_active.is_(True), models.GroupMember.is_active.is_(None)),
            )
            .group_by(models.GroupMember.group_id)
            .all()
        )
        counts = {int(group_id): int(count) for group_id, count in rows}
    storage = get_storage()
    results = []
    for m in active:
        g = m.group
        cover = getattr(g, "cover_image_path", None)
        results.append(
            schemas.GroupListOut(
                id=g.id,
                name=g.name,
                currency=g.currency or "USD",
                invite_code=g.invite_code,
                created_at=g.created_at,
                has_cover=bool(cover and storage.exists(cover)),
                role=m.role,
                member_count=counts.get(g.id, 0),
            )
        )
    return results


@app.post("/api/groups", response_model=schemas.GroupOut)
def create_group(
    payload: schemas.GroupCreate,
    request: Request,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    curr = _normalize_currency(payload.currency, "USD")
    friend_ids = list(dict.fromkeys(member_id for member_id in payload.friend_ids if member_id != account.id))
    invite_emails = list(dict.fromkeys(str(email).strip().lower() for email in payload.invite_emails if str(email).strip()))
    if len(friend_ids) + len(invite_emails) > 50:
        raise HTTPException(400, "You can add up to 50 people when creating a group")
    for email in invite_emails:
        _validate_invite_email(email, account, -1, db)

    valid_friend_ids = set()
    for friend_id in friend_ids:
        pair = _friend_pair(account.id, friend_id)
        if db.query(models.FriendConnection).filter_by(account_one_id=pair[0], account_two_id=pair[1], status="ACTIVE").first():
            valid_friend_ids.add(friend_id)
    invalid_friend_ids = set(friend_ids) - valid_friend_ids
    if invalid_friend_ids:
        raise HTTPException(400, "Choose people from your current Friends list")

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
            db.flush()

            added_accounts: dict[int, models.Account] = {}
            for friend_id in valid_friend_ids:
                friend = db.query(models.Account).get(friend_id)
                if friend:
                    db.add(models.GroupMember(group_id=group.id, account_id=friend.id, display_name=friend.display_name, role="member"))
                    added_accounts[friend.id] = friend

            for email in invite_emails:
                invited_account = db.query(models.Account).filter_by(email=email).first()
                if invited_account:
                    if invited_account.id != account.id and invited_account.id not in added_accounts:
                        db.add(models.GroupMember(group_id=group.id, account_id=invited_account.id, display_name=invited_account.display_name, role="member"))
                        added_accounts[invited_account.id] = invited_account
                else:
                    db.add(models.GroupInvite(group_id=group.id, email=email, status="pending"))

            for invited_account in added_accounts.values():
                _record_activity(db, group.id, account, "member_added", f"{account.display_name} added {invited_account.display_name} to the group", "member", None)
                _notify_group_members(db, group.id, f"{account.display_name} added {invited_account.display_name} to the group.", _group_focus_link(group.id, "members", "groupMembers"), exclude_account_id=account.id)
            db.commit()
            invite_url = _invite_url(request, group.invite_code)
            for email in invite_emails:
                if is_email_configured():
                    try:
                        send_invite_email(email, group.name, invite_url, account.display_name)
                    except Exception:
                        logger.exception("Failed to send initial group invite email to %s", email)
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
    group = db.query(models.Group).get(group_id)
    revision = int(getattr(group, "revision", 1) or 1) if group else 1
    # Durable monotonic revision is the source of truth; include a content hash
    # suffix only for older clients that still compare opaque version strings.
    exp = (
        db.query(
            func.coalesce(func.max(models.Expense.id), 0),
            func.count(models.Expense.id),
            func.coalesce(func.sum(models.Expense.total_amount), 0),
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
            func.coalesce(
                func.sum(case((or_(models.GroupMember.is_active.is_(True), models.GroupMember.is_active.is_(None)), 1), else_=0)),
                0,
            ),
        )
        .filter(models.GroupMember.group_id == group_id)
        .first()
    )
    pay = (
        db.query(
            func.coalesce(func.max(models.GroupPayment.id), 0),
            func.count(models.GroupPayment.id),
        )
        .select_from(models.GroupPayment)
        .join(models.MonthlyPeriod, models.GroupPayment.period_id == models.MonthlyPeriod.id)
        .filter(models.MonthlyPeriod.group_id == group_id)
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
    raw = f"{revision}:{exp[0]}:{exp[1]}:{exp[2]}:{mem[0]}:{mem[1]}:{mem[2]}:{pay[0]}:{pay[1]}:{period_sig}"
    version = f"r{revision}-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"
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
    member.left_at = datetime.now(timezone.utc)
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
    _notify_group_members(db, group_id, f"{name} was removed from active group activity. Settled history is preserved.", _group_focus_link(group_id, "members", "groupMembers"), exclude_account_id=account.id)
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
    email = str(payload.email).strip().lower()
    require_group_member(group_id, account, db)
    group = db.query(models.Group).get(group_id)
    if not group:
        raise HTTPException(404, "Group not found")
    _validate_invite_email(email, account, group_id, db)
    invite_url = _invite_url(request, group.invite_code)
    existing_account = db.query(models.Account).filter_by(email=email).first()
    if existing_account:
        result = _add_existing_group_member(group_id, existing_account.id, account, db)
        pending = db.query(models.GroupInvite).filter_by(group_id=group_id, email=email).first()
        if pending:
            pending.status = "accepted"
            db.commit()
    else:
        pending = db.query(models.GroupInvite).filter_by(group_id=group_id, email=email).first()
        if pending:
            pending.status = "pending"
        else:
            db.add(models.GroupInvite(group_id=group_id, email=email, status="pending"))
        db.commit()
        result = schemas.InviteCreateOut(
            email=email,
            email_sent=False,
            already_member=False,
            invite_url=invite_url,
            message="",
        )

    email_sent = False
    if is_email_configured():
        try:
            email_sent = bool(send_invite_email(email, group.name, invite_url, account.display_name))
        except Exception:
            logger.exception("Failed to send group invite email to %s", email)

    if existing_account:
        message = (
            f"{existing_account.display_name} is already in this group."
            if result.already_member
            else f"{existing_account.display_name} was added to the group."
        )
        if email_sent:
            message += " A group link was emailed."
    elif email_sent:
        message = "Invite email sent. They will be added automatically when they sign in with this email."
    else:
        message = "Invite saved. Share the group link; signing in with this email will add them automatically."

    return schemas.InviteCreateOut(
        email=email,
        email_sent=email_sent,
        already_member=result.already_member,
        invite_url=invite_url,
        message=message,
    )


def _add_existing_group_member(group_id: int, member_account_id: int, actor: models.Account, db: Session) -> schemas.InviteCreateOut:
    require_group_member(group_id, actor, db)
    group = db.query(models.Group).get(group_id)
    target = db.query(models.Account).get(member_account_id)
    if not group or not target:
        raise HTTPException(404, "Group member not found")
    if target.id == actor.id:
        raise HTTPException(400, "You are already in this group")
    membership = db.query(models.GroupMember).filter_by(group_id=group.id, account_id=target.id).first()
    already_member = bool(membership and membership.is_active)
    if membership:
        membership.is_active = True
        membership.display_name = target.display_name
        if not already_member:
            membership.joined_at = datetime.now(timezone.utc)
            membership.left_at = None
    else:
        db.add(models.GroupMember(group_id=group.id, account_id=target.id, display_name=target.display_name, role="member"))
    if not already_member:
        _record_activity(db, group_id, actor, "member_added", f"{actor.display_name} added {target.display_name} to the group", "member", None)
        _notify_group_members(db, group_id, f"{actor.display_name} added {target.display_name} to the group.", _group_focus_link(group_id, "members", "groupMembers"), exclude_account_id=actor.id)
    db.commit()
    if not already_member:
        notify_group_change(group_id, "member-added")
    return schemas.InviteCreateOut(email=target.email, email_sent=False, already_member=already_member, invite_url="", message=f"{target.display_name} was added to the group.")


@app.post("/api/groups/{group_id}/members", response_model=schemas.InviteCreateOut)
def add_existing_group_member(group_id: int, payload: schemas.GroupMemberAddRequest, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    return _add_existing_group_member(group_id, payload.account_id, account, db)


@app.post("/api/groups/join", response_model=schemas.GroupOut)
def join_group(
    payload: schemas.JoinGroupRequest,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    group = db.query(models.Group).filter_by(invite_code=payload.invite_code.strip()).first()
    if not group:
        raise HTTPException(404, "Invalid invite code")

    membership = db.query(models.GroupMember).filter_by(group_id=group.id, account_id=account.id).first()
    newly_joined = not membership or not membership.is_active
    if membership:
        membership.is_active = True
        membership.display_name = account.display_name
        if newly_joined:
            membership.joined_at = datetime.now(timezone.utc)
            membership.left_at = None
    else:
        db.add(models.GroupMember(group_id=group.id, account_id=account.id, display_name=account.display_name, role="member"))
    if newly_joined:
        _record_activity(db, group.id, account, "member_joined", f"{account.display_name} joined the group", "member", None)
        _notify_group_members(db, group.id, f"{account.display_name} joined the group.", _group_focus_link(group.id, "members", "groupMembers"), exclude_account_id=account.id)
        db.commit()
        notify_group_change(group.id, "member-joined")
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

    cat = _normalize_category(category)
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
            row.category = cat
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
        category=_normalize_category(category),
    )
    for sid in store_ids[1:]:
        _upsert_account_vendor(db, account_id, chosen, store_id=sid, category=_normalize_category(category))


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
def _scan_job_out(job: models.ScanJob) -> schemas.ScanJobOut:
    extraction = None
    if job.result_json:
        try:
            extraction = schemas.ReceiptExtraction(**json.loads(job.result_json))
        except Exception:
            extraction = None
    return schemas.ScanJobOut(
        job_id=job.id,
        upload_id=job.upload_id,
        image_url=job.image_url,
        status=job.status,
        extraction=extraction,
        source=job.source,
        message=_public_scan_message(job.message) if job.message else job.message,
        error=job.error,
    )


def _queue_receipt_scan(
    *,
    db: Session,
    account: models.Account,
    group_id: Optional[int],
    contents: bytes,
    mime: str,
    image_url: str,
    tmp_filename: str,
    upload_id: str,
) -> schemas.ScanJobOut | schemas.ExtractionResponse:
    storage = get_storage()
    key = f"uploads/tmp/{tmp_filename}"
    tmp_path = storage.save_bytes(key, contents)

    pending = models.PendingUpload(id=upload_id, group_id=group_id, account_id=account.id, file_path=tmp_path)
    db.add(pending)
    db.commit()

    job = enqueue_scan_job(
        job_id=uuid.uuid4().hex,
        account_id=account.id,
        group_id=group_id,
        upload_id=upload_id,
        file_path=tmp_path,
        mime_type=mime,
        image_url=image_url,
        fingerprint=content_fingerprint(contents),
    )
    vlm_config = resolve_vlm_config(db, account.id, group_id)
    if ASYNC_OCR:
        kick_scan_worker(job.id, vlm_config)
        fresh = db.query(models.ScanJob).filter_by(id=job.id).first() or job
        return _scan_job_out(fresh)

    process_scan_job(job.id, vlm_config)
    db.expire_all()
    fresh = db.query(models.ScanJob).filter_by(id=job.id).first()
    if not fresh or fresh.status != "succeeded" or not fresh.result_json:
        raise HTTPException(500, fresh.error if fresh and fresh.error else "Receipt scan failed")
    extraction = schemas.ReceiptExtraction(**json.loads(fresh.result_json))
    return schemas.ExtractionResponse(
        upload_id=upload_id,
        image_url=image_url,
        extraction=extraction,
        source=fresh.source or "ocr",
        message=_public_scan_message(fresh.message),
    )


@app.get("/api/scans/{job_id}", response_model=schemas.ScanJobOut)
def get_scan_job(job_id: str, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    job = db.query(models.ScanJob).filter_by(id=job_id, account_id=account.id).first()
    if not job:
        raise HTTPException(404, "Scan job not found")
    return _scan_job_out(job)


@app.post("/api/groups/{group_id}/receipts/upload")
async def upload_receipt(
    group_id: int,
    request: Request,
    file: UploadFile = File(...),
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    _require_shareable_group(db, group_id)
    check_rate_limit(request, limit=20, window_seconds=60)

    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in [".jpg", ".jpeg", ".png", ".webp", ".heic"]:
        raise HTTPException(400, "Unsupported image format (JPEG, PNG, WEBP allowed)")

    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(400, f"File exceeds maximum size of {MAX_FILE_SIZE // (1024*1024)}MB")

    upload_id = uuid.uuid4().hex
    tmp_filename = f"{group_id}_{upload_id}{ext}"
    image_url = f"/api/groups/{group_id}/receipts/tmp/{tmp_filename}"
    return _queue_receipt_scan(
        db=db,
        account=account,
        group_id=group_id,
        contents=contents,
        mime=file.content_type or "image/jpeg",
        image_url=image_url,
        tmp_filename=tmp_filename,
        upload_id=upload_id,
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

    storage = get_storage()
    path = storage.open_path(f"uploads/tmp/{filename}") or storage.open_path(os.path.join(TMP_UPLOADS_DIR, filename))
    if not path:
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


def _apply_expense_payers(db: Session, expense: models.Expense, total_amount: float, payer_member_id: int, payers: Optional[list]) -> int:
    """Store extra payers only when two or more people paid. Returns the primary payer."""
    for row in list(expense.payers):
        db.delete(row)
    db.flush()
    if not payers or len(payers) < 2:
        return payer_member_id
    total = Decimal(str(total_amount)).quantize(Decimal("0.01"))
    seen = set()
    paid = Decimal("0.00")
    normalized = []
    for item in payers:
        if item.member_id in seen:
            raise HTTPException(400, "Each person can be listed once as a payer")
        seen.add(item.member_id)
        amount = Decimal(str(item.amount)).quantize(Decimal("0.01"))
        if amount <= 0:
            raise HTTPException(400, "Each payer amount must be greater than zero")
        paid += amount
        normalized.append((item.member_id, amount))
    if paid != total:
        raise HTTPException(400, "Payments by several people must add up to the bill total")
    for member_id, amount in normalized:
        db.add(models.ExpensePayer(expense_id=expense.id, member_id=member_id, amount_paid=amount))
    return max(normalized, key=lambda item: item[1])[0]


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
        detail_ids = [int(item.member_id) for item in split_details]
        if len(detail_ids) != len(set(detail_ids)):
            raise HTTPException(400, "Duplicate split participants are not allowed")
        inputs = {item.member_id: (item.value if item.value is not None else 1.0) for item in split_details}
    elif split_with and len(split_with) > 0:
        if len(split_with) != len(set(int(mid) for mid in split_with)):
            raise HTTPException(400, "Duplicate split participants are not allowed")
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
        message = str(e) if "Duplicate" in str(e) else "Invalid split values — check the amounts entered"
        raise HTTPException(400, message) from e

    total_dec = Decimal(str(total_amount)).quantize(Decimal("0.01"))
    share_sum = sum((Decimal(str(amount)).quantize(Decimal("0.01")) for amount in shares.values()), Decimal("0.00"))
    if abs(share_sum - total_dec) > Decimal("0.01"):
        raise HTTPException(400, "Split shares must add up to the bill total")

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


def _next_monthly_run_date(after: date, day_of_month: int) -> date:
    """Return the next month's occurrence of day_of_month after `after` (clamped)."""
    year = after.year + (1 if after.month == 12 else 0)
    month = 1 if after.month == 12 else after.month + 1
    day = max(1, min(int(day_of_month or 1), monthrange(year, month)[1]))
    return date(year, month, day)


def _serialize_expense_split_snapshot(payload) -> str:
    split_with = list(payload.split_with or []) if getattr(payload, "split_with", None) else []
    split_details = []
    if getattr(payload, "split_details", None):
        split_details = [
            {"member_id": int(d.member_id), "value": float(d.value)}
            for d in payload.split_details
        ]
    return json.dumps({"split_with": split_with, "split_details": split_details})


def _serialize_expense_payers_snapshot(payload) -> Optional[str]:
    if not getattr(payload, "payers", None) or len(payload.payers) < 2:
        return None
    return json.dumps([
        {"member_id": int(p.member_id), "amount": float(p.amount)}
        for p in payload.payers
    ])


def _expense_to_out(db: Session, expense: models.Expense) -> schemas.ExpenseOut:
    row = schemas.ExpenseOut.model_validate(expense)
    active = False
    if expense.recurring_template_id:
        tmpl = db.query(models.RecurringExpenseTemplate).get(expense.recurring_template_id)
        active = bool(tmpl and tmpl.active)
    return row.model_copy(update={"recurring_active": active})


def _expenses_to_out(db: Session, expenses: list[models.Expense]) -> list[schemas.ExpenseOut]:
    ids = {e.recurring_template_id for e in expenses if e.recurring_template_id}
    active_ids: set[int] = set()
    if ids:
        active_ids = {
            tid
            for (tid,) in db.query(models.RecurringExpenseTemplate.id).filter(
                models.RecurringExpenseTemplate.id.in_(ids),
                models.RecurringExpenseTemplate.active.is_(True),
            )
        }
    out: list[schemas.ExpenseOut] = []
    for expense in expenses:
        row = schemas.ExpenseOut.model_validate(expense)
        out.append(
            row.model_copy(
                update={"recurring_active": bool(expense.recurring_template_id in active_ids)}
            )
        )
    return out


def _upsert_recurring_template_for_expense(
    db: Session,
    *,
    group_id: int,
    account: models.Account,
    expense: models.Expense,
    payload,
    enable: bool,
) -> None:
    """Create/update/stop a monthly template linked to this expense."""
    existing = None
    if expense.recurring_template_id:
        existing = db.query(models.RecurringExpenseTemplate).get(expense.recurring_template_id)
    if not existing and expense.id:
        existing = (
            db.query(models.RecurringExpenseTemplate)
            .filter_by(group_id=group_id, source_expense_id=expense.id)
            .order_by(models.RecurringExpenseTemplate.id.desc())
            .first()
        )

    if not enable:
        if existing and existing.active:
            existing.active = False
            existing.updated_at = datetime.utcnow()
        return

    day = max(1, min(int(expense.bill_date.day), 31))
    next_run = _next_monthly_run_date(_business_today(), day)
    payers_json = _serialize_expense_payers_snapshot(payload)
    splits_json = _serialize_expense_split_snapshot(payload)
    if existing:
        existing.active = True
        existing.store_name = expense.store_name
        existing.total_amount = expense.total_amount
        existing.currency = expense.currency
        existing.category = expense.category
        existing.split_type = expense.split_type
        existing.payer_member_id = int(expense.payer_member_id)
        existing.payers_json = payers_json
        existing.splits_json = splits_json
        existing.day_of_month = day
        existing.next_run_on = next_run
        existing.source_expense_id = expense.id
        existing.updated_at = datetime.utcnow()
        expense.recurring_template_id = existing.id
        return

    tmpl = models.RecurringExpenseTemplate(
        group_id=group_id,
        created_by_account_id=account.id,
        active=True,
        store_name=expense.store_name,
        total_amount=expense.total_amount,
        currency=expense.currency,
        category=expense.category,
        split_type=expense.split_type,
        payer_member_id=int(expense.payer_member_id),
        payers_json=payers_json,
        splits_json=splits_json,
        day_of_month=day,
        next_run_on=next_run,
        source_expense_id=expense.id,
        last_expense_id=expense.id,
    )
    db.add(tmpl)
    db.flush()
    expense.recurring_template_id = tmpl.id


def _materialize_recurring_template(db: Session, tmpl: models.RecurringExpenseTemplate) -> Optional[models.Expense]:
    """Create one expense occurrence for a due template. Returns expense or None if skipped."""
    if not tmpl.active:
        return None
    run_on = tmpl.next_run_on
    period_key = _period_key_for(run_on)
    if tmpl.last_posted_period_key == period_key:
        tmpl.next_run_on = _next_monthly_run_date(run_on, tmpl.day_of_month)
        return None

    already = (
        db.query(models.Expense)
        .join(models.MonthlyPeriod)
        .filter(
            models.MonthlyPeriod.group_id == tmpl.group_id,
            models.MonthlyPeriod.period_key == period_key,
            models.Expense.recurring_template_id == tmpl.id,
        )
        .first()
    )
    if already:
        tmpl.last_posted_period_key = period_key
        tmpl.last_expense_id = already.id
        tmpl.next_run_on = _next_monthly_run_date(run_on, tmpl.day_of_month)
        return None

    active_members = {
        m.id: m
        for m in db.query(models.GroupMember).filter_by(group_id=tmpl.group_id, is_active=True).all()
    }
    if tmpl.payer_member_id not in active_members:
        logger.warning(
            "Skipping recurring template %s — payer member %s inactive",
            tmpl.id,
            tmpl.payer_member_id,
        )
        tmpl.next_run_on = _next_monthly_run_date(run_on, tmpl.day_of_month)
        return None

    try:
        split_payload = json.loads(tmpl.splits_json or "{}")
    except json.JSONDecodeError:
        split_payload = {}
    split_with = [int(x) for x in (split_payload.get("split_with") or []) if int(x) in active_members]
    split_details_raw = split_payload.get("split_details") or []
    split_details = [
        schemas.SplitDetailInput(member_id=int(d["member_id"]), value=float(d["value"]))
        for d in split_details_raw
        if int(d.get("member_id", 0)) in active_members
    ]
    if not split_with and not split_details:
        logger.warning("Skipping recurring template %s — no active split members", tmpl.id)
        tmpl.next_run_on = _next_monthly_run_date(run_on, tmpl.day_of_month)
        return None

    payers = None
    if tmpl.payers_json:
        try:
            raw_payers = json.loads(tmpl.payers_json)
        except json.JSONDecodeError:
            raw_payers = []
        payers = [
            schemas.ExpensePayerInput(member_id=int(p["member_id"]), amount=float(p["amount"]))
            for p in raw_payers
            if int(p.get("member_id", 0)) in active_members and float(p.get("amount", 0)) > 0
        ]
        if len(payers) < 2:
            payers = None

    group = db.query(models.Group).get(tmpl.group_id)
    if not group:
        tmpl.active = False
        return None

    period = get_or_create_period(db, tmpl.group_id, period_key)
    _unlock_group_period(period)
    expense_currency = _normalize_currency(tmpl.currency, group.currency or "USD")
    group_currency = _normalize_currency(group.currency, "USD")
    fx_rate = _fx_rate_between(expense_currency, group_currency)
    total = float(tmpl.total_amount)

    expense = models.Expense(
        period_id=period.id,
        store_name=tmpl.store_name,
        bill_date=run_on,
        total_amount=Decimal(str(total)),
        currency=expense_currency,
        category=tmpl.category or "General",
        split_type=tmpl.split_type or "EQUAL",
        payer_member_id=tmpl.payer_member_id,
        image_path=None,
        fx_rate_to_group=fx_rate,
        recurring_template_id=tmpl.id,
    )
    db.add(expense)
    db.flush()
    expense.payer_member_id = _apply_expense_payers(
        db, expense, total, tmpl.payer_member_id, payers,
    )
    _apply_expense_splits_custom(
        db,
        expense,
        total,
        tmpl.split_type or "EQUAL",
        split_with or None,
        split_details or None,
    )

    tmpl.last_expense_id = expense.id
    tmpl.last_posted_period_key = period_key
    tmpl.next_run_on = _next_monthly_run_date(run_on, tmpl.day_of_month)
    tmpl.updated_at = datetime.utcnow()
    return expense


def _process_due_recurring_expenses() -> None:
    db = SessionLocal()
    try:
        today = _business_today()
        due = (
            db.query(models.RecurringExpenseTemplate)
            .filter(
                models.RecurringExpenseTemplate.active.is_(True),
                models.RecurringExpenseTemplate.next_run_on <= today,
            )
            .order_by(models.RecurringExpenseTemplate.id.asc())
            .all()
        )
        for tmpl in due:
            try:
                # Catch up at most a few missed months per tick.
                for _ in range(3):
                    if not tmpl.active or tmpl.next_run_on > today:
                        break
                    expense = _materialize_recurring_template(db, tmpl)
                    if expense:
                        _record_activity(
                            db,
                            tmpl.group_id,
                            None,
                            "expense_created",
                            f"Monthly bill posted: {expense.store_name}",
                            "expense",
                            expense.id,
                        )
                        _notify_group_members(
                            db,
                            tmpl.group_id,
                            f"Monthly recurring bill added: {expense.store_name}.",
                            _group_focus_link(tmpl.group_id, "expenses", f"expense-{expense.id}"),
                        )
                        notify_group_change(tmpl.group_id, "expense-created")
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("Failed recurring template %s", tmpl.id)
    finally:
        db.close()


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

    participant_ids = []
    if payload.split_details:
        participant_ids = [d.member_id for d in payload.split_details]
    elif payload.split_with:
        participant_ids = payload.split_with
    else:
        raise HTTPException(400, "Must specify members to split with")
    if len(participant_ids) != len(set(int(mid) for mid in participant_ids)):
        raise HTTPException(400, "Duplicate split participants are not allowed")

    payer_ids = [item.member_id for item in payload.payers] if payload.payers and len(payload.payers) >= 2 else [payload.payer_member_id]
    _validate_expense_members(db, group_id, payer_ids[0], participant_ids + payer_ids[1:])

    # Period = month the bill is saved on the server. Client period_key is ignored.
    # bill_date stays the receipt date (Added late when older than the save month).
    period_key = _period_key_for(_business_today())
    period = get_or_create_period(db, group_id, period_key)
    _unlock_group_period(period)

    expense_currency = _normalize_currency(payload.currency, group.currency or "USD")
    group_currency = _normalize_currency(group.currency, "USD")
    fx_rate = _fx_rate_between(expense_currency, group_currency)

    final_path = None
    pending = None
    pending_src = None
    if payload.upload_id:
        pending = (
            db.query(models.PendingUpload)
            .filter_by(id=payload.upload_id, group_id=group_id)
            .first()
        )
        if not pending or not os.path.exists(pending.file_path):
            raise HTTPException(400, "Upload not found or already used. Re-upload the receipt.")
        pending_src = pending.file_path
        ext = os.path.splitext(pending.file_path)[1]
        final_filename = f"{uuid.uuid4().hex}{ext}"
        group_uploads = _group_uploads_dir(group_id)
        final_path = os.path.join(group_uploads, final_filename)
        # Move only after membership/split validation above succeeded.
        shutil.move(pending.file_path, final_path)

    try:
        expense = models.Expense(
            period_id=period.id,
            store_name=payload.store_name.strip(),
            bill_date=payload.bill_date,
            total_amount=Decimal(str(payload.total_amount)),
            currency=expense_currency,
            category=_normalize_category(payload.category),
            split_type=payload.split_type or "EQUAL",
            payer_member_id=payload.payer_member_id,
            image_path=final_path,
            fx_rate_to_group=fx_rate,
        )
        db.add(expense)
        db.flush()
        expense.payer_member_id = _apply_expense_payers(db, expense, payload.total_amount, payload.payer_member_id, payload.payers)

        _apply_expense_splits_custom(
            db,
            expense,
            payload.total_amount,
            payload.split_type or "EQUAL",
            payload.split_with,
            payload.split_details,
        )

        _remember_account_vendor(
            db, account.id, payload.store_name, pending=pending, category=_normalize_category(payload.category),
        )
        if pending:
            db.delete(pending)

        db.flush()
        if payload.recurring_monthly:
            _upsert_recurring_template_for_expense(
                db,
                group_id=group_id,
                account=account,
                expense=expense,
                payload=payload,
                enable=True,
            )

        # A new bill starts an immutable financial history immediately.
        _record_expense_audit(
            db, group_id=group_id, expense_id=expense.id, actor=account,
            event_type="CREATED", before=None, after=_expense_snapshot(db, expense),
        )
        _reconcile_latest_weekly_lock(db, group, period, actor=account)
        _bump_group_revision(db, group_id)
        db.commit()
    except Exception:
        db.rollback()
        # Restore the pending upload file if we moved it before a later failure.
        if pending_src and final_path and os.path.exists(final_path) and not os.path.exists(pending_src):
            try:
                os.makedirs(os.path.dirname(pending_src), exist_ok=True)
                shutil.move(final_path, pending_src)
            except OSError:
                logger.exception("Could not restore pending upload after failed expense save")
        raise
    db.refresh(expense)
    _record_activity(db, group_id, account, "expense_created", f"{account.display_name} added {expense.store_name}", "expense", expense.id)
    note = f"{account.display_name} added {expense.store_name} to this group."
    if payload.recurring_monthly:
        note = f"{account.display_name} added {expense.store_name} (repeats every month)."
    _notify_group_members(db, group_id, note, _group_focus_link(group_id, "expenses", f"expense-{expense.id}"), exclude_account_id=account.id)
    db.commit()
    notify_group_change(group_id, "expense-created")
    return _expense_to_out(db, expense)


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
    before_snapshot = _expense_snapshot(db, expense)

    participant_ids = []
    if payload.split_details:
        participant_ids = [d.member_id for d in payload.split_details]
    elif payload.split_with:
        participant_ids = payload.split_with

    payer_ids = [item.member_id for item in payload.payers] if payload.payers and len(payload.payers) >= 2 else [payload.payer_member_id]
    _validate_expense_members(db, group_id, payer_ids[0], participant_ids + payer_ids[1:])

    # Keep the month-saved period sticky; editing bill_date must not move the ledger month.
    _unlock_group_period(expense.period)
    group = db.query(models.Group).get(group_id)
    group_currency = _normalize_currency(group.currency if group else "USD", "USD")
    expense.store_name = payload.store_name.strip()
    expense.bill_date = payload.bill_date
    expense.total_amount = Decimal(str(payload.total_amount))
    expense.currency = _normalize_currency(payload.currency, expense.currency or "USD")
    expense.fx_rate_to_group = _fx_rate_between(expense.currency, group_currency)
    expense.category = _normalize_category(payload.category or expense.category)
    expense.split_type = payload.split_type or expense.split_type or "EQUAL"
    expense.payer_member_id = _apply_expense_payers(db, expense, payload.total_amount, payload.payer_member_id, payload.payers)

    _apply_expense_splits_custom(
        db,
        expense,
        payload.total_amount,
        expense.split_type,
        payload.split_with,
        payload.split_details,
    )

    _remember_account_vendor(db, account.id, expense.store_name, category=expense.category)

    if payload.recurring_monthly is not None:
        _upsert_recurring_template_for_expense(
            db,
            group_id=group_id,
            account=account,
            expense=expense,
            payload=payload,
            enable=bool(payload.recurring_monthly),
        )

    db.flush()
    after_snapshot = _expense_snapshot(db, expense)
    _record_expense_audit(
        db, group_id=group_id, expense_id=expense.id, actor=account,
        event_type="UPDATED", before=before_snapshot, after=after_snapshot,
    )
    _reconcile_latest_weekly_lock(db, group, expense.period, actor=account)
    _bump_group_revision(db, group_id)
    db.commit()
    db.refresh(expense)
    _record_activity(db, group_id, account, "expense_updated", f"{account.display_name} updated {expense.store_name}", "expense", expense.id)
    if was_locked:
        _notify_group_members(db, group_id, f"{account.display_name} corrected {expense.store_name}. Recorded payments stay applied; any overpay is now credit on the balance. Locked Friends instructions for this week were updated.", _group_focus_link(group_id, "expenses", f"expense-{expense.id}"), exclude_account_id=account.id)
    else:
        _notify_group_members(db, group_id, f"{account.display_name} updated {expense.store_name}.", _group_focus_link(group_id, "expenses", f"expense-{expense.id}"), exclude_account_id=account.id)
    db.commit()
    notify_group_change(group_id, "expense-updated")
    return _expense_to_out(db, expense)


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
    period = expense.period
    group = db.query(models.Group).get(group_id)
    before_snapshot = _expense_snapshot(db, expense)
    _record_expense_audit(
        db, group_id=group_id, expense_id=expense.id, actor=account,
        event_type="DELETED", before=before_snapshot, after=None,
    )
    db.delete(expense)
    db.flush()
    if group:
        _reconcile_latest_weekly_lock(db, group, period, actor=account)
    _bump_group_revision(db, group_id)
    _record_activity(db, group_id, account, "expense_deleted", f"{account.display_name} deleted {store_name}", "expense", expense_id)
    text = f"{account.display_name} {'corrected the period by deleting' if was_locked else 'deleted'} {store_name}."
    if was_locked:
        text += " Recorded payments stay applied; any overpay is now credit on the balance. Locked Friends instructions for this week were updated."
    _notify_group_members(db, group_id, text, _group_focus_link(group_id, "expenses", "expensesBody"), exclude_account_id=account.id)
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
        normalized = _normalize_category(category)
        aliases = set(_CATEGORY_ALIASES.get(normalized, {normalized}))
        aliases.add(normalized)
        q = q.filter(models.Expense.category.in_(list(aliases)))
    if search:
        term = f"%{search.strip()}%"
        q = q.filter(or_(models.Expense.store_name.ilike(term), models.Expense.category.ilike(term)))
    if member_id:
        q = q.filter(or_(models.Expense.payer_member_id == member_id, models.Expense.payers.any(models.ExpensePayer.member_id == member_id), models.Expense.splits.any(models.ExpenseSplit.member_id == member_id)))
    if involved_me:
        q = q.filter(or_(models.Expense.payer_member_id == current_member.id, models.Expense.payers.any(models.ExpensePayer.member_id == current_member.id), models.Expense.splits.any(models.ExpenseSplit.member_id == current_member.id)))
    if date_from:
        q = q.filter(models.Expense.bill_date >= date_from)
    if date_to:
        q = q.filter(models.Expense.bill_date <= date_to)
    if min_amount is not None:
        q = q.filter(models.Expense.total_amount >= Decimal(str(min_amount)))
    if max_amount is not None:
        q = q.filter(models.Expense.total_amount <= Decimal(str(max_amount)))

    # Newest saves first; bill_date is display-only on the expenses ledger.
    expenses = q.order_by(models.Expense.created_at.desc(), models.Expense.id.desc()).all()
    return _expenses_to_out(db, expenses)


@app.get("/api/groups/{group_id}/recurring-expenses", response_model=list[schemas.RecurringExpenseOut])
def list_recurring_expenses(
    group_id: int,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    return (
        db.query(models.RecurringExpenseTemplate)
        .filter_by(group_id=group_id, active=True)
        .order_by(models.RecurringExpenseTemplate.next_run_on.asc(), models.RecurringExpenseTemplate.id.asc())
        .all()
    )


@app.post("/api/groups/{group_id}/recurring-expenses/{template_id}/stop")
def stop_recurring_expense(
    group_id: int,
    template_id: int,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    tmpl = db.query(models.RecurringExpenseTemplate).get(template_id)
    if not tmpl or tmpl.group_id != group_id:
        raise HTTPException(404, "Recurring bill not found")
    tmpl.active = False
    tmpl.updated_at = datetime.utcnow()
    db.commit()
    notify_group_change(group_id, "recurring-stopped")
    return {"ok": True, "message": "Monthly repeat turned off"}


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
    _notify_group_members(db, group_id, f"{account.display_name} commented on {expense.store_name}.", _group_focus_link(group_id, "expenses", f"expense-{expense.id}"), exclude_account_id=account.id)
    db.commit()
    db.refresh(comment)
    notify_group_change(group_id, "comment-added")
    return schemas.ExpenseCommentOut(id=comment.id, body=comment.body, author_name=account.display_name, created_at=comment.created_at, updated_at=comment.updated_at)


@app.get("/api/groups/{group_id}/activity", response_model=list[schemas.GroupActivityOut])
def list_group_activity(group_id: int, limit: int = Query(30, ge=1, le=100), account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    require_group_member(group_id, account, db)
    rows = db.query(models.GroupActivity).filter_by(group_id=group_id).order_by(models.GroupActivity.created_at.desc(), models.GroupActivity.id.desc()).limit(limit).all()
    return [schemas.GroupActivityOut(id=row.id, event_type=row.event_type, summary=row.summary, actor_name=row.actor.display_name if row.actor else None, entity_type=row.entity_type, entity_id=row.entity_id, created_at=row.created_at) for row in rows]


def _audit_event_out(row: models.ExpenseAuditEvent) -> schemas.ExpenseAuditEventOut:
    return schemas.ExpenseAuditEventOut(
        id=row.id, expense_id=row.expense_id, event_type=row.event_type, revision=row.revision,
        actor_name=row.actor.display_name if row.actor else None,
        before=json.loads(row.before_json) if row.before_json else None,
        after=json.loads(row.after_json) if row.after_json else None,
        changed_fields=json.loads(row.changed_fields_json or "[]"), event_hash=row.event_hash,
        previous_hash=row.previous_hash, created_at=row.created_at,
    )


@app.get("/api/groups/{group_id}/expenses/{expense_id}/audit", response_model=list[schemas.ExpenseAuditEventOut])
def list_expense_audit(group_id: int, expense_id: int, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    require_group_member(group_id, account, db)
    # A deleted bill deliberately has no Expense row, but its audit remains readable.
    rows = db.query(models.ExpenseAuditEvent).filter_by(group_id=group_id, expense_id=expense_id).order_by(models.ExpenseAuditEvent.revision.asc()).all()
    if not rows and not _group_expense_or_404(db, group_id, expense_id):
        raise HTTPException(404, "Expense not found")
    return [_audit_event_out(row) for row in rows]


@app.get("/api/groups/{group_id}/audit/integrity", response_model=schemas.AuditIntegrityOut)
def verify_group_audit_integrity(group_id: int, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    require_group_member(group_id, account, db)
    rows = db.query(models.ExpenseAuditEvent).filter_by(group_id=group_id).order_by(models.ExpenseAuditEvent.id.asc()).all()
    prior_hash = None
    for row in rows:
        before = json.loads(row.before_json) if row.before_json else None
        after = json.loads(row.after_json) if row.after_json else None
        changed = json.loads(row.changed_fields_json or "[]")
        timestamp = _as_utc(row.created_at).isoformat()
        material = {"group_id": row.group_id, "expense_id": row.expense_id, "event_type": row.event_type,
                    "revision": row.revision, "before": before, "after": after, "changed_fields": changed,
                    "previous_hash": prior_hash, "created_at": timestamp}
        calculated = hashlib.sha256(_audit_json(material).encode("utf-8")).hexdigest()
        if row.previous_hash != prior_hash or row.event_hash != calculated:
            return schemas.AuditIntegrityOut(valid=False, checked_events=len(rows), issue=f"Audit chain mismatch at event {row.id}")
        prior_hash = row.event_hash
    return schemas.AuditIntegrityOut(valid=True, checked_events=len(rows))


def _weekly_batch_out(db: Session, batch: models.WeeklySettlementBatch) -> schemas.WeeklySettlementBatchOut:
    period = db.query(models.MonthlyPeriod).get(batch.period_id)
    legs = db.query(models.WeeklySettlementLeg).filter_by(batch_id=batch.id).all()
    return schemas.WeeklySettlementBatchOut(
        id=batch.id, period_key=period.period_key if period else "", window_starts_at=batch.window_starts_at,
        window_ends_at=batch.window_ends_at, status=batch.status, batch_hash=batch.batch_hash,
        created_at=batch.created_at,
        legs=[schemas.WeeklySettlementLegOut(payer_member_id=leg.payer_member_id, recipient_member_id=leg.recipient_member_id, amount=float(leg.amount), currency=leg.currency) for leg in legs],
    )


@app.get("/api/groups/{group_id}/periods/{period_key}/weekly-settlements", response_model=list[schemas.WeeklySettlementBatchOut])
def list_weekly_settlement_batches(group_id: int, period_key: str, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    require_group_member(group_id, account, db)
    period = db.query(models.MonthlyPeriod).filter_by(group_id=group_id, period_key=period_key).first()
    if not period:
        return []
    rows = db.query(models.WeeklySettlementBatch).filter_by(group_id=group_id, period_id=period.id).order_by(models.WeeklySettlementBatch.window_ends_at.asc()).all()
    return [_weekly_batch_out(db, row) for row in rows]


@app.post("/api/groups/{group_id}/periods/{period_key}/weekly-settlements/lock", response_model=schemas.WeeklySettlementBatchOut)
def lock_weekly_settlement_now(group_id: int, period_key: str, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    require_group_member(group_id, account, db)
    group = db.query(models.Group).get(group_id)
    period = get_or_create_period(db, group_id, period_key)
    # Manual mid-week lock: freeze current residual for Friends now.
    # Later bill edits reconcile this same locked batch instead of waiting a week.
    now = datetime.now(timezone.utc).replace(microsecond=0)
    batch = _lock_weekly_settlement_batch(db, group, period, window_ends_at=now, created_by=account)
    _bump_group_revision(db, group_id)
    db.commit()
    db.refresh(batch)
    notify_group_change(group_id, "weekly-settlement-locked")
    return _weekly_batch_out(db, batch)


@app.get("/api/notifications", response_model=list[schemas.NotificationOut])
def list_notifications(unread_only: bool = False, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    # Stale read purge runs on the scheduler owner — keep the read path SELECT-only.
    base = db.query(models.Notification).filter_by(account_id=account.id)
    if unread_only:
        return (
            base.filter(models.Notification.read_at.is_(None))
            .order_by(models.Notification.created_at.desc(), models.Notification.id.desc())
            .limit(50)
            .all()
        )
    # Surface unread first so older unread are never buried under newer read items
    # (which would hide the Mark all read action while the badge still shows unread).
    unread = (
        base.filter(models.Notification.read_at.is_(None))
        .order_by(models.Notification.created_at.desc(), models.Notification.id.desc())
        .limit(50)
        .all()
    )
    if len(unread) >= 50:
        return unread
    read = (
        db.query(models.Notification)
        .filter(
            models.Notification.account_id == account.id,
            models.Notification.read_at.isnot(None),
        )
        .order_by(models.Notification.created_at.desc(), models.Notification.id.desc())
        .limit(50 - len(unread))
        .all()
    )
    return unread + read


@app.post("/api/notifications/read-all")
def mark_all_notifications_read(account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    updated = (
        db.query(models.Notification)
        .filter(
            models.Notification.account_id == account.id,
            models.Notification.read_at.is_(None),
        )
        .update({models.Notification.read_at: now}, synchronize_session=False)
    )
    db.commit()
    return {"updated": int(updated or 0)}


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
    """Record a transfer from the payer; balances update immediately (no second confirmation)."""
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
    now = datetime.now(timezone.utc)
    expense_cutoff_id = db.query(func.max(models.Expense.id)).filter_by(period_id=period.id).scalar()
    payment = models.GroupPayment(
        period_id=period.id, payer_member_id=payload.payer_member_id,
        recipient_member_id=payload.recipient_member_id,
        amount=Decimal(str(payload.amount)), currency=currency,
        status="CONFIRMED", expense_cutoff_id=expense_cutoff_id, marked_paid_at=now, confirmed_at=now,
    )
    db.add(payment)
    db.flush()
    _repair_group_payment_snapshots(db, period)
    recipient = db.query(models.GroupMember).get(payload.recipient_member_id)
    _record_activity(db, group_id, account, "payment_marked_paid", f"{payer.display_name} recorded a payment of {currency} {payload.amount:.2f} to {recipient.display_name}", "payment", payment.id)
    _notify_group_members(db, group_id, f"{payer.display_name} recorded a {currency} {payload.amount:.2f} payment to {recipient.display_name}. Balances were recalculated.", _group_focus_link(group_id, "balances", "paymentHistory"), exclude_account_id=account.id)
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
    db.flush()
    _repair_group_payment_snapshots(db, period)
    payer = db.query(models.GroupMember).get(payload.payer_member_id)
    _record_activity(db, group_id, account, "payment_received", f"{recipient.display_name} recorded {currency} {payload.amount:.2f} received from {payer.display_name}", "payment", None)
    _notify_group_members(db, group_id, f"{recipient.display_name} recorded a {currency} {payload.amount:.2f} payment received from {payer.display_name}. Balances were recalculated.", _group_focus_link(group_id, "balances", "paymentHistory"), exclude_account_id=account.id)
    db.commit()
    db.refresh(payment)
    notify_group_change(group_id, "payment-received")
    return payment


@app.post("/api/groups/{group_id}/payments/{payment_id}/confirm", response_model=schemas.GroupPaymentOut)
def confirm_group_payment(group_id: int, payment_id: int, account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    """Legacy endpoint: new payments apply on record. Still finalizes old PAID rows."""
    recipient = require_group_member(group_id, account, db)
    payment = db.query(models.GroupPayment).join(models.MonthlyPeriod).filter(
        models.GroupPayment.id == payment_id, models.MonthlyPeriod.group_id == group_id,
    ).first()
    if not payment:
        raise HTTPException(404, "Payment not found")
    if payment.status == "CONFIRMED":
        return payment
    if payment.period.status != "SETTLED":
        raise HTTPException(400, "Settle this period before confirming payments")
    if payment.recipient_member_id != recipient.id:
        raise HTTPException(403, "Only the recipient can confirm this payment")
    if payment.status != "PAID":
        raise HTTPException(400, "This payment cannot be confirmed")
    payment.status = "CONFIRMED"
    payment.confirmed_at = datetime.now(timezone.utc)
    _repair_group_payment_snapshots(db, payment.period)
    payer = db.query(models.GroupMember).get(payment.payer_member_id)
    _record_activity(db, group_id, account, "payment_confirmed", f"{recipient.display_name} confirmed payment from {payer.display_name}", "payment", payment.id)
    _notify_group_members(db, group_id, f"{recipient.display_name} confirmed a {payment.currency} {float(payment.amount):.2f} payment from {payer.display_name}. Balances were recalculated.", _group_focus_link(group_id, "balances", "paymentHistory"), exclude_account_id=account.id)
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
    group_currency = (group.currency if group else "USD") or "USD"

    def _in_group_currency(expense: models.Expense) -> float:
        """Sum Insights in group currency for any expense currency (AUD, EUR, JPY, …)."""
        amount = float(expense.total_amount or 0)
        expense_currency = (expense.currency or group_currency or "USD").upper()
        grp = group_currency.upper()
        if expense_currency == grp:
            return amount
        rate = float(expense.fx_rate_to_group or 0) or 0.0
        # Live-resolve when an old row still has the unset 1.0 default.
        if _stored_fx_looks_unset(rate, expense_currency, grp):
            rate = float(_fx_rate_between(expense_currency, grp))
        if rate <= 0:
            rate = 1.0
        return round(amount * rate, 2)

    category_totals: dict[str, float] = {}
    total_spent = 0.0
    ranked: list[tuple[float, models.Expense]] = []
    for expense in expenses:
        converted = _in_group_currency(expense)
        total_spent += converted
        category = _normalize_category(expense.category)
        category_totals[category] = round(category_totals.get(category, 0) + converted, 2)
        ranked.append((converted, expense))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return schemas.GroupDashboardOut(
        total_spent=round(total_spent, 2),
        currency=group_currency,
        category_totals=category_totals,
        member_totals=balances,
        largest_expenses=[expense for _amount, expense in ranked[:5]],
    )

@app.get("/api/groups/{group_id}/periods", response_model=list[schemas.PeriodOut])
def list_periods(
    group_id: int,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    # The UI must advance to the new calendar month even before its first
    # expense is recorded. This also covers deployments restarted after midnight.
    current_key = _period_key_for(_business_today())
    if not db.query(models.MonthlyPeriod.id).filter_by(group_id=group_id, period_key=current_key).first():
        db.add(models.MonthlyPeriod(group_id=group_id, period_key=current_key, status="ACTIVE"))
        db.commit()
    periods = (
        db.query(models.MonthlyPeriod)
        .filter_by(group_id=group_id)
        .order_by(models.MonthlyPeriod.period_key.desc())
        .all()
    )
    return periods


@app.get("/api/groups/{group_id}/balances", response_model=list[schemas.BalanceOut])
def get_group_balances_all_time(
    group_id: int,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    """Overall balances across every month for the Balances & Settlement tab."""
    require_group_member(group_id, account, db)
    balances, _ = build_group_balances_all_time(db, group_id, settlement_mode="smart")
    return balances


@app.get("/api/groups/{group_id}/settlements", response_model=list[schemas.SettlementOut])
def get_group_settlements_all_time(
    group_id: int,
    mode: str = Query("smart", pattern="^(smart|simplified)$"),
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    """Overall Who pays whom across every month for the Balances & Settlement tab."""
    require_group_member(group_id, account, db)
    _, settlements = build_group_balances_all_time(db, group_id, settlement_mode=mode)
    return settlements


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
    mode: str = Query("smart", pattern="^(smart|simplified)$"),
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    period = db.query(models.MonthlyPeriod).filter_by(group_id=group_id, period_key=period_key).first()
    if not period:
        return []
    _, settlements, _ = build_balances_and_settlements(db, group_id, period, settlement_mode=mode)
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
    period.report_revision = int(period.report_revision or 1) + 1
    period.pdf_filename = None
    period.pdf_path = None
    period.settled_at = None
    _record_activity(db, group_id, account, "period_reopened", f"{account.display_name} reopened {period_key}", "period", period.id)
    _notify_group_members(db, group_id, f"{account.display_name} reopened the {period_key} period. Balances may have changed.", _group_focus_link(group_id, "balances", "periodSelect"), exclude_account_id=account.id)
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
    group = db.query(models.Group).get(group_id)
    pdf_path, filename = _snapshot_group_period_pdf(db, group, period)
    if not pdf_path or not filename:
        raise HTTPException(503, "Could not generate the PDF report. Try again or contact support.")

    # Generating a PDF is a snapshot only — do not lock the month as settled.
    _record_activity(db, group_id, account, "report_generated", f"{account.display_name} generated a PDF report for {period_key}", "period", period.id)
    _notify_group_members(db, group_id, f"{account.display_name} generated a PDF report for {period_key}.", _group_focus_link(group_id, "reports"), exclude_account_id=account.id)
    db.commit()
    notify_group_change(group_id, "report-generated")

    return {"ok": True, "filename": filename, "pdf_url": f"/api/groups/{group_id}/reports/{filename}"}


@app.get("/api/reports/monthly-digest/{period_key}")
def download_monthly_digest(
    period_key: str,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    """A user can download their own cross-group monthly record on demand."""
    _period_dates(period_key)
    digest = _build_account_monthly_digest(db, account, period_key)
    db.commit()
    if not digest.pdf_path or not os.path.exists(digest.pdf_path):
        raise HTTPException(404, "Monthly digest is not available yet")
    return FileResponse(digest.pdf_path, media_type="application/pdf", filename=digest.pdf_filename)


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
        cat = _normalize_category(e.category)
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
        category=_normalize_category(payload.category),
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
    expense.category = _normalize_category(payload.category or expense.category)
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


@app.post("/api/personal/receipts/upload")
async def upload_personal_receipt(
    request: Request,
    file: UploadFile = File(...),
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    check_rate_limit(request, limit=20, window_seconds=60)
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in [".jpg", ".jpeg", ".png", ".webp", ".heic"]:
        raise HTTPException(400, "Unsupported image format")

    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(400, f"File exceeds maximum size of {MAX_FILE_SIZE // (1024*1024)}MB")

    upload_id = uuid.uuid4().hex
    tmp_filename = f"personal_{account.id}_{upload_id}{ext}"
    image_url = f"/api/personal/receipts/tmp/{tmp_filename}"
    try:
        return _queue_receipt_scan(
            db=db,
            account=account,
            group_id=None,
            contents=contents,
            mime=file.content_type or "image/jpeg",
            image_url=image_url,
            tmp_filename=tmp_filename,
            upload_id=upload_id,
        )
    except Exception:
        logger.exception("Could not queue personal receipt scan")
        _ensure_schema()
        raise


@app.get("/api/personal/receipts/tmp/{filename}")
def get_personal_tmp_receipt(
    filename: str,
    account: models.Account = Depends(get_current_account),
):
    if not filename.startswith(f"personal_{account.id}_"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")

    storage = get_storage()
    path = storage.open_path(f"uploads/tmp/{filename}") or os.path.join(TMP_UPLOADS_DIR, filename)
    if not (path and os.path.exists(path)):
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
    settings = get_group_member_settings(db, account.id, group_id)
    return group_settings_out(settings)


@app.put("/api/settings/groups/{group_id}", response_model=schemas.GroupSettingsOut)
def update_group_settings_endpoint(
    group_id: int,
    payload: schemas.GroupSettingsUpdate,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    settings = update_group_settings(db, account.id, group_id, payload)
    return group_settings_out(settings)


@app.get("/api/vlm/providers", response_model=list[schemas.VlmProviderOption])
def list_vlm_providers():
    return provider_catalog()


# ---------------------------------------------------------------------------
# Static Files & Mounts
# ---------------------------------------------------------------------------
_CLEAN_PAGE_FILES = {
    "app": "index.html",
    "friends": "friends.html",
    "group": "group.html",
    "personal": "personal.html",
    "settings": "settings.html",
    "quick-split": "quick-split.html",
    "login": "login.html",
}


def _clean_page_response(page: str) -> FileResponse:
    response = FileResponse(os.path.join(STATIC_DIR, _CLEAN_PAGE_FILES[page]))
    # Login especially must not be served from a stale cache that redirected to "/".
    if page in {"login", "app"}:
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


def _legacy_page_redirect(request: Request, page: str) -> RedirectResponse:
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(url=f"/{page}{query}", status_code=308)


@app.get("/app", include_in_schema=False)
def clean_dashboard():
    return _clean_page_response("app")


@app.get("/friends", include_in_schema=False)
def clean_friends():
    return _clean_page_response("friends")


@app.get("/group", include_in_schema=False)
def clean_group():
    return _clean_page_response("group")


@app.get("/personal", include_in_schema=False)
def clean_personal():
    return _clean_page_response("personal")


@app.get("/settings", include_in_schema=False)
def clean_settings():
    return _clean_page_response("settings")


@app.get("/quick-split", include_in_schema=False)
def clean_quick_split():
    return _clean_page_response("quick-split")


@app.get("/login", include_in_schema=False)
def clean_login():
    return _clean_page_response("login")


@app.get("/index.html", include_in_schema=False)
def redirect_index(request: Request):
    return _legacy_page_redirect(request, "app")


@app.get("/friends.html", include_in_schema=False)
def redirect_friends(request: Request):
    return _legacy_page_redirect(request, "friends")


@app.get("/group.html", include_in_schema=False)
def redirect_group(request: Request):
    return _legacy_page_redirect(request, "group")


@app.get("/personal.html", include_in_schema=False)
def redirect_personal(request: Request):
    return _legacy_page_redirect(request, "personal")


@app.get("/settings.html", include_in_schema=False)
def redirect_settings(request: Request):
    return _legacy_page_redirect(request, "settings")


@app.get("/quick-split.html", include_in_schema=False)
def redirect_quick_split(request: Request):
    return _legacy_page_redirect(request, "quick-split")


@app.get("/login.html", include_in_schema=False)
def redirect_login(request: Request):
    return _legacy_page_redirect(request, "login")

if os.path.exists(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
