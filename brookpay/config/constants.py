"""Shared constants: statuses, limits, cache keys, audit events, services.

Keeping these strings and tables in one module avoids drift between
producers and consumers spread across the codebase. Nothing here reads the
environment; runtime tuning belongs to `brookpay.config.settings`.

Tables that mirror external standards (ISO 4217 minor units, ISO 13616
IBAN lengths, ISO 20022 purpose codes) are vendored snapshots, refreshed
manually when the standards change. Do not "fix" entries without checking
the standard first.
"""

# ---------------------------------------------------------------------------
# Account lifecycle statuses
# ---------------------------------------------------------------------------

STATUS_ACTIVE = "active"
STATUS_FROZEN = "frozen"
STATUS_DORMANT = "dormant"
STATUS_CLOSED = "closed"

ALL_STATUSES = (STATUS_ACTIVE, STATUS_FROZEN, STATUS_DORMANT, STATUS_CLOSED)

# Statuses a customer can transact from. Dormant accounts can receive
# credits (payroll keeps arriving) but cannot spend until reactivated.
SPENDABLE_STATUSES = (STATUS_ACTIVE,)
CREDITABLE_STATUSES = (STATUS_ACTIVE, STATUS_DORMANT, STATUS_FROZEN)

# ---------------------------------------------------------------------------
# KYC / customer due diligence
# ---------------------------------------------------------------------------

# Levels follow the AMLD5 tiering the compliance team applies:
#   0  prospect, email verified only, cannot hold funds
#   1  simplified due diligence, capped wallet
#   2  standard due diligence, document verified
#   3  enhanced due diligence, source-of-funds on file
KYC_LEVEL_PROSPECT = 0
KYC_LEVEL_SIMPLIFIED = 1
KYC_LEVEL_STANDARD = 2
KYC_LEVEL_ENHANCED = 3

KYC_LEVELS = (
    KYC_LEVEL_PROSPECT,
    KYC_LEVEL_SIMPLIFIED,
    KYC_LEVEL_STANDARD,
    KYC_LEVEL_ENHANCED,
)

# Document types accepted by the verification vendor, keyed by the code the
# vendor webhook sends back.
KYC_DOC_PASSPORT = "passport"
KYC_DOC_NATIONAL_ID = "national_id"
KYC_DOC_RESIDENCE_PERMIT = "residence_permit"
KYC_DOC_DRIVING_LICENCE = "driving_licence"
KYC_DOC_PROOF_OF_ADDRESS = "proof_of_address"
KYC_DOC_SOURCE_OF_FUNDS = "source_of_funds"

# Minimum document set required to reach each level. Order matters: the
# onboarding checklist is rendered in this order.
KYC_REQUIRED_DOCS = {
    KYC_LEVEL_PROSPECT: (),
    KYC_LEVEL_SIMPLIFIED: (KYC_DOC_NATIONAL_ID,),
    KYC_LEVEL_STANDARD: (
        KYC_DOC_PASSPORT,
        KYC_DOC_PROOF_OF_ADDRESS,
    ),
    KYC_LEVEL_ENHANCED: (
        KYC_DOC_PASSPORT,
        KYC_DOC_PROOF_OF_ADDRESS,
        KYC_DOC_SOURCE_OF_FUNDS,
    ),
}

# ---------------------------------------------------------------------------
# Currencies
# ---------------------------------------------------------------------------

# Currencies accepted anywhere in the system (ISO 4217).
SUPPORTED_CURRENCIES = ("EUR", "USD", "GBP", "JPY", "CHF", "THB", "SGD")
DEFAULT_CURRENCY = "EUR"

# ISO 4217 minor units (decimal places). Zero-decimal currencies must never
# be quantized to cents; the money helpers consult this table.
CURRENCY_MINOR_UNITS = {
    "EUR": 2, "USD": 2, "GBP": 2, "CHF": 2, "THB": 2, "SGD": 2,
    "JPY": 0, "KRW": 0, "ISK": 0, "CLP": 0, "VND": 0, "XOF": 0,
    "BHD": 3, "KWD": 3, "OMR": 3, "TND": 3, "JOD": 3, "IQD": 3,
    "AUD": 2, "CAD": 2, "NZD": 2, "SEK": 2, "NOK": 2, "DKK": 2,
    "PLN": 2, "CZK": 2, "HUF": 2, "RON": 2, "HKD": 2, "MXN": 2,
}

