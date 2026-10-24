"""Public API for israeli-pension-scrapers."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from israeli_pension_scrapers.base import OTP_CANCEL_SENTINEL, BasePensionScraper, ScraperOptions
from israeli_pension_scrapers.exceptions import (
    InvalidCredentialsError,
    InvalidOtpError,
    OtpCanceledError,
    PensionScraperError,
    ScraperErrorType,
    TokenExpiredError,
)
from israeli_pension_scrapers.models import (
    AssetExposure,
    BalancePoint,
    DepositTransaction,
    EmploymentStatus,
    ExpectedPayment,
    ExposureBucket,
    FundPolicy,
    InsuranceCover,
    InsuredProfile,
    InvestmentTrack,
    LoginResult,
    ManagementFees,
    MovementItem,
    PensionScrapingResult,
    PensionType,
    PolicyStatus,
    PortfolioSummary,
    ProductType,
    YearlyMovementSummary,
)
from israeli_pension_scrapers.providers.haphoenix import HaPhoenixScraper
from israeli_pension_scrapers.providers.migdal import MigdalScraper, encrypt_migdal_pin_token


@dataclass(frozen=True)
class ProviderMetadata:
    key: str
    display_name: str
    hebrew_name: str
    login_fields: tuple[str, ...]
    supports_otp: bool = True
    supports_long_term_token: bool = True


PROVIDERS: dict[str, ProviderMetadata] = {
    "haphoenix": ProviderMetadata(
        key="haphoenix",
        display_name="HaPhoenix",
        hebrew_name="הפניקס",
        login_fields=("id", "phoneNumber"),
        supports_otp=True,
        supports_long_term_token=True,
    ),
    "migdal": ProviderMetadata(
        key="migdal",
        display_name="Migdal",
        hebrew_name="מגדל",
        login_fields=("id", "phoneNumber"),
        supports_otp=True,
        supports_long_term_token=True,
    ),
}

_ALIASES: dict[str, str] = {
    "haphoenix": "haphoenix",
    "hafenix": "haphoenix",
    "phoenix": "haphoenix",
    "fnx": "haphoenix",
    "migdal": "migdal",
}


def create_scraper(
    provider: str,
    credentials: dict,
    options: ScraperOptions | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> BasePensionScraper:
    """Create a pension/savings scraper instance for the given provider key."""
    canonical = _ALIASES.get((provider or "").strip().lower())
    if canonical == "haphoenix":
        return HaPhoenixScraper(credentials, options, client=client)
    if canonical == "migdal":
        return MigdalScraper(credentials, options, client=client)
    raise ValueError(f"Unknown pension provider: {provider!r}. Supported: {sorted(PROVIDERS)}")


__all__ = [
    "OTP_CANCEL_SENTINEL",
    "PROVIDERS",
    "AssetExposure",
    "BalancePoint",
    "BasePensionScraper",
    "DepositTransaction",
    "EmploymentStatus",
    "ExpectedPayment",
    "ExposureBucket",
    "FundPolicy",
    "HaPhoenixScraper",
    "InsuranceCover",
    "InsuredProfile",
    "InvalidCredentialsError",
    "InvalidOtpError",
    "InvestmentTrack",
    "LoginResult",
    "ManagementFees",
    "MigdalScraper",
    "MovementItem",
    "OtpCanceledError",
    "PensionScraperError",
    "PensionScrapingResult",
    "PensionType",
    "PolicyStatus",
    "PortfolioSummary",
    "ProductType",
    "ProviderMetadata",
    "ScraperErrorType",
    "ScraperOptions",
    "TokenExpiredError",
    "YearlyMovementSummary",
    "create_scraper",
    "encrypt_migdal_pin_token",
]
