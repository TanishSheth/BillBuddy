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
    password: str = Field(min_length=6, max_length=128)
    display_name: str = Field(min_length=1, max_length=100)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=10)
    password: str = Field(min_length=6, max_length=128)


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
    new_password: Optional[str] = Field(default=None, min_length=6, max_length=128)


class MeOut(BaseModel):
    account: AccountOut
    groups: List["GroupOut"]


# ---------- Groups ----------
class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    currency: str = Field(default="USD", max_length=10)


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


class InviteByEmailRequest(BaseModel):
    email: EmailStr


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
