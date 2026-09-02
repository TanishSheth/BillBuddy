from datetime import date, datetime
from typing import List, Optional
from pydantic import BaseModel, EmailStr, Field, ConfigDict


# ---------- Auth ----------
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)
    display_name: str = Field(min_length=1, max_length=100)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=10)
    password: str = Field(min_length=6)


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


class MeOut(BaseModel):
    account: AccountOut
    groups: List["GroupOut"]


# ---------- Groups ----------
class GroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)


class GroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    invite_code: str
    created_at: datetime


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


class InviteByEmailRequest(BaseModel):
    email: EmailStr


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


class ExtractionResponse(BaseModel):
    upload_id: str
    image_url: str
    extraction: ReceiptExtraction
    source: str
    message: Optional[str] = None


# ---------- Expenses ----------
class ExpenseCreate(BaseModel):
    upload_id: Optional[str] = None
    store_name: str
    bill_date: date
    total_amount: float
    payer_member_id: int
    split_with: List[int]
    period_key: Optional[str] = None


class ExpenseUpdate(BaseModel):
    store_name: str
    bill_date: date
    total_amount: float
    payer_member_id: int
    split_with: List[int]
    period_key: Optional[str] = None


class SplitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    member_id: int
    share_amount: float


class ExpenseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    store_name: str
    bill_date: date
    total_amount: float
    payer_member_id: int
    image_path: Optional[str]
    created_at: datetime
    splits: List[SplitOut]


# ---------- Balances / Settlements ----------
class BalanceOut(BaseModel):
    member_id: int
    name: str
    total_paid: float
    total_share_owed: float
    net_balance: float


class SettlementOut(BaseModel):
    from_member_id: int
    from_name: str
    to_member_id: int
    to_name: str
    amount: float


class PeriodOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    period_key: str
    status: str
    pdf_filename: Optional[str]
    settled_at: Optional[datetime]


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


class AccountSettingsUpdate(BaseModel):
    use_own_vlm: Optional[bool] = None
    vlm_provider: Optional[str] = None
    vlm_model: Optional[str] = None
    vlm_api_key: Optional[str] = None
    clear_api_key: Optional[bool] = None
    email_monthly_reports: Optional[bool] = None
    compact_list_view: Optional[bool] = None


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