# Business days a payout in this currency needs to settle through the local
# rail. Used by the payouts ETA display, not by any accounting logic.
CURRENCY_SETTLEMENT_DAYS = {
    "EUR": 0,  # SEPA Instant where reachable, same-day fallback
    "GBP": 0,  # Faster Payments
    "USD": 1,  # ACH next-day tier
    "CHF": 1,
    "SGD": 1,  # FAST
    "THB": 1,  # PromptPay corporate cut-off dependent
    "JPY": 2,  # Zengin via correspondent
}

# ---------------------------------------------------------------------------
# ISO 20022 purpose codes
# ---------------------------------------------------------------------------

# Subset of ExternalPurpose1Code the payout initiation flow exposes. The
# label is what the customer sees in the dropdown; the code is what goes on
# the wire in the pain.001 file.
PURPOSE_CODES = {
    "SALA": "Salary payment",
    "PENS": "Pension payment",
    "BONU": "Bonus payment",
    "COMM": "Commission",
    "TAXS": "Tax payment",
    "VATX": "Value added tax payment",
    "TRAD": "Trade settlement",
    "INTC": "Intra-company payment",
    "TREA": "Treasury payment",
    "LOAN": "Loan disbursement",
    "LOAR": "Loan repayment",
    "RENT": "Rent payment",
    "INSU": "Insurance premium",
    "DIVI": "Dividend",
    "INTE": "Interest",
    "GDDS": "Purchase or sale of goods",
    "SCVE": "Purchase or sale of services",
    "SUPP": "Supplier payment",
    "SUBS": "Subscription",
    "CHAR": "Charity payment",
    "EDUC": "Education fees",
    "MDCS": "Medical services",
    "HLTI": "Health insurance",
    "GOVT": "Government payment",
    "BENE": "Unemployment or disability benefit",
    "ALMY": "Alimony payment",
    "CBFF": "Capital building fringe fortune",
    "FEES": "Payment of fees",
    "GIFT": "Gift",
    "OTHR": "Other",
    "PHON": "Telephone bill",
    "ELEC": "Electricity bill",
    "GASB": "Gas bill",
    "WTER": "Water bill",
    "NWCM": "Network communication",
    "CASH": "Cash management transfer",
}

# ---------------------------------------------------------------------------
# IBAN structure (ISO 13616)
# ---------------------------------------------------------------------------

# Country code to expected IBAN length. Destination validation refuses any
# IBAN whose length does not match before even computing the checksum.
IBAN_LENGTHS = {
    "AD": 24, "AT": 20, "BE": 16, "BG": 22, "CH": 21, "CY": 28,
    "CZ": 24, "DE": 22, "DK": 18, "EE": 20, "ES": 24, "FI": 18,
    "FR": 27, "GB": 22, "GR": 27, "HR": 21, "HU": 28, "IE": 22,
    "IT": 27, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "MC": 27,
    "MT": 31, "NL": 18, "NO": 15, "PL": 28, "PT": 25, "RO": 24,
    "SE": 24, "SI": 19, "SK": 24, "SM": 27,
}

# ---------------------------------------------------------------------------
# Per-KYC operational limits
# ---------------------------------------------------------------------------

# Hard ceilings applied on top of the velocity limits. Amounts are EUR
# reference; enforcement converts through the FX engine first. "None" means
# no ceiling at that level.
KYC_LIMITS_EUR = {
    KYC_LEVEL_PROSPECT: {
        "max_wallet": "0.00",
        "max_single_withdrawal": "0.00",
        "max_monthly_outbound": "0.00",
    },
    KYC_LEVEL_SIMPLIFIED: {
        "max_wallet": "150.00",
        "max_single_withdrawal": "50.00",
        "max_monthly_outbound": "300.00",
    },
    KYC_LEVEL_STANDARD: {
        "max_wallet": "15000.00",
        "max_single_withdrawal": "2500.00",
        "max_monthly_outbound": "10000.00",
    },
    KYC_LEVEL_ENHANCED: {
        "max_wallet": None,
        "max_single_withdrawal": "50000.00",
        "max_monthly_outbound": None,
    },
}

