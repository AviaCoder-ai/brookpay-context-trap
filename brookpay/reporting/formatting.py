"""Presentation helpers for monetary values and report layout.

Everything here is display-only: it turns numbers, snapshots and rows into
strings for statements, receipts and the CLI. No function in this module
reads account state, hits the network, or makes a financial decision; a bug
here is a cosmetic bug, with one historically important exception noted on
`format_money` further down.

The module is larger than a naive "format a number" helper because BrookPay
prints in several locales and currencies, masks sensitive identifiers, and
renders both fixed-width text statements and plain receipts from the same
primitives. Keeping all of that in one place stops five slightly different
money formatters from drifting apart across the codebase.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from brookpay.config.constants import CURRENCY_MINOR_UNITS


# ---------------------------------------------------------------------------
# Currency presentation tables
# ---------------------------------------------------------------------------

# Symbols shown in compact contexts (chips, mobile). Full statements print
# the ISO code instead, which is unambiguous across locales.
_CURRENCY_SYMBOLS = {
    "EUR": "\u20ac",
    "USD": "$",
    "GBP": "\u00a3",
    "JPY": "\u00a5",
    "CHF": "CHF",
    "THB": "\u0e3f",
    "SGD": "S$",
}

# Whether the symbol sits before or after the amount, per currency. Wrong
# placement looks unprofessional, so this is explicit rather than guessed.
_SYMBOL_BEFORE = {"USD", "GBP", "JPY", "SGD"}


def currency_symbol(currency: str) -> str:
    """Symbol for a currency, falling back to the ISO code when unknown."""
    return _CURRENCY_SYMBOLS.get(currency.upper(), currency.upper())


def minor_units(currency: str) -> int:
    """Decimal places for a currency, defaulting to 2 for unknown codes."""
    return CURRENCY_MINOR_UNITS.get(currency.upper(), 2)


def _grouping_for(locale: str) -> tuple[str, str]:
    return _LOCALE_SEPARATORS.get(locale, (",", "."))


# Thousands/decimal separators per locale tag. The statement renderer passes
# the customer locale; anything unknown falls back to the plain grouping the
# f-string ",.2f" produces, which is the en_GB/en_US convention.
_LOCALE_SEPARATORS = {
    "en_GB": (",", "."),
    "en_US": (",", "."),
    "fr_FR": (" ", ","),
    "de_DE": (".", ","),
    "th_TH": (",", "."),
}


def _regroup(plain: str, thousands: str, decimal: str) -> str:
    """Rewrite a canonical "1,234.56" string into a locale's separators.

    `plain` is always produced first with the ",." convention, then the
    separators are swapped. Done as a two-step swap through a sentinel so a
    locale whose thousands separator is "." (de_DE) does not clobber the
    decimal point mid-substitution.
    """
    return (
        plain.replace(",", "\0")
        .replace(".", decimal)
        .replace("\0", thousands)
    )


def format_amount_locale(value, locale: str = "en_GB", places: int = 2) -> str:
    """Group a bare number for display in the given locale."""
    thousands, decimal = _grouping_for(locale)
    plain = f"{float(value):,.{places}f}"
    if (thousands, decimal) == (",", "."):
        return plain
    return _regroup(plain, thousands, decimal)


def format_currency_compact(value, currency: str, locale: str = "en_GB") -> str:
    """Symbol + grouped amount, minor-unit aware, for compact surfaces."""
    places = minor_units(currency)
    body = format_amount_locale(value, locale, places=places)
    symbol = currency_symbol(currency)
    if currency.upper() in _SYMBOL_BEFORE:
        return f"{symbol}{body}"
    return f"{body}\u00a0{symbol}"


# ---------------------------------------------------------------------------
# Number spelling (cheque printing)
# ---------------------------------------------------------------------------

_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen",
)
_TENS = (
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
    "eighty", "ninety",
)

# Scale groups, largest first. Anything above the largest scale is spelled by
# the leading group, which is enough for cheque amounts; BrookPay caps cheque
# printing well below a billion.
_SCALE_GROUPS = ((1_000_000, "million"), (1_000, "thousand"), (1, ""))


def _spell_below_thousand(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, rest = divmod(n, 10)
        return _TENS[tens] + (f"-{_ONES[rest]}" if rest else "")
    hundreds, rest = divmod(n, 100)
    head = f"{_ONES[hundreds]} hundred"
    return f"{head} {_spell_below_thousand(rest)}" if rest else head


def spell_integer(n: int) -> str:
    """Spell a non-negative integer up to the low millions, for cheques.

    Walks the scale groups largest first, spelling each non-empty group and
    carrying the remainder down. A zero group is skipped entirely, so 1000005
    reads "one million five" rather than naming an empty thousands group.
    """
    if n <= 0:
        return "zero"
    remaining = n
    words: list[str] = []
    for scale, name in _SCALE_GROUPS:
        if remaining >= scale:
            count, remaining = divmod(remaining, scale)
            words.append(f"{_spell_below_thousand(count)} {name}".strip())
    return " ".join(words)


def spell_amount(value, currency: str = "EUR") -> str:
    """Spell a monetary amount as "one hundred twenty and 50/100 EUR"."""
    number = Decimal(str(value))
    whole = int(number)
    cents = int((number - whole) * 100)
    return f"{spell_integer(whole)} and {cents:02d}/100 {currency.upper()}"


# ---------------------------------------------------------------------------
# Date and time presentation
# ---------------------------------------------------------------------------

_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)
_MONTH_ABBR = tuple(name[:3] for name in _MONTH_NAMES)
_WEEKDAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def format_date(dt) -> str:
    """Statement date as "05 Jun 2026"."""
    return f"{dt.day:02d} {_MONTH_ABBR[dt.month - 1]} {dt.year}"


def format_datetime(dt) -> str:
    """Timestamp as "05 Jun 2026 14:03 UTC"."""
    return f"{format_date(dt)} {dt.hour:02d}:{dt.minute:02d} UTC"


def format_month_year(year: int, month: int) -> str:
    """Statement period header as "June 2026"."""
    return f"{_MONTH_NAMES[month - 1]} {year}"


def format_weekday(dt) -> str:
    return _WEEKDAY_ABBR[dt.weekday()]


def relative_time(delta_seconds: float) -> str:
    """Human relative time such as "3 days ago" / "in 2 hours".

    Positive deltas are in the past, negative in the future, matching a
    "now - event" convention. Only the largest unit is shown; statements do
    not need "1 day 3 hours" precision.
    """
    future = delta_seconds < 0
    seconds = abs(delta_seconds)
    units = (
        ("year", 365 * 86400),
        ("month", 30 * 86400),
        ("day", 86400),
        ("hour", 3600),
        ("minute", 60),
    )
    for name, size in units:
        if seconds >= size:
            count = int(seconds // size)
            plural = "s" if count != 1 else ""
            phrase = f"{count} {name}{plural}"
            return f"in {phrase}" if future else f"{phrase} ago"
    return "just now"


def format_duration(seconds: float) -> str:
    """Compact duration like "2h 05m" for the ops/latency views."""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


# ---------------------------------------------------------------------------
# Text progress bars
# ---------------------------------------------------------------------------

def progress_bar(fraction: float, width: int = 20) -> str:
    """A [#####-----] style bar for budget and limit usage displays."""
    fraction = max(0.0, min(1.0, float(fraction)))
    filled = int(round(fraction * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def ratio_bar(value, total, width: int = 20) -> str:
    """Progress bar plus a "value/total" caption."""
    total_f = float(total)
    fraction = float(value) / total_f if total_f else 0.0
    return f"{progress_bar(fraction, width)} {format_amount(value)}/{format_amount(total)}"


# ---------------------------------------------------------------------------
# Identifier masking
# ---------------------------------------------------------------------------

def mask_iban(iban: str) -> str:
    """Show the first four and last two characters, mask the middle."""
    compact = "".join(iban.split()).upper()
    if len(compact) <= 6:
        return compact
    return f"{compact[:4]}{'*' * (len(compact) - 6)}{compact[-2:]}"


def mask_card_token(token: str) -> str:
    """Reveal only the last four digits of a card-like token."""
    tail = token[-4:]
    return f"\u2022\u2022\u2022\u2022 {tail}" if len(token) >= 4 else token


def format_iban_groups(iban: str) -> str:
    """Group an IBAN into the conventional blocks of four for display."""
    compact = "".join(iban.split()).upper()
    return " ".join(compact[i : i + 4] for i in range(0, len(compact), 4))


# ---------------------------------------------------------------------------
# Balance snapshot rendering (backward-compatibility path)
# ---------------------------------------------------------------------------

def format_money(payload, fallback_currency: str) -> str:
    """Render a balance snapshot as "1,234.56 CCY".

    Accepts the structured snapshot produced by the balance pipeline: a
    mapping carrying an "amount" and a "currency". The amount is already
    expressed in its own currency by the read path, so the currency printed
    here comes from the snapshot itself, not from the caller.

    A backward compatible path remains for snapshots produced before 1.3.0,
    which were bare numeric amounts without any currency metadata. Those are
    rendered with the caller's fallback currency, because it is the only
    currency information available in that shape. This path is the reason a
    multi-currency statement silently mislabels its balance if the snapshot
    ever loses its structure: a JPY amount arriving as a bare number is
    printed with whatever fallback the caller happened to pass, typically
    the display currency, so 100000 JPY reads as "100,000.00 EUR" with no
    error raised anywhere.
    """
    if isinstance(payload, Mapping):
        return f"{float(payload['amount']):,.2f} {payload['currency']}"
    return f"{float(payload):,.2f} {fallback_currency}"


def format_money_locale(payload, fallback_currency: str, locale: str = "en_GB") -> str:
    """Locale-aware variant of format_money, same snapshot contract.

    Shares the exact backward-compatibility behaviour of format_money for
    bare snapshots: without currency metadata the fallback currency is the
    only label available, so the same silent mislabelling applies.
    """
    if isinstance(payload, Mapping):
        amount = payload["amount"]
        currency = payload["currency"]
    else:
        amount = payload
        currency = fallback_currency
    return f"{format_amount_locale(amount, locale)} {currency}"


def safe_amount(value, default: Decimal = Decimal("0")) -> Decimal:
    """Parse a display amount defensively; never raises for the renderer."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# Scalars
# ---------------------------------------------------------------------------

def format_amount(value) -> str:
    return f"{float(value):,.2f}"


def format_signed(value) -> str:
    """Amount with an explicit sign, for ledger deltas."""
    number = float(value)
    return f"{number:+,.2f}"


def format_pct(value, digits: int = 1) -> str:
    return f"{float(value) * 100:.{digits}f}%"


def format_bps(value) -> str:
    """Basis points, for the fee and interest displays."""
    return f"{float(value) * 10000:.0f} bps"


def truncate_label(text: str, width: int = 32) -> str:
    """Shorten a label to width, adding an ellipsis when clipped."""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "\u2026"


# ---------------------------------------------------------------------------
# Column layout
# ---------------------------------------------------------------------------

def pad_columns(rows: list[tuple[str, ...]], gap: int = 2) -> list[str]:
    """Align tuples of strings into fixed-width text columns."""
    if not rows:
        return []
    widths = [0] * max(len(r) for r in rows)
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    out = []
    for row in rows:
        line = (" " * gap).join(
            cell.ljust(widths[i]) for i, cell in enumerate(row)
        )
        out.append(line.rstrip())
    return out


def right_align_column(rows: list[tuple[str, ...]], column: int, gap: int = 2) -> list[str]:
    """Like pad_columns but right-aligns one column (amounts read better)."""
    if not rows:
        return []
    widths = [0] * max(len(r) for r in rows)
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    out = []
    for row in rows:
        cells = []
        for i, cell in enumerate(row):
            cells.append(cell.rjust(widths[i]) if i == column else cell.ljust(widths[i]))
        out.append((" " * gap).join(cells).rstrip())
    return out


def rule_line(width: int = 78, char: str = "-") -> str:
    return char * width


def kv_block(pairs: list[tuple[str, str]], gap: int = 2) -> list[str]:
    """Render key/value pairs as an aligned two-column block."""
    if not pairs:
        return []
    key_width = max(len(k) for k, _ in pairs)
    return [f"{k.ljust(key_width)}{' ' * gap}{v}" for k, v in pairs]


def box(title: str, lines: list[str], width: int = 60) -> list[str]:
    """Wrap lines in a simple ASCII box with a title bar, for the CLI."""
    inner = width - 2
    top = "+" + "-" * inner + "+"
    header = "|" + f" {truncate_label(title, inner - 2)} ".ljust(inner) + "|"
    sep = "+" + "-" * inner + "+"
    body = ["|" + f" {truncate_label(line, inner - 2)} ".ljust(inner) + "|" for line in lines]
    return [top, header, sep, *body, top]


def sparkline(values: list[float]) -> str:
    """Tiny unicode sparkline for balance trend chips."""
    if not values:
        return ""
    blocks = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
    lo, hi = min(values), max(values)
    if hi == lo:
        return blocks[0] * len(values)
    span = hi - lo
    return "".join(blocks[int((v - lo) / span * (len(blocks) - 1))] for v in values)


# ---------------------------------------------------------------------------
# Markdown tables (for the HTML statement beta and support exports)
# ---------------------------------------------------------------------------

def markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render a GitHub-flavoured markdown table from headers and rows.

    Used by the statement HTML beta and by support when pasting account
    activity into a ticket. Cells are escaped minimally: pipes are the only
    character that would break the table, so only pipes are replaced.
    """
    def esc(cell: str) -> str:
        return str(cell).replace("|", "\\|")

    head = "| " + " | ".join(esc(h) for h in headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = [
        "| " + " | ".join(esc(c) for c in row) + " |"
        for row in rows
    ]
    return "\n".join([head, sep, *body])


def csv_quote(value: str) -> str:
    """Quote a single CSV field per RFC 4180 when it needs quoting."""
    text = str(value)
    if any(ch in text for ch in (",", '"', "\n", "\r")):
        escaped = text.replace('"', '""')
        return f'"{escaped}"'
    return text


def csv_row(cells: list) -> str:
    return ",".join(csv_quote(c) for c in cells)


# ---------------------------------------------------------------------------
# Receipt and statement templates
# ---------------------------------------------------------------------------

def render_receipt(
    title: str,
    reference: str,
    amount_line: str,
    pairs: list[tuple[str, str]],
    width: int = 40,
) -> str:
    """Render a compact payment receipt as fixed-width text.

    `amount_line` is expected to be pre-formatted (the caller decides the
    currency and grouping); this function only lays it out. Nothing here
    interprets the amount, it is placed verbatim.
    """
    lines = [
        title.upper().center(width),
        rule_line(width, "="),
        f"Ref: {reference}",
        "",
        amount_line.rjust(width),
        "",
    ]
    lines.extend(kv_block(pairs))
    lines.append(rule_line(width, "-"))
    lines.append("Thank you".center(width))
    return "\n".join(lines)


def statement_header(user_id: str, period: str, account_currency: str) -> list[str]:
    """The three-line header every statement rendering shares."""
    return [
        f"BrookPay statement {period} for {user_id}",
        f"Account currency: {account_currency}",
        rule_line(78, "-"),
    ]


def statement_footer(transaction_count: int, credits: str, debits: str, currency: str) -> list[str]:
    """The summary footer every statement rendering shares."""
    return [
        rule_line(78, "-"),
        f"Movements: {transaction_count} (in {credits}, out {debits} {currency})",
    ]


def paginate_footer(page: int, pages: int, width: int = 78) -> str:
    """Right-aligned "Page 1 of 3" footer."""
    return f"Page {page} of {pages}".rjust(width)


# ---------------------------------------------------------------------------
# Free-text redaction
# ---------------------------------------------------------------------------

def redact_emails(text: str) -> str:
    """Mask the local part of any email address in free text.

    Used before free-text notes are attached to an export that leaves the
    trust boundary. Cheap and deliberately conservative: it masks anything
    shaped like an address rather than trying to be clever.
    """
    out = []
    for token in text.split(" "):
        if "@" in token and "." in token.split("@")[-1]:
            local, _, domain = token.partition("@")
            keep = local[:1]
            out.append(f"{keep}{'*' * max(1, len(local) - 1)}@{domain}")
        else:
            out.append(token)
    return " ".join(out)


def collapse_whitespace(text: str) -> str:
    """Collapse runs of whitespace to single spaces and strip the ends."""
    return " ".join(text.split())
