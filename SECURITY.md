# Security policy

`israeli-pension-scrapers` handles authentication and financial portfolio scraping for Israeli institutional providers, so we take security reports seriously.

## Reporting a vulnerability

**Do not open a public issue.** Report privately through
[GitHub Security Advisories](https://github.com/RebliNk17/israeli-pension-scrapers/security/advisories/new).

Include what you found, how to reproduce it, and the impact. We aim to reply within 7 days.

## Supported versions

Only the latest release on `master` receives security fixes.

## Guidelines for callers

- Store `otpLongTermToken` and user credentials encrypted at rest.
- Never log raw credentials, OTP codes, JWT bearer tokens or `otpLongTermToken` values.
