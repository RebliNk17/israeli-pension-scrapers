"""Custom exception hierarchy for israeli-pension-scrapers."""

from __future__ import annotations

from enum import Enum


class ScraperErrorType(str, Enum):
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    INVALID_OTP = "INVALID_OTP"
    OTP_CANCELED = "OTP_CANCELED"
    OTP_TIMEOUT = "OTP_TIMEOUT"
    ACCOUNT_BLOCKED = "ACCOUNT_BLOCKED"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    NETWORK_ERROR = "NETWORK_ERROR"
    GENERAL_ERROR = "GENERAL_ERROR"


class PensionScraperError(Exception):
    """Base exception carrying a structured ScraperErrorType."""

    def __init__(
        self,
        message: str,
        error_type: ScraperErrorType = ScraperErrorType.GENERAL_ERROR,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type


class InvalidCredentialsError(PensionScraperError):
    def __init__(self, message: str = "Invalid credentials") -> None:
        super().__init__(message, ScraperErrorType.INVALID_CREDENTIALS)


class InvalidOtpError(PensionScraperError):
    def __init__(self, message: str = "Invalid OTP code") -> None:
        super().__init__(message, ScraperErrorType.INVALID_OTP)


class OtpCanceledError(PensionScraperError):
    def __init__(self, message: str = "Two-factor authentication canceled by user") -> None:
        super().__init__(message, ScraperErrorType.OTP_CANCELED)


class TokenExpiredError(PensionScraperError):
    def __init__(self, message: str = "Refresh token expired or invalid") -> None:
        super().__init__(message, ScraperErrorType.TOKEN_EXPIRED)
