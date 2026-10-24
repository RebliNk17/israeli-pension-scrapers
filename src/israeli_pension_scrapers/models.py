"""Unified, provider-agnostic data models and JSON schema for Israeli pension & savings scrapers.

Designed so that any Israeli institutional provider (HaPhoenix, Menora, Harel, Migdal, Clal,
Meitav, Altshuler Shaham, Analyst, Mor, or the Mislaka clearing house) normalizes its output
into the exact same JSON structure without provider-specific schema drift.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class ProductType(str, Enum):
    """Normalized Israeli long-term savings / pension product types."""

    PENSION = "pension"  # קרן פנסיה (מקיפה / משלימה / כללית / ותיקה)
    HISHTALMUT = "hishtalmut"  # קרן השתלמות
    GEMEL = "gemel"  # קופת גמל לתגמולים / קצבה
    GEMEL_INVESTMENT = "gemel_investment"  # קופת גמל להשקעה
    FINANCIAL_SAVINGS = "financial_savings"  # פוליסת חיסכון / השקעה
    PIZUIM = "pizuim"  # קופת פיצויים אישית
    CHILD_SAVINGS = "child_savings"  # חיסכון לכל ילד
    LIFE_SAVINGS = "life_savings"  # ביטוח חיים עם חיסכון / ביטוח מנהלים
    UNKNOWN = "unknown"


class PensionType(str, Enum):
    """Sub-type for pension funds."""

    MAKIFA = "makifa"  # פנסיה מקיפה
    MASHLIMA = "mashlima"  # פנסיה משלימה / כללית
    VATIKA = "vatika"  # פנסיה ותיקה
    OTHER = "other"


class PolicyStatus(str, Enum):
    """Lifecycle status of a policy/fund."""

    ACTIVE = "active"  # פעילה (הפקדות שוטפות או יתרה פעילה)
    INACTIVE = "inactive"  # מסולקת / מוקפאת (ללא הפקדות פעילות)
    EXPIRED = "expired"  # פגה / נפדתה


class EmploymentStatus(str, Enum):
    """Saver's employment status on the policy."""

    EMPLOYEE = "employee"  # שכיר
    SELF_EMPLOYED = "self_employed"  # עצמאי
    OTHER = "other"


class LoginResult(str, Enum):
    """Outcome of the authentication step."""

    SUCCESS = "success"
    INVALID_CREDENTIALS = "invalid_credentials"
    INVALID_OTP = "invalid_otp"
    ACCOUNT_BLOCKED = "account_blocked"
    UNKNOWN_ERROR = "unknown_error"


@dataclass
class InsuredProfile:
    """Normalized personal details of the policy owner."""

    id_number: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    birth_date: str | None = None  # YYYY-MM-DD
    gender: str | None = None  # "M" | "F" | None
    city: str | None = None
    street: str | None = None
    house_number: str | None = None


@dataclass
class BalancePoint:
    """A single point in a fund or portfolio historical balance time-series."""

    date: str  # YYYY-MM-DD
    balance: float
    currency: str = "ILS"


@dataclass
class ExposureBucket:
    """An asset-allocation exposure slice (e.g. Israel vs. Abroad, FX vs. ILS)."""

    id: str
    label: str
    percentage: float
    amount: float | None = None


@dataclass
class AssetExposure:
    """Portfolio or fund-level asset exposure breakdown."""

    geographic: list[ExposureBucket] = field(default_factory=list)
    currency: list[ExposureBucket] = field(default_factory=list)
    equity_pct: float | None = None


@dataclass
class PortfolioSummary:
    """High-level summary across all funds held at the provider."""

    total_balance: float
    liquid_balance: float | None = None
    currency: str = "ILS"
    balance_history: list[BalancePoint] = field(default_factory=list)
    asset_exposure: AssetExposure | None = None


@dataclass
class ManagementFees:
    """Normalized management fee structure (דמי ניהול)."""

    from_deposit_pct: float | None = None  # דמי ניהול מהפקדה (%)
    from_savings_pct: float | None = None  # דמי ניהול מצבירה/חיסכון (%)
    investment_expense_pct: float | None = None  # הוצאות ניהול השקעות (%)
    discount_valid_from: str | None = None  # YYYY-MM-DD
    discount_valid_until: str | None = None  # YYYY-MM-DD


