# israeli-pension-scrapers

[![PyPI version](https://img.shields.io/pypi/v/israeli-pension-scrapers.svg)](https://pypi.org/project/israeli-pension-scrapers/)
[![Python versions](https://img.shields.io/pypi/pyversions/israeli-pension-scrapers.svg)](https://pypi.org/project/israeli-pension-scrapers/)
[![CI](https://github.com/RebliNk17/israeli-pension-scrapers/actions/workflows/ci.yml/badge.svg)](https://github.com/RebliNk17/israeli-pension-scrapers/actions/workflows/ci.yml)
[![License](https://img.shields.io/github/license/RebliNk17/israeli-pension-scrapers)](LICENSE)

A unified, modern async Python library to scrape your pension (*קרן פנסיה*), education funds (*קרן השתלמות*), and provident funds (*קופת גמל*) from Israeli insurance and pension companies.

Instead of parsing proprietary, ever-changing web pages, **`israeli-pension-scrapers`** connects directly to provider APIs and returns your financial data in a clean, standardized format.

---

## Supported Providers

| Provider Key | Institution | Hebrew Name | Supported Products |
|---|---|---|---|
| `haphoenix` | The Phoenix | הפניקס | Pension, Hishtalmut, Kupat Gemel, Financial Savings |
| `migdal` | Migdal | מגדל | Pension, Hishtalmut, Kupat Gemel, Executive Insurance |

*(More providers coming soon! PRs and contributions welcome.)*

---

## Installation

Using `pip`:
```bash
pip install israeli-pension-scrapers
```

Using `uv`:
```bash
uv add israeli-pension-scrapers
```

*(Requires Python 3.11+)*

---

## Quickstart

Here is a complete, copy-pasteable example:

```python
import asyncio
from israeli_pension_scrapers import create_scraper


async def prompt_sms_otp() -> str:
    # When a code arrives via SMS, ask the user to enter it:
    return input("Enter the SMS OTP code you received: ").strip()


async def main():
    # 1. Initialize the scraper for your provider
    scraper = create_scraper(
        provider="haphoenix",  # or "migdal"
        credentials={
            "id": "123456789",  # Teudat Zehut (9 digits)
            "phoneNumber": "0501234567",  # Mobile phone number
        },
    )

    # 2. Attach the OTP handler for SMS verification
    scraper.on_otp_request = prompt_sms_otp

    # 3. Run the scraper
    result = await scraper.scrape()

    if not result.success:
        print(f"Scraping failed: {result.error_type} - {result.error_message}")
        return

    # 4. View your portfolio overview
    print(f"👤 Insured: {result.insured.first_name} {result.insured.last_name}")
    print(f"💰 Total Savings Balance: ₪{result.portfolio_summary.total_balance:,.2f}")
    print(f"💧 Liquid (Withdrawable): ₪{result.portfolio_summary.liquid_balance:,.2f}")

    # 5. Inspect individual policies and funds
    for fund in result.funds:
        print(f"\n📂 {fund.name} ({fund.product_type})")
        print(f"   Balance: ₪{fund.balance:,.2f}")
        print(
            f"   Management Fees: {fund.fees.from_deposit_pct}% from deposit, {fund.fees.from_savings_pct}% from savings"
        )

        # Active investment tracks
        for track in fund.tracks:
            print(
                f"   - Track: {track.name} ({track.allocation_pct}% allocation, ₪{track.balance:,.2f})"
            )

    # 6. Save long-term token for passwordless future runs!
    if result.long_term_token:
        print(f"\n🔑 Save this token for subsequent runs: {result.long_term_token}")


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Silent Logins (Skip the SMS OTP!)

You don't want to type an SMS verification code every time your script runs. 

When you log in for the first time, the provider issues a **long-term refresh token** (valid for several months). Save `result.long_term_token` in your environment or database:

```python
# On subsequent runs, pass the saved otpLongTermToken:
scraper = create_scraper(
    provider="haphoenix",
    credentials={
        "id": "123456789",
        "phoneNumber": "0501234567",
        "otpLongTermToken": "YOUR_SAVED_TOKEN_HERE",  # <-- skips SMS prompt entirely!
    },
)

result = await scraper.scrape()
```
The scraper will authenticate silently in the background without sending any SMS.

---

## What Data Do You Get?

The returned `PensionScrapingResult` provides:

### 1. Portfolio Summary (`result.portfolio_summary`)
- `total_balance`: Total savings balance across all policies in ILS.
- `liquid_balance`: Amount immediately available for withdrawal.
- `asset_exposure`: Exposure breakdown across equities, foreign currency, bonds, etc.
- `balance_history`: Historical monthly balances.

### 2. Fund Policies (`result.funds`)
Each policy in the `funds` list contains:
- **Details**: Policy number, fund name, employer name, managing company.
- **Type**: `pension`, `hishtalmut`, `gemel`, `gemel_investment`, `financial_savings`.
- **Balances**: Current balance, liquid balance, seniority date, liquidity date.
- **Fees**: Deposit fee percentage (`from_deposit_pct`) and accumulated savings fee percentage (`from_savings_pct`).
- **Investment Tracks**: List of tracks (e.g. S&P 500, General Track), allocation percentage, balance, and YTD yield.
- **Deposits**: Recent deposit history with employee, employer, and compensation breakdown.
- **Insurance Coverage**: Death, disability, and monthly pension coverages (if applicable).

### 3. Export to Dictionary or JSON
Easily serialize the entire output:
```python
# As a Python dictionary
data = result.to_dict()

# As formatted JSON string
json_str = result.to_json(indent=2)
```

---

## Advanced Options

You can customize timeout, proxy, and data extraction settings:

```python
from israeli_pension_scrapers import ScraperOptions, create_scraper

options = ScraperOptions(
    timeout=45.0,  # Request timeout in seconds
    proxy="http://user:pass@proxy.example.com:8080",  # Optional HTTP/HTTPS proxy
    include_balance_history=True,  # Fetch historical balance points
    include_policy_details=True,  # Fetch deeper details (tracks, fees, deposits)
)

scraper = create_scraper("haphoenix", credentials, options=options)

# Track progress events:
scraper.on_progress = lambda stage: print(f"Stage: {stage}")
```

---

## Contributing

We welcome contributions for new providers, bug fixes, and documentation improvements! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for local development instructions.

## License

This project is licensed under the [Apache-2.0 License](LICENSE).