# ---------------------------------------------------------------------------
# Country risk tiers (FATF alignment)
# ---------------------------------------------------------------------------

# Tier 1: standard monitoring. Tier 2: increased monitoring (grey list
# snapshot). Tier 3: call-for-action or internally blocked corridors.
# Vendored from the compliance quarterly review; destination validation
# consults this before anything touches the payment rails.
COUNTRY_TIER_2 = (
    "BF", "CM", "HR", "CD", "HT", "KE", "MC", "ML", "MZ", "NA",
    "NG", "PH", "SN", "ZA", "SS", "SY", "TZ", "VE", "VN", "YE",
)
COUNTRY_TIER_3 = ("IR", "KP", "MM")

# Corridors legal asked us to hold even though FATF does not list them.
# Reviewed monthly; keep the ticket reference next to each entry.
COUNTRY_INTERNAL_HOLDS = (
    "RU",  # PAY-1290, sanctions programme scope review
    "BY",  # PAY-1290
)

# ---------------------------------------------------------------------------
# Merchant category codes (card programme)
# ---------------------------------------------------------------------------

# MCCs the issuing programme treats specially: either blocked outright at
# authorization, or flagged for the enhanced statement wording the card
# scheme mandates. Codes are ISO 18245.
MCC_BLOCKED = {
    "4829": "Money transfer (quasi-cash)",
    "6010": "Manual cash disbursement",
    "6011": "Automated cash disbursement",
    "6051": "Quasi cash: crypto assets and non-fiat",
    "7273": "Dating and escort services",
    "7995": "Betting, casino gaming, lottery",
    "9223": "Bail and bond payments",
}
MCC_FLAGGED = {
    "5912": "Drug stores and pharmacies",
    "5921": "Package stores: beer, wine, liquor",
    "5993": "Cigar stores and stands",
    "6211": "Security brokers and dealers",
    "6300": "Insurance sales and underwriting",
    "7801": "Government licensed online casino",
    "7802": "Government licensed horse or dog racing",
    "8398": "Charitable social service organizations",
}

# ---------------------------------------------------------------------------
# Cache keyspace
# ---------------------------------------------------------------------------

# Balance snapshots are cached under "<BALANCE_CACHE_PREFIX><user_id>" by
# the balance read path and consumed by background jobs that must not
# hammer the primary store.
BALANCE_CACHE_PREFIX = "balance:"
BALANCE_CACHE_TTL_SECONDS = 300

# FX quotes handed to the payout preview screen. Short TTL: the customer
# must re-quote if they sit on the confirmation page.
FX_QUOTE_CACHE_PREFIX = "fxq:"
FX_QUOTE_CACHE_TTL_SECONDS = 45

# Idempotency keys for the public API. TTL matches the retry window the
# gateway documents to integrators.
IDEMPOTENCY_CACHE_PREFIX = "idem:"
IDEMPOTENCY_CACHE_TTL_SECONDS = 86400

# ---------------------------------------------------------------------------
# Audit trail event names
# ---------------------------------------------------------------------------

EVENT_BALANCE_READ = "balance.read"
EVENT_INVOICE_CHARGED = "invoice.charged"
EVENT_ACCOUNT_FROZEN = "account.frozen"
EVENT_ACCOUNT_UNFROZEN = "account.unfrozen"
EVENT_ACCOUNT_CLOSED = "account.closed"
EVENT_WITHDRAWAL_DENIED = "withdrawal.denied"
EVENT_ONBOARDING_STARTED = "onboarding.started"
EVENT_RISK_REVIEW_OPENED = "risk.review.opened"
EVENT_RISK_REVIEW_CLOSED = "risk.review.closed"
EVENT_PAYOUT_SETTLED = "payout.settled"
EVENT_KYC_ESCALATED = "kyc.escalated"

# ---------------------------------------------------------------------------
# Service registry names (cross-module invocation without hard imports)
# ---------------------------------------------------------------------------

SERVICE_BALANCE_READ = "accounts.balance"
SERVICE_ONBOARDING_CHECK = "accounts.onboarding_check"

# ---------------------------------------------------------------------------
# Withdrawal denial reason catalog
# ---------------------------------------------------------------------------

