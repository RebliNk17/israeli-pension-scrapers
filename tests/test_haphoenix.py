"""Unit tests for israeli-pension-scrapers and the HaPhoenix provider."""

from __future__ import annotations

import json

import httpx
import pytest

from israeli_pension_scrapers import (
    EmploymentStatus,
    PensionType,
    PolicyStatus,
    ProductType,
    create_scraper,
)
from israeli_pension_scrapers.utils import (
    extract_number,
    normalize_israeli_id,
    normalize_israeli_phone,
    parse_il_date,
    parse_salary_month,
    split_policy_id,
)


def _mock_transport(routes: dict[str, dict | callable]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.host}{request.url.path}"
        entry = routes.get(key) or routes.get(request.url.path)
        if entry is None:
            return httpx.Response(200, json={"response": {}, "status": 200})
        if callable(entry):
            return entry(request)
        return httpx.Response(200, json=entry)

    return httpx.MockTransport(handler)


def test_utils_normalizers() -> None:
    assert split_policy_id("001-234-567890 (01234567)") == ("001-234-567890", "01234567")
    assert split_policy_id("001-234-567890") == ("001-234-567890", None)
    assert normalize_israeli_id("123456789") == "123456789"
    assert normalize_israeli_id("1234567") == "001234567"
    assert normalize_israeli_phone("+972-50-123-4567") == "0501234567"
    assert normalize_israeli_phone("501234567") == "0501234567"
    assert parse_il_date("20.10.2026") == "2026-10-20"
    assert parse_il_date("06.09.26") == "2026-09-06"
    assert parse_il_date("10.2025") == "2025-10-01"
    assert parse_il_date("יתרת הכספים החל מ31.01.2031") == "2031-01-31"
    assert parse_salary_month("08/2026") == "2026-08"
    assert extract_number({"value": 114909.36, "currency": "₪"}) == 114909.36
    assert extract_number({"percentageData": {"value": 0.6, "sign": "%"}}) == 0.6
    assert extract_number("51,851") == 51851.0


