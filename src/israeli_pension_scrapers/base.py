"""Abstract base scraper orchestrating the lifecycle for all pension/savings providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from israeli_pension_scrapers.exceptions import OtpCanceledError, PensionScraperError
from israeli_pension_scrapers.models import (
    FundPolicy,
    InsuredProfile,
    LoginResult,
    PensionScrapingResult,
    PortfolioSummary,
)

OTP_CANCEL_SENTINEL = "cancel"


@dataclass
class ScraperOptions:
    """Runtime options for a pension scraper execution."""

    timeout: float = 30.0
    proxy: str | None = None
    include_balance_history: bool = True
    include_policy_details: bool = True


class BasePensionScraper(ABC):
    """Base class for HTTP API-based Israeli pension & savings scrapers."""

    provider_name: str = "unknown"

    def __init__(
        self,
        credentials: dict,
        options: ScraperOptions | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.credentials = dict(credentials)
        self.options = options or ScraperOptions()
        self._external_client = client is not None
        self.client: httpx.AsyncClient | None = client
        self.on_progress: Callable[[str], None] | None = None
        self.on_otp_request: Callable[[], Awaitable[str]] | None = None
        self.refreshed_long_term_token: str | None = None
        self.insured: InsuredProfile | None = None
        self.portfolio_summary: PortfolioSummary | None = None
        self._login_error_detail: str | None = None

    def _emit_progress(self, stage: str) -> None:
        if self.on_progress is not None:
            self.on_progress(stage)

    def _fail_login(self, result: LoginResult, detail: str | Exception) -> LoginResult:
        self._login_error_detail = str(detail)
        return result

    async def initialize(self) -> None:
        """Create the async HTTP client if one was not injected."""
        if self.client is None:
            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.options.timeout),
                follow_redirects=True,
                proxy=self.options.proxy,
            )

    async def terminate(self) -> None:
        """Close the HTTP client unless it was injected externally."""
        if self.client is not None and not self._external_client:
            await self.client.aclose()
            self.client = None

    @abstractmethod
    async def login(self) -> LoginResult:
        """Authenticate against the provider (via refresh token or OTP)."""

    @abstractmethod
    async def fetch_data(self) -> list[FundPolicy]:
        """Fetch all pension, hishtalmut, gemel, and savings policies."""

    async def scrape(self) -> PensionScrapingResult:
        """Execute the full initialize -> login -> fetch_data -> terminate lifecycle."""
        self._emit_progress("initializing")
        try:
            await self.initialize()
        except Exception as exc:  # noqa: BLE001
            return PensionScrapingResult(
                success=False,
                provider=self.provider_name,
                error_type="INIT_ERROR",
                error_message=f"{type(exc).__name__}: {exc}",
            )

        try:
            self._emit_progress("logging in")
            login_result = await self.login()
            if login_result != LoginResult.SUCCESS:
                return PensionScrapingResult(
                    success=False,
                    provider=self.provider_name,
                    error_type=login_result.value.upper(),
                    error_message=self._login_error_detail or login_result.value,
                )

            self._emit_progress("fetching data")
            funds = await self.fetch_data()
            return PensionScrapingResult(
                success=True,
                provider=self.provider_name,
                refreshed_long_term_token=self.refreshed_long_term_token,
                insured=self.insured,
                portfolio_summary=self.portfolio_summary,
                funds=funds,
            )
        except TimeoutError as exc:
            return PensionScrapingResult(
                success=False,
                provider=self.provider_name,
                error_type="TIMEOUT",
                error_message=f"TimeoutError: {exc}",
            )
        except OtpCanceledError as exc:
            return PensionScrapingResult(
                success=False,
                provider=self.provider_name,
                error_type=exc.error_type.value,
                error_message=str(exc),
            )
        except PensionScraperError as exc:
            return PensionScrapingResult(
                success=False,
                provider=self.provider_name,
                error_type=exc.error_type.value,
                error_message=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            return PensionScrapingResult(
                success=False,
                provider=self.provider_name,
                error_type="GENERAL_ERROR",
                error_message=f"{type(exc).__name__}: {exc}",
            )
        finally:
            await self.terminate()
