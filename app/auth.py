import os
import re
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

try:
    from jose import JWTError, jwt
except ImportError:
    import jwt
    JWTError = jwt.PyJWTError

from .config import ACCESS_TOKEN_EXPIRE_DAYS, SECRET_KEY
from .database import get_db
from . import models

ALGORITHM = "HS256"

# New / changed passwords must meet this policy. Existing weaker passwords
# can still sign in until the user resets or updates them.
PASSWORD_MIN_LENGTH = 8
PASSWORD_HINT = (
    "Password must be at least 8 characters and include 1 uppercase letter, "
    "2 numbers, and 1 special character."
)

bearer_scheme = HTTPBearer(auto_error=False)


def validate_password_strength(password: str) -> str | None:
    """Return an error message if the password is too weak, else None."""
    value = (password or "").strip()
    if len(value) < PASSWORD_MIN_LENGTH:
        return PASSWORD_HINT
    if not re.search(r"[A-Z]", value):
        return PASSWORD_HINT
    if len(re.findall(r"\d", value)) < 2:
        return PASSWORD_HINT
    if not re.search(r"[^A-Za-z0-9]", value):
        return PASSWORD_HINT
    return None


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_access_token(account_id: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    payload = {"sub": str(account_id), "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def get_current_account(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> models.Account:
    if not credentials or not credentials.credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        account_id = int(payload.get("sub", 0))
    except (JWTError, ValueError, Exception):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    account = db.query(models.Account).get(account_id)
    if not account:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account not found")
    return account


def process_pending_invites(db: Session, account: models.Account) -> list[models.GroupMember]:
    added: list[models.GroupMember] = []
    invites = (
        db.query(models.GroupInvite)
        .filter_by(email=account.email.lower(), status="pending")
        .all()
    )
    for invite in invites:
        existing = (
            db.query(models.GroupMember)
            .filter_by(group_id=invite.group_id, account_id=account.id)
            .first()
        )
        if not existing:
            member = models.GroupMember(
                group_id=invite.group_id,
                account_id=account.id,
                display_name=account.display_name,
                role="member",
            )
            db.add(member)
            added.append(member)
        invite.status = "accepted"
    if invites:
        db.commit()
    # Quick Split invitations do not create a group membership. Claim any
    # pending shares addressed to this account as soon as they sign in.
    quick_participants = (
        db.query(models.QuickSplitParticipant)
        .filter(
            models.QuickSplitParticipant.email == account.email.lower(),
            models.QuickSplitParticipant.account_id.is_(None),
        )
        .all()
    )
    if quick_participants:
        for participant in quick_participants:
            participant.account_id = account.id
            participant.display_name = account.display_name
        db.commit()
    return added
