# israeli-pension-scrapers

Unified async Python scrapers (`httpx`) for Israeli pension, keren hishtalmut, kupat gemel, gemel le-hashkaa, and long-term savings providers — outputting a canonical, provider-agnostic JSON schema (`PensionScrapingResult`).

## Supported Providers

| Provider Key | Institution | Hebrew Name | Auth Flow |
|---|---|---|---|
| `haphoenix` (`hafenix`) | The Phoenix Insurance Co. | הפניקס | SMS OTP (`id` + `phoneNumber`) + 6-month `otpLongTermToken` (`refresh_token`) |
| `migdal` | Migdal Insurance & Financial Holdings | מגדל | SMS OTP (`id`) + AES-256-CBC `otpLongTermToken` (`PNLoginV2` / `PNRegistrationV2`) |

## Unified JSON Output Schema

Every provider returns a `PensionScrapingResult` (`.to_dict()` / `.to_json()`) with:
- `insured`: `InsuredProfile` (`id_number`, `first_name`, `last_name`, `email`, `phone`, `birth_date`, `gender`, `city`, `street`)
- `portfolio_summary`: `PortfolioSummary` (`total_balance`, `liquid_balance`, `currency`, `balance_history`, `asset_exposure`)
- `funds`: `list[FundPolicy]`, each containing:
  - Normalized `policy_id` + `raw_policy_id` + `internal_account_id`
  - `product_type` (`pension` | `hishtalmut` | `gemel` | `gemel_investment` | `financial_savings` | `pizuim` | `child_savings` | `life_savings`)
  - `pension_type` (`makifa` | `mashlima` | `vatika` | `None`)
  - `status` (`active` | `inactive` | `expired`), `employment_status` (`employee` | `self_employed`)
  - `name`, `manufacturer`, `employer_name`, `agent_name`
  - `balance`, `as_of_balance`, `liquid_balance`, `taxable_balance`, `currency`
  - `balance_date`, `data_as_of_date`, `establishment_date` (seniority/vetek), `start_date`, `liquidity_date`
  - `fees`: `ManagementFees` (`from_deposit_pct`, `from_savings_pct`, `investment_expense_pct`, `discount_valid_from`, `discount_valid_until`)
  - `tracks`: `list[InvestmentTrack]` (`code`, `name`, `allocation_pct`, `balance`, `yield_ytd_pct`, `yield_as_of_date`, `fee_deposit_pct`, `fee_savings_pct`, `investment_expense_pct`)
  - `deposits`: `list[DepositTransaction]` (`identifier`, `date`, `salary_month`, `total_amount`, `employee_amount`, `employer_amount`, `compensation_amount`, `disability_amount`, `insured_salary`, `employer_name`, `payment_method`, `description`)
  - `yearly_movements`: `list[YearlyMovementSummary]` (`year`, `as_of_date`, `opening_balance`, `deposits`, `gains`, `management_fees`, `insurance_costs`, `closing_balance`, `items`)
  - `insurance_covers`: `list[InsuranceCover]`
  - `expected_payments`: `list[ExpectedPayment]`
  - `balance_history`: `list[BalancePoint]`

## Usage

```python
import asyncio
from israeli_pension_scrapers import create_scraper


async def main():
    scraper = create_scraper(
        "haphoenix",
        {
            "id": "123456789",
            "phoneNumber": "0501234567",
            # Optional on subsequent runs to skip SMS OTP:
            # "otpLongTermToken": "v1.M...",
        },
    )
    scraper.on_otp_request = async_prompt_for_otp
    result = await scraper.scrape()
    print(result.to_json(indent=2))


asyncio.run(main())
```
