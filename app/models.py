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
    vendors = relationship("AccountVendor", back_populates="account", cascade="all, delete-orphan")


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
    ocr_store_name = Column(String(150), nullable=True)
    ocr_phones = Column(String(200), nullable=True)
    ocr_store_ids = Column(String(80), nullable=True)
    ocr_header_key = Column(String(200), nullable=True)
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
    comments = relationship("ExpenseComment", back_populates="expense", cascade="all, delete-orphan")


class ExpenseSplit(Base):
    __tablename__ = "expense_splits"

    id = Column(Integer, primary_key=True, index=True)
    expense_id = Column(Integer, ForeignKey("expenses.id", ondelete="CASCADE"))
    member_id = Column(Integer, ForeignKey("group_members.id", ondelete="CASCADE"))
    share_amount = Column(Numeric(10, 2), nullable=False)
    split_value = Column(Numeric(10, 2), nullable=True)  # Stores %, exact, or share ratio entered

    expense = relationship("Expense", back_populates="splits")
    member = relationship("GroupMember", back_populates="splits")


class ExpenseComment(Base):
    __tablename__ = "expense_comments"

    id = Column(Integer, primary_key=True, index=True)
    expense_id = Column(Integer, ForeignKey("expenses.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, nullable=True)

    expense = relationship("Expense", back_populates="comments")
    account = relationship("Account")


class GroupPayment(Base):
    """A recorded group transfer. Confirmed payments reduce live balances."""
    __tablename__ = "group_payments"

    id = Column(Integer, primary_key=True, index=True)
    period_id = Column(Integer, ForeignKey("monthly_periods.id", ondelete="CASCADE"), nullable=False, index=True)
    payer_member_id = Column(Integer, ForeignKey("group_members.id", ondelete="RESTRICT"), nullable=False)
    recipient_member_id = Column(Integer, ForeignKey("group_members.id", ondelete="RESTRICT"), nullable=False)
    amount = Column(Numeric(10, 2), nullable=False)
    currency = Column(String(10), nullable=False, default="USD")
    status = Column(String(20), nullable=False, default="PENDING")  # PENDING, PAID, CONFIRMED, DECLINED
    # Last expense in the ledger when this transfer was recorded. This keeps
    # later expenses from being included in an earlier settlement.
    expense_cutoff_id = Column(Integer, nullable=True)
    marked_paid_at = Column(DateTime, nullable=True)
    confirmed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    period = relationship("MonthlyPeriod")
    payer = relationship("GroupMember", foreign_keys=[payer_member_id])
    recipient = relationship("GroupMember", foreign_keys=[recipient_member_id])


class GroupActivity(Base):
    __tablename__ = "group_activities"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    actor_account_id = Column(Integer, ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)
    event_type = Column(String(50), nullable=False)
    summary = Column(String(300), nullable=False)
    entity_type = Column(String(50), nullable=True)
    entity_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), index=True)

    actor = relationship("Account")


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=True, index=True)
    message = Column(String(300), nullable=False)
    link = Column(String(300), nullable=True)
    read_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), index=True)

    account = relationship("Account")


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


# A QuickSplit is a one-off bill shared directly with people, without asking
# the creator to organise an ongoing group first.
class QuickSplit(Base):
    __tablename__ = "quick_splits"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(150), nullable=False)
    total_amount = Column(Numeric(10, 2), nullable=False)
    currency = Column(String(10), default="USD", nullable=False)
    bill_date = Column(Date, nullable=False)
    category = Column(String(50), default="General")
    split_type = Column(String(20), default="EQUAL", nullable=False)
    payer_share_amount = Column(Numeric(10, 2), default=0, nullable=False)
    created_by_account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True)
    payer_account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    invite_code = Column(String(64), unique=True, nullable=False, default=generate_invite_code)
    status = Column(String(20), default="OPEN", nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    creator = relationship("Account", foreign_keys=[created_by_account_id])
    payer = relationship("Account", foreign_keys=[payer_account_id])
    participants = relationship("QuickSplitParticipant", back_populates="quick_split", cascade="all, delete-orphan")


class QuickSplitParticipant(Base):
    __tablename__ = "quick_split_participants"
    __table_args__ = (UniqueConstraint("quick_split_id", "email", name="uq_quick_split_email"),)

    id = Column(Integer, primary_key=True, index=True)
    quick_split_id = Column(Integer, ForeignKey("quick_splits.id", ondelete="CASCADE"), nullable=False)
    email = Column(String(255), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)
    display_name = Column(String(100), nullable=True)
    share_amount = Column(Numeric(10, 2), nullable=False)
    split_value = Column(Numeric(10, 2), nullable=True)
    payment_status = Column(String(20), default="PENDING", nullable=False)
    payment_marked_at = Column(DateTime, nullable=True)
    invite_token = Column(String(64), unique=True, nullable=True, default=generate_invite_code)

    quick_split = relationship("QuickSplit", back_populates="participants")
    account = relationship("Account", foreign_keys=[account_id])


class FriendMonthlySettlement(Base):
    """A two-person monthly settlement that needs confirmation from both sides."""
    __tablename__ = "friend_monthly_settlements"
    __table_args__ = (UniqueConstraint("account_one_id", "account_two_id", "month_key", name="uq_friend_month"),)

    id = Column(Integer, primary_key=True, index=True)
    account_one_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    account_two_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    month_key = Column(String(7), nullable=False)
    payer_account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    recipient_account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    amount = Column(Numeric(10, 2), nullable=False)
    currency = Column(String(10), nullable=False, default="USD")
    payer_confirmed = Column(Boolean, default=False, nullable=False)
    recipient_confirmed = Column(Boolean, default=False, nullable=False)
    status = Column(String(20), default="PENDING", nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    settled_at = Column(DateTime, nullable=True)


class FriendConnection(Base):
    """A durable, mutual friendship; REMOVED prevents a group re-adding it."""
    __tablename__ = "friend_connections"
    __table_args__ = (UniqueConstraint("account_one_id", "account_two_id", name="uq_friend_connection"),)

    id = Column(Integer, primary_key=True, index=True)
    account_one_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    account_two_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String(20), default="ACTIVE", nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class FriendInvite(Base):
    __tablename__ = "friend_invites"
    __table_args__ = (UniqueConstraint("invite_code", name="uq_friend_invite_code"),)

    id = Column(Integer, primary_key=True, index=True)
    inviter_account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    email = Column(String(255), nullable=True, index=True)
    invite_code = Column(String(64), nullable=False, default=generate_invite_code)
    status = Column(String(20), default="PENDING", nullable=False)
    created_at = Column(DateTime, server_default=func.now())


class AccountVendor(Base):
    """Per-account gazetteer: trusted store names learned from saved bills."""
    __tablename__ = "account_vendors"

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(150), nullable=False)
    name_key = Column(String(80), nullable=False, default="", index=True)
    phone = Column(String(20), nullable=False, default="")
    store_id = Column(String(20), nullable=False, default="")
    header_key = Column(String(200), nullable=False, default="")
    category = Column(String(50), nullable=False, default="General")
    created_at = Column(DateTime, server_default=func.now())

    account = relationship("Account", back_populates="vendors")