# Machine reason code to customer facing label. The risk engine emits the
# code; the notification templates and the support back-office render the
# label. Codes never change once shipped (support macros key on them).
DENIAL_REASON_LABELS = {
    "invalid_user_id": "The account reference is malformed.",
    "unsupported_currency": "This currency is not available for withdrawals.",
    "non_positive_amount": "The amount must be greater than zero.",
    "unknown_account": "No wallet exists for this account.",
    "account_frozen": "This account is temporarily restricted.",
    "account_dormant": "This account is dormant. Reactivate it first.",
    "account_closed": "This account has been closed.",
    "unreadable_balance": "We could not read the available balance.",
    "insufficient_funds": "The available balance does not cover this amount.",
    "velocity_hourly_count": "Too many withdrawals in the last hour.",
    "velocity_daily_volume": "The daily withdrawal volume has been reached.",
    "destination_invalid": "The destination account details are invalid.",
    "destination_country_blocked": "Withdrawals to this country are not available.",
    "destination_name_mismatch": "The recipient name does not match the account.",
    "kyc_limit_single": "This amount exceeds the limit for your verification level.",
    "kyc_limit_monthly": "Your monthly withdrawal limit has been reached.",
    "duplicate_request": "An identical withdrawal was just submitted.",
    "cooling_off": "Large withdrawals require a short confirmation delay.",
    "manual_review": "This withdrawal needs a quick manual check.",
    "sanctions_screening": "This withdrawal cannot be processed.",
}

# Reasons that must never be exposed verbatim to the customer; support gets
# the code, the customer gets the generic restriction wording.
OPAQUE_DENIAL_REASONS = (
    "account_frozen",
    "sanctions_screening",
)

# ---------------------------------------------------------------------------
# Notification kinds
# ---------------------------------------------------------------------------

NOTIFY_LOW_BALANCE = "low_balance"
NOTIFY_WITHDRAWAL_DENIED = "withdrawal_denied"
NOTIFY_STATEMENT_READY = "statement_ready"
NOTIFY_KYC_ACTION_NEEDED = "kyc_action_needed"
NOTIFY_PAYOUT_SETTLED = "payout_settled"

# ---------------------------------------------------------------------------
# Webhook event names (outbound, partner facing)
# ---------------------------------------------------------------------------

# Names are frozen API surface: partners pattern-match on them. Additions
# only; renames require a versioned migration window.
WEBHOOK_EVENTS = (
    "account.opened",
    "account.frozen",
    "account.unfrozen",
    "account.closed",
    "balance.low",
    "invoice.charged",
    "invoice.failed",
    "payout.created",
    "payout.settled",
    "payout.returned",
    "withdrawal.denied",
    "kyc.level_changed",
    "statement.available",
    "dispute.opened",
)

# ---------------------------------------------------------------------------
# Public API rate limits
# ---------------------------------------------------------------------------

# Requests per rolling minute, by API key tier. Enforced at the gateway;
# mirrored here so the handlers can emit accurate RateLimit-* headers.
API_RATE_LIMITS_PER_MINUTE = {
    "sandbox": 30,
    "starter": 120,
    "growth": 600,
    "enterprise": 3000,
}

# ---------------------------------------------------------------------------
# Exports, retention, reporting
# ---------------------------------------------------------------------------

# File name templates for the regulatory drops. The date label is always
# UTC; the receiving SFTP rejects duplicate names, which is exactly the
# idempotency the ops runbook relies on.
EXPORT_NAME_BALANCE_READS = "balance_reads_{label}.csv"
EXPORT_NAME_FROZEN_ACCOUNTS = "frozen_accounts_{label}.csv"
EXPORT_NAME_SAR_BUNDLE = "sar_bundle_{label}.jsonl"

# Days each record family is kept before the purge job may touch it.
# Regulatory minimums, not targets; legal signs off any change here.
RETENTION_DAYS = {
    "audit_trail": 400,
    "balance_read_exports": 1830,   # 5 years
    "sar_bundles": 1830,
    "notifications": 90,
    "risk_reviews": 1830,
    "closed_account_records": 3650,  # 10 years
}

# Reporting.
STATEMENT_LOCALE_DEFAULT = "en_GB"
STATEMENT_LOCALES = ("en_GB", "fr_FR", "de_DE", "th_TH", "ja_JP")
EXPORTS_DIR = "var/exports"

