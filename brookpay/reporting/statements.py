"""Customer facing statement views.

Statements render ledger activity plus the current balance line in the
customer's preferred display currency, whatever currency the account is
actually held in. That last part is the whole reason this module is not
trivial: the ledger is kept in the account's own currency, but a customer
holding a JPY or THB wallet routinely reads the statement in EUR, so the
balance line has to be fetched already-converted and rendered without any
further currency juggling here.

The balance line is the only value on a statement that comes from the live
balance read path rather than from the stored ledger. It is fetched through
the stable facade (not the service module directly) so that the reporting
layer stays decoupled from the account service internals.
"""

from __future__ import annotations

from decimal import Decimal

from brookpay.config.constants import DEFAULT_CURRENCY
from brookpay.core.api import fetch_balance_snapshot
from brookpay.reporting.formatting import (
    format_amount,
    format_date,
    format_money,
    format_month_year,
    markdown_table,
    pad_columns,
    paginate_footer,
    statement_footer,
    statement_header,
)
from brookpay.services.account_service import monthly_statement
from brookpay.utils.timeutils import iso


# ---------------------------------------------------------------------------
# Period helpers
# ---------------------------------------------------------------------------

def period_label(year: int, month: int) -> str:
    """Canonical statement period key, "YYYY-MM"."""
    return f"{year:04d}-{month:02d}"


def previous_period(year: int, month: int) -> tuple[int, int]:
    """The calendar month before the given one."""
    if month == 1:
        return year - 1, 12
    return year, month - 1


def next_period(year: int, month: int) -> tuple[int, int]:
    if month == 12:
        return year + 1, 1
    return year, month + 1


def last_n_periods(year: int, month: int, n: int) -> list[tuple[int, int]]:
    """The n periods ending at (year, month), oldest first."""
    periods: list[tuple[int, int]] = []
    cur = (year, month)
    for _ in range(max(0, n)):
        periods.append(cur)
        cur = previous_period(*cur)
    return list(reversed(periods))


# ---------------------------------------------------------------------------
# Ledger row projection
# ---------------------------------------------------------------------------

def _row_tuple(txn) -> tuple[str, str, str, str]:
    """Project one transaction into a display tuple."""
    return (
        iso(txn.created_at)[:10],
        f"{txn.direction.value}/{txn.category.value}",
        f"{txn.amount} {txn.currency}",
        txn.description,
    )


def _split_by_direction(transactions) -> tuple[list, list]:
    """Partition transactions into (credits, debits) by direction value."""
    credits, debits = [], []
    for txn in transactions:
        if txn.direction.value == "credit":
            credits.append(txn)
        else:
            debits.append(txn)
    return credits, debits


def category_breakdown(transactions) -> dict[str, str]:
    """Sum transaction amounts per category, formatted for display.

    Presentation-only aggregation over the already-fetched rows; it never
    touches the store. Amounts are summed in the transaction currency and
    the caller is expected to have a single-currency statement, which is the
    case because the ledger is kept in the account currency.
    """
    totals: dict[str, Decimal] = {}
    for txn in transactions:
        key = txn.category.value
        totals[key] = totals.get(key, Decimal("0")) + txn.amount
    return {key: format_amount(value) for key, value in sorted(totals.items())}


def movement_counts(transactions) -> dict[str, int]:
    credits, debits = _split_by_direction(transactions)
    return {"credits": len(credits), "debits": len(debits), "total": len(transactions)}


# ---------------------------------------------------------------------------
# Running balance reconstruction
# ---------------------------------------------------------------------------

def reconstruct_running_balance(opening: Decimal, transactions) -> list[tuple[object, Decimal]]:
    """Compute the running balance after each transaction.

    Historical balances are rebuilt here from an opening figure plus the
    signed ledger, rather than re-read from the live account, because the
    live balance is "now" and a statement is "as of the period end". Credits
    add, everything else subtracts, matching the ledger sign convention.
    """
    running = opening
    out: list[tuple[object, Decimal]] = []
    for txn in transactions:
        if txn.direction.value == "credit":
            running = running + txn.amount
        else:
            running = running - txn.amount
        out.append((txn, running))
    return out


def closing_from_opening(opening: Decimal, transactions) -> Decimal:
    """The closing balance implied by an opening balance and the ledger."""
    series = reconstruct_running_balance(opening, transactions)
    return series[-1][1] if series else opening


