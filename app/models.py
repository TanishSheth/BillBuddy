import secrets
from sqlalchemy import (
    Column, Integer, String, Numeric, Date, DateTime, Boolean, ForeignKey, UniqueConstraint, Text, func
)
from sqlalchemy.orm import relationship
from .database import Base


def generate_invite_code() -> str:
    return secrets.token_urlsafe(16)


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    display_name = Column(String(100), nullable=False)
    avatar_path = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    memberships = relationship("GroupMember", back_populates="account")
    groups_created = relationship("Group", back_populates="creator")
    settings = relationship("AccountSettings", back_populates="account", uselist=False)
    password_reset_tokens = relationship("PasswordResetToken", back_populates="account")
    personal_expenses = relationship("PersonalExpense", back_populates="account", cascade="all, delete-orphan")


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    token = Column(String(64), unique=True, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    account = relationship("Account", back_populates="password_reset_tokens")


class AccountSettings(Base):
    __tablename__ = "account_settings"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), unique=True, nullable=False)
    use_own_vlm = Column(Boolean, default=False)
    vlm_provider = Column(String(50), default="google")
    vlm_model = Column(String(100), default="gemini-2.5-flash")
    vlm_api_key_encrypted = Column(String(500), nullable=True)
    email_monthly_reports = Column(Boolean, default=True)
    compact_list_view = Column("compact_ledger_view", Boolean, default=False)
    preferred_currency = Column(String(10), default="USD")
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    account = relationship("Account", back_populates="settings")


class Group(Base):
    __tablename__ = "groups"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    currency = Column(String(10), default="USD", nullable=False)
    invite_code = Column(String(64), unique=True, nullable=False, default=generate_invite_code)
    created_by_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    cover_image_path = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    creator = relationship("Account", back_populates="groups_created")
    members = relationship("GroupMember", back_populates="group", cascade="all, delete-orphan")
    invites = relationship("GroupInvite", back_populates="group", cascade="all, delete-orphan")
    periods = relationship("MonthlyPeriod", back_populates="group", cascade="all, delete-orphan")
    pending_uploads = relationship("PendingUpload", back_populates="group", cascade="all, delete-orphan")


class GroupMember(Base):
    __tablename__ = "group_members"
    __table_args__ = (UniqueConstraint("group_id", "account_id", name="uq_group_account"),)

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    display_name = Column(String(100), nullable=False)
    role = Column(String(20), default="member")
    is_active = Column(Boolean, default=True, nullable=False)
    joined_at = Column(DateTime, server_default=func.now())

    group = relationship("Group", back_populates="members")
    account = relationship("Account", back_populates="memberships")
    expenses_paid = relationship("Expense", back_populates="payer")
    splits = relationship("ExpenseSplit", back_populates="member")


class GroupMemberSettings(Base):
    __tablename__ = "group_member_settings"
    __table_args__ = (UniqueConstraint("group_id", "account_id", name="uq_group_member_settings"),)

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    override_global_vlm = Column(Boolean, default=False)
    vlm_provider = Column(String(50), default="google")
    vlm_model = Column(String(100), default="gemini-2.5-flash")
    vlm_api_key_encrypted = Column(String(500), nullable=True)
    equal_split_default = Column(Boolean, default=True)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class GroupInvite(Base):
    __tablename__ = "group_invites"
    __table_args__ = (UniqueConstraint("group_id", "email", name="uq_group_invite_email"),)

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    email = Column(String(255), nullable=False)
    status = Column(String(20), default="pending")
    created_at = Column(DateTime, server_default=func.now())

    group = relationship("Group", back_populates="invites")


class PendingUpload(Base):
    __tablename__ = "pending_uploads"

    id = Column(String(64), primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True)
    file_path = Column(String, nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    group = relationship("Group", back_populates="pending_uploads")


class MonthlyPeriod(Base):
    __tablename__ = "monthly_periods"
    __table_args__ = (UniqueConstraint("group_id", "period_key", name="uq_group_period"),)

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    period_key = Column(String(7), nullable=False)
    status = Column(String(20), default="ACTIVE")
    pdf_filename = Column(String, nullable=True)
    pdf_path = Column(String, nullable=True)
    target_currency = Column(String(10), nullable=True)
    settlement_fx_rate = Column(Numeric(12, 6), nullable=True)
    settled_at = Column(DateTime, nullable=True)

    group = relationship("Group", back_populates="periods")
    expenses = relationship("Expense", back_populates="period")


class Expense(Base):
    __tablename__ = "expenses"

    id = Column(Integer, primary_key=True, index=True)
    period_id = Column(Integer, ForeignKey("monthly_periods.id"))
    store_name = Column(String(150), nullable=False)
    bill_date = Column(Date, nullable=False)
    total_amount = Column(Numeric(10, 2), nullable=False)
    currency = Column(String(10), default="USD")
    category = Column(String(50), default="General")
    split_type = Column(String(20), default="EQUAL")  # EQUAL, PERCENTAGE, EXACT, SHARES
    fx_rate_to_group = Column(Numeric(12, 6), default=1.0)
    payer_member_id = Column(Integer, ForeignKey("group_members.id", ondelete="CASCADE"))
    image_path = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    period = relationship("MonthlyPeriod", back_populates="expenses")
    payer = relationship("GroupMember", back_populates="expenses_paid")
    splits = relationship("ExpenseSplit", back_populates="expense", cascade="all, delete-orphan")


class ExpenseSplit(Base):
    __tablename__ = "expense_splits"

    id = Column(Integer, primary_key=True, index=True)
    expense_id = Column(Integer, ForeignKey("expenses.id", ondelete="CASCADE"))
    member_id = Column(Integer, ForeignKey("group_members.id", ondelete="CASCADE"))
    share_amount = Column(Numeric(10, 2), nullable=False)
    split_value = Column(Numeric(10, 2), nullable=True)  # Stores %, exact, or share ratio entered

    expense = relationship("Expense", back_populates="splits")
    member = relationship("GroupMember", back_populates="splits")


class PersonalExpense(Base):
    __tablename__ = "personal_expenses"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    store_name = Column(String(150), nullable=False)
    bill_date = Column(Date, nullable=False)
    total_amount = Column(Numeric(10, 2), nullable=False)
    currency = Column(String(10), default="USD", nullable=False)
    category = Column(String(50), default="General", nullable=False)
    notes = Column(Text, nullable=True)
    image_path = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    account = relationship("Account", back_populates="personal_expenses")
