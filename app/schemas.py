from datetime import date, datetime
from typing import List, Optional, Union, Dict
from pydantic import BaseModel, Field, ConfigDict

try:
    import email_validator  # noqa: F401
    from pydantic import EmailStr
except ImportError:
    EmailStr = str


# ---------- Auth ----------
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=100)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=10)
    password: str = Field(min_length=8, max_length=128)


class MessageResponse(BaseModel):
    message: str
    reset_url: Optional[str] = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    display_name: str
    has_avatar: bool = False


class AccountProfileUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    email: Optional[EmailStr] = None
    current_password: Optional[str] = None
    new_password: Optional[str] = Field(default=None, min_length=8, max_length=128)


class AccountDeletionRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)


class AccountRestoreRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class MeOut(BaseModel):
    account: AccountOut
    groups: List["GroupOut"]


# ---------- Groups ----------
class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    currency: str = Field(default="USD", max_length=10)
    friend_ids: list[int] = Field(default_factory=list)
    invite_emails: list[str] = Field(default_factory=list)


class GroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    currency: str = "USD"
    invite_code: str
    created_at: datetime
    has_cover: bool = False


class GroupUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=150)


class GroupListOut(GroupOut):
    role: str
    member_count: int


class GroupDetailOut(GroupOut):
    members: List["MemberOut"]


class MemberOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    display_name: str
    email: str
    role: str
    is_active: bool = True
    account_id: Optional[int] = None
    has_avatar: bool = False


class InviteByEmailRequest(BaseModel):
    email: EmailStr


class GroupMemberAddRequest(BaseModel):
    account_id: int


class InviteCreateOut(BaseModel):
    email: str
    email_sent: bool
    already_member: bool = False
    invite_url: str
    message: str


class InviteLinkOut(BaseModel):
    invite_code: str
    url: str


class JoinGroupRequest(BaseModel):
    invite_code: str


# ---------- Quick Splits ----------
class QuickSplitParticipantInput(BaseModel):
    email: Optional[EmailStr] = None
    account_id: Optional[int] = None
    label: Optional[str] = Field(default=None, max_length=100)
    value: Optional[float] = None


class QuickSplitCreate(BaseModel):
    title: str = Field(min_length=1, max_length=150)
    total_amount: float = Field(gt=0)
    currency: str = Field(default="USD", max_length=10)
    bill_date: date
    category: str = Field(default="General", max_length=50)
    split_type: str = Field(default="EQUAL", max_length=20)
    payer_value: Optional[float] = None
    payer_account_id: Optional[int] = None
    silent_invite: bool = False
    # A quick split is deliberately a one-to-one expense.  Shared expenses
    # with two or more other people belong in a group.
    participants: List[QuickSplitParticipantInput] = Field(min_length=1, max_length=1)


class QuickSplitUpdate(QuickSplitCreate):
    pass


class QuickSplitParticipantOut(BaseModel):
    id: int
    account_id: Optional[int] = None
    email: Optional[str] = None
    display_name: Optional[str] = None
    share_amount: float
    split_value: Optional[float] = None
    payment_status: str
    payment_marked_at: Optional[datetime] = None
    is_me: bool = False
    invite_token: Optional[str] = None
    has_avatar: bool = False


class QuickSplitJoinRequest(BaseModel):
    invite_code: str
    participant_token: Optional[str] = None


class QuickSplitOut(BaseModel):
    id: int
    title: str
    total_amount: float
    currency: str
    bill_date: date
    category: str
    split_type: str
    payer_share_amount: float
    payer_name: str
    payer_account_id: int
    payer_email: Optional[str] = None
    payer_has_avatar: bool = False
    invite_code: str
    status: str
    created_at: datetime
    is_payer: bool
    is_owner: bool
    participants: List[QuickSplitParticipantOut]


class FriendSettlementOut(BaseModel):
    month_key: str
    payer_account_id: int
    recipient_account_id: int
    amount: float
    currency: str
    payer_confirmed: bool
    recipient_confirmed: bool
    status: str
    settled_at: Optional[datetime] = None


class FriendSettlementConfirm(BaseModel):
    month_key: str = Field(pattern=r"^\d{4}-\d{2}$")
    action: str = Field(pattern=r"^(paid|received)$")