def opening_from_closing(closing: Decimal, transactions) -> Decimal:
    """Invert the ledger to recover the opening balance from the closing one.

    Useful when only the current (closing-for-the-period) figure is known
    and the statement needs the opening line; applying the inverse of each
    movement walks backwards to the period start.
    """
    opening = closing
    for txn in transactions:
        if txn.direction.value == "credit":
            opening = opening - txn.amount
        else:
            opening = opening + txn.amount
    return opening


# ---------------------------------------------------------------------------
# Statement numbering and delivery
# ---------------------------------------------------------------------------

def statement_number(user_id: str, year: int, month: int) -> str:
    """Stable human-facing statement number, "user-YYYYMM".

    Deterministic so a re-render of the same period yields the same number;
    the customer references it in support tickets.
    """
    return f"{user_id}-{year:04d}{month:02d}"


DELIVERY_EMAIL = "email"
DELIVERY_POST = "post"
DELIVERY_NONE = "none"

_DELIVERY_CHANNELS = (DELIVERY_EMAIL, DELIVERY_POST, DELIVERY_NONE)


def normalise_delivery(channel: str) -> str:
    """Coerce a delivery preference to a known channel, defaulting to email."""
    lowered = (channel or "").strip().lower()
    return lowered if lowered in _DELIVERY_CHANNELS else DELIVERY_EMAIL


def delivery_is_physical(channel: str) -> bool:
    return normalise_delivery(channel) == DELIVERY_POST


# ---------------------------------------------------------------------------
# Statement annotations and legal footers
# ---------------------------------------------------------------------------

# Standing footer lines every statement carries, per delivery channel. The
# text is owned by legal; engineering only guarantees the lines are appended
# verbatim and in order.
_LEGAL_FOOTER_COMMON = (
    "This statement is provided for information only.",
    "Please report any discrepancy within 60 days of the statement date.",
)
_LEGAL_FOOTER_POST = (
    "BrookPay, registered office on file. Do not reply to this letter.",
)


def legal_footer_lines(channel: str) -> list[str]:
    """Assemble the legal footer for a delivery channel."""
    lines = list(_LEGAL_FOOTER_COMMON)
    if delivery_is_physical(channel):
        lines.extend(_LEGAL_FOOTER_POST)
    return lines


def annotate_view(view: dict, channel: str = DELIVERY_EMAIL) -> dict:
    """Attach delivery metadata and the legal footer to a statement view.

    Non-destructive: returns a shallow copy with extra keys, so the original
    view (and its balance line) is untouched and can still be rendered by the
    plain text or markdown renderers.
    """
    annotated = dict(view)
    annotated["delivery"] = normalise_delivery(channel)
    annotated["statement_number"] = statement_number(
        view["user_id"],
        *(int(p) for p in view["period"].split("-")),
    )
    annotated["legal_footer"] = legal_footer_lines(channel)
    return annotated


def disclaimer_for_currency(display_currency: str, account_currency: str) -> str:
    """One-line FX disclaimer shown when the display currency differs.

    Purely informational text; it does not change any figure. Shown only
    when the customer is reading in a currency other than the one the wallet
    is held in, which is exactly the multi-currency statement case.
    """
    if display_currency == account_currency:
        return ""
    return (
        f"Amounts shown in {display_currency} are converted from "
        f"{account_currency} at the mid-market rate on the statement date."
    )


# ---------------------------------------------------------------------------
# Statement assembly (balance line fetched from the read path)
# ---------------------------------------------------------------------------

def build_statement_view(
    user_id: str,
    year: int,
    month: int,
    display_currency: str = DEFAULT_CURRENCY,
) -> dict:
    """Assemble everything the statement template needs.

    The balance line must reflect the requested display currency; customers
    holding a JPY or THB wallet routinely read their statement in EUR. The
    snapshot is fetched already converted into `display_currency` and handed
    to the formatter, which reads the currency back out of the snapshot. If
    the snapshot ever arrives without its currency metadata, the formatter
    falls back to the display currency label, which is how a multi-currency
    balance can end up mislabelled with no error raised.
    """
    stmt = monthly_statement(user_id, year, month)

    user_funds = fetch_balance_snapshot(user_id, currency=display_currency)
    balance_line = format_money(user_funds, display_currency)

    return {
        "user_id": user_id,
        "period": stmt["period"],
        "display_currency": display_currency,
        "account_currency": stmt["account_currency"],
        "credits": format_amount(stmt["credits"]),
        "debits": format_amount(stmt["debits"]),
        "transaction_count": stmt["transaction_count"],
        "balance_line": balance_line,
        "transactions": stmt["transactions"],
    }


