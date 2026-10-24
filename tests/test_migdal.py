"""Unit tests for MigdalScraper and AES-256-CBC PIN token encryption."""

from __future__ import annotations

import json

import httpx
import pytest

from israeli_pension_scrapers import (
    PensionType,
    ProductType,
    create_scraper,
    encrypt_migdal_pin_token,
)


def test_encrypt_migdal_pin_token_matches_native_jni() -> None:
    """Verify AES-256-CBC encryption matches AesCryptorManager.b() output byte-for-byte."""
    token = encrypt_migdal_pin_token(
        "123456789",
        "0123456789abcdef",
        "00000000-1111-2222-3333-444444444444",
        "1234",
    )
    expected = (
        "pQmgio6QNpxlDwqM69MD7JZkDRkEKU6HloC3IAO1ggK1Evw73IXRTvtYQSaf0UL2Tlz0miMHR29a\n"
        "rTKhpnhvMyQ9frKZeOrq3FyS9KaiWBs=\n"
    )
    assert token == expected


@pytest.mark.asyncio
async def test_migdal_pnlogin_and_full_portfolio_parsing() -> None:
    """Verify MigdalScraper parses Pensia (Makifa + Mashlima), tracks, fees, covers, and deposits."""
    account_settings_resp = {
        "Status": 0,
        "Data": {
            "entityCustomerDetails": [
                {
                    "PersonalDetails": {
                        "BirthDate": "01/01/1990",
                        "FirstName": "ישראל",
                        "LastName": "ישראלי",
                        "Gender": 1,
                        "IdNumber": "123456789",
                    },
                    "CommunicationDetails": {
                        "Email": "israel@example.com",
                        "MobileNumber": "0501234567",
                        "City": "תל אביב",
                        "Street": "הרצל",
                        "HouseNumber": "10",
                    },
                }
            ]
        },
    }
    worlds_resp = {
        "Status": 0,
        "Data": {
            "Pensia": {
                "RedemptionValues": "1,177,248",
                "Footer": [{"Text": "נכון לתאריך 25/10/2026"}],
                "Policies": [
                    {
                        "PolicyNumber": 1,
                        "PolicyType": "1",
                        "SugKeren": 1,
                        "Title": "מגדל מקפת אישית",
                        "MoneyVal": "747,388",
                        "EmploymentStatus": 1,
                        "ExpectedPremium": "12,162",
                    },
                    {
                        "PolicyNumber": 2,
                        "PolicyType": "1",
                        "SugKeren": 2,
                        "Title": "מגדל מקפת משלימה",
                        "MoneyVal": "429,860",
                        "EmploymentStatus": 1,
                        "ExpectedPremium": "5,071",
                    },
                ],
            }
        },
    }
    tabs_resp = {
        "Status": 0,
        "Data": {
            "UserAgentsData": {
                "Agents": [
                    {
                        "AgencyName": 'סוכנות לדוגמה בע"מ',
                        "Name": "סוכן ראשי",
                        "Worlds": [2],
                    }
                ]
            },
            "WorldPoliciesList": [
                {
                    "World": 2,
                    "Policies": [
                        {
                            "PolicyNumber": 1,
                            "PolicyType": "1",
                            "SugKeren": 1,
                            "PolicyHeaderSubtitle": "מגדל מקפת אישית",
                            "Tabs": [
                                {
                                    "TabType": 0,
                                    "TabTitle": "תשלומים צפויים מקרן הפנסיה",
                                    "TabSections": [
                                        {
                                            "TabData": [
                                                {
                                                    "SubTitle": "תחזית קצבה חודשית הצפויה לך בפרישה בגיל 67",
                                                    "Value1": '12,162 ש"ח',
                                                },
                                                {
                                                    "SubTitle": "קצבה חודשית לאלמן/ה במקרה מוות",
                                                    "Value1": '16,364 ש"ח',
                                                },
                                                {
                                                    "SubTitle": "קצבה חודשית במקרה של נכות מלאה",
                                                    "Value1": '20,455 ש"ח',
                                                },
                                            ]
                                        }
                                    ],
                                },
                                {
                                    "TabType": 0,
                                    "TabTitle": "תנועות בקרן",
                                    "TabSections": [
                                        {
                                            "TabData": [
                                                {
                                                    "SubTitle": "יתרת הכספים בקרן בתחילת השנה",
                                                    "Value1": '630,163 ש"ח',
                                                },
                                                {
                                                    "SubTitle": "כספים שהופקדו לקרן",
                                                    "Value1": '56,267 ש"ח',
                                                },
                                                {
                                                    "SubTitle": "רווחים בניכוי הוצאות ניהול השקעות",
                                                    "Value1": '65,400 ש"ח',
                                                },
                                                {
                                                    "SubTitle": "דמי ניהול שנגבו השנה",
                                                    "Value1": '-1,179 ש"ח',
                                                },
                                                {
                                                    "SubTitle": "עלות ביטוח נכות שנגבתה השנה",
                                                    "Value1": '-2,102 ש"ח',
                                                },
                                                {
                                                    "SubTitle": "יתרת הכספים בקרן בתאריך 25/10/2026",
                                                    "Value1": '747,388 ש"ח',
                                                },
                                            ]
                                        }
                                    ],
                                },
                                {
                                    "TabType": 0,
                                    "TabTitle": "דמי ניהול",
                                    "TabSections": [
                                        {
                                            "TabData": [
                                                {"SubTitle": "דמי ניהול מהפקדה", "Value1": "1.30%"},
                                                {
                                                    "SubTitle": "דמי ניהול מחיסכון",
                                                    "Value1": "0.08%",
                                                },
                                            ]
                                        }
                                    ],
                                },
                                {
                                    "TabType": 0,
                                    "TabTitle": "פרטי הקרן",
                                    "TabSections": [
                                        {
                                            "TabData": [
                                                {
                                                    "SubTitle": "מועד הצטרפות לראשונה לקרן",
                                                    "Value1": "02/03/2025",
                                                },
                                                {
                                                    "SubTitle": "מועד תחילת חישוב ותק העמית בקרן",
                                                    "Value1": "01/04/2017",
                                                },
                                                {"SubTitle": "סוג עמית", "Value1": "פעיל"},
                                                {"SubTitle": "מעמד עמית", "Value1": "שכיר"},
                                                {
                                                    "SubTitle": "שם המעסיק האחרון",
                                                    "Value1": 'חברה לדוגמה בע"מ',
                                                },
                                            ]
                                        }
                                    ],
                                },
                            ],
                        }
                    ],
                }
            ],
        },
    }
    change_inv_resp = {
        "Status": 0,
        "Data": {
            "content": [
                {
                    "policies": {
                        "content": [
                            {
                                "policyType": "1",
                                "policyKey": {
                                    "PolicyNumber": "11223344",
                                    "PolicyType": "1",
                                    "WorldType": 2,
                                },
                                "policyNumber": {"Title": "מגדל מקפת אישית", "Value": "11223344"},
                            }
                        ]
                    }
                }
            ]
        },
    }
    inv_path_resp = {
        "entityTabSections": [
            {
                "tabData": [
                    {"SubTitle": "חלוקת תשלומים עתידיים למסלולי השקעה"},
                    {"SubTitle": "מניות", "Value1": "100%", "Value2": "100%"},
                    {"SubTitle": "צבירה קיימת במסלולי השקעה"},
                    {"SubTitle": "מניות", "Value1": "310,008", "Value2": "437,380"},
                    {"SubTitle": "נכון לתאריך 25/10/2026"},
                    {"SubTitle": "תשואות למסלולי ההשקעה"},
                    {"SubTitle": "מניות", "Value1": "10.02%"},
                ]
            }
        ]
    }
    deposits_resp = {
        "entityTabSections": [
            {
                "CompositeData": [
                    {
                        "TabData": [
                            {"SubTitle": "עבור חודש משכורת", "Value1": "09/2026"},
                            {"SubTitle": "משכורת", "Value1": '27,102 ש"ח'},
                            {"SubTitle": "תגמולי עובד", "Value1": '1,626 ש"ח'},
                            {"SubTitle": "תגמולי מעסיק", "Value1": '1,762 ש"ח'},
                            {"SubTitle": "פיצויים", "Value1": '2,258 ש"ח'},
                        ]
                    }
                ]
            }
        ]
    }

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/ApplicationSettings/AppSettings"):
            return httpx.Response(200, headers={"usersessionguid": "guid-1"}, json={"Status": 0})
        if path.endswith("/Login/PNLoginV2"):
            return httpx.Response(
                200,
                headers={"Authorization": "Bearer jwt-tok", "usersessionguid": "guid-1"},
                json={"Status": 0, "Data": {"ResultCode": 0, "AppLoginSettings": {"age": 36}}},
            )
        if path.endswith("/Account/GetAccountSettings"):
            return httpx.Response(200, json=account_settings_resp)
        if path.endswith("/InsuranceWorlds/All"):
            return httpx.Response(200, json=worlds_resp)
        if path.endswith("/Tabs/All"):
            return httpx.Response(200, json=tabs_resp)
        if path.endswith("/ChangeInvestment/GetWorldsAndPolicies"):
            return httpx.Response(200, json=change_inv_resp)
        if path.endswith("/Tabs/InvestmentPathUpdated"):
            return httpx.Response(200, json=inv_path_resp)
        if path.endswith("/Deposits/GetDeposits"):
            return httpx.Response(200, json=deposits_resp)
        return httpx.Response(200, json={"Status": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        scraper = create_scraper(
            "migdal",
            {
                "id": "123456789",
                "otpLongTermToken": json.dumps(
                    {
                        "userId": "123456789",
                        "androidId": "0123456789abcdef",
                        "guId": "00000000-1111-2222-3333-444444444444",
                        "pinCode": "1234",
                    }
                ),
            },
            client=client,
        )
        res = await scraper.scrape()

    assert res.success is True
    assert res.insured is not None
    assert res.insured.first_name == "ישראל"
    assert res.insured.last_name == "ישראלי"
    assert res.insured.email == "israel@example.com"
    assert len(res.funds) == 1

    f = res.funds[0]
    assert f.policy_id == "11223344"
    assert f.product_type == ProductType.PENSION
    assert f.pension_type == PensionType.MAKIFA
    assert f.balance == 747388.0
    assert f.start_date == "2025-03-02"
    assert f.establishment_date == "2017-04-01"
    assert f.employer_name == 'חברה לדוגמה בע"מ'
    assert f.fees.from_deposit_pct == 1.3
    assert f.fees.from_savings_pct == 0.08
    assert len(f.tracks) == 1
    assert f.tracks[0].name == "מניות"
    assert f.tracks[0].balance == 747388.0
    assert f.tracks[0].yield_ytd_pct == 10.02
    assert len(f.deposits) == 1
    assert f.deposits[0].salary_month == "2026-09"
    assert f.deposits[0].total_amount == 5646.0
    assert f.deposits[0].employee_amount == 1626.0
    assert f.deposits[0].employer_amount == 1762.0
    assert f.deposits[0].compensation_amount == 2258.0