@dataclass
class InvestmentTrack:
    """A single investment track / route (מסלול השקעה) within a fund."""

    name: str
    code: str | None = None
    allocation_pct: float | None = None
    balance: float | None = None
    currency: str = "ILS"
    yield_ytd_pct: float | None = None
    yield_as_of_date: str | None = None  # YYYY-MM-DD
    fee_deposit_pct: float | None = None
    fee_savings_pct: float | None = None
    investment_expense_pct: float | None = None


@dataclass
class DepositTransaction:
    """A single monthly or one-off deposit into a pension/hishtalmut/gemel fund."""

    identifier: str
    date: str  # YYYY-MM-DD (actual deposit / value date)
    total_amount: float
    currency: str = "ILS"
    salary_month: str | None = None  # YYYY-MM (חודש שכר)
    employer_name: str | None = None
    payment_method: str | None = None
    employee_amount: float | None = None  # תגמולי עובד
    employer_amount: float | None = None  # תגמולי מעסיק
    compensation_amount: float | None = None  # פיצויים
    disability_amount: float | None = None  # אובדן כושר עבודה
    insured_salary: float | None = None  # שכר מבוטח / שכר מחושב
    description: str = "הפקדה"


@dataclass
class MovementItem:
    """Raw line item from a yearly movement statement."""

    title: str
    subtitle: str | None = None
    amount: float | None = None


@dataclass
class YearlyMovementSummary:
    """Annual movement summary (יתרת פתיחה, הפקדות, רווחים, דמי ניהול, עלות ביטוח, יתרת סגירה)."""

    year: str
    as_of_date: str | None = None  # YYYY-MM-DD
    opening_balance: float | None = None
    deposits: float | None = None
    gains: float | None = None
    management_fees: float | None = None
    insurance_costs: float | None = None
    closing_balance: float | None = None
    items: list[MovementItem] = field(default_factory=list)


@dataclass
class InsuranceCover:
    """Insurance coverage attached to a pension/managers policy (נכות / שארים / ריסק)."""

    title: str
    description: str | None = None
    insured_amount: float | None = None
    monthly_cost: float | None = None


@dataclass
class ExpectedPayment:
    """Projected payout / lump-sum availability item."""

    title: str
    subtitle: str | None = None
    amount: float | None = None
    currency: str = "ILS"


@dataclass
class FundPolicy:
    """Unified representation of a single pension, hishtalmut, gemel, or savings policy."""

    policy_id: str  # Normalized stable ID (e.g. "001-234-567890")
    raw_policy_id: str  # Raw display ID from provider (e.g. "001-234-567890 (01234567)")
    product_type: ProductType
    name: str
    manufacturer: str
    balance: float
    currency: str = "ILS"
    internal_account_id: str | None = None
    pension_type: PensionType | None = None
    status: PolicyStatus = PolicyStatus.ACTIVE
    employment_status: EmploymentStatus | None = None
    employer_name: str | None = None
    agent_name: str | None = None
    as_of_balance: float | None = None
    liquid_balance: float | None = None
    taxable_balance: float | None = None
    balance_date: str | None = None  # YYYY-MM-DD
    data_as_of_date: str | None = None  # YYYY-MM-DD
    establishment_date: str | None = None  # YYYY-MM-DD (תאריך הצטרפות / ותק)
    start_date: str | None = None  # YYYY-MM-DD
    liquidity_date: str | None = None  # YYYY-MM-DD (תאריך נזילות)
    is_independent: bool = False  # IRA / קופה בניהול אישי
    is_section_14: bool | None = None  # סעיף 14
    fees: ManagementFees = field(default_factory=ManagementFees)
    tracks: list[InvestmentTrack] = field(default_factory=list)
    deposits: list[DepositTransaction] = field(default_factory=list)
    yearly_movements: list[YearlyMovementSummary] = field(default_factory=list)
    insurance_covers: list[InsuranceCover] = field(default_factory=list)
    expected_payments: list[ExpectedPayment] = field(default_factory=list)
    balance_history: list[BalancePoint] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


def _serialize_value(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _serialize_value(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _serialize_value(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize_value(v) for v in obj]
    return obj


@dataclass
class PensionScrapingResult:
    """Top-level unified result returned by every provider scraper."""

    success: bool
    provider: str
    scraped_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    error_type: str | None = None
    error_message: str | None = None
    refreshed_long_term_token: str | None = None
    insured: InsuredProfile | None = None
    portfolio_summary: PortfolioSummary | None = None
    funds: list[FundPolicy] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert the scraping result into a plain JSON-serializable dictionary."""
        return _serialize_value(self)

    def to_json(self, *, indent: int | None = 2) -> str:
        """Serialize the scraping result to a UTF-8 JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)
