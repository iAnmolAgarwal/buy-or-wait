"""Output formatting rules, derived from problem_statement.md and the style of
dataset/sample_requests.csv. Pure functions, no I/O."""
from __future__ import annotations

from datetime import date


def fmt_amount(x: float) -> str:
    """Amount as written in output columns: integer if whole, else 2 decimals.
    25256 -> "25256"; 620.4 -> "620.40"; 22590.19 -> "22590.19"."""
    r = round(float(x) + 0.0, 2)
    if abs(r - round(r)) < 1e-9:
        return str(int(round(r)))
    return f"{r:.2f}"


def fmt_safe_amount(x: float) -> str:
    """amount_safe_to_pay column: shortest representation of value rounded to 2dp.
    17229139.2 -> "17229139.2"; 737 -> "737"; 87170.56 -> "87170.56"."""
    r = round(float(x) + 0.0, 2)
    if abs(r - round(r)) < 1e-9:
        return str(int(round(r)))
    s = f"{r:.2f}".rstrip("0").rstrip(".")
    return s


def fmt_money(ccy: str, x: float) -> str:
    """Explanation-text amount: 'INR 22,590.19', 'ZAR 25,256', 'EUR 620.40'."""
    r = round(float(x) + 0.0, 2)
    if abs(r - round(r)) < 1e-9:
        return f"{ccy} {int(round(r)):,}"
    return f"{ccy} {r:,.2f}"


def fmt_date_iso(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def fmt_date_long(d: date) -> str:
    """'15 November 2019' (no leading zero)."""
    return f"{d.day} {d.strftime('%B %Y')}"


def fmt_plan(payments: list[tuple[date, float]]) -> str:
    if not payments:
        return "none"
    return "|".join(f"{fmt_date_iso(d)}:{fmt_amount(a)}" for d, a in payments)
