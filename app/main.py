import os
import uuid
import shutil
import logging
import secrets
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal
from calendar import monthrange

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from apscheduler.schedulers.background import BackgroundScheduler

from . import models, schemas
from .config import (
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
from .split_logic import equal_split, compute_balances, simplify_debts
from .pdf_gen import generate_monthly_pdf, pdf_reports_available
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

app = FastAPI(title="BillBuddy — Multi-User Expense Splitter")


@app.middleware("http")
async def dev_disable_static_cache(request, call_next):
    """In local dev, avoid 304 cached JS/CSS so UI changes show without hard-refresh."""
    response = await call_next(request)
    if os.environ.get("BILLBUDDY_ENV") == "development":
        path = request.url.path
        if path == "/" or path.endswith((".html", ".js", ".css")):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
    return response


@app.get("/api/health")
def health():
    return {
        "ok": True,
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


def _group_uploads_dir(group_id: int) -> str:
    path = os.path.join(UPLOADS_DIR, str(group_id))
    os.makedirs(path, exist_ok=True)
    return path


def require_group_member(
    group_id: int, account: models.Account, db: Session
) -> models.GroupMember:
    member = (
        db.query(models.GroupMember)
        .filter_by(group_id=group_id, account_id=account.id)
        .first()
    )
    if not member:
        raise HTTPException(403, "Not a member of this group")
    return member


def require_group_owner(
    group_id: int, account: models.Account, db: Session
) -> models.GroupMember:
    member = require_group_member(group_id, account, db)
    if member.role != "owner":
        raise HTTPException(403, "Only the group owner can do this")
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


def _members_as_dicts(db: Session, group_id: int) -> list[dict]:
    members = (
        db.query(models.GroupMember)
        .filter_by(group_id=group_id)
        .order_by(models.GroupMember.display_name)
        .all()
    )
    return [{"id": m.id, "name": m.display_name} for m in members]


def _expenses_as_dicts(expenses: list[models.Expense]) -> list[dict]:
    return [
        {
            "payer_id": e.payer_member_id,
            "total_amount": float(e.total_amount),
            "splits": [{"user_id": s.member_id, "share_amount": float(s.share_amount)} for s in e.splits],
        }
        for e in expenses
    ]


def build_balances_and_settlements(db: Session, group_id: int, period: models.MonthlyPeriod):
    members = _members_as_dicts(db, group_id)
    expenses = db.query(models.Expense).filter_by(period_id=period.id).all()
    balances = compute_balances(members, _expenses_as_dicts(expenses))
    settlements = simplify_debts(balances)
    return list(balances.values()), settlements, expenses


def _member_to_out(member: models.GroupMember) -> schemas.MemberOut:
    return schemas.MemberOut(
        id=member.id,
        display_name=member.display_name,
        email=member.account.email,
        role=member.role,
    )


def _cleanup_group_files(group_id: int) -> None:
    for path in (
        os.path.join(UPLOADS_DIR, str(group_id)),
        os.path.join(REPORTS_DIR, str(group_id)),
    ):
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)


def _group_list_item(membership: models.GroupMember, db: Session) -> schemas.GroupListOut:
    group = membership.group
    member_count = db.query(models.GroupMember).filter_by(group_id=group.id).count()
    return schemas.GroupListOut(
        id=group.id,
        name=group.name,
        invite_code=group.invite_code,
        created_at=group.created_at,
        role=membership.role,
        member_count=member_count,
    )


def _add_member_to_group(
    db: Session, group: models.Group, account: models.Account, role: str = "member"
) -> models.GroupMember:
    existing = (
        db.query(models.GroupMember)
        .filter_by(group_id=group.id, account_id=account.id)
        .first()
    )
    if existing:
        return existing
    member = models.GroupMember(
        group_id=group.id,
        account_id=account.id,
        display_name=account.display_name,
        role=role,
    )
    db.add(member)
    db.commit()
    db.refresh(member)
    return member


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@app.post("/api/auth/register", response_model=schemas.TokenResponse)
def register(payload: schemas.RegisterRequest, db: Session = Depends(get_db)):
    email = payload.email.lower()
    if db.query(models.Account).filter_by(email=email).first():
        raise HTTPException(400, "Email already registered")
    account = models.Account(
        email=email,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name.strip(),
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    process_pending_invites(db, account)
    return schemas.TokenResponse(access_token=create_access_token(account.id))


@app.post("/api/auth/login", response_model=schemas.TokenResponse)
def login(payload: schemas.LoginRequest, db: Session = Depends(get_db)):
    account = db.query(models.Account).filter_by(email=payload.email.lower()).first()
    if not account or not verify_password(payload.password, account.password_hash):
        raise HTTPException(401, "Invalid email or password")
    process_pending_invites(db, account)
    return schemas.TokenResponse(access_token=create_access_token(account.id))


FORGOT_PASSWORD_MESSAGE = (
    "If an account with that email exists, a password reset link has been sent."
)


@app.post("/api/auth/forgot-password", response_model=schemas.MessageResponse)
def forgot_password(
    payload: schemas.ForgotPasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    email = payload.email.lower()
    account = db.query(models.Account).filter_by(email=email).first()
    if not account:
        return schemas.MessageResponse(message=FORGOT_PASSWORD_MESSAGE)

    if not is_email_configured() and not IS_DEVELOPMENT:
        raise HTTPException(
            503,
            "Password reset is unavailable — email is not configured on this server.",
        )

    db.query(models.PasswordResetToken).filter_by(
        account_id=account.id,
        used_at=None,
    ).delete(synchronize_session=False)

    token = secrets.token_urlsafe(32)
    reset_token = models.PasswordResetToken(
        account_id=account.id,
        token=token,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db.add(reset_token)
    db.commit()

    reset_url = f"{_public_base_url(request)}/reset-password.html?token={token}"
    dev_reset_url = None

    if is_email_configured():
        try:
            send_password_reset_email(account.email, reset_url)
        except Exception:
            logger.exception("Failed to send password reset email to %s", account.email)
            raise HTTPException(500, "Failed to send reset email. Try again later.") from None
    else:
        logger.info("Password reset link (dev, no SMTP): %s", reset_url)
        dev_reset_url = reset_url

    return schemas.MessageResponse(message=FORGOT_PASSWORD_MESSAGE, reset_url=dev_reset_url)


@app.post("/api/auth/reset-password", response_model=schemas.MessageResponse)
def reset_password(payload: schemas.ResetPasswordRequest, db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    reset_token = (
        db.query(models.PasswordResetToken)
        .filter_by(token=payload.token, used_at=None)
        .first()
    )
    if not reset_token:
        raise HTTPException(400, "Invalid or expired reset link")

    expires_at = reset_token.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < now:
        raise HTTPException(400, "Invalid or expired reset link")

    account = db.query(models.Account).get(reset_token.account_id)
    if not account:
        raise HTTPException(400, "Invalid or expired reset link")

    account.password_hash = hash_password(payload.password)
    reset_token.used_at = now
    db.commit()

    return schemas.MessageResponse(message="Password updated. You can sign in now.")


@app.get("/api/auth/me", response_model=schemas.MeOut)
def me(account: models.Account = Depends(get_current_account), db: Session = Depends(get_db)):
    memberships = (
        db.query(models.GroupMember)
        .filter_by(account_id=account.id)
        .all()
    )
    groups = [m.group for m in memberships]
    return schemas.MeOut(
        account=account,
        groups=[schemas.GroupOut.model_validate(g) for g in groups],
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@app.get("/api/settings/vlm-providers", response_model=list[schemas.VlmProviderOption])
def list_vlm_providers():
    return provider_catalog()


@app.get("/api/settings", response_model=schemas.AccountSettingsOut)
def get_settings(
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    row = get_account_settings(db, account.id)
    return schemas.AccountSettingsOut(**account_settings_out(row))


@app.put("/api/settings", response_model=schemas.AccountSettingsOut)
def put_settings(
    payload: schemas.AccountSettingsUpdate,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    try:
        row = update_account_settings(db, account.id, payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return schemas.AccountSettingsOut(**account_settings_out(row))


@app.get("/api/groups/{group_id}/settings", response_model=schemas.GroupSettingsOut)
def get_group_settings(
    group_id: int,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    row = get_group_member_settings(db, account.id, group_id)
    return schemas.GroupSettingsOut(**group_settings_out(row))


@app.put("/api/groups/{group_id}/settings", response_model=schemas.GroupSettingsOut)
def put_group_settings(
    group_id: int,
    payload: schemas.GroupSettingsUpdate,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    try:
        row = update_group_settings(db, account.id, group_id, payload.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return schemas.GroupSettingsOut(**group_settings_out(row))


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------
@app.post("/api/groups", response_model=schemas.GroupOut)
def create_group(
    payload: schemas.GroupCreate,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    group = models.Group(name=payload.name.strip(), created_by_id=account.id)
    db.add(group)
    db.flush()
    _add_member_to_group(db, group, account, role="owner")
    db.refresh(group)
    return group


@app.get("/api/groups", response_model=list[schemas.GroupListOut])
def list_groups(
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    memberships = db.query(models.GroupMember).filter_by(account_id=account.id).all()
    return [_group_list_item(m, db) for m in memberships]


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
    _cleanup_group_files(group_id)
    db.delete(group)
    db.commit()
    return {"ok": True}


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
    members = db.query(models.GroupMember).filter_by(group_id=group_id).all()
    return schemas.GroupDetailOut(
        id=group.id,
        name=group.name,
        invite_code=group.invite_code,
        created_at=group.created_at,
        members=[_member_to_out(m) for m in members],
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
    _add_member_to_group(db, group, account)
    return group


@app.get("/api/groups/{group_id}/invite-link", response_model=schemas.InviteLinkOut)
def get_invite_link(
    group_id: int,
    request: Request,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    group = db.query(models.Group).get(group_id)
    if not group:
        raise HTTPException(404, "Group not found")
    return schemas.InviteLinkOut(
        invite_code=group.invite_code,
        url=_invite_url(request, group.invite_code),
    )


@app.post("/api/groups/{group_id}/invites")
def invite_by_email(
    group_id: int,
    payload: schemas.InviteByEmailRequest,
    request: Request,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    group = db.query(models.Group).get(group_id)
    if not group:
        raise HTTPException(404, "Group not found")

    email = payload.email.lower()
    _validate_invite_email(email, account, group_id, db)
    invite_url = _invite_url(request, group.invite_code)
    inviter_name = account.display_name

    target = db.query(models.Account).filter_by(email=email).first()
    if target:
        _add_member_to_group(db, group, target)
        if is_email_configured():
            try:
                send_invite_email(email, group.name, invite_url, inviter_name)
                return {"ok": True, "status": "joined_emailed"}
            except Exception as exc:
                logger.warning("Added member but email failed: %s", exc)
        return {"ok": True, "status": "joined"}

    invite = (
        db.query(models.GroupInvite)
        .filter_by(group_id=group_id, email=email)
        .first()
    )
    if not invite:
        invite = models.GroupInvite(group_id=group_id, email=email, status="pending")
        db.add(invite)
    else:
        invite.status = "pending"
    db.commit()

    if is_email_configured():
        try:
            send_invite_email(email, group.name, invite_url, inviter_name)
            return {"ok": True, "status": "emailed", "invite_url": invite_url}
        except Exception as exc:
            raise HTTPException(502, f"Email failed to send: {exc}") from exc

    return {
        "ok": True,
        "status": "pending_no_email",
        "invite_url": invite_url,
        "message": "Email is not configured — copy the invite link below and send it to them.",
    }


@app.delete("/api/groups/{group_id}/members/{member_id}")
def remove_member(
    group_id: int,
    member_id: int,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    owner = require_group_owner(group_id, account, db)
    member = db.query(models.GroupMember).filter_by(id=member_id, group_id=group_id).first()
    if not member:
        raise HTTPException(404, "Member not found")
    if member.id == owner.id:
        raise HTTPException(400, "Cannot remove yourself as owner")
    db.delete(member)
    db.commit()
    return {"ok": True}


@app.post("/api/groups/{group_id}/leave")
def leave_group(
    group_id: int,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    membership = require_group_member(group_id, account, db)
    if membership.role == "owner":
        raise HTTPException(400, "Owners must delete the group instead of leaving")
    db.delete(membership)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Members (group-scoped)
# ---------------------------------------------------------------------------
@app.get("/api/groups/{group_id}/members", response_model=list[schemas.MemberOut])
def list_members(
    group_id: int,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    members = db.query(models.GroupMember).filter_by(group_id=group_id).all()
    return [_member_to_out(m) for m in members]


# ---------------------------------------------------------------------------
# Receipt upload + VLM extraction
# ---------------------------------------------------------------------------
@app.post("/api/groups/{group_id}/receipts/extract", response_model=schemas.ExtractionResponse)
async def extract_receipt(
    group_id: int,
    file: UploadFile = File(...),
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "File must be an image")

    upload_id = uuid.uuid4().hex
    ext = os.path.splitext(file.filename or "")[1] or ".jpg"
    tmp_path = os.path.join(TMP_UPLOADS_DIR, f"{group_id}_{upload_id}{ext}")

    contents = await file.read()
    with open(tmp_path, "wb") as f:
        f.write(contents)

    db.add(models.PendingUpload(id=upload_id, group_id=group_id, file_path=tmp_path))
    db.commit()

    vlm_config = resolve_vlm_config(db, account.id, group_id)
    extraction, source, message = extract_receipt_data(contents, file.content_type, vlm_config)
    if source == "vlm":
        message = f"Extracted using {vlm_config.provider}/{vlm_config.model} ({vlm_config.source} settings)"

    tmp_filename = f"{group_id}_{upload_id}{ext}"
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
    if not filename.startswith(f"{group_id}_") and "_" not in filename:
        pass
    path = os.path.join(TMP_UPLOADS_DIR, filename)
    if not os.path.exists(path):
        raise HTTPException(404, "Not found")
    return FileResponse(path)


# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------
def _validate_expense_members(db: Session, group_id: int, payer_id: int, split_with: list[int]) -> None:
    if not split_with:
        raise HTTPException(400, "split_with must include at least one member")
    member_ids = set(split_with + [payer_id])
    existing = {
        m.id
        for m in db.query(models.GroupMember.id)
        .filter(
            models.GroupMember.group_id == group_id,
            models.GroupMember.id.in_(member_ids),
        )
        .all()
    }
    for mid in member_ids:
        if mid not in existing:
            raise HTTPException(400, f"Member id {mid} is not in this group")


def _apply_expense_splits(
    db: Session, expense: models.Expense, total_amount: float, split_with: list[int]
) -> None:
    for split in list(expense.splits):
        db.delete(split)
    db.flush()
    shares = equal_split(Decimal(str(total_amount)), split_with)
    for mid, amount in shares.items():
        db.add(models.ExpenseSplit(expense_id=expense.id, member_id=mid, share_amount=amount))


@app.post("/api/groups/{group_id}/expenses", response_model=schemas.ExpenseOut)
def create_expense(
    group_id: int,
    payload: schemas.ExpenseCreate,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)

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

    if not payload.split_with:
        raise HTTPException(400, "split_with must include at least one member")

    _validate_expense_members(db, group_id, payload.payer_member_id, payload.split_with)

    period_key = payload.period_key or _period_key_for(payload.bill_date)
    period = get_or_create_period(db, group_id, period_key)
    if period.status == "SETTLED":
        raise HTTPException(400, f"Period {period_key} is already settled; cannot add expenses")

    expense = models.Expense(
        period_id=period.id,
        store_name=payload.store_name,
        bill_date=payload.bill_date,
        total_amount=Decimal(str(payload.total_amount)),
        payer_member_id=payload.payer_member_id,
        image_path=final_path,
    )
    db.add(expense)
    db.flush()

    _apply_expense_splits(db, expense, payload.total_amount, payload.split_with)

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

    _validate_expense_members(db, group_id, payload.payer_member_id, payload.split_with)

    period_key = payload.period_key or _period_key_for(payload.bill_date)
    period = get_or_create_period(db, group_id, period_key)
    if period.status == "SETTLED":
        raise HTTPException(400, f"Period {period_key} is already settled; cannot edit expense")

    expense.period_id = period.id
    expense.store_name = payload.store_name
    expense.bill_date = payload.bill_date
    expense.total_amount = Decimal(str(payload.total_amount))
    expense.payer_member_id = payload.payer_member_id

    _apply_expense_splits(db, expense, payload.total_amount, payload.split_with)

    db.commit()
    db.refresh(expense)
    return expense


@app.get("/api/groups/{group_id}/expenses", response_model=list[schemas.ExpenseOut])
def list_expenses(
    group_id: int,
    period: str | None = None,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    q = (
        db.query(models.Expense)
        .join(models.MonthlyPeriod)
        .filter(models.MonthlyPeriod.group_id == group_id)
    )
    if period:
        p = db.query(models.MonthlyPeriod).filter_by(group_id=group_id, period_key=period).first()
        if not p:
            return []
        q = q.filter(models.Expense.period_id == p.id)
    return q.order_by(models.Expense.bill_date.desc()).all()


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
        os.remove(expense.image_path)
    db.delete(expense)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Periods / Balances / Settlements
# ---------------------------------------------------------------------------
@app.get("/api/groups/{group_id}/periods", response_model=list[schemas.PeriodOut])
def list_periods(
    group_id: int,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    return (
        db.query(models.MonthlyPeriod)
        .filter_by(group_id=group_id)
        .order_by(models.MonthlyPeriod.period_key.desc())
        .all()
    )


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
        return list(compute_balances(members, []).values())
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


def _reopen_period(db: Session, period: models.MonthlyPeriod) -> models.MonthlyPeriod:
    if period.status != "SETTLED":
        raise HTTPException(400, f"Period {period.period_key} is not settled")

    if period.pdf_path and os.path.exists(period.pdf_path):
        try:
            os.remove(period.pdf_path)
        except OSError as exc:
            logger.error("Failed to delete report %s: %s", period.pdf_path, exc)
            raise HTTPException(500, "Failed to delete archived report") from exc

    period.pdf_filename = None
    period.pdf_path = None
    period.settled_at = None
    period.status = "ACTIVE"
    db.commit()
    db.refresh(period)
    return period


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
    return _reopen_period(db, period)


def _email_report_to_members(
    db: Session,
    group_id: int,
    period_key: str,
    pdf_path: str,
    pdf_filename: str,
) -> int:
    if not is_email_configured():
        logger.info("SMTP not configured — skipping report emails for group %s", group_id)
        return 0

    group = db.query(models.Group).get(group_id)
    if not group:
        return 0

    members = (
        db.query(models.GroupMember)
        .join(models.Account)
        .filter(models.GroupMember.group_id == group_id)
        .all()
    )

    sent = 0
    for member in members:
        acct_settings = get_account_settings(db, member.account_id)
        if not acct_settings.email_monthly_reports:
            logger.info("Skipping report email to %s (opted out)", member.account.email)
            continue
        try:
            send_monthly_report_email(
                to_email=member.account.email,
                member_name=member.display_name,
                group_name=group.name,
                period_key=period_key,
                pdf_path=pdf_path,
                pdf_filename=pdf_filename,
            )
            sent += 1
        except Exception:
            logger.exception(
                "Failed to email report to %s for group %s period %s",
                member.account.email,
                group_id,
                period_key,
            )
    return sent


def _generate_report_for_period(
    db: Session, group_id: int, period: models.MonthlyPeriod
) -> models.MonthlyPeriod:
    if period.status == "SETTLED":
        raise HTTPException(400, f"Period {period.period_key} already settled")

    balances, settlements, expenses = build_balances_and_settlements(db, group_id, period)

    members_by_id = {
        m.id: m.display_name
        for m in db.query(models.GroupMember).filter_by(group_id=group_id).all()
    }
    expense_dicts = []
    for e in expenses:
        participant_names = [members_by_id.get(s.member_id, "?") for s in e.splits]
        expense_dicts.append({
            "bill_date": e.bill_date.isoformat(),
            "store_name": e.store_name,
            "payer_name": members_by_id.get(e.payer_member_id, "?"),
            "participant_names": participant_names,
            "total_amount": float(e.total_amount),
            "image_path": e.image_path,
        })

    reports_dir = _group_reports_dir(group_id)
    try:
        pdf_path, filename = generate_monthly_pdf(
            period.period_key, balances, settlements, expense_dicts, reports_dir
        )
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc

    deleted, missing = 0, 0
    for e in expenses:
        if e.image_path:
            if os.path.exists(e.image_path):
                try:
                    os.remove(e.image_path)
                    deleted += 1
                except OSError as exc:
                    logger.error("Failed to delete image %s: %s", e.image_path, exc)
            else:
                missing += 1
            e.image_path = None
    logger.info("Group %s period %s: deleted %d receipt images (%d missing)", group_id, period.period_key, deleted, missing)

    period.pdf_filename = filename
    period.pdf_path = pdf_path
    period.status = "SETTLED"
    period.settled_at = datetime.now()
    db.commit()
    db.refresh(period)

    emails_sent = _email_report_to_members(db, group_id, period.period_key, pdf_path, filename)
    if emails_sent:
        logger.info("Emailed monthly report to %d member(s) in group %s", emails_sent, group_id)

    return period, emails_sent


@app.post("/api/groups/{group_id}/periods/{period_key}/generate-report")
def generate_report(
    group_id: int,
    period_key: str,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    period = db.query(models.MonthlyPeriod).filter_by(group_id=group_id, period_key=period_key).first()
    if not period:
        raise HTTPException(404, "Period not found")
    period, emails_sent = _generate_report_for_period(db, group_id, period)
    return {
        **schemas.PeriodOut.model_validate(period).model_dump(),
        "emails_sent": emails_sent,
    }


@app.get("/api/groups/{group_id}/periods/{period_key}/report")
def download_report(
    group_id: int,
    period_key: str,
    account: models.Account = Depends(get_current_account),
    db: Session = Depends(get_db),
):
    require_group_member(group_id, account, db)
    period = db.query(models.MonthlyPeriod).filter_by(group_id=group_id, period_key=period_key).first()
    if not period or not period.pdf_path or not os.path.exists(period.pdf_path):
        raise HTTPException(404, "Report not generated yet")
    return FileResponse(period.pdf_path, filename=period.pdf_filename, media_type="application/pdf")


# ---------------------------------------------------------------------------
# Month-end cron
# ---------------------------------------------------------------------------
def _cron_month_end_job():
    from .database import SessionLocal
    today = datetime.now().date()
    last_day = monthrange(today.year, today.month)[1]
    if today.day != last_day:
        return
    period_key = _period_key_for(today)
    db = SessionLocal()
    try:
        periods = (
            db.query(models.MonthlyPeriod)
            .filter_by(period_key=period_key, status="ACTIVE")
            .all()
        )
        for period in periods:
            logger.info("Cron: auto-generating month-end report for group %s period %s", period.group_id, period_key)
            _, emails_sent = _generate_report_for_period(db, period.group_id, period)
            if emails_sent:
                logger.info("Cron: emailed report to %d member(s) for group %s", emails_sent, period.group_id)
    except Exception:
        logger.exception("Cron month-end job failed")
    finally:
        db.close()


scheduler = BackgroundScheduler()
scheduler.add_job(_cron_month_end_job, "cron", hour=23, minute=55, id="month_end_settlement")


@app.on_event("startup")
def _startup():
    if SECRET_KEY == "dev-only-change-me-in-production" and not IS_DEVELOPMENT:
        logger.warning("SECRET_KEY is not set — set it in your .env before going live")
    if DISABLE_SCHEDULER:
        logger.info("Background scheduler disabled (DISABLE_SCHEDULER=true).")
        return
    scheduler.start()
    logger.info("Scheduler started: month-end settlement runs daily at 23:55 on last day of month.")


@app.on_event("shutdown")
def _shutdown():
    if not DISABLE_SCHEDULER:
        scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
