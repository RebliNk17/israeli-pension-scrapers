"""HaPhoenix (הפניקס) pure-HTTP mobile & portal API scraper.

Uses the reverse-engineered HaPhoenix mobile API (`pinfo-mobileapp-services.fnx.co.il`)
for Auth0 MFA/OTP login, 6-month `refresh_token` rotation, portfolio graphs, and exposure
charts, combined with the mobile portal API (`pinfo-mobile.fnx.co.il/api/v1/info`) using
the exact same Bearer JWT for deep per-policy details (employer/employee deposit splits,
yearly movement statements, fee discount schedules, insurance covers, and seniority dates).
"""

from __future__ import annotations

import logging
from typing import Any

from israeli_pension_scrapers.base import OTP_CANCEL_SENTINEL, BasePensionScraper
from israeli_pension_scrapers.exceptions import (
    InvalidOtpError,
    OtpCanceledError,
    PensionScraperError,
    ScraperErrorType,
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
    PensionType,
    PolicyStatus,
    PortfolioSummary,
    ProductType,
    YearlyMovementSummary,
)
from israeli_pension_scrapers.utils import (
    extract_number,
    normalize_israeli_id,
    normalize_israeli_phone,
    parse_il_date,
    parse_salary_month,
    policy_id_key,
    split_policy_id,
)

logger = logging.getLogger(__name__)

MOBILE_SERVICES_BASE_URL = "https://pinfo-mobileapp-services.fnx.co.il"
PORTAL_API_BASE_URL = "https://pinfo-mobile.fnx.co.il/api/v1/info"
APP_VERSION = "3.9.0"
USER_AGENT = "okhttp/4.12.0"
MANUFACTURER_NAME = "הפניקס"

_PRODUCT_TYPE_MAP: dict[str, ProductType] = {
    "pension": ProductType.PENSION,
    "hishtalmut": ProductType.HISHTALMUT,
    "hishtalmutira": ProductType.HISHTALMUT,
    "gemel": ProductType.GEMEL,
    "gemelira": ProductType.GEMEL,
    "gemelinvestment": ProductType.GEMEL_INVESTMENT,
    "financial": ProductType.FINANCIAL_SAVINGS,
    "pizuim": ProductType.PIZUIM,
    "eachchildsavings": ProductType.CHILD_SAVINGS,
    "lifesaving": ProductType.LIFE_SAVINGS,
}

_EXCELLENCE_PRODUCT_TYPES = frozenset(
    {
        ProductType.HISHTALMUT,
        ProductType.GEMEL,
        ProductType.GEMEL_INVESTMENT,
        ProductType.PIZUIM,
        ProductType.CHILD_SAVINGS,
    }
)


def _map_product_type(raw_type: str | None, description: str | None = None) -> ProductType:
    key = (raw_type or "").strip().lower()
    if key in _PRODUCT_TYPE_MAP:
        return _PRODUCT_TYPE_MAP[key]
    desc = (description or "").strip()
    if "השתלמות" in desc:
        return ProductType.HISHTALMUT
    if "פנסי" in desc:
        return ProductType.PENSION
    if "גמל להשקעה" in desc:
        return ProductType.GEMEL_INVESTMENT
    if "גמל" in desc:
        return ProductType.GEMEL
    if "חיסכון" in desc:
        return ProductType.FINANCIAL_SAVINGS
    return ProductType.UNKNOWN


def _map_employment_status(
    state: str | None, is_employee_flag: bool | None
) -> EmploymentStatus | None:
    text = (state or "").strip()
    if "שכיר" in text or is_employee_flag is True:
        return EmploymentStatus.EMPLOYEE
    if "עצמאי" in text or is_employee_flag is False:
        return EmploymentStatus.SELF_EMPLOYED
    return None


