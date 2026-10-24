# Contributing to israeli-pension-scrapers

Thanks for helping. Bug reports, new pension/insurance provider scrapers, fixes and docs are all welcome.

## Before you start

- For anything bigger than a small fix, open an issue first so we can agree on the approach.
- **Never paste real pension/insurance data, ID numbers, phone numbers, tokens, policy numbers or screenshots with personal details into issues, PRs, tests or fixtures.** Always use synthetic fake data (`123456789`, `0501234567`, etc.).
- Security problems go through [SECURITY.md](SECURITY.md), not public issues.

## Setup

```bash
uv sync
```

## Adding a new provider

1. Create `src/israeli_pension_scrapers/providers/<provider>.py` subclassing `BasePensionScraper`.
2. Map all provider endpoints into the unified `PensionScrapingResult` schema (`InsuredProfile`, `PortfolioSummary`, `FundPolicy`, `InvestmentTrack`, `DepositTransaction`, `YearlyMovementSummary`, `InsuranceCover`, `ExpectedPayment`).
3. Persist long-term refresh/PIN tokens on `self.refreshed_long_term_token` so background syncs do not require interactive SMS OTP on every run.
4. Register the provider in `src/israeli_pension_scrapers/__init__.py` (`PROVIDERS` and `create_scraper`).
5. Add unit tests under `tests/test_<provider>.py` using `httpx.MockTransport` and synthetic fixtures.

## Making a change

1. Fork the repo and create a branch from `master`.
2. Make your change and add or update tests.
3. Run the checks:

   ```bash
   uv run ruff check . && uv run ruff format --check .
   uv run pytest -q
   ```

4. Use [Conventional Commits](https://www.conventionalcommits.org) for commit messages, for example `feat(migdal): support hishtalmut world` or `fix(haphoenix): handle empty deposit list`.
5. Open a pull request. CI must pass before merge.

## Code of conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).