# Statement descriptor shown on counterparty statements. Networks truncate
# hard at 22 characters; keep the prefix short.
DESCRIPTOR_PREFIX = "BROOKPAY*"
DESCRIPTOR_MAX_LENGTH = 22

# ---------------------------------------------------------------------------
# SEPA return reason codes
# ---------------------------------------------------------------------------

# Subset of ISO 20022 return codes the reconciliation job maps to internal
# outcomes. Anything not listed lands in the manual exceptions queue.
SEPA_RETURN_CODES = {
    "AC01": "Incorrect account number",
    "AC04": "Closed account number",
    "AC06": "Blocked account",
    "AG01": "Transaction forbidden on this account type",
    "AG02": "Invalid bank operation code",
    "AM04": "Insufficient funds",
    "AM05": "Duplication",
    "BE04": "Missing creditor address",
    "CNOR": "Creditor bank is not registered",
    "DNOR": "Debtor bank is not registered",
    "FF01": "Invalid file format",
    "MD01": "No mandate",
    "MD07": "Debtor deceased",
    "MS02": "Refusal by the debtor",
    "MS03": "Reason not specified",
    "RC01": "Bank identifier incorrect",
    "RR01": "Missing debtor account or identification",
    "RR03": "Missing creditor name or address",
    "RR04": "Regulatory reason",
    "SL01": "Specific service offered by the debtor bank",
}

# ---------------------------------------------------------------------------
# Settlement calendars
# ---------------------------------------------------------------------------

# TARGET2 closing days (EUR rail). Fixed-date entries only; Easter-linked
# closures are computed by the calendar helper at runtime.
TARGET2_FIXED_CLOSED = (
    (1, 1),    # New Year's Day
    (5, 1),    # Labour Day
    (12, 25),  # Christmas Day
    (12, 26),  # Christmas Holiday
)

# UK bank holidays with fixed dates (GBP rail, Faster Payments settles
# every day but same-day CHAPS fallback does not).
UK_FIXED_CLOSED = (
    (1, 1),
    (12, 25),
    (12, 26),
)

# Cut-off hours (UTC) after which a payout instruction rolls to the next
# settlement window, per rail.
RAIL_CUTOFF_HOUR_UTC = {
    "sepa": 15,
    "sepa_instant": 23,
    "fps": 23,
    "ach": 13,
    "promptpay": 14,
    "fast_sg": 15,
    "zengin": 6,
}

# ---------------------------------------------------------------------------
# Dispute reason codes
# ---------------------------------------------------------------------------

# Internal dispute taxonomy. The card scheme codes (Visa 10.x / 13.x,
# Mastercard 48xx) map onto these; keeping our own stable set means the
# back-office does not churn when a scheme renumbers.
DISPUTE_REASONS = {
    "fraud_card_absent": "Cardholder denies participating (CNP)",
    "fraud_card_present": "Cardholder denies participating (CP)",
    "goods_not_received": "Goods or services not received",
    "not_as_described": "Goods or services not as described",
    "duplicate_processing": "Duplicate processing",
    "credit_not_processed": "Refund not processed",
    "incorrect_amount": "Incorrect transaction amount",
    "cancelled_recurring": "Cancelled recurring transaction billed",
    "authorization_missing": "No valid authorization",
    "late_presentment": "Late presentment",
}

# Days the customer has to open a dispute after the statement date, per
# reason family. Scheme minimums; the product surfaces these verbatim.
DISPUTE_WINDOW_DAYS = {
    "fraud_card_absent": 120,
    "fraud_card_present": 120,
    "goods_not_received": 120,
    "not_as_described": 120,
    "duplicate_processing": 90,
    "credit_not_processed": 90,
    "incorrect_amount": 90,
    "cancelled_recurring": 120,
    "authorization_missing": 75,
    "late_presentment": 75,
}

# ---------------------------------------------------------------------------
# Support macros
# ---------------------------------------------------------------------------

# Back-office quick replies keyed by denial reason family. Text is owned by
# the support content team; engineering only guarantees the keys exist.
SUPPORT_MACRO_KEYS = (
    "restriction_generic",
    "limits_kyc_upgrade",
    "velocity_wait",
    "destination_recheck",
    "dispute_intake",
    "statement_resend",
)

# Maximum attachments per support ticket, mirrored from the helpdesk plan.
SUPPORT_MAX_ATTACHMENTS = 10
