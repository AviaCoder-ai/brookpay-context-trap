"""Invoice construction and charging against wallet balances.

An invoice is assembled from priced line items plus a processing fee, then
charged against the customer's wallet. The charge path is the interesting
one: it reads the balance through the account service, compares sufficiency
in the invoice currency, and debits the account. That balance read returns a
structured snapshot, and the charge logic depends on its shape (status,
amount, currency), not merely on a numeric balance.

Construction is pure and side-effect free; charging mutates the account and
writes a ledger transaction plus an audit event. The two are kept as
separate functions so a quote can be produced and shown before any money
moves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from brookpay.billing.tariffs import fee_for, line_total
from brookpay.config.constants import EVENT_INVOICE_CHARGED, STATUS_ACTIVE
from brookpay.core import audit
from brookpay.models.transaction import Category, Direction, Transaction
from brookpay.services.account_service import get_user_balance
from brookpay.store.repository import accounts, transactions
from brookpay.utils.idgen import new_id
from brookpay.utils.money import quantize2, to_decimal
from brookpay.utils.timeutils import utc_now
from brookpay.utils.validation import is_supported_currency


# ---------------------------------------------------------------------------
# Invoice data model
# ---------------------------------------------------------------------------

@dataclass
class InvoiceLine:
    sku: str
    quantity: int
    total: Decimal


@dataclass
class Invoice:
    invoice_id: str
    user_id: str
    currency: str
    lines: list[InvoiceLine] = field(default_factory=list)
    processing_fee: Decimal = Decimal("0.00")

    @property
    def subtotal(self) -> Decimal:
        return quantize2(sum((l.total for l in self.lines), Decimal("0")))

    @property
    def total(self) -> Decimal:
        return quantize2(self.subtotal + self.processing_fee)

    @property
    def line_count(self) -> int:
        return len(self.lines)

    def describe(self) -> dict:
        """Machine-readable summary for the invoice API and receipts."""
        return {
            "invoice_id": self.invoice_id,
            "user_id": self.user_id,
            "currency": self.currency,
            "subtotal": str(self.subtotal),
            "processing_fee": str(self.processing_fee),
            "total": str(self.total),
            "lines": [
                {"sku": l.sku, "quantity": l.quantity, "total": str(l.total)}
                for l in self.lines
            ],
        }


@dataclass
class InvoiceOutcome:
    invoice_id: str
    status: str  # charged | skipped_unknown_account | blocked_account_status
    #            # | insufficient_funds
    reason: str = ""
    charged_amount: Decimal = Decimal("0.00")
    currency: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status == "charged"


# ---------------------------------------------------------------------------
# Invoice construction
# ---------------------------------------------------------------------------

def build_invoice(user_id: str, items: list[tuple[str, int]], currency: str) -> Invoice:
    """Assemble an invoice from (sku, quantity) pairs plus processing fee."""
    if not is_supported_currency(currency):
        raise ValueError(f"unsupported invoice currency '{currency}'")
    invoice = Invoice(invoice_id=new_id("inv"), user_id=user_id, currency=currency)
    for sku, quantity in items:
        invoice.lines.append(
            InvoiceLine(sku=sku, quantity=quantity, total=line_total(sku, quantity))
        )
    invoice.processing_fee = fee_for("invoice", invoice.subtotal)
    return invoice


def build_line(sku: str, quantity: int) -> InvoiceLine:
    """Construct a single priced line, for incremental invoice building."""
    return InvoiceLine(sku=sku, quantity=quantity, total=line_total(sku, quantity))


def merge_lines(lines: list[InvoiceLine]) -> list[InvoiceLine]:
    """Collapse duplicate SKUs into single lines, summing quantity and total.

    Invoices assembled from a cart can carry the same SKU twice; merging
    keeps the printed invoice tidy without changing the total.
    """
    by_sku: dict[str, InvoiceLine] = {}
    for line in lines:
        if line.sku in by_sku:
            existing = by_sku[line.sku]
            existing.quantity += line.quantity
            existing.total = quantize2(existing.total + line.total)
        else:
            by_sku[line.sku] = InvoiceLine(line.sku, line.quantity, line.total)
    return list(by_sku.values())


# ---------------------------------------------------------------------------
# Tax and proration
# ---------------------------------------------------------------------------

# VAT rates by jurisdiction tag. Applied at display time for jurisdictions
# that require tax-inclusive invoices; BrookPay's own fees are quoted net and
# this table is used only where a reseller invoice needs a tax line.
_VAT_RATES = {
    "EU_STANDARD": Decimal("0.20"),
    "EU_REDUCED": Decimal("0.10"),
    "UK_STANDARD": Decimal("0.20"),
    "CH_STANDARD": Decimal("0.081"),
    "SG_GST": Decimal("0.09"),
    "TH_VAT": Decimal("0.07"),
    "NONE": Decimal("0"),
}


def vat_amount(net: Decimal, jurisdiction: str = "NONE") -> Decimal:
    """VAT on a net amount for a jurisdiction tag, quantized."""
    rate = _VAT_RATES.get(jurisdiction, Decimal("0"))
    return quantize2(to_decimal(net) * rate)


def gross_amount(net: Decimal, jurisdiction: str = "NONE") -> Decimal:
    """Net plus applicable VAT."""
    return quantize2(to_decimal(net) + vat_amount(net, jurisdiction))


def prorate(amount: Decimal, days_used: int, days_in_period: int) -> Decimal:
    """Prorate a periodic charge for partial-period usage.

    Used when a plan starts or ends mid-cycle. Guards a zero-length period
    (returns zero) so a misconfigured cycle cannot divide by zero on a
    customer-facing invoice.
    """
    if days_in_period <= 0:
        return Decimal("0.00")
    fraction = Decimal(max(0, days_used)) / Decimal(days_in_period)
    return quantize2(to_decimal(amount) * fraction)


def apply_discount(amount: Decimal, percent_off: Decimal) -> Decimal:
    """Reduce an amount by a percentage, clamped to [0, 100]."""
    pct = max(Decimal("0"), min(Decimal("100"), to_decimal(percent_off)))
    factor = (Decimal("100") - pct) / Decimal("100")
    return quantize2(to_decimal(amount) * factor)


# ---------------------------------------------------------------------------
# Dunning configuration
# ---------------------------------------------------------------------------

# Days after an unpaid invoice that each dunning action fires. Consumed by
# the dunning scheduler; kept here so billing owns the retry cadence.
DUNNING_SCHEDULE_DAYS = (1, 3, 7, 14)
DUNNING_MAX_ATTEMPTS = len(DUNNING_SCHEDULE_DAYS)


def next_dunning_day(attempt: int) -> int:
    """Days-after-due for the given retry attempt (1-indexed)."""
    if attempt < 1 or attempt > DUNNING_MAX_ATTEMPTS:
        return -1
    return DUNNING_SCHEDULE_DAYS[attempt - 1]


def is_final_attempt(attempt: int) -> bool:
    return attempt >= DUNNING_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Invoice numbering and references
# ---------------------------------------------------------------------------

# Human-facing invoice number prefix. The opaque invoice_id is the primary
# key; this number is what the customer sees and quotes to support.
_INVOICE_NUMBER_PREFIX = "BP"


def invoice_number(invoice: Invoice, sequence: int) -> str:
    """Stable human invoice number, "BP-000123", from a sequence value.

    The sequence is assigned by the billing run, not derived from the id, so
    numbers are contiguous per run even though ids are opaque and unordered.
    """
    return f"{_INVOICE_NUMBER_PREFIX}-{sequence:06d}"


def credit_note_number(invoice_number_str: str) -> str:
    """Credit-note number derived from an invoice number, "CN-000123"."""
    tail = invoice_number_str.split("-", 1)[-1]
    return f"CN-{tail}"


# ---------------------------------------------------------------------------
# Subscription periods
# ---------------------------------------------------------------------------

BILLING_MONTHLY = "monthly"
BILLING_QUARTERLY = "quarterly"
BILLING_ANNUAL = "annual"

_PERIOD_MONTHS = {
    BILLING_MONTHLY: 1,
    BILLING_QUARTERLY: 3,
    BILLING_ANNUAL: 12,
}


def period_months(cadence: str) -> int:
    """Number of months a billing cadence covers, defaulting to monthly."""
    return _PERIOD_MONTHS.get(cadence, 1)


def period_fee(base_monthly: Decimal, cadence: str) -> Decimal:
    """Fee for a full billing period given a base monthly amount."""
    return quantize2(to_decimal(base_monthly) * period_months(cadence))


# ---------------------------------------------------------------------------
# Charging (depends on the balance snapshot shape)
# ---------------------------------------------------------------------------

def charge_invoice(invoice: Invoice) -> InvoiceOutcome:
    """Charge an invoice against the user's wallet.

    The balance is requested in the invoice currency so that sufficiency is
    compared like for like, whatever the account's own currency is. The read
    returns a structured snapshot; this function reads three fields off it:
    the status (to block non-active accounts), the amount (to compare
    against the invoice total), and the currency (echoed back on the
    outcome). All three come from the snapshot's structure, so a balance
    read that returns a bare number instead of the snapshot breaks this
    function at the first field access rather than merely changing a value.
    """
    balance = get_user_balance(invoice.user_id, currency=invoice.currency)

    if balance is None:
        return InvoiceOutcome(
            invoice_id=invoice.invoice_id,
            status="skipped_unknown_account",
            reason="no wallet for user",
        )

    if balance["status"] != STATUS_ACTIVE:
        return InvoiceOutcome(
            invoice_id=invoice.invoice_id,
            status="blocked_account_status",
            reason=f"account status is '{balance['status']}'",
        )

    available = to_decimal(balance["amount"])
    if available < invoice.total:
        return InvoiceOutcome(
            invoice_id=invoice.invoice_id,
            status="insufficient_funds",
            reason=f"available {available} {balance['currency']} "
                   f"< total {invoice.total} {invoice.currency}",
        )

    # Debit in the account currency: convert the invoice total back through
    # the same snapshot ratio to stay consistent with what was quoted.
    account = accounts.get(invoice.user_id)
    if account.currency == invoice.currency:
        debit_amount = invoice.total
    else:
        ratio = to_decimal(account.raw_balance) / available
        debit_amount = quantize2(invoice.total * ratio)
    account.debit(debit_amount)

    transactions.add(Transaction(
        txn_id=new_id("txn"),
        user_id=invoice.user_id,
        amount=debit_amount,
        currency=account.currency,
        direction=Direction.DEBIT,
        category=Category.INVOICE,
        created_at=utc_now(),
        description=f"Invoice {invoice.invoice_id}",
    ))
    audit.record(
        EVENT_INVOICE_CHARGED,
        user_id=invoice.user_id,
        invoice_id=invoice.invoice_id,
        amount=str(invoice.total),
        currency=invoice.currency,
    )
    return InvoiceOutcome(
        invoice_id=invoice.invoice_id,
        status="charged",
        charged_amount=invoice.total,
        currency=balance["currency"],
    )


def can_afford(user_id: str, amount, currency: str = "EUR") -> bool:
    """Cheap pre-check used by the UI before showing the pay button.

    Reads the same snapshot as charge_invoice and compares the amount field.
    Advisory only: the authoritative sufficiency check is inside
    charge_invoice, under the account lock.
    """
    snapshot = get_user_balance(user_id, currency=currency)
    if snapshot is None:
        return False
    if snapshot["status"] != STATUS_ACTIVE:
        return False
    return to_decimal(snapshot["amount"]) >= to_decimal(amount)


# ---------------------------------------------------------------------------
# Refunds
# ---------------------------------------------------------------------------

def refund_invoice(invoice: Invoice, reason: str = "") -> InvoiceOutcome:
    """Credit a previously charged invoice back to the wallet.

    Mirrors charge_invoice's debit: it credits the account in the account
    currency and writes a ledger transaction. It does not consult the
    balance snapshot because a refund is always allowed regardless of the
    current balance.
    """
    account = accounts.get(invoice.user_id)
    credit_amount = invoice.total
    account.credit(credit_amount)
    transactions.add(Transaction(
        txn_id=new_id("txn"),
        user_id=invoice.user_id,
        amount=credit_amount,
        currency=account.currency,
        direction=Direction.CREDIT,
        category=Category.ADJUSTMENT,
        created_at=utc_now(),
        description=f"Refund {invoice.invoice_id} {reason}".strip(),
    ))
    return InvoiceOutcome(
        invoice_id=invoice.invoice_id,
        status="refunded",
        charged_amount=credit_amount,
        currency=account.currency,
    )


# ---------------------------------------------------------------------------
# Batch billing
# ---------------------------------------------------------------------------

def charge_many(invoices: list[Invoice]) -> list[InvoiceOutcome]:
    """Charge a batch of invoices, isolating per-invoice failures."""
    outcomes: list[InvoiceOutcome] = []
    for invoice in invoices:
        try:
            outcomes.append(charge_invoice(invoice))
        except Exception as exc:  # noqa: BLE001 - isolate per-invoice failures
            outcomes.append(InvoiceOutcome(
                invoice_id=invoice.invoice_id,
                status="error",
                reason=type(exc).__name__,
            ))
    return outcomes


def batch_summary(outcomes: list[InvoiceOutcome]) -> dict:
    """Aggregate a batch charge run for the billing dashboard."""
    by_status: dict[str, int] = {}
    charged_total = Decimal("0")
    for outcome in outcomes:
        by_status[outcome.status] = by_status.get(outcome.status, 0) + 1
        if outcome.succeeded:
            charged_total += outcome.charged_amount
    return {
        "count": len(outcomes),
        "by_status": by_status,
        "charged_total": str(quantize2(charged_total)),
    }


# ---------------------------------------------------------------------------
# Receipt rendering
# ---------------------------------------------------------------------------

def render_receipt_lines(invoice: Invoice, outcome: InvoiceOutcome) -> list[str]:
    """Plain text receipt lines for a charged invoice."""
    lines = [
        f"Receipt for invoice {invoice.invoice_id}",
        f"Status: {outcome.status}",
        "",
    ]
    for line in invoice.lines:
        lines.append(f"  {line.sku} x{line.quantity}  {line.total} {invoice.currency}")
    lines.append("")
    lines.append(f"Subtotal: {invoice.subtotal} {invoice.currency}")
    lines.append(f"Fee:      {invoice.processing_fee} {invoice.currency}")
    lines.append(f"Total:    {invoice.total} {invoice.currency}")
    return lines


# ---------------------------------------------------------------------------
# Dunning schedule
# ---------------------------------------------------------------------------

@dataclass
class DunningStep:
    attempt: int
    days_after_due: int
    is_final: bool


def build_dunning_schedule() -> list[DunningStep]:
    """Materialise the dunning schedule as ordered steps.

    Derived entirely from DUNNING_SCHEDULE_DAYS so the cadence lives in one
    place; the scheduler consumes these steps to time its retries.
    """
    steps: list[DunningStep] = []
    for index, days in enumerate(DUNNING_SCHEDULE_DAYS, start=1):
        steps.append(DunningStep(
            attempt=index,
            days_after_due=days,
            is_final=is_final_attempt(index),
        ))
    return steps


def dunning_due_today(days_overdue: int) -> bool:
    """Whether a dunning action fires at exactly this overdue day count."""
    return days_overdue in DUNNING_SCHEDULE_DAYS


# ---------------------------------------------------------------------------
# Aging buckets
# ---------------------------------------------------------------------------

# Standard accounts-receivable aging buckets, upper bound in days. The last
# bucket is open-ended and represented by a sentinel upper bound.
_AGING_BUCKETS = (("current", 0), ("1-30", 30), ("31-60", 60), ("61-90", 90))
_AGING_OVERFLOW = "90+"


def aging_bucket(days_overdue: int) -> str:
    """Classify an overdue age into a standard AR bucket."""
    if days_overdue <= 0:
        return "current"
    for label, upper in _AGING_BUCKETS:
        if label == "current":
            continue
        if days_overdue <= upper:
            return label
    return _AGING_OVERFLOW


def aging_report(rows: list[tuple[str, int, Decimal]]) -> dict:
    """Aggregate (invoice_id, days_overdue, amount) rows into aging buckets.

    Pure aggregation for the finance dashboard; sums outstanding amounts per
    bucket. Amounts are assumed to be in a single reporting currency, which
    the caller normalises before calling.
    """
    totals: dict[str, Decimal] = {}
    counts: dict[str, int] = {}
    for _invoice_id, days_overdue, amount in rows:
        bucket = aging_bucket(days_overdue)
        totals[bucket] = totals.get(bucket, Decimal("0")) + to_decimal(amount)
        counts[bucket] = counts.get(bucket, 0) + 1
    return {
        "buckets": {k: str(quantize2(v)) for k, v in totals.items()},
        "counts": counts,
        "outstanding_total": str(quantize2(sum(totals.values(), Decimal("0")))),
    }


# ---------------------------------------------------------------------------
# Statement of account
# ---------------------------------------------------------------------------

def statement_of_account(user_id: str, outcomes: list[InvoiceOutcome]) -> dict:
    """Summarise a customer's billing outcomes for a period.

    Rolls up charged, refunded and failed invoices into a compact record the
    account-management screen shows. Operates on already-computed outcomes,
    so it performs no charging itself.
    """
    charged = [o for o in outcomes if o.status == "charged"]
    refunded = [o for o in outcomes if o.status == "refunded"]
    failed = [o for o in outcomes if o.status not in ("charged", "refunded")]
    charged_total = sum((o.charged_amount for o in charged), Decimal("0"))
    refunded_total = sum((o.charged_amount for o in refunded), Decimal("0"))
    return {
        "user_id": user_id,
        "charged_count": len(charged),
        "refunded_count": len(refunded),
        "failed_count": len(failed),
        "charged_total": str(quantize2(charged_total)),
        "refunded_total": str(quantize2(refunded_total)),
        "net": str(quantize2(charged_total - refunded_total)),
    }


# ---------------------------------------------------------------------------
# Invoice validation
# ---------------------------------------------------------------------------

def validate_invoice(invoice: Invoice) -> list[str]:
    """Return a list of problems with an invoice, empty when it is clean.

    Front-door validation used before an invoice is persisted or charged: it
    checks currency support, non-empty lines, non-negative totals and a
    coherent fee. Purely structural; it never reads account state.
    """
    problems: list[str] = []
    if not is_supported_currency(invoice.currency):
        problems.append(f"unsupported currency '{invoice.currency}'")
    if not invoice.lines:
        problems.append("invoice has no lines")
    if invoice.subtotal < 0:
        problems.append("negative subtotal")
    if invoice.processing_fee < 0:
        problems.append("negative processing fee")
    if invoice.total < invoice.processing_fee:
        problems.append("total is below the processing fee")
    return problems


def is_chargeable(invoice: Invoice) -> bool:
    """Whether an invoice is structurally fit to be charged."""
    return not validate_invoice(invoice) and invoice.total > 0