def render_text_statement(view: dict) -> str:
    """Plain text rendering used by the email template and the CLI."""
    header = [
        f"BrookPay statement {view['period']} for {view['user_id']}",
        f"Account currency: {view['account_currency']}",
        f"Current balance: {view['balance_line']}",
        "",
    ]
    rows: list[tuple[str, ...]] = [("DATE", "TYPE", "AMOUNT", "DESCRIPTION")]
    for txn in view["transactions"]:
        rows.append(_row_tuple(txn))
    body = pad_columns(rows)
    footer = [
        "",
        f"Movements: {view['transaction_count']} "
        f"(in {view['credits']}, out {view['debits']} {view['account_currency']})",
    ]
    return "\n".join(header + body + footer)


# ---------------------------------------------------------------------------
# HTML statement beta
# ---------------------------------------------------------------------------

def render_markdown_statement(view: dict) -> str:
    """Markdown rendering for the statement HTML beta.

    Reuses the same view dict as the text renderer, so the balance line and
    its currency label are identical across renderings; only the layout
    differs.
    """
    headers = ["Date", "Type", "Amount", "Description"]
    rows = [list(_row_tuple(txn)) for txn in view["transactions"]]
    table = markdown_table(headers, rows)
    lines = [
        f"## Statement {view['period']} for {view['user_id']}",
        "",
        f"**Account currency:** {view['account_currency']}  ",
        f"**Current balance:** {view['balance_line']}",
        "",
        table,
        "",
        f"_Movements: {view['transaction_count']} "
        f"(in {view['credits']}, out {view['debits']} {view['account_currency']})_",
    ]
    return "\n".join(lines)


