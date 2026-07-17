# Changelog

## 1.6.1
- Account aggregate: lifecycle transition table, dormancy policy thresholds,
  customer tier badges and savings-pilot eligibility helpers.
- Analytics: balance distribution percentiles, concentration and Gini,
  daily flow series, treasury liquidity view.

## 1.6.0
- Payouts: non-binding quotes (fee plus settlement ETA), submission batches,
  rail return-code handling and statement reconciliation.
- Velocity: per-tier and per-channel caps, step-up thresholds, shadow mode
  for trialling a cap before enforcing it.

## 1.5.2
- Facade (`brookpay.core.api`): capability catalogue, deprecation aliases
  (`get_balance`, `read_balance`, `account_snapshot`) scheduled for removal
  in 4.0.0, and a surface-consistency check.
- Wiring: registry naming convention enforced by the deploy verification
  step; binding report exposed on the diagnostics endpoint.

## 1.5.1
- Statements: markdown rendering for the HTML beta, batch generation,
  archival manifests, correction diffs and per-channel legal footers.
- Formatting: locale-aware grouping, minor-unit awareness, cheque number
  spelling, IBAN and card masking.

## 1.5.0
- Compliance export: regulator profiles (delimiter and date format), schema
  versioning, SHA-256 manifest and submission tracking.
- Alerting: critical severity tier, quiet hours, per-user rate limiting and
  a per-pass notification budget.
- API: statement, payout-quote and dispute-intake endpoints; route
  descriptors now drive the dispatch table.

## 1.4.2
- Statement rendering: thousands separators and per-currency balance line.
- Compliance: daily export of balance read events (CSV under `var/exports/`).

## 1.4.0
- Low balance alerting job driven by the balance snapshot cache.
- Velocity limits on withdrawals (per hour count, per day EUR volume).

## 1.3.0 (ticket PAY-1187)
- Account freezing. Compliance can freeze an account; frozen accounts must be
  blocked from withdrawing.
- Balance snapshots now carry a `status` field. Consumers keep a backward
  compatible path for snapshots produced before 1.3.0, which stored a bare
  numeric amount (freezing did not exist at the time).

## 1.2.0
- Service registry for cross-module invocation, used by the HTTP handlers.
- Audit trail with queryable events.

## 1.1.0
- Multi-currency conversion on balance reads (account currency to requested
  currency), static rate table refreshed daily by ops.

## 1.0.0
- Initial extraction from the legacy monolith: accounts, billing, reporting.