@pytest.mark.asyncio
async def test_haphoenix_otp_login_and_unified_scrape() -> None:
    captured_auth_bodies: list[dict] = []

    def handle_auth(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content.decode())
        captured_auth_bodies.append(body)
        return httpx.Response(
            200,
            json={"response": {"oob_code": "oob-123", "mfa_token": "mfa-456"}, "status": 200},
        )

    def handle_get_token(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content.decode())
        assert body == {"mfa_token": "mfa-456", "otp_code": "654321", "oob_code": "oob-123"}
        return httpx.Response(
            200,
            json={
                "response": {
                    "access_token": "jwt-access-token",
                    "refresh_token": "v1.new-refresh-token",
                    "id_token": "jwt-id-token",
                },
                "status": 200,
            },
        )

    routes = {
        "/auth/v1/login/authenticate": handle_auth,
        "/auth/v1/token/get-token": handle_get_token,
        "/api/v1/info/user/insured-details": {
            "response": {
                "firstName": "ישראל",
                "lastName": "ישראלי",
                "gender": "M",
                "phone": "0501234567",
                "email": "israel@example.com",
                "birthdate": "1990-01-01T00:00:00",
            }
        },
        "/investments/v1/investments-products/get-investments-products": {
            "response": {
                "mainHeader": {"totalSaving": 206868, "fluentWithdraw": "51,851"},
                "productTabsList": [
                    {
                        "id": "hishtalmut",
                        "data": [
                            {
                                "policyId": "001-234-567890 (01234567)",
                                "updateTo": "30.09.2026",
                                "dailyUpdateTo": "20.10.2026",
                                "withdrawDate": "31.01.2031",
                                "savingSum": 38351,
                                "managementFee": {
                                    "fromSaving": {"sign": "%", "value": 0.6},
                                    "fromDeposit": {"sign": "%", "value": 0},
                                },
                                "investmentRoutes": [
                                    {
                                        "accumulation": {"currency": "₪", "value": 38351},
                                        "basketCode": "9102",
                                        "dailyUpdateDate": "31.08.2026",
                                        "managementFeeFromDeposit": {"sign": "%", "value": 0},
                                        "managementFeeFromSaving": {"sign": "%", "value": 0.6},
                                        "name": "הפניקס השתלמות עוקב מדד s&p500",
                                        "percent": {"sign": "%", "value": 100},
                                        "yieldBeginningYear": {"sign": "%", "value": 5.7},
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        },
        "/investments/v1/investments-graphs/get-accumulation-graph-details": {
            "response": {
                "accumulationDetails": {
                    "last12Months": {
                        "values": [
                            {
                                "date": "09.2026",
                                "total": {"value": 206787, "currency": "₪"},
                                "byProduct": {"hishtalmut": {"value": 206787, "currency": "₪"}},
                            },
                            {
                                "date": "10.2026",
                                "total": {"value": 206868, "currency": "₪"},
                                "byProduct": {"hishtalmut": {"value": 206868, "currency": "₪"}},
                            },
                        ]
                    }
                }
            }
        },
        "/investments/v1/investments-charts/get-routes-composition-chart-details": {
            "response": {
                "charts": [
                    {
                        "type": "geographic",
                        "items": [{"id": "abroad", "label": 'חו"ל', "percentage": {"value": 77}}],
                        "byProduct": [{"hishtalmut": [{"id": "abroad", "sum": 159276}]}],
                    },
                    {
                        "type": "equityExposure",
                        "items": [{"id": "equity", "label": "מניות", "percentage": {"value": 99}}],
                    },
                ]
            }
        },
        "/api/v1/info/policy/all-user-products": {
            "response": {
                "data": [
                    {
                        "id": "hishtalmut",
                        "data": [
                            {
                                "policyId": "001-234-567890 (01234567)",
                                "policyName": "קרן השתלמות",
                                "status": "active",
                                "state": "שכיר",
                                "sum": {"value": 38351.21, "currency": "₪"},
                                "isNew": False,
                            }
                        ],
                    },
                    {
                        "id": "pension",
                        "data": [
                            {
                                "policyId": "111-222-333444 (998877)",
                                "policyName": "פנסיה מקיפה",
                                "pensionType": "makifa",
                                "status": "active",
                                "state": "שכיר",
                                "sum": {"value": 450000.50, "currency": "₪"},
                                "isNew": False,
                            }
                        ],
                    },
                ]
            }
        },
        "/api/v1/info/withdrawals/withdrawal-list": {
            "response": {
                "list": [
                    {
                        "policyNumber": "001-234-567890 (01234567)",
                        "totalSaving": {"currency": "₪", "value": 37586.8},
                        "taxFreeSaving": None,
                        "taxableSaving": {"currency": "₪", "value": 37586.8},
                        "updateDate": "30.09.2026",
                        "liquidityDate": "31.01.2031",
                    }
                ]
            }
        },
        "/api/v1/info/policy/excellence-saving": {
            "response": {
                "general": {
                    "policyName": "קרן השתלמות",
                    "policyNumber": "001-234-567890 (01234567)",
                    "updatedAt": "20.10.2026",
                    "isSeif14": False,
                },
                "agentDetails": [{"id": "51298", "name": 'סוכנות לדוגמה בע"מ'}],
                "noticeUpdate": {
                    "generalDetails": {
                        "startDate": "18.09.2025",
                        "employerName": "חברה לדוגמה בעמ",
                        "withdrawDate": "31.01.2031",
                        "establishmentDate": "01.01.2025",
                    }
                },
                "managementFee": {
                    "percentageMngFee": {
                        "fromDeposit": {"percentageData": {"value": 0, "sign": "%"}},
                        "fromSaving": {"percentageData": {"value": 0.6, "sign": "%"}},
                    },
                    "updatedMngFee": {
                        "fromSaving": {
                            "percentageData": {"value": 0.6, "sign": "%"},
                            "popupData": {
                                "list": [{"dateFrom": "01.01.2025", "dateTo": "30.09.2030"}]
                            },
                        }
                    },
                },
                "yearCostPrediction": {
                    "list": [
                        {
                            "title": "הפניקס השתלמות עוקב מדד s&p500",
                            "savingFee": {"value": 0.6},
                            "expenseCommission": {"value": 0.05},
                            "expenseNonCommission": {"value": 0.01},
                        }
                    ]
                },
                "investmentRoutesTransferConcentration": {
                    "investmentRoutes": {
                        "list": [
                            {
                                "investmentRouteTitle": "הפניקס השתלמות עוקב מדד s&p500",
                                "investmentSum": {"value": 38351.21, "currency": "₪"},
                                "investmentPercent": {"value": 100, "sign": "%"},
                                "yieldPercentage": {"value": 5.7, "sign": "%"},
                                "dailyYieldUpdateDate": "31.08.2026",
                            }
                        ]
                    }
                },
                "accountTransactions": {
                    "list": [
                        {
                            "year": "2026",
                            "updateDate": "30.09.2026",
                            "list": [
                                {"title": "יתרה לתחילת שנה", "sum": {"value": 20817}},
                                {"title": "הפקדות", "sum": {"value": 14141}},
                                {"title": "רווחים", "sum": {"value": 2629}},
                                {"title": "דמי ניהול", "sum": {"value": -130}},
                                {"title": "יתרה לתאריך 30.09.2026", "sum": {"value": 37587}},
                            ],
                        }
                    ]
                },
                "deposits": {
                    "yearlyDeposits": {
                        "list": [
                            {
                                "year": "2026",
                                "list": [
                                    {
                                        "depositDate": "06.09.2026",
                                        "salaryMonth": "08/2026",
                                        "employerName": "חברה לדוגמה בעמ",
                                        "totalDeposit": {"value": 1571.2, "currency": "₪"},
                                        "employerDeposit": {"value": 1178.4, "currency": "₪"},
                                        "employeeDeposit": {"value": 392.8, "currency": "₪"},
                                        "compensationDeposit": {"value": 0, "currency": "₪"},
                                        "totalSalary": {"value": 15712, "currency": "₪"},
                                    }
                                ],
                            }
                        ]
                    }
                },
            }
        },
        "/api/v1/info/policy/pension": {
            "response": {
                "general": {"policyName": "פנסיה מקיפה"},
                "managementFee": {
                    "updatedMngFee": {
                        "fromDeposit": {"percentageData": {"value": 1.49}},
                        "fromSaving": {"percentageData": {"value": 0.1}},
                    }
                },
                "investmentRoutes": {
                    "routes": [
                        {
                            "investmentRouteTitle": "הפניקס פנסיה מניות",
                            "yieldPercentage": "11.2%",
                        }
                    ]
                },
                "covers": {
                    "list": [
                        {
                            "coverTitle": "קצבת שארים",
                            "coverDesc": "כיסוי לשארים",
                            "coverSum": 25000,
                        }
                    ]
                },
                "depositsYear": {
                    "list": [
                        {
                            "year": "2026",
                            "list": [
                                {
                                    "depositDate": "10.09.2026",
                                    "salaryMonth": "08/2026",
                                    "employerName": "חברה לדוגמה בעמ",
                                    "totalDeposit": 4200,
                                    "employeeDeposit": 1200,
                                    "employerDeposit": 1400,
                                    "compensationDeposit": 1600,
                                }
                            ],
                        }
                    ]
                },
            }
        },
    }

    async with httpx.AsyncClient(transport=_mock_transport(routes)) as client:
        scraper = create_scraper(
            "haphoenix",
            {"id": "123456789", "phoneNumber": "+972-50-123-4567"},
            client=client,
        )

        async def provide_otp() -> str:
            return "654321"

        scraper.on_otp_request = provide_otp
        result = await scraper.scrape()

    assert result.success is True
    assert result.provider == "haphoenix"
    assert result.refreshed_long_term_token == "v1.new-refresh-token"
    assert captured_auth_bodies == [
        {
            "to": "0501234567",
            "mfa_type": "sms",
            "id_type": "id",
            "id_number": "123456789",
        }
    ]

    assert result.insured is not None
    assert result.insured.first_name == "ישראל"
    assert result.insured.birth_date == "1990-01-01"

    assert result.portfolio_summary is not None
    assert result.portfolio_summary.total_balance == 206868.0
    assert result.portfolio_summary.liquid_balance == 51851.0
    assert len(result.portfolio_summary.balance_history) == 2
    assert result.portfolio_summary.asset_exposure is not None
    assert result.portfolio_summary.asset_exposure.equity_pct == 99.0

    assert len(result.funds) == 2
    hish = result.funds[0]
    assert hish.policy_id == "001-234-567890"
    assert hish.internal_account_id == "01234567"
    assert hish.product_type == ProductType.HISHTALMUT
    assert hish.status == PolicyStatus.ACTIVE
    assert hish.employment_status == EmploymentStatus.EMPLOYEE
    assert hish.employer_name == "חברה לדוגמה בעמ"
    assert hish.balance == 38351.21
    assert hish.establishment_date == "2025-01-01"
    assert hish.liquidity_date == "2031-01-31"
    assert hish.fees.from_deposit_pct == 0.0
    assert hish.fees.from_savings_pct == 0.6
    assert hish.fees.investment_expense_pct == 0.06
    assert hish.fees.discount_valid_until == "2030-09-30"
    assert len(hish.tracks) == 1
    assert hish.tracks[0].code == "9102"
    assert hish.tracks[0].yield_ytd_pct == 5.7
    assert len(hish.deposits) == 1
    assert hish.deposits[0].date == "2026-09-06"
    assert hish.deposits[0].salary_month == "2026-08"
    assert hish.deposits[0].employee_amount == 392.8
    assert hish.deposits[0].employer_amount == 1178.4
    assert hish.deposits[0].insured_salary == 15712.0
    assert len(hish.yearly_movements) == 1
    assert hish.yearly_movements[0].opening_balance == 20817.0
    assert hish.yearly_movements[0].gains == 2629.0
    assert hish.yearly_movements[0].management_fees == -130.0

    pen = result.funds[1]
    assert pen.policy_id == "111-222-333444"
    assert pen.product_type == ProductType.PENSION
    assert pen.pension_type == PensionType.MAKIFA
    assert pen.fees.from_deposit_pct == 1.49
    assert pen.fees.from_savings_pct == 0.1
    assert len(pen.insurance_covers) == 1
    assert pen.insurance_covers[0].insured_amount == 25000.0
    assert len(pen.deposits) == 1
    assert pen.deposits[0].compensation_amount == 1600.0

    # Verify JSON round-trip
    as_json = json.loads(result.to_json())
    assert as_json["funds"][0]["product_type"] == "hishtalmut"
    assert as_json["funds"][1]["pension_type"] == "makifa"


@pytest.mark.asyncio
async def test_haphoenix_refresh_token_login_skips_otp() -> None:
    routes = {
        "/auth/v1/token/refresh-token": lambda req: httpx.Response(
            200,
            json={
                "response": {
                    "access_token": "refreshed-jwt",
                    "refresh_token": "v1.rotated-token",
                }
            },
        ),
    }
    async with httpx.AsyncClient(transport=_mock_transport(routes)) as client:
        scraper = create_scraper(
            "haphoenix",
            {"id": "123456789", "otpLongTermToken": "v1.initial-token"},
            client=client,
        )
        result = await scraper.scrape()

    assert result.success is True
    assert result.refreshed_long_term_token == "v1.rotated-token"