def statement_metadata(view: dict) -> dict:
    """Small machine-readable header for the statement API envelope."""
    return {
        "user_id": view["user_id"],
        "period": view["period"],
        "display_currency": view["display_currency"],
        "account_currency": view["account_currency"],
        "movements": view["transaction_count"],
        "multi_currency": view["display_currency"] != view["account_currency"],
    }


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def statement_csv(view: dict) -> str:
    """Render a statement's rows as a CSV document string."""
    from brookpay.reporting.formatting import csv_row

    lines = [csv_row(["date", "direction", "category", "amount", "currency", "description"])]
    for txn in view["transactions"]:
        lines.append(csv_row([
            iso(txn.created_at),
            txn.direction.value,
            txn.category.value,
            str(txn.amount),
            txn.currency,
            txn.description,
        ]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Multi-period summary
# ---------------------------------------------------------------------------

def build_multi_period_summary(
    user_id: str,
    year: int,
    month: int,
    months: int = 3,
) -> dict:
    """Roll up several consecutive statements for the trends view.

    Each period reuses monthly_statement, so this stays a pure aggregation
    over stored ledger data; the live balance line is fetched once, for the
    latest period only, because historical balances are reconstructed by the
    reporting layer rather than re-read from the account.
    """
    periods = last_n_periods(year, month, months)
    per_period = []
    for py, pm in periods:
        stmt = monthly_statement(user_id, py, pm)
        per_period.append({
            "period": stmt["period"],
            "label": format_month_year(py, pm),
            "credits": format_amount(stmt["credits"]),
            "debits": format_amount(stmt["debits"]),
            "count": stmt["transaction_count"],
        })
    return {"user_id": user_id, "periods": per_period}


def render_summary_footer(page: int, pages: int) -> str:
    return paginate_footer(page, pages)


# ---------------------------------------------------------------------------
# Batch statement generation
# ---------------------------------------------------------------------------

def generate_statements(user_ids, year: int, month: int, display_currency: str = DEFAULT_CURRENCY) -> list[dict]:
    """Build statement views for many users in one pass.

    Each user is independent; a failure to build one user's statement is
    isolated so a single bad account does not abort the whole run. The
    balance line for each is fetched through the same read path as the
    single-statement flow.
    """
    out: list[dict] = []
    for user_id in user_ids:
        try:
            out.append(build_statement_view(user_id, year, month, display_currency))
        except Exception as exc:  # noqa: BLE001 - isolate per-user failures
            out.append({
                "user_id": user_id,
                "period": period_label(year, month),
                "error": type(exc).__name__,
            })
    return out


def render_batch_text(views: list[dict]) -> str:
    """Concatenate text statements with a page break between them."""
    chunks = []
    total = len([v for v in views if "error" not in v])
    page = 0
    for view in views:
        if "error" in view:
            continue
        page += 1
        chunks.append(render_text_statement(view))
        chunks.append(render_summary_footer(page, total))
        chunks.append("\f")
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# Archival envelope
# ---------------------------------------------------------------------------

def archival_record(view: dict) -> dict:
    """The record persisted to cold storage for regulatory retention.

    Retention is on the rendered figures, not on a re-derivable pointer, so
    a statement reads identically years later even if formatting rules or
    rates change. The balance line is stored verbatim as rendered.
    """
    return {
        "statement_number": statement_number(
            view["user_id"],
            *(int(p) for p in view["period"].split("-")),
        ),
        "user_id": view["user_id"],
        "period": view["period"],
        "account_currency": view["account_currency"],
        "display_currency": view["display_currency"],
        "balance_line": view["balance_line"],
        "movements": view["transaction_count"],
    }


def archival_manifest(views: list[dict]) -> dict:
    """Manifest summarising an archival batch for the retention index."""
    records = [archival_record(v) for v in views if "error" not in v]
    return {
        "count": len(records),
        "periods": sorted({r["period"] for r in records}),
        "records": records,
    }


# ---------------------------------------------------------------------------
# Annual summary
# ---------------------------------------------------------------------------

def build_annual_summary(user_id: str, year: int, display_currency: str = DEFAULT_CURRENCY) -> dict:
    """Twelve-period roll-up for the year-end summary email."""
    summary = build_multi_period_summary(user_id, year, 12, months=12)
    total_credits = Decimal("0")
    total_debits = Decimal("0")
    for row in summary["periods"]:
        total_credits += Decimal(row["credits"].replace(",", ""))
        total_debits += Decimal(row["debits"].replace(",", ""))
    return {
        "user_id": user_id,
        "year": year,
        "display_currency": display_currency,
        "periods": summary["periods"],
        "total_credits": format_amount(total_credits),
        "total_debits": format_amount(total_debits),
    }


# ---------------------------------------------------------------------------
# Corrected statements and diffs
# ---------------------------------------------------------------------------

def diff_statements(before: dict, after: dict) -> dict:
    """Describe what changed between two renderings of the same period.

    Used when a correction is issued: the customer gets a "what changed"
    note alongside the corrected statement. Compares the display-relevant
    fields only; the transaction list is compared by count and by id set.
    """
    changed: dict[str, tuple] = {}
    for field_name in ("balance_line", "credits", "debits", "account_currency"):
        if before.get(field_name) != after.get(field_name):
            changed[field_name] = (before.get(field_name), after.get(field_name))

    before_ids = {t.txn_id for t in before.get("transactions", [])}
    after_ids = {t.txn_id for t in after.get("transactions", [])}
    added = sorted(after_ids - before_ids)
    removed = sorted(before_ids - after_ids)

    return {
        "period": after.get("period"),
        "fields_changed": changed,
        "transactions_added": added,
        "transactions_removed": removed,
        "is_material": bool(changed or added or removed),
    }


def render_correction_note(diff: dict) -> str:
    """Human-readable correction note from a statement diff."""
    if not diff["is_material"]:
        return "No material change in this period."
    lines = [f"Corrected statement for {diff['period']}:"]
    for field_name, (old, new) in diff["fields_changed"].items():
        lines.append(f"  {field_name}: {old} -> {new}")
    if diff["transactions_added"]:
        lines.append(f"  transactions added: {len(diff['transactions_added'])}")
    if diff["transactions_removed"]:
        lines.append(f"  transactions removed: {len(diff['transactions_removed'])}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Delivery envelope
# ---------------------------------------------------------------------------

def build_delivery_envelope(view: dict, channel: str = DELIVERY_EMAIL) -> dict:
    """Everything the delivery worker needs to send one statement.

    Bundles the chosen rendering with its metadata and footer. The renderer
    is selected by channel: physical post gets the plain text layout, email
    gets markdown for the HTML beta. The balance line is identical in both.
    """
    annotated = annotate_view(view, channel)
    disclaimer = disclaimer_for_currency(
        view["display_currency"], view["account_currency"]
    )
    if delivery_is_physical(channel):
        body = render_text_statement(view)
    else:
        body = render_markdown_statement(view)
    return {
        "statement_number": annotated["statement_number"],
        "user_id": view["user_id"],
        "period": view["period"],
        "channel": annotated["delivery"],
        "disclaimer": disclaimer,
        "footer": annotated["legal_footer"],
        "body": body,
    }