class PairPaymentCreate(BaseModel):
    amount: float = Field(gt=0)
    currency: str = Field(default="USD", min_length=3, max_length=10)


class PairPaymentOut(BaseModel):
    id: int
    payer_account_id: int
    recipient_account_id: int
    amount: float
    currency: str
    status: str
    reported_by_account_id: int
    created_at: datetime
    confirmed_at: Optional[datetime] = None


class ConsolidatedSourceOut(BaseModel):
    kind: str
    label: str
    amount: float
    currency: str
    period_key: Optional[str] = None
    group_id: Optional[int] = None


class ConsolidatedFriendBalanceOut(BaseModel):
    friend_id: int
    balance: float
    # The amount that can be recorded as a consolidated payment today. Live
    # group shares are reflected in balance but stay read-only until settled.
    settleable_balance: float
    currency: str
    payer_account_id: Optional[int] = None
    recipient_account_id: Optional[int] = None
    sources: List[ConsolidatedSourceOut]
    payments: List[PairPaymentOut]


class FriendGroupContributionOut(BaseModel):
    group_id: int
    group_name: str
    expense_id: int
    title: str
    bill_date: date
    period_key: str
    currency: str
    # Positive means the friend owes the current account; negative reverses it.
    pair_amount: float


class FriendOut(BaseModel):
    id: int
    display_name: str
    email: str
    has_avatar: bool = False
    source: str = "friend"


class FriendInviteRequest(BaseModel):
    email: Optional[EmailStr] = None


class FriendInviteOut(BaseModel):
    status: str
    email: Optional[str] = None
    invite_url: Optional[str] = None
    email_sent: bool = False
    message: str


# ---------- VLM Extraction ----------
class ReceiptExtraction(BaseModel):
    store_name: str = Field(description="Name of the store/merchant on the receipt")
    bill_date: str = Field(description="Date of purchase in YYYY-MM-DD format")
    total_amount: float = Field(description="Total amount charged, as a float with 2 decimals")
    currency: Optional[str] = Field(default="USD", description="Currency code (USD, INR, EUR, etc.)")
    category: Optional[str] = Field(default="General", description="Category of purchase")
    currency_confidence: float = Field(
        default=0,
        description="0-100. UI should keep the group/default currency unless this is at least 85.",
    )


class ExtractionResponse(BaseModel):
    upload_id: str
    image_url: str
    extraction: ReceiptExtraction
    source: str
    message: Optional[str] = None


# ---------- Custom Splits & Expenses ----------
class SplitDetailInput(BaseModel):
    member_id: int
    value: Optional[float] = None  # Percentage, exact amount, or share weight


class ExpenseCreate(BaseModel):
    upload_id: Optional[str] = None
    store_name: str
    bill_date: date
    total_amount: float
    currency: Optional[str] = "USD"
    category: Optional[str] = "General"
    split_type: Optional[str] = "EQUAL"  # EQUAL, PERCENTAGE, EXACT, SHARES
    payer_member_id: int
    split_with: Optional[List[int]] = None
    split_details: Optional[List[SplitDetailInput]] = None
    period_key: Optional[str] = None


class ExpenseUpdate(BaseModel):
    store_name: str
    bill_date: date
    total_amount: float
    currency: Optional[str] = "USD"
    category: Optional[str] = "General"
    split_type: Optional[str] = "EQUAL"
    payer_member_id: int
    split_with: Optional[List[int]] = None
    split_details: Optional[List[SplitDetailInput]] = None
    period_key: Optional[str] = None


class SplitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    member_id: int
    share_amount: float
    split_value: Optional[float] = None


class ExpenseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    store_name: str
    bill_date: date
    total_amount: float
    currency: str = "USD"
    category: str = "General"
    split_type: str = "EQUAL"
    payer_member_id: int
    image_path: Optional[str]
    created_at: datetime
    splits: List[SplitOut]


# ---------- Balances / Settlements / Currency Conversion ----------
class BalanceOut(BaseModel):
    member_id: int
    name: str
    total_paid: float
    total_share_owed: float
    net_balance: float
    settlement_paid: float = 0.0
    settlement_received: float = 0.0
    currency: str = "USD"
    converted_total_paid: Optional[float] = None
    converted_total_share_owed: Optional[float] = None
    converted_net_balance: Optional[float] = None
    converted_currency: Optional[str] = None