class HaPhoenixScraper(BasePensionScraper):
    """Pure-HTTP scraper for HaPhoenix Insurance & Investments (הפניקס)."""

    provider_name = "haphoenix"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._access_token: str | None = None
        self._oob_code: str | None = None
        self._mfa_token: str | None = None

    def _mobile_headers(self, *, authenticated: bool = True) -> dict[str, str]:
        headers = {
            "Accept": "application/json; charset=utf-8",
            "Content-Type": "application/json; charset=utf-8",
            "e_platform": "mobile",
            "app_version": APP_VERSION,
            "User-Agent": USER_AGENT,
        }
        if authenticated and self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

    def _portal_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": USER_AGENT,
        }
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

    async def _post_mobile(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        assert self.client is not None
        url = f"{MOBILE_SERVICES_BASE_URL}{path}"
        resp = await self.client.post(
            url,
            json=body if body is not None else {},
            headers=self._mobile_headers(authenticated=authenticated),
        )
        if resp.status_code >= 400:
            raise PensionScraperError(
                f"HaPhoenix mobile API {path} returned HTTP {resp.status_code}: {resp.text[:300]}",
                ScraperErrorType.NETWORK_ERROR,
            )
        return resp.json() if resp.content else {}

    async def _post_portal(
        self,
        endpoint: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        assert self.client is not None
        url = f"{PORTAL_API_BASE_URL}/{endpoint.lstrip('/')}"
        resp = await self.client.post(
            url,
            json=body if body is not None else {},
            headers=self._portal_headers(),
        )
        if resp.status_code >= 400:
            raise PensionScraperError(
                f"HaPhoenix portal API {endpoint} returned HTTP {resp.status_code}: {resp.text[:300]}",
                ScraperErrorType.NETWORK_ERROR,
            )
        return resp.json() if resp.content else {}

    async def login(self) -> LoginResult:
        """Authenticate using accessToken, long-term refresh_token, or interactive SMS OTP."""
        # 1. Direct access token injection (useful for testing or live sessions)
        if direct_token := self.credentials.get("accessToken"):
            self._access_token = str(direct_token).removeprefix("Bearer ").strip()
            if refresh := (
                self.credentials.get("otpLongTermToken") or self.credentials.get("refreshToken")
            ):
                self.refreshed_long_term_token = str(refresh)
            return LoginResult.SUCCESS

        # 2. Long-term refresh token (passwordless / OTP-free resume)
        long_term_token = self.credentials.get("otpLongTermToken") or self.credentials.get(
            "refreshToken"
        )
        if long_term_token:
            self._emit_progress("refreshing token")
            try:
                data = await self._post_mobile(
                    "/auth/v1/token/refresh-token",
                    {"refresh_token": str(long_term_token)},
                    authenticated=False,
                )
                payload = data.get("response") if isinstance(data.get("response"), dict) else data
                access_token = (payload or {}).get("access_token")
                new_refresh = (payload or {}).get("refresh_token") or str(long_term_token)
                if access_token:
                    self._access_token = access_token
                    self.refreshed_long_term_token = new_refresh
                    return LoginResult.SUCCESS
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "HaPhoenix refresh_token failed (%s), falling back to OTP if possible", exc
                )
                if self.on_otp_request is None:
                    return self._fail_login(
                        LoginResult.INVALID_CREDENTIALS,
                        f"Refresh token expired or rejected and no OTP callback configured: {exc}",
                    )

        # 3. Interactive 2FA OTP login
        raw_id = self.credentials.get("id") or self.credentials.get("idNumber")
        raw_phone = (
            self.credentials.get("phoneNumber")
            or self.credentials.get("phone")
            or self.credentials.get("email")
        )
        if not raw_id or not raw_phone:
            return self._fail_login(
                LoginResult.INVALID_CREDENTIALS,
                "Missing 'id' or 'phoneNumber' in credentials",
            )

        try:
            id_number = normalize_israeli_id(str(raw_id))
        except ValueError as exc:
            return self._fail_login(LoginResult.INVALID_CREDENTIALS, exc)

        mfa_type = str(self.credentials.get("mfaType") or "sms")
        id_type = str(self.credentials.get("idType") or "id")
        to_value = (
            str(raw_phone).strip()
            if "@" in str(raw_phone)
            else normalize_israeli_phone(str(raw_phone))
        )

        self._emit_progress("requesting OTP")
        try:
            auth_resp = await self._post_mobile(
                "/auth/v1/login/authenticate",
                {
                    "to": to_value,
                    "mfa_type": mfa_type,
                    "id_type": id_type,
                    "id_number": id_number,
                },
                authenticated=False,
            )
        except Exception as exc:  # noqa: BLE001
            return self._fail_login(LoginResult.INVALID_CREDENTIALS, exc)

        auth_data = (
            auth_resp.get("response") if isinstance(auth_resp.get("response"), dict) else auth_resp
        ) or {}

        # Some accounts may return tokens directly on authenticate
        if auth_data.get("access_token"):
            self._access_token = auth_data["access_token"]
            self.refreshed_long_term_token = auth_data.get("refresh_token")
            return LoginResult.SUCCESS

        self._oob_code = auth_data.get("oob_code")
        self._mfa_token = auth_data.get("mfa_token")
        if not self._oob_code or not self._mfa_token:
            return self._fail_login(
                LoginResult.INVALID_CREDENTIALS,
                f"Unexpected authenticate response: {auth_resp}",
            )

        if self.on_otp_request is None:
            return self._fail_login(
                LoginResult.UNKNOWN_ERROR,
                "OTP required by HaPhoenix, but on_otp_request callback is not set",
            )

        self._emit_progress("waiting for OTP code")
        otp_code = (await self.on_otp_request()).strip()
        if otp_code == OTP_CANCEL_SENTINEL:
            raise OtpCanceledError()

        self._emit_progress("verifying OTP code")
        try:
            token_resp = await self._post_mobile(
                "/auth/v1/token/get-token",
                {
                    "mfa_token": self._mfa_token,
                    "otp_code": otp_code,
                    "oob_code": self._oob_code,
                },
                authenticated=False,
            )
        except Exception as exc:
            raise InvalidOtpError(f"OTP verification failed: {exc}") from exc

        token_data = (
            token_resp.get("response")
            if isinstance(token_resp.get("response"), dict)
            else token_resp
        ) or {}
        access_token = token_data.get("access_token")
        if not access_token:
            raise InvalidOtpError(f"No access_token in OTP response: {token_resp}")

        self._access_token = access_token
        self.refreshed_long_term_token = token_data.get("refresh_token")
        return LoginResult.SUCCESS

    async def resend_otp(self) -> None:
        """Re-issue the SMS OTP code in-place using the current mfa_token."""
        if not self._mfa_token:
            raise PensionScraperError("Cannot resend OTP before login/authenticate")
        raw_phone = self.credentials.get("phoneNumber") or self.credentials.get("phone") or ""
        resp = await self._post_mobile(
            "/auth/v1/otp/refresh-otp",
            {
                "mfa_token": self._mfa_token,
                "to": normalize_israeli_phone(str(raw_phone)),
                "mfa_type": str(self.credentials.get("mfaType") or "sms"),
            },
            authenticated=False,
        )
        data = resp.get("response") if isinstance(resp.get("response"), dict) else resp
        if isinstance(data, dict) and data.get("oob_code"):
            self._oob_code = data["oob_code"]

    async def fetch_data(self) -> list[FundPolicy]:
        """Fetch insured profile, portfolio summary, and all fund policies with full details."""
        if not self._access_token:
            raise PensionScraperError("Cannot fetch data before login")

        # 1. Insured profile
        await self._fetch_insured_profile()

        # 2. Native investments overview, accumulation history, routes composition, and deposits
        inv_products_by_id, total_saving, liquid_saving = await self._fetch_native_investments()
        balance_history_by_type, portfolio_history = await self._fetch_accumulation_graph()
        exposure = await self._fetch_routes_composition()
        native_deposits_by_id = await self._fetch_native_deposits()

        # 3. Portal savings active, withdrawals list (liquidity/taxable split), and all-user-products
        portal_savings_by_id, portal_total = await self._fetch_portal_active_savings()
        withdrawals_by_id = await self._fetch_portal_withdrawals()
        all_products = await self._fetch_portal_all_products()

        effective_total = total_saving if total_saving is not None else (portal_total or 0.0)
        self.portfolio_summary = PortfolioSummary(
            total_balance=effective_total,
            liquid_balance=liquid_saving,
            currency="ILS",
            balance_history=portfolio_history,
            asset_exposure=exposure,
        )

        # Merge discovered policies from all sources while preserving order
        discovered: dict[str, dict[str, Any]] = {}
        for item in all_products:
            raw_id = item["raw_policy_id"]
            discovered[raw_id] = item

        for raw_id, s_item in portal_savings_by_id.items():
            if raw_id not in discovered:
                discovered[raw_id] = s_item
            else:
                existing_bal = discovered[raw_id].get("balance")
                discovered[raw_id].update({k: v for k, v in s_item.items() if v is not None})
                if existing_bal is not None:
                    discovered[raw_id]["balance"] = existing_bal

        for raw_id, n_item in inv_products_by_id.items():
            if raw_id not in discovered:
                discovered[raw_id] = {
                    "raw_policy_id": raw_id,
                    "product_type": n_item.get("product_type", ProductType.HISHTALMUT),
                    "name": n_item.get("title") or "קרן השתלמות",
                    "balance": extract_number(n_item.get("savingSum"), 0.0),
                    "is_new": bool(n_item.get("isNew", False)),
                }

        funds: list[FundPolicy] = []
        for raw_id, summary in discovered.items():
            policy_id, internal_id = split_policy_id(raw_id)
            p_type: ProductType = summary.get("product_type", ProductType.UNKNOWN)
            if p_type == ProductType.UNKNOWN:
                continue

            native_card = inv_products_by_id.get(raw_id, {})
            portal_saving = portal_savings_by_id.get(raw_id, {})
            withdrawal_info = withdrawals_by_id.get(raw_id, {})

            balance = (
                summary.get("balance")
                if summary.get("balance") is not None
                else extract_number(native_card.get("savingSum"), 0.0) or 0.0
            )
            balance_date = (
                parse_il_date(native_card.get("dailyUpdateTo"))
                or parse_il_date(portal_saving.get("tarNehunut"))
                or parse_il_date(withdrawal_info.get("updateDate"))
            )
            data_as_of_date = parse_il_date(native_card.get("updateTo")) or parse_il_date(
                withdrawal_info.get("updateDate")
            )
            liquidity_date = parse_il_date(native_card.get("withdrawDate")) or parse_il_date(
                withdrawal_info.get("liquidityDate")
            )

            liquid_bal = extract_number(withdrawal_info.get("taxFreeSaving"), None)
            if liquid_bal is None and portal_saving.get("availableSum") not in (None, ""):
                liquid_bal = extract_number(portal_saving.get("availableSum"), None)
            taxable_bal = extract_number(withdrawal_info.get("taxableSaving"), None)
            as_of_bal = extract_number(withdrawal_info.get("totalSaving"), None)

            emp_status = _map_employment_status(
                summary.get("state") or portal_saving.get("MAAMAD"),
                portal_saving.get("kiumRechivSachir"),
            )

            # Native tracks & fees fallback
            native_fee = native_card.get("managementFee") or {}
            fees = ManagementFees(
                from_deposit_pct=extract_number(native_fee.get("fromDeposit"), None),
                from_savings_pct=extract_number(native_fee.get("fromSaving"), None),
            )
            tracks = self._build_tracks_from_native(native_card.get("investmentRoutes") or [])

            fund = FundPolicy(
                policy_id=policy_id,
                raw_policy_id=raw_id,
                internal_account_id=internal_id,
                product_type=p_type,
                pension_type=summary.get("pension_type"),
                status=summary.get("status", PolicyStatus.ACTIVE),
                employment_status=emp_status,
                name=summary.get("name") or "קרן השתלמות",
                manufacturer=MANUFACTURER_NAME,
                balance=float(balance),
                as_of_balance=as_of_bal,
                liquid_balance=liquid_bal,
                taxable_balance=taxable_bal,
                currency="ILS",
                balance_date=balance_date,
                data_as_of_date=data_as_of_date,
                liquidity_date=liquidity_date,
                is_independent=bool(native_card.get("isIndependent", False)),
                fees=fees,
                tracks=tracks,
                deposits=list(native_deposits_by_id.get(raw_id, [])),
                balance_history=list(balance_history_by_type.get(p_type.value, [])),
            )

            if self.options.include_policy_details:
                await self._enrich_policy_details(
                    fund,
                    is_new=bool(summary.get("is_new", False)),
                    native_routes=native_card.get("investmentRoutes") or [],
                )

            funds.append(fund)

        return funds

    async def _fetch_insured_profile(self) -> None:
        try:
            resp = await self._post_portal("user/insured-details", {})
            data = resp.get("response") or {}
            if isinstance(data, dict) and data:
                address = data.get("address") or {}
                raw_id = self.credentials.get("id") or self.credentials.get("idNumber")
                self.insured = InsuredProfile(
                    id_number=normalize_israeli_id(str(raw_id)) if raw_id else None,
                    first_name=data.get("firstName"),
                    last_name=data.get("lastName"),
                    email=data.get("email"),
                    phone=data.get("phone"),
                    birth_date=parse_il_date(data.get("birthdate")),
                    gender=data.get("gender"),
                    city=address.get("cityName") if isinstance(address, dict) else None,
                    street=address.get("streetName") if isinstance(address, dict) else None,
                    house_number=address.get("houseNumber") if isinstance(address, dict) else None,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not fetch insured-details: %s", exc)

    async def _fetch_native_investments(
        self,
    ) -> tuple[dict[str, dict[str, Any]], float | None, float | None]:
        by_id: dict[str, dict[str, Any]] = {}
        total_saving: float | None = None
        liquid_saving: float | None = None
        try:
            resp = await self._post_mobile(
                "/investments/v1/investments-products/get-investments-products"
            )
            data = resp.get("response") or {}
            header = data.get("mainHeader") or {}
            total_saving = extract_number(header.get("totalSaving"), None)
            liquid_saving = extract_number(header.get("fluentWithdraw"), None)
            for tab in data.get("productTabsList") or []:
                tab_type = _map_product_type(tab.get("id"))
                for card in tab.get("data") or []:
                    raw_id = card.get("policyId")
                    if raw_id:
                        card_copy = dict(card)
                        card_copy["product_type"] = tab_type
                        by_id[raw_id] = card_copy
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not fetch native investments-products: %s", exc)
        return by_id, total_saving, liquid_saving

    async def _fetch_accumulation_graph(
        self,
    ) -> tuple[dict[str, list[BalancePoint]], list[BalancePoint]]:
        by_type: dict[str, list[BalancePoint]] = {}
        portfolio_points: list[BalancePoint] = []
        if not self.options.include_balance_history:
            return by_type, portfolio_points
        try:
            resp = await self._post_mobile(
                "/investments/v1/investments-graphs/get-accumulation-graph-details"
            )
            acc = (resp.get("response") or {}).get("accumulationDetails") or {}
            last12 = acc.get("last12Months") or {}
            for item in last12.get("values") or []:
                pt_date = parse_il_date(item.get("date"))
                if not pt_date:
                    continue
                total_val = extract_number(item.get("total"), None)
                if total_val is not None:
                    portfolio_points.append(BalancePoint(date=pt_date, balance=total_val))
                by_prod = item.get("byProduct") or {}
                for prod_key, prod_val in by_prod.items():
                    amount = extract_number(prod_val, None)
                    if amount is not None:
                        mapped = _map_product_type(prod_key).value
                        by_type.setdefault(mapped, []).append(
                            BalancePoint(date=pt_date, balance=amount)
                        )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not fetch accumulation graph: %s", exc)
        return by_type, portfolio_points

    async def _fetch_routes_composition(self) -> AssetExposure | None:
        try:
            resp = await self._post_mobile(
                "/investments/v1/investments-charts/get-routes-composition-chart-details"
            )
            data = resp.get("response") or {}
            charts = data.get("charts") or []
            if not charts:
                return None
            geo: list[ExposureBucket] = []
            curr: list[ExposureBucket] = []
            equity_pct: float | None = None
            for chart in charts:
                c_type = chart.get("type")
                sums_by_id: dict[str, float] = {}
                for prod_entry in chart.get("byProduct") or []:
                    if isinstance(prod_entry, dict):
                        for val in prod_entry.values():
                            if isinstance(val, list):
                                for sub in val:
                                    if isinstance(sub, dict) and sub.get("id"):
                                        s_val = extract_number(sub.get("sum"), None)
                                        if s_val is not None:
                                            sums_by_id[sub["id"]] = (
                                                sums_by_id.get(sub["id"], 0.0) + s_val
                                            )
                            elif isinstance(val, dict) and val.get("id"):
                                s_val = extract_number(val.get("sum"), None)
                                if s_val is not None:
                                    sums_by_id[val["id"]] = sums_by_id.get(val["id"], 0.0) + s_val

                if c_type == "geographic":
                    for it in chart.get("items") or []:
                        pct = extract_number(it.get("percentage"), 0.0) or 0.0
                        geo.append(
                            ExposureBucket(
                                id=str(it.get("id") or ""),
                                label=str(it.get("label") or ""),
                                percentage=pct,
                                amount=sums_by_id.get(str(it.get("id") or "")),
                            )
                        )
                elif c_type == "currency":
                    for it in chart.get("items") or []:
                        pct = extract_number(it.get("percentage"), 0.0) or 0.0
                        curr.append(
                            ExposureBucket(
                                id=str(it.get("id") or ""),
                                label=str(it.get("label") or ""),
                                percentage=pct,
                                amount=sums_by_id.get(str(it.get("id") or "")),
                            )
                        )
                elif c_type == "equityExposure":
                    items = chart.get("items") or []
                    if items:
                        equity_pct = extract_number(items[0].get("percentage"), None)
            return AssetExposure(geographic=geo, currency=curr, equity_pct=equity_pct)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not fetch routes composition: %s", exc)
            return None

    async def _fetch_native_deposits(self) -> dict[str, list[DepositTransaction]]:
        by_id: dict[str, list[DepositTransaction]] = {}
        try:
            resp = await self._post_mobile("/investments/v1/deposits/get-all-deposits")
            deposits_list = (resp.get("response") or {}).get("depositsList") or []
            for d in deposits_list:
                raw_id = d.get("policyId")
                dt = parse_il_date(d.get("date"))
                amt = extract_number(d.get("amount"), None)
                if not raw_id or not dt or amt is None:
                    continue
                p_key = policy_id_key(raw_id)
                method = d.get("method")
                desc = (
                    f"{d.get('type') or 'הפקדה'} - {method}"
                    if method
                    else (d.get("type") or "הפקדה")
                )
                by_id.setdefault(raw_id, []).append(
                    DepositTransaction(
                        identifier=f"{p_key}_{dt}_{amt}",
                        date=dt,
                        total_amount=amt,
                        payment_method=method,
                        description=desc,
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not fetch native deposits: %s", exc)
        return by_id

    async def _fetch_portal_active_savings(self) -> tuple[dict[str, dict[str, Any]], float | None]:
        by_id: dict[str, dict[str, Any]] = {}
        total_val: float | None = None
        try:
            resp = await self._post_portal("policy/savings/active", {})
            data = resp.get("response") or {}
            dist = data.get("savingDistribution") or {}
            total_val = extract_number(dist.get("totalSavings"), None)
            for s in data.get("savingList") or []:
                raw_id = s.get("policyId")
                if raw_id:
                    s_copy = dict(s)
                    s_copy["raw_policy_id"] = raw_id
                    s_copy["product_type"] = _map_product_type(
                        s.get("policyType"), s.get("productDescription")
                    )
                    s_copy["name"] = s.get("productDescription") or s.get("name") or "קרן השתלמות"
                    s_copy["balance"] = extract_number(s.get("sum"), None)
                    by_id[raw_id] = s_copy
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not fetch portal active savings: %s", exc)
        return by_id, total_val

    async def _fetch_portal_withdrawals(self) -> dict[str, dict[str, Any]]:
        by_id: dict[str, dict[str, Any]] = {}
        try:
            resp = await self._post_portal("withdrawals/withdrawal-list", {})
            data = resp.get("response") or {}
            for item in data.get("list") or []:
                raw_id = item.get("policyNumber") or item.get("policyId")
                if raw_id:
                    by_id[raw_id] = item
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not fetch portal withdrawals: %s", exc)
        return by_id

    async def _fetch_portal_all_products(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        try:
            resp = await self._post_portal("policy/all-user-products", {})
            categories = (resp.get("response") or {}).get("data") or []
            for cat in categories:
                cat_id = cat.get("id")
                p_type = _map_product_type(cat_id)
                if p_type == ProductType.UNKNOWN:
                    continue
                for item in cat.get("data") or []:
                    raw_id = item.get("policyId")
                    if not raw_id:
                        continue
                    pension_sub = item.get("pensionType")
                    p_sub_enum: PensionType | None = None
                    if p_type == ProductType.PENSION and pension_sub:
                        sub_low = str(pension_sub).lower()
                        if "makifa" in sub_low or "מקיפה" in sub_low:
                            p_sub_enum = PensionType.MAKIFA
                        elif "mashlima" in sub_low or "משלימה" in sub_low:
                            p_sub_enum = PensionType.MASHLIMA
                        elif "vatika" in sub_low or "ותיקה" in sub_low:
                            p_sub_enum = PensionType.VATIKA
                        else:
                            p_sub_enum = PensionType.OTHER
                    status_str = str(item.get("status") or "active").lower()
                    status = (
                        PolicyStatus.ACTIVE
                        if status_str in ("active", "פעילה")
                        else PolicyStatus.INACTIVE
                    )
                    out.append(
                        {
                            "raw_policy_id": raw_id,
                            "product_type": p_type,
                            "pension_type": p_sub_enum,
                            "name": item.get("policyName") or cat_id,
                            "status": status,
                            "state": item.get("state"),
                            "balance": extract_number(item.get("sum"), None),
                            "is_new": bool(item.get("isNew", False)),
                        }
                    )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not fetch portal all-user-products: %s", exc)
        return out

    @staticmethod
    def _build_tracks_from_native(routes: list[dict[str, Any]]) -> list[InvestmentTrack]:
        tracks: list[InvestmentTrack] = []
        for r in routes:
            tracks.append(
                InvestmentTrack(
                    name=str(r.get("name") or ""),
                    code=str(r["basketCode"]) if r.get("basketCode") is not None else None,
                    allocation_pct=extract_number(r.get("percent"), None),
                    balance=extract_number(r.get("accumulation"), None),
                    yield_ytd_pct=extract_number(r.get("yieldBeginningYear"), None),
                    yield_as_of_date=parse_il_date(r.get("dailyUpdateDate")),
                    fee_deposit_pct=extract_number(r.get("managementFeeFromDeposit"), None),
                    fee_savings_pct=extract_number(r.get("managementFeeFromSaving"), None),
                )
            )
        return tracks

    async def _enrich_policy_details(
        self,
        fund: FundPolicy,
        *,
        is_new: bool,
        native_routes: list[dict[str, Any]],
    ) -> None:
        if fund.product_type in _EXCELLENCE_PRODUCT_TYPES:
            await self._enrich_excellence_policy(fund, is_new=is_new, native_routes=native_routes)
        elif fund.product_type == ProductType.PENSION:
            await self._enrich_pension_policy(fund)
        elif fund.product_type == ProductType.FINANCIAL_SAVINGS:
            await self._enrich_financial_policy(fund)

    async def _enrich_excellence_policy(
        self,
        fund: FundPolicy,
        *,
        is_new: bool,
        native_routes: list[dict[str, Any]],
    ) -> None:
        try:
            resp = await self._post_portal(
                "policy/excellence-saving",
                {"savingNum": fund.raw_policy_id, "isNew": is_new},
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not fetch excellence-saving detail for %s: %s", fund.policy_id, exc)
            return

        detail = resp.get("response") or {}
        if not isinstance(detail, dict) or not detail:
            return

        general = detail.get("general") or {}
        if general.get("policyName"):
            fund.name = general["policyName"]
        if general.get("isSeif14") is not None:
            fund.is_section_14 = bool(general["isSeif14"])
        if general.get("updatedAt"):
            fund.balance_date = parse_il_date(general["updatedAt"]) or fund.balance_date

        agents = detail.get("agentDetails") or []
        if isinstance(agents, list) and agents and isinstance(agents[0], dict):
            fund.agent_name = agents[0].get("name") or fund.agent_name

        notice_gen = (detail.get("noticeUpdate") or {}).get("generalDetails") or {}
        if notice_gen.get("employerName"):
            fund.employer_name = notice_gen["employerName"]
        if notice_gen.get("establishmentDate"):
            fund.establishment_date = parse_il_date(notice_gen["establishmentDate"])
        if notice_gen.get("startDate"):
            fund.start_date = parse_il_date(notice_gen["startDate"])
        if notice_gen.get("withdrawDate") and not fund.liquidity_date:
            fund.liquidity_date = parse_il_date(notice_gen["withdrawDate"])

        # Management fees + discount validity + investment expense commission
        mng_fee = detail.get("managementFee") or {}
        pct_fee = mng_fee.get("percentageMngFee") or {}
        upd_fee = mng_fee.get("updatedMngFee") or {}

        dep_fee = extract_number(pct_fee.get("fromDeposit"), None)
        if dep_fee is None:
            dep_fee = extract_number(upd_fee.get("fromDeposit"), fund.fees.from_deposit_pct)
        sav_fee = extract_number(pct_fee.get("fromSaving"), None)
        if sav_fee is None:
            sav_fee = extract_number(upd_fee.get("fromSaving"), fund.fees.from_savings_pct)

        disc_from: str | None = None
        disc_to: str | None = None
        popup_list = ((upd_fee.get("fromSaving") or {}).get("popupData") or {}).get("list") or []
        if popup_list and isinstance(popup_list[0], dict):
            disc_from = parse_il_date(popup_list[0].get("dateFrom"))
            disc_to = parse_il_date(popup_list[0].get("dateTo"))

        cost_pred_list = (detail.get("yearCostPrediction") or {}).get("list") or []
        inv_exp_by_title: dict[str, float] = {}
        first_inv_exp: float | None = None
        for cp in cost_pred_list:
            if not isinstance(cp, dict):
                continue
            exp_c = extract_number(cp.get("expenseCommission"), None)
            exp_nc = extract_number(cp.get("expenseNonCommission"), 0.0) or 0.0
            if exp_c is not None:
                total_exp = round(exp_c + exp_nc, 4)
                if first_inv_exp is None and cp.get("savingFee") is not None:
                    first_inv_exp = total_exp
                if cp.get("title"):
                    inv_exp_by_title[str(cp["title"]).strip()] = total_exp

        fund.fees = ManagementFees(
            from_deposit_pct=dep_fee,
            from_savings_pct=sav_fee,
            investment_expense_pct=first_inv_exp,
            discount_valid_from=disc_from,
            discount_valid_until=disc_to,
        )

        # Investment tracks (merge native basketCode & fees with portal route details)
        native_by_name = {str(r.get("name") or "").strip(): r for r in native_routes}
        portal_routes = (detail.get("investmentRoutesTransferConcentration") or {}).get(
            "investmentRoutes", {}
        ).get("list") or []
        if portal_routes:
            merged_tracks: list[InvestmentTrack] = []
            for idx, pr in enumerate(portal_routes):
                title = str(pr.get("investmentRouteTitle") or "").strip()
                nat = native_by_name.get(title) or (
                    native_routes[idx] if idx < len(native_routes) else {}
                )
                merged_tracks.append(
                    InvestmentTrack(
                        name=title,
                        code=str(nat["basketCode"]) if nat.get("basketCode") is not None else None,
                        allocation_pct=extract_number(
                            pr.get("investmentPercent"),
                            extract_number(nat.get("percent"), None),
                        ),
                        balance=extract_number(
                            pr.get("investmentSum"),
                            extract_number(nat.get("accumulation"), None),
                        ),
                        yield_ytd_pct=extract_number(
                            pr.get("yieldPercentage"),
                            extract_number(nat.get("yieldBeginningYear"), None),
                        ),
                        yield_as_of_date=(
                            parse_il_date(pr.get("dailyYieldUpdateDate"))
                            or parse_il_date(pr.get("tsuotLastUpdateDate"))
                            or parse_il_date(nat.get("dailyUpdateDate"))
                        ),
                        fee_deposit_pct=extract_number(
                            nat.get("managementFeeFromDeposit"), dep_fee
                        ),
                        fee_savings_pct=extract_number(nat.get("managementFeeFromSaving"), sav_fee),
                        investment_expense_pct=inv_exp_by_title.get(title, first_inv_exp),
                    )
                )
            fund.tracks = merged_tracks

        # Expected payments & liquidity date fallback
        exp_payments = (detail.get("expectedPayments") or {}).get("list") or []
        for ep in exp_payments:
            if not isinstance(ep, dict):
                continue
            title = str(ep.get("title") or "")
            subtitle = ep.get("subTitle")
            amt = extract_number(ep.get("sum"), None)
            fund.expected_payments.append(
                ExpectedPayment(title=title, subtitle=subtitle, amount=amt)
            )
            if not fund.liquidity_date and ("משיכה חד פעמית" in title or "סכום למשיכה" in title):
                fund.liquidity_date = parse_il_date(subtitle)

        # Yearly movements (יתרה לתחילת שנה, הפקדות, רווחים, דמי ניהול, יתרה לתאריך)
        acct_txns = detail.get("accountTransactions") or {}
        if not fund.liquidity_date and acct_txns.get("oneTimeWithdrawDate"):
            fund.liquidity_date = parse_il_date(acct_txns.get("oneTimeWithdrawDate"))
        fund.yearly_movements = self._parse_yearly_movements(acct_txns.get("list") or [])

        # Detailed deposits from yearlyDeposits (with employee/employer split & salaryMonth)
        yearly_deps = ((detail.get("deposits") or {}).get("yearlyDeposits") or {}).get("list") or []
        detailed_deposits = self._parse_yearly_deposits(fund.raw_policy_id, yearly_deps)
        if detailed_deposits:
            fund.deposits = detailed_deposits

    async def _enrich_pension_policy(self, fund: FundPolicy) -> None:
        p_type_str = (fund.pension_type or PensionType.MAKIFA).value
        body: dict[str, Any] = {"type": p_type_str}
        if fund.pension_type == PensionType.VATIKA:
            body["savingNum"] = fund.raw_policy_id
        try:
            resp = await self._post_portal("policy/pension", body)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not fetch pension detail for %s: %s", fund.policy_id, exc)
            return

        detail = resp.get("response") or {}
        if not isinstance(detail, dict) or not detail:
            return

        general = detail.get("general") or {}
        if general.get("policyName"):
            fund.name = general["policyName"]

        mng_fee = (detail.get("managementFee") or {}).get("updatedMngFee") or {}
        fund.fees = ManagementFees(
            from_deposit_pct=extract_number(mng_fee.get("fromDeposit"), fund.fees.from_deposit_pct),
            from_savings_pct=extract_number(mng_fee.get("fromSaving"), fund.fees.from_savings_pct),
        )

        routes = (detail.get("investmentRoutes") or {}).get("routes") or []
        if routes:
            fund.tracks = [
                InvestmentTrack(
                    name=str(r.get("investmentRouteTitle") or ""),
                    allocation_pct=100.0
                    if len(routes) == 1
                    else extract_number(r.get("percent"), None),
                    yield_ytd_pct=extract_number(r.get("yieldPercentage"), None),
                    fee_deposit_pct=fund.fees.from_deposit_pct,
                    fee_savings_pct=fund.fees.from_savings_pct,
                )
                for r in routes
            ]

        for cover in (detail.get("covers") or {}).get("list") or []:
            if isinstance(cover, dict):
                fund.insurance_covers.append(
                    InsuranceCover(
                        title=str(cover.get("coverTitle") or ""),
                        description=cover.get("coverDesc"),
                        insured_amount=extract_number(cover.get("coverSum"), None),
                    )
                )

        acct_txns = (detail.get("accountTransactions") or {}).get("list") or []
        if acct_txns:
            fund.yearly_movements = self._parse_yearly_movements(acct_txns)

        deps_year = (detail.get("depositsYear") or {}).get("list") or []
        detailed_deposits = self._parse_yearly_deposits(fund.raw_policy_id, deps_year)
        if detailed_deposits:
            fund.deposits = detailed_deposits

    async def _enrich_financial_policy(self, fund: FundPolicy) -> None:
        try:
            resp = await self._post_portal("policy/financial", {"savingNum": fund.raw_policy_id})
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not fetch financial detail for %s: %s", fund.policy_id, exc)
            return

        detail = resp.get("response") or {}
        if not isinstance(detail, dict) or not detail:
            return
        general = detail.get("general") or {}
        if general.get("policyName"):
            fund.name = general["policyName"]

    @staticmethod
    def _parse_yearly_deposits(
        raw_policy_id: str,
        years_list: list[dict[str, Any]],
    ) -> list[DepositTransaction]:
        p_key = policy_id_key(raw_policy_id)
        out: list[DepositTransaction] = []
        for year_entry in years_list:
            if not isinstance(year_entry, dict):
                continue
            for dep in year_entry.get("list") or []:
                if not isinstance(dep, dict):
                    continue
                dt = parse_il_date(dep.get("depositDate"))
                total = extract_number(dep.get("totalDeposit"), None)
                if not dt or total is None:
                    continue
                emp_name = dep.get("employerName") or None
                emp_amt = extract_number(dep.get("employeeDeposit"), None)
                empr_amt = extract_number(dep.get("employerDeposit"), None)
                comp_amt = extract_number(dep.get("compensationDeposit"), None)
                dis_amt = extract_number(dep.get("workDisfunctionLossDeposit"), None)
                sal_amt = extract_number(dep.get("totalSalary"), None)
                sal_month = parse_salary_month(dep.get("salaryMonth"))
                desc = f"הפקדה - {emp_name}" if emp_name else "הפקדה"
                out.append(
                    DepositTransaction(
                        identifier=f"{p_key}_{dt}_{total}",
                        date=dt,
                        total_amount=total,
                        salary_month=sal_month,
                        employer_name=emp_name,
                        employee_amount=emp_amt,
                        employer_amount=empr_amt,
                        compensation_amount=comp_amt,
                        disability_amount=dis_amt,
                        insured_salary=sal_amt,
                        description=desc,
                    )
                )
        return out

    @staticmethod
    def _parse_yearly_movements(raw_list: list[dict[str, Any]]) -> list[YearlyMovementSummary]:
        if not raw_list:
            return []
        # Pension sometimes returns a flat list of MovementItems for the current year,
        # while Hishtalmut returns a list of {year, updateDate, list: [...]}.
        if raw_list and "year" not in raw_list[0] and "title" in raw_list[0]:
            raw_list = [{"year": "current", "updateDate": None, "list": raw_list}]

        summaries: list[YearlyMovementSummary] = []
        for yr_block in raw_list:
            if not isinstance(yr_block, dict):
                continue
            yr = str(yr_block.get("year") or "")
            as_of = parse_il_date(yr_block.get("updateDate"))
            opening: float | None = None
            deposits: float | None = None
            gains: float | None = None
            fees: float | None = None
            ins_cost: float | None = None
            closing: float | None = None
            items: list[MovementItem] = []

            for row in yr_block.get("list") or []:
                if not isinstance(row, dict):
                    continue
                title = str(row.get("title") or "").strip()
                sub = row.get("subTitle")
                amt = extract_number(row.get("sum"), None)
                items.append(MovementItem(title=title, subtitle=sub, amount=amt))
                if "יתרה לתחילת" in title or "יתרת פתיחה" in title:
                    opening = amt
                elif "הפקדות" in title:
                    deposits = amt
                elif "רווחים" in title or "תשואה" in title:
                    gains = amt
                elif "דמי ניהול" in title:
                    fees = amt
                elif "ביטוח" in title or "כיסוי" in title:
                    ins_cost = amt
                elif "יתרה לתאריך" in title or "יתרת סגירה" in title:
                    closing = amt
                    if not as_of:
                        as_of = parse_il_date(title)

            summaries.append(
                YearlyMovementSummary(
                    year=yr,
                    as_of_date=as_of,
                    opening_balance=opening,
                    deposits=deposits,
                    gains=gains,
                    management_fees=fees,
                    insurance_costs=ins_cost,
                    closing_balance=closing,
                    items=items,
                )
            )
        return summaries
