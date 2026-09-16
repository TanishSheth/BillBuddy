import os
import uuid
import shutil
import logging
import secrets
import time
import hashlib
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal
from calendar import monthrange
from typing import Dict, List, Optional

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Request, Query, status
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlalchemy.orm import Session
from apscheduler.schedulers.background import BackgroundScheduler

from . import models, schemas
from .config import (
    APP_VERSION,
    BASE_URL,
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
from .split_logic import calculate_splits, compute_balances, simplify_debts
from .fx_service import get_exchange_rates, get_conversion_rate, convert_amount, SUPPORTED_CURRENCIES
from .pdf_gen import generate_monthly_pdf, generate_personal_pdf, pdf_reports_available
from .emailer import (
    is_email_configured,
    send_invite_email,
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


def _validate_invite_email(email: str, account: models.Account, group_id: int, db: Session) -> None:
    if email == account.email.lower():
        raise HTTPException(400, "You are already in this group")
    domain = email.rsplit("@", 1)[-1]
    if domain in BLOCKED_INVITE_DOMAINS:
        raise HTTPException(400, f"Cannot send invites to @{domain} — use a real email address")
    already = (
        db.query(models.GroupMember)
        .join(models.Account)
        .filter(models.GroupMember.group_id == group_id, models.Account.email == email)
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


def _public_scan_message(source: str, message: Optional[str]) -> str:
    safe = "Read from the receipt photo. Please double-check merchant, date, and total before saving."
    fallback = "Could not auto-read every field. Please check merchant, date, and total before saving."
    if not message:
        return safe
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
        return fallback
    if len(message) > 180:
        return fallback
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


def _members_as_dicts(db: Session, group_id: int) -> list[dict]:
    members = db.query(models.GroupMember).filter_by(group_id=group_id).all()
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
                "total_amount": float(converted_total),
                "splits": splits,
            }
        )
    return rows


def build_balances_and_settlements(
    db: Session, group_id: int, period: models.MonthlyPeriod
):
    group = db.query(models.Group).get(group_id)
    base_currency = group.currency if group and group.currency else "USD"

    members = _members_as_dicts(db, group_id)
    expenses = (
        db.query(models.Expense)
        .filter_by(period_id=period.id)
        .order_by(models.Expense.bill_date.asc(), models.Expense.id.asc())
        .all()
    )
    raw_balances = compute_balances(
        members, _expenses_as_dicts(expenses, base_currency, base_currency)
    )
    raw_settlements = simplify_debts(raw_balances)

    target_currency = (period.target_currency or "").upper() or None
    fx_rate = float(period.settlement_fx_rate) if period.settlement_fx_rate else None
    converted_balances = {}
    converted_settlements = {}

    if target_currency and target_currency != base_currency:
        if not fx_rate:
            fx_rate = get_conversion_rate(base_currency, target_currency)
        tgt_balances = compute_balances(
            members,
            _expenses_as_dicts(
                expenses,
                dest_currency=target_currency,
                base_currency=base_currency,
                custom_base_to_dest=fx_rate,
            ),
        )
        converted_balances = tgt_balances
        for s in simplify_debts(tgt_balances):
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
    if len(payload.password.strip()) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    
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
            )
        )
    return schemas.MeOut(
        account=schemas.AccountOut(
            id=account.id,
            email=account.email,
            display_name=account.display_name,
        ),
        groups=groups_out,
    )


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

    if len(payload.password.strip()) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")

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
# Groups Endpoints
# ---------------------------------------------------------------------------
@app.get("/api/groups", response_model=list[schemas.GroupListOut])
def list_groups(account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    memberships = db.query(models.GroupMember).filter_by(account_id=account.id).all()
    results = []
    for m in memberships:
        g = m.group
        member_count = db.query(models.GroupMember).filter_by(group_id=g.id).count()
        results.append(
            schemas.GroupListOut(
                id=g.id,
                name=g.name,
                currency=g.currency or "USD",
                invite_code=g.invite_code,
                created_at=g.created_at,
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
        .filter(models.GroupMember.group_id == group_id)
        .all()
    )
    members_out = [
        schemas.MemberOut(
            id=m.id,
            display_name=m.display_name,
            email=m.account.email,
            role=m.role,
        )
        for m in members
    ]

    return schemas.GroupDetailOut(
        id=group.id,
        name=group.name,
        currency=group.currency or "USD",
        invite_code=group.invite_code,
        created_at=group.created_at,
        members=members_out,
    )


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

    db.delete(member)
    db.commit()
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
        db.add(
            models.GroupMember(
                group_id=group.id,
                account_id=existing_account.id,
                display_name=existing_account.display_name,
                role="member",
            )
        )
        pending = (
            db.query(models.GroupInvite)
            .filter_by(group_id=group.id, email=email)
            .first()
        )
        if pending:
            pending.status = "accepted"
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

    pending = models.PendingUpload(id=upload_id, group_id=group_id, file_path=tmp_path)
    db.add(pending)
    db.commit()

    mime = file.content_type or "image/jpeg"
    vlm_config = resolve_vlm_config(db, account.id, group_id)
    extraction, source, message = extract_receipt_data(contents, mime, vlm_config)
    message = _public_scan_message(source, message)

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
        .filter(models.GroupMember.group_id == group_id, models.GroupMember.id.in_(member_ids))
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
        shares = calculate_splits(split_type, total_amount, inputs)
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
    group = db.query(models.Group).get(group_id)
    if not group:
        raise HTTPException(404, "Group not found")

    final_path = None
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
        db.delete(pending)

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

    db.commit()
    db.refresh(expense)
    return expense


@app.put("/api/groups/{group_id}/expenses/{expense_id}", response_model=schemas.ExpenseOut)
def update_expense(
    group_id: int,
    expense_id: int,
    payload: schemas.ExpenseUpdate,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    expense = db.query(models.Expense).get(expense_id)
    if not expense or expense.period.group_id != group_id:
        raise HTTPException(404, "Expense not found")
    if expense.period.status == "SETTLED":
        raise HTTPException(400, "Cannot edit an expense from a settled period")

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

    db.commit()
    db.refresh(expense)
    return expense


@app.delete("/api/groups/{group_id}/expenses/{expense_id}")
def delete_expense(
    group_id: int,
    expense_id: int,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    expense = db.query(models.Expense).get(expense_id)
    if not expense or expense.period.group_id != group_id:
        raise HTTPException(404, "Expense not found")
    if expense.period.status == "SETTLED":
        raise HTTPException(400, "Cannot delete an expense from a settled period")

    if expense.image_path and os.path.exists(expense.image_path):
        try:
            os.remove(expense.image_path)
        except OSError:
            pass

    db.delete(expense)
    db.commit()
    return {"ok": True, "message": "Expense deleted"}


@app.get("/api/groups/{group_id}/expenses", response_model=list[schemas.ExpenseOut])
def list_expenses(
    group_id: int,
    period_key: Optional[str] = None,
    category: Optional[str] = None,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    q = (
        db.query(models.Expense)
        .join(models.MonthlyPeriod)
        .filter(models.MonthlyPeriod.group_id == group_id)
    )
    if period_key:
        q = q.filter(models.MonthlyPeriod.period_key == period_key)
    if category and category != "All":
        q = q.filter(models.Expense.category == category)

    return q.order_by(models.Expense.bill_date.desc(), models.Expense.id.desc()).all()


# ---------------------------------------------------------------------------
# Periods, Balances, Settlements, Reports
# ---------------------------------------------------------------------------
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
    db.commit()
    db.refresh(period)
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
    if period.status == "SETTLED":
        raise HTTPException(400, "Period is already settled")

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
    db.commit()

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
def _claim_personal_upload(db: Session, account_id: int, upload_id: str) -> str:
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
        final_path = _claim_personal_upload(db, account.id, payload.upload_id)

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
    extraction, source, message = extract_receipt_data(contents, mime, vlm_config)
    message = _public_scan_message(source, message)

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