class SettlementOut(BaseModel):
    from_member_id: int
    from_name: str
    to_member_id: int
    to_name: str
    amount: float
    currency: str = "USD"
    converted_amount: Optional[float] = None
    converted_currency: Optional[str] = None
    exchange_rate: Optional[float] = None


class GroupPaymentCreate(BaseModel):
    payer_member_id: int
    recipient_member_id: int
    amount: float = Field(gt=0)
    currency: str = Field(default="USD", min_length=3, max_length=10)


class GroupPaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    payer_member_id: int
    recipient_member_id: int
    amount: float
    currency: str
    status: str
    marked_paid_at: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None


class ExpenseCommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=1000)


class ExpenseCommentOut(BaseModel):
    id: int
    body: str
    author_name: str
    created_at: datetime
    updated_at: Optional[datetime] = None


class GroupActivityOut(BaseModel):
    id: int
    event_type: str
    summary: str
    actor_name: Optional[str] = None
    entity_type: Optional[str] = None
    entity_id: Optional[int] = None
    created_at: datetime


class NotificationOut(BaseModel):
    id: int
    message: str
    link: Optional[str] = None
    read_at: Optional[datetime] = None
    created_at: datetime


class GroupDashboardOut(BaseModel):
    total_spent: float
    currency: str
    category_totals: Dict[str, float]
    member_totals: List[BalanceOut]
    largest_expenses: List[ExpenseOut]


class ConvertCurrencyRequest(BaseModel):
    target_currency: str = Field(min_length=3, max_length=10)
    custom_exchange_rate: Optional[float] = None
    clear: bool = False


class ExchangeRatesOut(BaseModel):
    base_currency: str
    rates: Dict[str, float]


class GroupSyncOut(BaseModel):
    version: str


class PeriodOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    period_key: str
    status: str
    pdf_filename: Optional[str]
    target_currency: Optional[str] = None
    settlement_fx_rate: Optional[float] = None
    settled_at: Optional[datetime]


# ---------- Personal Expenses ----------
class PersonalExpenseCreate(BaseModel):
    upload_id: Optional[str] = None
    store_name: str
    bill_date: date
    total_amount: float
    currency: str = "USD"
    category: str = "General"
    notes: Optional[str] = None


class PersonalExpenseUpdate(BaseModel):
    store_name: str
    bill_date: date
    total_amount: float
    currency: str = "USD"
    category: str = "General"
    notes: Optional[str] = None


class PersonalExpenseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    account_id: int
    store_name: str
    bill_date: date
    total_amount: float
    currency: str
    category: str
    notes: Optional[str]
    image_path: Optional[str]
    created_at: datetime


class PersonalMonthlySummaryOut(BaseModel):
    period_key: str
    total_spent: float
    currency: str
    category_breakdown: Dict[str, float]
    expense_count: int
    expenses: List[PersonalExpenseOut]


# ---------- Settings ----------
class VlmModelOption(BaseModel):
    id: str
    label: str


class VlmProviderOption(BaseModel):
    id: str
    label: str
    models: List[VlmModelOption]


class AccountSettingsOut(BaseModel):
    use_own_vlm: bool
    vlm_provider: str
    vlm_model: str
    has_api_key: bool
    email_monthly_reports: bool
    compact_list_view: bool
    preferred_currency: str = "USD"


class AccountSettingsUpdate(BaseModel):
    use_own_vlm: Optional[bool] = None
    vlm_provider: Optional[str] = None
    vlm_model: Optional[str] = None
    vlm_api_key: Optional[str] = None
    clear_api_key: Optional[bool] = None
    email_monthly_reports: Optional[bool] = None
    compact_list_view: Optional[bool] = None
    preferred_currency: Optional[str] = None


class GroupSettingsOut(BaseModel):
    override_global_vlm: bool
    vlm_provider: str
    vlm_model: str
    has_api_key: bool
    equal_split_default: bool


class GroupSettingsUpdate(BaseModel):
    override_global_vlm: Optional[bool] = None
    vlm_provider: Optional[str] = None
    vlm_model: Optional[str] = None
    vlm_api_key: Optional[str] = None
    clear_api_key: Optional[bool] = None
    equal_split_default: Optional[bool] = None
