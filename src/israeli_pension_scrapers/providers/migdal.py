"""Migdal (מגדל) pure-HTTP mobile API scraper.

Reverse-engineered from the Migdal Android app (`com.migdal.migdalcustomers` v5.2.8 / 1611)
against `https://customersservices.migdal.co.il/v1/api`.

Supports:
1. Interactive SMS/Email OTP login (`/Login/ByOtpToMobileV2` -> `/Login/ActivationByOtpConfirmV2`)
   followed by automatic long-term PIN token registration (`/Login/PNRegistrationV2`) encrypted
   with Migdal's native AES-256-CBC key (`AesCryptorManager`).
2. Passwordless / OTP-free login on subsequent runs via `/Login/PNLoginV2` using the stored
   `otpLongTermToken`.
3. Full portfolio extraction into the unified `PensionScrapingResult` schema:
   - `/InsuranceWorlds/All` (pension, hishtalmut, gemel, gemel_investment, managers, life_savings, child_savings)
   - `/Tabs/All` (balances, fees, yearly movements, insurance covers, retirement forecasts, seniority dates, employer & agent details)
   - `/ChangeInvestment/GetWorldsAndPolicies` (canonical policy numbers & track assignments)
   - `/Tabs/InvestmentPathUpdated` (per-track balances, allocations, and YTD yields)
   - `/Deposits/GetDeposits` (monthly salary deposits with employee, employer, and compensation splits)
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from israeli_pension_scrapers.base import OTP_CANCEL_SENTINEL, BasePensionScraper
from israeli_pension_scrapers.exceptions import (
    InvalidOtpError,
    OtpCanceledError,
    PensionScraperError,
    ScraperErrorType,
)
from israeli_pension_scrapers.models import (
    DepositTransaction,
    EmploymentStatus,
    ExpectedPayment,
    FundPolicy,
    InsuranceCover,
    InsuredProfile,
    InvestmentTrack,
    LoginResult,
    ManagementFees,
    MovementItem,
    PensionType,
    PolicyStatus,
    PortfolioSummary,
    ProductType,
    YearlyMovementSummary,
)
from israeli_pension_scrapers.utils import (
    extract_number,
    normalize_israeli_id,
    parse_il_date,
    parse_salary_month,
)

logger = logging.getLogger(__name__)

MIGDAL_API_BASE_URL = "https://customersservices.migdal.co.il/v1/api"
APP_VERSION_NAME = "5.2.8"
APP_VERSION_CODE = "1611"
DEVICE_TYPE = "1"
SCREEN_RESOLUTION = "Xxhdpi"
OS_VERSION = "Android_11"
USER_AGENT = "Dalvik/2.1.0 (Linux; U; Android 11; Pixel 3 Build/RQ1A.210105.003)"
MANUFACTURER_NAME = "מגדל"

# Extracted from libnative-lib.so via AesCryptorManager.getJNIKey() / getJNIVector()
AES_KEY = b"MlGD@L!O12#4S6$VVY661lVu3yfFA0xQ"
AES_IV = b"69317a58ed6546d2"

_WORLD_TO_PRODUCT_TYPE: dict[str, ProductType] = {
    "2": ProductType.PENSION,  # פנסיה
    "1": ProductType.LIFE_SAVINGS,  # מנהלים / חיסכון
    "3": ProductType.GEMEL,  # גמל
    "4": ProductType.HISHTALMUT,  # השתלמות
    "5": ProductType.FINANCIAL_SAVINGS,  # קשת / חיסכון פרט
    "6": ProductType.GEMEL_INVESTMENT,  # גמל להשקעה
    "7": ProductType.CHILD_SAVINGS,  # חיסכון לכל ילד
    "2000": ProductType.LIFE_SAVINGS,  # ביטוח חיים אישי
}


def encrypt_migdal_pin_token(
    user_id: str,
    android_id: str,
    gu_id: str,
    pin_code: str,
) -> str:
    """Replicate ``AesCryptorManager.b(userId, androidId, guId, pinCode)``.

    Encrypts ``f"{user_id}_{android_id}_{gu_id}_{pin_code}"`` using AES-256-CBC with PKCS7
    padding and encodes using Android's ``Base64.DEFAULT`` (76-char line wrapping with ``\\n``).
    """
    plaintext = f"{user_id}_{android_id}_{gu_id}_{pin_code}".encode()
    proc = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-K", AES_KEY.hex(), "-iv", AES_IV.hex()],
        input=plaintext,
        capture_output=True,
        check=True,
    )
    return base64.encodebytes(proc.stdout).decode("ascii")


def _clean_text(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()


def _parse_pension_type(sug_keren: str | int | None, title: str | None) -> PensionType | None:
    sk = str(sug_keren or "").strip()
    t = (title or "").strip()
    if sk == "1" or "אישית" in t or "מקיפה" in t:
        return PensionType.MAKIFA
    if sk == "2" or "משלימה" in t or "כללית" in t:
        return PensionType.MASHLIMA
    if sk == "3" or "ותיקה" in t:
        return PensionType.VATIKA
    return None


class MigdalScraper(BasePensionScraper):
    """Pure-HTTP scraper for Migdal Insurance & Pension (מגדל)."""

    provider_name = "migdal"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._authorization: str = ""
        self._user_session_guid: str = ""
        self._customer_age: int = 36
        self._id_number: str = ""
        self._installation_id: str = str(
            self.credentials.get("installationId")
            or f"{int(time.time() * 1000)}-{secrets.randbelow(10**18):018d}"
        )
        self._cached_g: dict[str, Any] = {}
        self._cached_f: dict[str, Any] = {}

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "AppVersionName": APP_VERSION_NAME,
            "AppVersionCode": APP_VERSION_CODE,
            "DeviceType": DEVICE_TYPE,
            "ScreenResolution": SCREEN_RESOLUTION,
            "Authorization": self._authorization,
            "usersessionguid": self._user_session_guid,
            "RequestUID": str(uuid.uuid4()),
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": USER_AGENT,
        }
        if extra:
            headers.update(extra)
        return headers

    def _capture_session_headers(self, resp_headers: Any) -> None:
        if auth := (resp_headers.get("Authorization") or resp_headers.get("authorization")):
            self._authorization = auth
        if guid := (resp_headers.get("usersessionguid") or resp_headers.get("UserSessionGuid")):
            self._user_session_guid = guid

    async def _post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        assert self.client is not None
        url = f"{MIGDAL_API_BASE_URL}/{path.lstrip('/')}"
        content = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        resp = await self.client.post(
            url,
            content=content,
            headers=self._headers(extra_headers),
        )
        self._capture_session_headers(resp.headers)
        if resp.status_code >= 400:
            raise PensionScraperError(
                f"Migdal API {path} returned HTTP {resp.status_code}: {resp.text[:300]}",
                ScraperErrorType.NETWORK_ERROR,
            )
        return resp.json() if resp.content else {}

    async def login(self) -> LoginResult:
        """Authenticate against Migdal via pre-authenticated session, PNLoginV2 token, or OTP."""
        raw_id = self.credentials.get("id") or self.credentials.get("idNumber") or ""
        if raw_id:
            try:
                self._id_number = normalize_israeli_id(str(raw_id))
            except ValueError as exc:
                return self._fail_login(LoginResult.INVALID_CREDENTIALS, exc)

        # 1. Pre-authenticated session headers (or offline snapshot injection)
        if self.credentials.get("authorization") and self.credentials.get("userSessionGuid"):
            self._authorization = str(self.credentials["authorization"])
            self._user_session_guid = str(self.credentials["userSessionGuid"])
            return LoginResult.SUCCESS

        # Bootstrap server-side session GUID (usersessionguid) via ApplicationSettings/AppSettings
        if not self._user_session_guid:
            try:
                await self._post(
                    "ApplicationSettings/AppSettings",
                    {
                        "ApplicationVersion": APP_VERSION_NAME,
                        "DeviceType": int(DEVICE_TYPE),
                        "HasScreenReader": False,
                    },
                    extra_headers={"ClientLocation": "0"},
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Migdal AppSettings bootstrap failed: %s", exc)

        # 2. Long-term PIN token (PNLoginV2)
        long_term = self.credentials.get("otpLongTermToken") or self.credentials.get("token")
        if long_term:
            self._emit_progress("logging in with device token")
            token_str = self._resolve_pin_token(str(long_term))
            try:
                resp = await self._post(
                    "Login/PNLoginV2",
                    {
                        "idNumber": self._id_number,
                        "IdNumber": self._id_number,
                        "Token": token_str,
                        "DeviceType": DEVICE_TYPE,
                        "DeviceVersion": APP_VERSION_NAME,
                        "SilverpopData": {
                            "appKey": "",
                            "channelId": "",
                            "mobileUserId": "",
                            "AppKey": "",
                            "ChannelId": "",
                            "MobileUserId": "",
                        },
                        "Splash": {"allSplashes": [], "splashToDisplay": None},
                        "IsDeepLink": False,
                        "InstallationId": self._installation_id,
                        "OsVersion": OS_VERSION,
                    },
                    extra_headers={
                        "CustomerAuditID": self._id_number,
                        "ClientLocation": "0",
                    },
                )
                data = resp.get("Data") or {}
                if resp.get("Status") == 0 and data.get("ResultCode") == 0 and self._authorization:
                    app_settings = data.get("AppLoginSettings") or {}
                    if isinstance(app_settings, dict) and app_settings.get("age"):
                        self._customer_age = int(app_settings["age"])
                    self.refreshed_long_term_token = str(long_term)
                    return LoginResult.SUCCESS
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Migdal PNLoginV2 failed (%s), falling back to OTP if available", exc
                )
                if self.on_otp_request is None:
                    return self._fail_login(LoginResult.INVALID_CREDENTIALS, exc)

        # 3. Interactive OTP flow (ByOtpToMobileV2 -> ActivationByOtpConfirmV2 -> PNRegistrationV2)
        if not self._id_number:
            return self._fail_login(
                LoginResult.INVALID_CREDENTIALS, "Missing 'id' in Migdal credentials"
            )

        self._emit_progress("requesting OTP")
        try:
            otp_req_resp = await self._post(
                "Login/ByOtpToMobileV2",
                {"idNumber": self._id_number},
                extra_headers={
                    "CustomerAuditID": self._id_number,
                    "BusinessOperationName": "CustomerAppLogin",
                    "BusinessOperationStep": "ByOtpToMobile",
                },
            )
        except Exception as exc:  # noqa: BLE001
            return self._fail_login(LoginResult.INVALID_CREDENTIALS, exc)

        otp_data = otp_req_resp.get("Data") or {}
        otp_key = otp_data.get("OtpKey") or otp_data.get("otpKey")
        otp_target = int(otp_data.get("OtpTarget") or otp_data.get("otpTarget") or 0)
        if not otp_key:
            msg = (
                otp_req_resp.get("Messages")
                or [otp_req_resp.get("DevMessage") or "OTP request failed"]
            )[0]
            return self._fail_login(LoginResult.INVALID_CREDENTIALS, str(msg))

        if self.on_otp_request is None:
            return self._fail_login(
                LoginResult.UNKNOWN_ERROR,
                "OTP required by Migdal, but on_otp_request callback is not set",
            )

        self._emit_progress("waiting for OTP code")
        otp_code = (await self.on_otp_request()).strip()
        if otp_code == OTP_CANCEL_SENTINEL:
            raise OtpCanceledError()

        self._emit_progress("verifying OTP code")
        try:
            confirm_resp = await self._post(
                "Login/ActivationByOtpConfirmV2",
                {
                    "IdNumber": self._id_number,
                    "OtpValue": otp_code,
                    "OtpKey": otp_key,
                    "OtpTarget": otp_target,
                    "PolicyAndStatementAgreements": False,
                    "DigitalMarketingAgreements": False,
                    "InstallationId": self._installation_id,
                    "OsVersion": OS_VERSION,
                },
                extra_headers={
                    "BusinessOperationName": "CustomerAppLogin",
                    "BusinessOperationStep": "ActivationByOtpConfirm",
                },
            )
        except Exception as exc:
            raise InvalidOtpError(f"Migdal OTP verification failed: {exc}") from exc

        confirm_data = confirm_resp.get("Data") or {}
        server_token = confirm_data.get("ServerToken") or confirm_data.get("serverToken")
        if (
            confirm_resp.get("Status") not in (0, None)
            and not server_token
            and not self._authorization
        ):
            raise InvalidOtpError(f"Invalid Migdal OTP response: {confirm_resp}")

        # Register a long-term PIN token (PNRegistrationV2) so future runs skip OTP
        if server_token:
            await self._register_long_term_pin_token(str(server_token))

        return LoginResult.SUCCESS

    def _resolve_pin_token(self, raw_token: str) -> str:
        text = raw_token.strip()
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
                if parsed.get("installationId"):
                    self._installation_id = str(parsed["installationId"])
                if parsed.get("token"):
                    return str(parsed["token"])
                if parsed.get("guId") and parsed.get("pinCode") and parsed.get("androidId"):
                    uid = parsed.get("userId") or self._id_number
                    return encrypt_migdal_pin_token(
                        str(uid),
                        str(parsed["androidId"]),
                        str(parsed["guId"]),
                        str(parsed["pinCode"]),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Could not parse JSON PIN token: %s", exc)
        return raw_token

    @staticmethod
    def _extract_doc_param_policy_number(stage2: dict[str, Any]) -> str | None:
        doc_params = stage2.get("DocumentsParams") or stage2.get("documentsParams") or {}
        if not isinstance(doc_params, dict):
            return None
        for group in doc_params.get("Params") or doc_params.get("params") or []:
            if not isinstance(group, list):
                continue
            for item in group:
                if (
                    isinstance(item, dict)
                    and item.get("Title") == "PolicyNumber"
                    and item.get("Value")
                ):
                    return str(item["Value"]).strip()
        return None

    @staticmethod
    def _load_snapshot_file(snapshot_path: str) -> dict[str, Any]:
        return json.loads(Path(snapshot_path).read_text(encoding="utf-8"))

    async def _register_long_term_pin_token(self, server_token: str) -> None:
        pin_code = f"{secrets.randbelow(9000) + 1000}"
        gu_id = str(uuid.uuid4())
        android_id = secrets.token_hex(8)
        encrypted_token = encrypt_migdal_pin_token(self._id_number, android_id, gu_id, pin_code)
        try:
            reg_resp = await self._post(
                "Login/PNRegistrationV2",
                {
                    "IdNumber": self._id_number,
                    "Token": encrypted_token,
                    "DeviceType": DEVICE_TYPE,
                    "DeviceVersion": APP_VERSION_NAME,
                    "SilverpopData": {
                        "appKey": "",
                        "channelId": "",
                        "mobileUserId": "",
                        "AppKey": "",
                        "ChannelId": "",
                        "MobileUserId": "",
                    },
                    "ServerToken": server_token,
                    "IsDeepLink": False,
                },
                extra_headers={"ClientLocation": "0"},
            )
            reg_data = reg_resp.get("Data") or {}
            settings = reg_data.get("EntityLoginSettings") or reg_data.get("AppLoginSettings") or {}
            if isinstance(settings, dict) and settings.get("age"):
                self._customer_age = int(settings["age"])
            self.refreshed_long_term_token = json.dumps(
                {
                    "userId": self._id_number,
                    "androidId": android_id,
                    "guId": gu_id,
                    "pinCode": pin_code,
                    "installationId": self._installation_id,
                    "token": encrypted_token,
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not register Migdal long-term PIN token: %s", exc)

    async def fetch_data(self) -> list[FundPolicy]:
        """Fetch customer details, insurance worlds, policy tabs, investment tracks, and deposits."""
        # 1. Customer details
        try:
            cust_resp = await self._post(
                "Account/GetAccountSettings",
                {"Company": 7},
                extra_headers={
                    "BusinessOperationName": "UpdateDetails-GetUserData",
                    "BusinessOperationStep": "Step1",
                },
            )
            self._parse_insured_profile(cust_resp)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not fetch Migdal GetAccountSettings: %s", exc)

        # 2. InsuranceWorlds/All & Tabs/All & ChangeInvestment/GetWorldsAndPolicies
        worlds_resp = await self._post("InsuranceWorlds/All", None)
        tabs_resp = await self._post(
            "Tabs/All",
            {"customerAge": self._customer_age, "CustomerAge": self._customer_age},
        )
        change_inv_resp: dict[str, Any] = {}
        try:
            change_inv_resp = await self._post(
                "ChangeInvestment/GetWorldsAndPolicies",
                None,
                extra_headers={
                    "BusinessOperationName": "ChangeInvestmentPath",
                    "BusinessOperationStep": "Step1",
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not fetch ChangeInvestment/GetWorldsAndPolicies: %s", exc)

        snapshot_path = self.credentials.get("cachedSnapshotPath")
        if snapshot_path:
            try:
                qk = self._load_snapshot_file(str(snapshot_path))
                self._cached_g = qk.get("g") or {}
                self._cached_f = qk.get("f") or {}
                if not worlds_resp.get("Data") and not tabs_resp.get("Data"):
                    d_map = qk.get("d") or {}
                    if d_map.get("4"):
                        self._parse_insured_profile(d_map["4"])
                    worlds_resp = d_map.get("2") or worlds_resp
                    tabs_resp = d_map.get("3") or tabs_resp
                    change_inv_resp = d_map.get("18") or change_inv_resp
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed loading Migdal cachedSnapshotPath: %s", exc)

        return await self._parse_all_policies(worlds_resp, tabs_resp, change_inv_resp)

    def _parse_insured_profile(self, cust_resp: dict[str, Any]) -> None:
        data = cust_resp.get("Data") or {}
        cust = data.get("CustomerDetails") or data.get("entityCustomerDetails") or {}
        if isinstance(cust, list) and cust:
            cust = cust[0]
        if not isinstance(cust, dict):
            return
        personal = (
            cust.get("PersonalDetails")
            or cust.get("personalDetails")
            or cust.get("mPersonalDetails")
            or {}
        )
        comm = (
            cust.get("CommunicationDetails")
            or cust.get("communicationDetails")
            or cust.get("mCommunicationDetails")
            or {}
        )
        gender_code = str(personal.get("Gender") or personal.get("mGender") or "")
        gender = "M" if gender_code == "1" else ("F" if gender_code == "2" else None)
        self.insured = InsuredProfile(
            id_number=personal.get("IdNumber")
            or personal.get("mIdNumber")
            or self._id_number
            or None,
            first_name=personal.get("FirstName") or personal.get("mFirstName"),
            last_name=personal.get("LastName") or personal.get("mLastName"),
            birth_date=parse_il_date(personal.get("BirthDate") or personal.get("mBirthDate")),
            gender=gender,
            email=comm.get("Email") or comm.get("mEmail"),
            phone=comm.get("MobileNumber") or comm.get("mMobileNumber") or comm.get("PhoneNumber"),
            city=comm.get("City") or comm.get("mCity"),
            street=comm.get("Street") or comm.get("mStreet"),
            house_number=comm.get("HouseNumber") or comm.get("mHouseNumber"),
        )

    async def _parse_all_policies(
        self,
        worlds_resp: dict[str, Any],
        tabs_resp: dict[str, Any],
        change_inv_resp: dict[str, Any],
    ) -> list[FundPolicy]:
        worlds_data = worlds_resp.get("Data") or {}
        tabs_data = tabs_resp.get("Data") or {}

        # Map canonical policy numbers & join dates from ChangeInvestment/GetWorldsAndPolicies
        ci_by_sug: dict[str, dict[str, Any]] = {}
        ci_data = (change_inv_resp.get("Data") or {}).get("content") or []
        for world_block in ci_data:
            if not isinstance(world_block, dict):
                continue
            pols = (world_block.get("policies") or {}).get("content") or []
            for p in pols:
                if not isinstance(p, dict):
                    continue
                pk = p.get("policyKey") or {}
                p_type = str(pk.get("PolicyType") or p.get("policyType") or "")
                if p_type:
                    ci_by_sug[p_type] = p

        # Agents by worldType
        agents_by_world: dict[str, str] = {}
        agents_block = (
            tabs_data.get("UserAgentsData") or tabs_data.get("entityUserAgentsData") or {}
        )
        for ag in agents_block.get("Agents") or agents_block.get("mAgents") or []:
            if not isinstance(ag, dict):
                continue
            agency = _clean_text(ag.get("AgencyName") or ag.get("mAgencyName"))
            name = _clean_text(ag.get("Name") or ag.get("mName"))
            full_agent = f"{agency} - {name}" if agency and name else (agency or name)
            for w_code in ag.get("Worlds") or ag.get("mWorlds") or []:
                agents_by_world[str(w_code)] = full_agent

        # Index Stage2 summaries from InsuranceWorlds/All
        stage2_by_world_and_key: dict[tuple[str, str], dict[str, Any]] = {}
        as_of_by_world: dict[str, str] = {}
        total_balance = 0.0

        world_sections = [
            ("2", worlds_data.get("Pensia") or worlds_data.get("pensia")),
            ("4", worlds_data.get("Hishtalmut") or worlds_data.get("hishtalmut")),
            ("3", worlds_data.get("Gemel") or worlds_data.get("gemel")),
            ("6", worlds_data.get("GemelInvestment") or worlds_data.get("gemelInvestment")),
            ("1", worlds_data.get("Managers") or worlds_data.get("managers")),
            ("1", worlds_data.get("LifeAndSavings") or worlds_data.get("lifeAndSavingsData")),
            ("5", worlds_data.get("Keshet") or worlds_data.get("keshet")),
            ("7", worlds_data.get("SavingsPerChild") or worlds_data.get("savingsPerChild")),
        ]
        for w_code, section in world_sections:
            if not isinstance(section, dict):
                continue
            for footer in (
                section.get("Footer")
                or section.get("Footers")
                or section.get("footers")
                or section.get("mFooters")
                or []
            ):
                if isinstance(footer, dict):
                    dt = parse_il_date(
                        footer.get("Text") or footer.get("text") or footer.get("mText")
                    )
                    if dt:
                        as_of_by_world[w_code] = dt
            for ep in (
                section.get("Policies")
                or section.get("EntityPolicy")
                or section.get("entityPolicy")
                or section.get("mEntityPolicy")
                or []
            ):
                if not isinstance(ep, dict):
                    continue
                sug = str(ep.get("SugKeren") or ep.get("sugKeren") or ep.get("mSugKeren") or "0")
                pnum = str(
                    ep.get("PolicyNumber")
                    or ep.get("policyNumber")
                    or ep.get("mPolicyNumber")
                    or sug
                )
                stage2_by_world_and_key[(w_code, pnum)] = ep
                stage2_by_world_and_key[(w_code, sug)] = ep

        # Iterate over WorldPoliciesList in Tabs/All
        world_lists = (
            tabs_data.get("WorldPoliciesList") or tabs_data.get("entityWorldPoliciesList") or []
        )
        funds: list[FundPolicy] = []

        for w_entry in world_lists:
            if not isinstance(w_entry, dict):
                continue
            world_code = str(w_entry.get("World") or w_entry.get("mWorld") or "")
            # Focus on pension, hishtalmut, gemel, and savings worlds (skip pure risk 2000 unless it has savings)
            if world_code == "2000":
                continue
            p_type = _WORLD_TO_PRODUCT_TYPE.get(world_code, ProductType.UNKNOWN)
            if p_type == ProductType.UNKNOWN:
                continue

            for pol in w_entry.get("Policies") or w_entry.get("mPolicies") or []:
                if not isinstance(pol, dict):
                    continue
                fund = await self._build_fund_from_policy_tabs(
                    world_code=world_code,
                    product_type=p_type,
                    pol=pol,
                    stage2_map=stage2_by_world_and_key,
                    ci_by_sug=ci_by_sug,
                    agent_name=agents_by_world.get(world_code),
                    default_as_of=as_of_by_world.get(world_code),
                )
                if fund is not None:
                    total_balance += fund.balance
                    funds.append(fund)

        self.portfolio_summary = PortfolioSummary(
            total_balance=round(total_balance, 2),
            currency="ILS",
        )
        return funds

    async def _build_fund_from_policy_tabs(
        self,
        *,
        world_code: str,
        product_type: ProductType,
        pol: dict[str, Any],
        stage2_map: dict[tuple[str, str], dict[str, Any]],
        ci_by_sug: dict[str, dict[str, Any]],
        agent_name: str | None,
        default_as_of: str | None,
    ) -> FundPolicy | None:
        raw_pnum_val = pol.get("PolicyNumber") or pol.get("mPolicyNumber") or 0
        try:
            pnum_int = int(float(raw_pnum_val))
        except (TypeError, ValueError):
            pnum_int = 0
        sug_keren = str(pol.get("SugKeren") or pol.get("mSugKeren") or "0")
        sug_int = int(sug_keren) if sug_keren.isdigit() else 0
        api_pnum = pnum_int if pnum_int > 0 else sug_int
        raw_pnum = str(api_pnum) if api_pnum > 0 else ""
        policy_type_code = pol.get("PolicyType") or pol.get("mPolicyType")
        subtitle = _clean_text(pol.get("PolicyHeaderSubtitle") or pol.get("mPolicyHeaderSubtitle"))
        title = _clean_text(pol.get("PolicyHeaderTitle") or pol.get("mPolicyHeaderTitle"))

        stage2 = (
            stage2_map.get((world_code, raw_pnum)) or stage2_map.get((world_code, sug_keren)) or {}
        )
        ci_info = ci_by_sug.get(sug_keren) or ci_by_sug.get(raw_pnum) or {}
        ci_key = ci_info.get("policyKey") or {}
        doc_pnum = self._extract_doc_param_policy_number(stage2)
        canonical_pnum = str(
            ci_key.get("PolicyNumber")
            or ci_key.get("policyNumber")
            or doc_pnum
            or (raw_pnum if len(raw_pnum) > 2 else f"migdal-{world_code}-{sug_keren}")
        )
        ci_title = _clean_text((ci_info.get("policyNumber") or {}).get("Title"))
        name = ci_title or subtitle or title or "פנסיה"

        balance = extract_number(stage2.get("MoneyVal") or stage2.get("mMoneyVal"), None)
        pension_type = (
            _parse_pension_type(sug_keren, name) if product_type == ProductType.PENSION else None
        )

        fund = FundPolicy(
            policy_id=canonical_pnum,
            raw_policy_id=raw_pnum or canonical_pnum,
            internal_account_id=sug_keren if sug_keren != "0" else None,
            product_type=product_type,
            pension_type=pension_type,
            name=name,
            manufacturer=MANUFACTURER_NAME,
            balance=balance or 0.0,
            agent_name=agent_name,
            data_as_of_date=default_as_of,
        )

        # Parse all tabs inside Tabs / entityTabs / mEntityTabs
        for tab in (
            pol.get("Tabs")
            or pol.get("EntityTabs")
            or pol.get("entityTabs")
            or pol.get("mEntityTabs")
            or []
        ):
            if not isinstance(tab, dict):
                continue
            self._apply_tab_to_fund(fund, tab)

        # Fetch / apply InvestmentPathUpdated (Tab 5) and GetDeposits (Tab 1) if not already populated
        if self.options.include_policy_details and raw_pnum.isdigit():
            if not fund.tracks:
                try:
                    inv_resp = await self._post(
                        "Tabs/InvestmentPathUpdated",
                        {
                            "policyNumber": int(raw_pnum),
                            "PolicyNumber": int(raw_pnum),
                            "PolicyType": policy_type_code,
                            "WorldType": int(world_code),
                        },
                    )
                    inv_tab = inv_resp.get("Data") if "Data" in inv_resp else inv_resp
                    if isinstance(inv_tab, dict):
                        self._parse_investment_tracks_tab(fund, inv_tab)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "Could not fetch InvestmentPathUpdated for %s: %s", fund.policy_id, exc
                    )
                if not fund.tracks and raw_pnum in self._cached_g:
                    self._parse_investment_tracks_tab(fund, self._cached_g[raw_pnum])

            if not fund.deposits:
                try:
                    emp_code = int(
                        stage2.get("EmploymentStatus") or stage2.get("mEmploymentStatus") or 0
                    )
                    exp_prem = stage2.get("ExpectedPremium") or stage2.get("mExpectedPremium")
                    dep_resp = await self._post(
                        "Deposits/GetDeposits",
                        {
                            "policyNumber": int(raw_pnum),
                            "PolicyNumber": int(raw_pnum),
                            "PolicyType": policy_type_code,
                            "ProductType": int(world_code),
                            "FundType": int(sug_keren) if sug_keren.isdigit() else None,
                            "EmploymentStatus": emp_code,
                            "ExpectedPremium": exp_prem,
                        },
                    )
                    dep_tab = dep_resp.get("Data") if "Data" in dep_resp else dep_resp
                    if isinstance(dep_tab, dict):
                        self._parse_deposits_tab(fund, dep_tab)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Could not fetch GetDeposits for %s: %s", fund.policy_id, exc)
                if not fund.deposits and raw_pnum in self._cached_f:
                    self._parse_deposits_tab(fund, self._cached_f[raw_pnum])

        # Fallback track from ChangeInvestment if InvestmentPathUpdated wasn't available
        if not fund.tracks and ci_info:
            acc_path = (
                ci_info.get("accumulationInvestmentPathList")
                or ci_info.get("AccumulationInvestmentPathList")
                or {}
            )
            track_name = _clean_text(
                acc_path.get("rewardsFundName") or acc_path.get("RewardsFundName")
            )
            if track_name:
                fund.tracks.append(
                    InvestmentTrack(
                        name=track_name,
                        allocation_pct=100.0,
                        balance=fund.balance,
                        fee_deposit_pct=fund.fees.from_deposit_pct,
                        fee_savings_pct=fund.fees.from_savings_pct,
                    )
                )

        for tr in fund.tracks:
            if tr.fee_deposit_pct is None:
                tr.fee_deposit_pct = fund.fees.from_deposit_pct
            if tr.fee_savings_pct is None:
                tr.fee_savings_pct = fund.fees.from_savings_pct
        for dep in fund.deposits:
            if not dep.employer_name and fund.employer_name:
                dep.employer_name = fund.employer_name
                dep.description = f"הפקדה - {fund.employer_name}"

        return fund

    def _apply_tab_to_fund(self, fund: FundPolicy, tab: dict[str, Any]) -> None:
        tab_title = _clean_text(tab.get("TabTitle") or tab.get("tabTitle") or tab.get("mTabTitle"))
        tab_type = str(tab.get("TabType") or tab.get("tabType") or tab.get("mTabType") or "")
        sections = (
            tab.get("TabSections")
            or tab.get("EntityTabSections")
            or tab.get("entityTabSections")
            or tab.get("mEntityTabSections")
            or []
        )

        if tab_type == "5" or "מסלולי השקעה" in tab_title:
            if sections:
                self._parse_investment_tracks_tab(fund, tab)
            return

        if tab_type == "1" or "פירוט הפקדות" in tab_title:
            if sections:
                self._parse_deposits_tab(fund, tab)
            return

        rows: list[tuple[str, str | None, str | None]] = []
        for sec in sections:
            if not isinstance(sec, dict):
                continue
            for td in sec.get("TabData") or sec.get("tabData") or sec.get("mTabData") or []:
                if not isinstance(td, dict):
                    continue
                sub = _clean_text(td.get("SubTitle") or td.get("subTitle") or td.get("mSubTitle"))
                v1 = (
                    td.get("Value1")
                    if "Value1" in td
                    else (td.get("value1") if "value1" in td else td.get("mValue1"))
                )
                v2 = (
                    td.get("Value2")
                    if "Value2" in td
                    else (td.get("value2") if "value2" in td else td.get("mValue2"))
                )
                rows.append(
                    (
                        sub,
                        _clean_text(v1) if v1 is not None else None,
                        _clean_text(v2) if v2 is not None else None,
                    )
                )

        if tab_type == "2" or tab_title == "T2":
            for sub, v1, _ in rows:
                if "יתרת הכספים" in sub and v1:
                    bal = extract_number(v1, None)
                    if bal is not None:
                        fund.balance = bal
                elif "קצבה צפויה" in sub and v1:
                    amt = extract_number(v1, None)
                    if amt is not None:
                        fund.expected_payments.append(ExpectedPayment(title=sub, amount=amt))

        elif "תשלומים צפויים" in tab_title:
            for sub, v1, _ in rows:
                amt = extract_number(v1, None)
                if amt is None:
                    continue
                if "בפרישה" in sub:
                    fund.extra["monthly_pension_forecast"] = amt
                elif any(k in sub for k in ("אלמן", "יתום", "הורה", "נכות", "שחרור")):
                    fund.insurance_covers.append(InsuranceCover(title=sub, insured_amount=amt))
                elif "תגמולי עובד" in sub:
                    fund.extra["redemption_employee"] = amt
                elif "תגמולי מעסיק" in sub:
                    fund.extra["redemption_employer"] = amt
                elif "פיצויים" in sub and 'סה"כ' in sub:
                    fund.extra["redemption_compensation"] = amt

        elif "תנועות בקרן" in tab_title:
            opening = deposits = gains = fees = ins_costs = closing = None
            as_of = fund.data_as_of_date
            items: list[MovementItem] = []
            for sub, v1, _ in rows:
                amt = extract_number(v1, None)
                if amt is not None:
                    items.append(MovementItem(title=sub, amount=amt))
                if "בתחילת השנה" in sub:
                    opening = amt
                elif "שהופקדו" in sub:
                    deposits = amt
                elif "רווחים" in sub:
                    gains = amt
                elif "דמי ניהול" in sub:
                    fees = amt
                elif "עלות ביטוח" in sub and amt is not None:
                    ins_costs = round((ins_costs or 0.0) + amt, 2)
                elif "יתרת הכספים בקרן בתאריך" in sub:
                    closing = amt
                    parsed_dt = parse_il_date(sub)
                    if parsed_dt:
                        as_of = parsed_dt
                        fund.data_as_of_date = parsed_dt
                        if not fund.balance_date:
                            fund.balance_date = parsed_dt
            if items:
                yr = (as_of or "2026")[:4]
                fund.yearly_movements.append(
                    YearlyMovementSummary(
                        year=yr,
                        as_of_date=as_of,
                        opening_balance=opening,
                        deposits=deposits,
                        gains=gains,
                        management_fees=fees,
                        insurance_costs=ins_costs,
                        closing_balance=closing,
                        items=items,
                    )
                )

        elif "דמי ניהול" in tab_title:
            dep_pct = fund.fees.from_deposit_pct
            sav_pct = fund.fees.from_savings_pct
            for sub, v1, _ in rows:
                if "דמי ניהול מהפקדה" in sub and v1:
                    dep_pct = extract_number(v1, dep_pct)
                elif "דמי ניהול מחיסכון" in sub and v1:
                    sav_pct = extract_number(v1, sav_pct)
            fund.fees = ManagementFees(from_deposit_pct=dep_pct, from_savings_pct=sav_pct)

        elif "פרטי הקרן" in tab_title or "פרטי הפוליסה" in tab_title:
            for sub, v1, _ in rows:
                if not v1:
                    continue
                if "מועד הצטרפות" in sub:
                    fund.start_date = parse_il_date(v1)
                elif "מועד תחילת חישוב ותק" in sub:
                    fund.establishment_date = parse_il_date(v1)
                elif "שם המעסיק" in sub:
                    fund.employer_name = v1
                elif "מעמד עמית" in sub:
                    if "שכיר" in v1:
                        fund.employment_status = EmploymentStatus.EMPLOYEE
                    elif "עצמאי" in v1:
                        fund.employment_status = EmploymentStatus.SELF_EMPLOYED
                elif "סוג עמית" in sub:
                    fund.status = PolicyStatus.ACTIVE if "פעיל" in v1 else PolicyStatus.INACTIVE
                elif "משכורת קובעת" in sub:
                    fund.extra["qualifying_salary"] = extract_number(v1, None)
                elif "מספר מעסיק" in sub:
                    fund.extra["employer_number"] = v1

    @staticmethod
    def _parse_investment_tracks_tab(fund: FundPolicy, tab: dict[str, Any]) -> None:
        sections = (
            tab.get("TabSections")
            or tab.get("EntityTabSections")
            or tab.get("entityTabSections")
            or tab.get("mEntityTabSections")
            or []
        )
        alloc_by_name: dict[str, float] = {}
        bal_by_name: dict[str, float] = {}
        yield_by_name: dict[str, float] = {}
        as_of_date: str | None = None
        mode = ""

        for sec in sections:
            if not isinstance(sec, dict):
                continue
            for td in sec.get("TabData") or sec.get("tabData") or sec.get("mTabData") or []:
                if not isinstance(td, dict):
                    continue
                sub = _clean_text(td.get("SubTitle") or td.get("subTitle") or td.get("mSubTitle"))
                v1 = _clean_text(td.get("Value1") or td.get("value1") or td.get("mValue1"))
                v2 = _clean_text(td.get("Value2") or td.get("value2") or td.get("mValue2"))

                if "חלוקת תשלומים עתידיים" in sub:
                    mode = "allocation"
                    continue
                if "צבירה קיימת במסלולי השקעה" in sub:
                    mode = "balance"
                    continue
                if "תשואות למסלולי ההשקעה" in sub:
                    mode = "yield"
                    continue
                if "נכון לתאריך" in sub:
                    as_of_date = parse_il_date(sub) or as_of_date
                    continue
                if (
                    not sub
                    or sub in ("מסלול", 'סה"כ')
                    or sub.startswith("*")
                    or "לשינוי" in sub
                    or "להתאמת" in sub
                ):
                    continue

                if mode == "allocation":
                    pct = extract_number(v2 or v1, None)
                    if pct is not None:
                        alloc_by_name[sub] = pct
                elif mode == "balance":
                    b1 = extract_number(v1, 0.0) or 0.0
                    b2 = extract_number(v2, 0.0) or 0.0
                    if b1 or b2:
                        bal_by_name[sub] = round(b1 + b2, 2)
                elif mode == "yield":
                    yld = extract_number(v1, None)
                    if yld is not None:
                        yield_by_name[sub] = yld

        track_names = list(
            dict.fromkeys([*bal_by_name.keys(), *alloc_by_name.keys(), *yield_by_name.keys()])
        )
        if track_names:
            fund.tracks = [
                InvestmentTrack(
                    name=t_name,
                    allocation_pct=alloc_by_name.get(
                        t_name, 100.0 if len(track_names) == 1 else None
                    ),
                    balance=bal_by_name.get(t_name),
                    yield_ytd_pct=yield_by_name.get(t_name),
                    yield_as_of_date=as_of_date,
                    fee_deposit_pct=fund.fees.from_deposit_pct,
                    fee_savings_pct=fund.fees.from_savings_pct,
                )
                for t_name in track_names
            ]

    @staticmethod
    def _parse_deposits_tab(fund: FundPolicy, tab: dict[str, Any]) -> None:
        sections = (
            tab.get("TabSections")
            or tab.get("EntityTabSections")
            or tab.get("entityTabSections")
            or tab.get("mEntityTabSections")
            or []
        )
        deposits: list[DepositTransaction] = []
        seen_ids: set[str] = set()

        for sec in sections:
            if not isinstance(sec, dict):
                continue
            comp_list = (
                sec.get("CompositeData")
                or sec.get("compositeData")
                or sec.get("mCompositeData")
                or []
            )
            for cd in comp_list:
                if not isinstance(cd, dict):
                    continue
                td_list = cd.get("TabData") or cd.get("tabData") or cd.get("mTabData") or []
                cur: dict[str, Any] = {}
                for td in td_list:
                    if not isinstance(td, dict):
                        continue
                    sub = _clean_text(
                        td.get("SubTitle") or td.get("subTitle") or td.get("mSubTitle")
                    )
                    v1 = _clean_text(td.get("Value1") or td.get("value1") or td.get("mValue1"))
                    if "עבור חודש משכורת" in sub:
                        if cur.get("salary_month"):
                            MigdalScraper._flush_deposit_row(fund, cur, deposits, seen_ids)
                            cur = {}
                        cur["salary_month"] = parse_salary_month(v1)
                    elif sub == "משכורת":
                        cur["salary"] = extract_number(v1, None)
                    elif sub == "תגמולי עובד":
                        cur["employee"] = extract_number(v1, 0.0) or 0.0
                    elif sub == "תגמולי מעסיק":
                        cur["employer"] = extract_number(v1, 0.0) or 0.0
                    elif sub == "פיצויים":
                        cur["compensation"] = extract_number(v1, 0.0) or 0.0
                if cur.get("salary_month"):
                    MigdalScraper._flush_deposit_row(fund, cur, deposits, seen_ids)

        if deposits:
            fund.deposits = deposits

    @staticmethod
    def _flush_deposit_row(
        fund: FundPolicy,
        cur: dict[str, Any],
        out: list[DepositTransaction],
        seen_ids: set[str],
    ) -> None:
        sal_month = cur.get("salary_month")
        if not sal_month:
            return
        emp = cur.get("employee", 0.0) or 0.0
        empr = cur.get("employer", 0.0) or 0.0
        comp = cur.get("compensation", 0.0) or 0.0
        total = round(emp + empr + comp, 2)
        if total <= 0:
            return
        dt = f"{sal_month}-01"
        ident = f"{fund.policy_id}_{dt}_{total}"
        if ident in seen_ids:
            return
        seen_ids.add(ident)
        desc = f"הפקדה - {fund.employer_name}" if fund.employer_name else "הפקדה"
        out.append(
            DepositTransaction(
                identifier=ident,
                date=dt,
                salary_month=sal_month,
                total_amount=total,
                employee_amount=emp,
                employer_amount=empr,
                compensation_amount=comp,
                insured_salary=cur.get("salary"),
                employer_name=fund.employer_name,
                description=desc,
            )
        )
