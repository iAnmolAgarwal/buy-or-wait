"""Recurring-stream detection and projection (ingest/recurrence.py).

Contract Section 2. Stdlib only; nothing here imports from `engine/`.
`policy` is duck-typed: it needs `.amount_estimator`, `.min_occurrences` and,
optionally, `.min_income_occurrences` (default 2 when the policy lacks it).
"""
from __future__ import annotations

import calendar
import re
import statistics
from datetime import date, timedelta
from typing import Optional

from buyorwait.types import Event, Profile, RecurringStream

# Never projected as a recurring flow.
EXCLUDED_EVENT_TYPES = {
    "refund",
    "investment_purchase",
    "investment_sale",
    "investment_valuation",
}
EXCLUDED_CATEGORIES = {"windfall"}
# Credits only form a stream when they are recurring income.
CREDIT_CATEGORIES = {"salary"}

_WS = re.compile(r"\s+")
# Month names and years are period labels, not identity: "August 2019 net salary"
# and "net salary" are the same commitment (D16).
_PERIOD = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|"
    r"\d{4}|q[1-4])\b",
    re.IGNORECASE,
)
# A salary event so described ends the employment relationship (D18).
_TERMINAL = re.compile(r"\bfinal\b", re.IGNORECASE)
# Staleness: a stream is dead when nothing has happened for 1.5 cadences (D17).
STALENESS_FACTOR = 1.5
MONTHLY_CADENCE = 30


def _numeric_id(s: str) -> tuple[int, str]:
    m = re.findall(r"(\d+)", s or "")
    return (int(m[-1]) if m else -1, s or "")


def _norm_desc(s: str) -> str:
    """Grouping key: case- and whitespace-insensitive, period labels removed."""
    return _WS.sub(" ", _PERIOD.sub(" ", (s or "").strip().lower())).strip()


def _round_half_up(x: float) -> int:
    return int(x + 0.5) if x >= 0 else -int(-x + 0.5)


def _date_gaps(dates: list[date]) -> list[int]:
    return [(b - a).days for a, b in zip(dates, dates[1:])]


def _gaps(occurrences: list[Event]) -> list[int]:
    return _date_gaps([e.event_date for e in occurrences])


def _is_regular(occurrences: list[Event]) -> bool:
    """True when the gaps between occurrences form one consistent cadence.

    Tolerance: every gap within max(2 days, 25%) of the median gap. This is what
    separates "two subscriptions sharing a category" (each sub-group regular) from
    "groceries with many different shop names" (only the whole category is regular).
    """
    gaps = _gaps(occurrences)
    if not gaps:
        return False
    med = statistics.median(gaps)
    if med <= 0:
        return False
    tol = max(2.0, 0.25 * med)
    return all(abs(g - med) <= tol for g in gaps)


def _amount_stable(occurrences: list[Event]) -> bool:
    """True when every occurrence costs (almost) the same -- the signature of a
    distinct commitment such as a subscription or a payroll credit, as opposed to
    a category of varying everyday spend."""
    amounts = [e.amount for e in occurrences if e.amount is not None]
    if len(amounts) < 2:
        return False
    mean = statistics.fmean(amounts)
    if mean == 0:
        return all(a == 0 for a in amounts)
    spread = (max(amounts) - min(amounts)) / abs(mean)
    return spread <= 0.05


def _same_day_of_month(dates: list[date]) -> bool:
    """True when every date falls on one day-of-month (month-end clamped)."""
    days = set()
    for d in dates:
        last = calendar.monthrange(d.year, d.month)[1]
        days.add("end" if d.day == last and d.day < 31 else d.day)
    if len(days) == 1:
        return True
    return days <= {28, 29, 30, 31, "end"} and all(
        isinstance(d, str) or d >= 28 for d in days
    )


def _is_monthly_series(dates: list[date]) -> bool:
    """True when consecutive dates sit roughly one month apart."""
    gaps = _date_gaps(dates)
    if not gaps:
        return False
    return 28 <= statistics.median(gaps) <= 31


def _dom_mode(dates: list[date], newest: date) -> int:
    """The day-of-month an income stream really lands on (D16).

    The most common day wins; ties go to the newest occurrence's day when it is
    among them, else to the later day (income arriving later is the safer read).
    """
    counts: dict[int, int] = {}
    for d in dates:
        counts[d.day] = counts.get(d.day, 0) + 1
    top = max(counts.values())
    tied = [day for day, n in counts.items() if n == top]
    if newest.day in tied:
        return newest.day
    return max(tied)


def _estimate(amounts: list[float], estimator: str) -> float:
    if estimator == "median":
        return float(statistics.median(amounts))
    if estimator == "last":
        return float(amounts[-1])
    if estimator == "max":
        return float(max(amounts))
    return float(statistics.fmean(amounts))  # "mean" (default)


def _is_income_event(e: Event) -> bool:
    """A credit that is recurring income -- never a windfall, refund or sale."""
    if e.direction != "credit":
        return False
    if e.category in EXCLUDED_CATEGORIES or e.event_type in EXCLUDED_EVENT_TYPES:
        return False
    return e.category in CREDIT_CATEGORIES or e.event_type == "income"


def _is_candidate(e: Event) -> bool:
    if e.status != "settled":
        return False
    if e.amount is None:
        return False
    if e.event_type in EXCLUDED_EVENT_TYPES:
        return False
    if e.category in EXCLUDED_CATEGORIES:
        return False
    if e.direction == "debit":
        return True
    return _is_income_event(e)


def salary_streams(streams: list[RecurringStream]) -> list[RecurringStream]:
    """The recurring-income streams among `streams` (for the engine's income rules)."""
    return [
        s
        for s in streams
        if s.direction == "credit"
        and s.category in CREDIT_CATEGORIES
        and s.category not in EXCLUDED_CATEGORIES
    ]


def _build(
    occurrences: list[Event],
    user_id: str,
    estimator: str,
    k: int,
    future_anchor: Optional[Event] = None,
    income: bool = False,
) -> RecurringStream:
    """Build one stream.

    `future_anchor` is a SCHEDULED income event dated after the request. It shapes
    the amount, the day-of-month and `last_date` only; it never enters `event_ids`,
    because the engine already projects scheduled events in their own right and
    must not pay the same salary twice (D16).
    """
    occurrences = sorted(occurrences, key=lambda e: (e.event_date, _numeric_id(e.event_id)))
    dates = [e.event_date for e in occurrences]
    if future_anchor is not None:
        dates = sorted(dates + [future_anchor.event_date])
    newest = future_anchor if future_anchor is not None else occurrences[-1]

    gaps = _date_gaps(dates)
    med = statistics.median(gaps) if gaps else 30.0
    cadence = max(1, _round_half_up(med))
    monthly = 28 <= med <= 31
    # D12: semi-monthly income (e.g. paid on the 8th and the 20th) alternates
    # 12/18-day gaps, so the median cadence would project 2.5 payments a month.
    # The MEAN gap is the honest period for such a stream: 2 payments per 30 days.
    if newest.direction == "credit" and gaps:
        mean_gap = statistics.fmean(gaps)
        if 13 <= mean_gap <= 17:
            cadence = 15
            monthly = False
    # A series that always lands on the same day of the month IS monthly even when
    # a month is missing from the history (payroll interrupted by unpaid leave
    # gives gaps of 31 and 90, whose median 60.5 would halve the projected salary).
    if not monthly and len(dates) >= 3 and med >= 28 and _same_day_of_month(dates):
        monthly = True
        cadence = MONTHLY_CADENCE

    last_date = newest.event_date
    if income and monthly:
        # D16: anchor on the day the stream usually lands, not on whichever day the
        # newest record happens to carry (a restated "August 2019 net salary" filed
        # on the 31st must not move payroll off the 15th).
        last_date = _clamp_day(
            last_date.year, last_date.month, _dom_mode(dates, newest.event_date)
        )

    if income and monthly:
        # Salary is a contract figure: the newest record is the current one (D16).
        # Non-monthly income (weekly gig payouts, freelance invoices) is a jittered
        # draw, not a contract, so it keeps the policy estimator.
        amount = float(newest.amount if newest.amount is not None else occurrences[-1].amount)
    else:
        amount = _estimate([e.amount for e in occurrences], estimator)

    return RecurringStream(
        stream_id=f"{user_id}:{newest.category}:{k}",
        category=newest.category,
        description=newest.description,
        direction=newest.direction,
        cadence_days=cadence,
        monthly=monthly,
        amount=round(amount, 2),
        anchor_event_id=newest.event_id,
        last_date=last_date,
        flexibility=newest.flexibility,
        minimum_allowed_amount=newest.minimum_allowed_amount,
        event_ids=[e.event_id for e in occurrences],
    )


def detect_streams(
    events: list[Event],
    profile: Profile,
    request_date: date,
    policy,
    notes: Optional[list[str]] = None,
) -> list[RecurringStream]:
    """Detect recurring debits and income credits from settled history.

    Expenses are grouped by category (descriptions inside a category vary freely:
    "Bulk pantry shop" and "Supermarket basket" are the same grocery stream) and
    split per description only when two descriptions are clearly separate
    commitments. INCOME is grouped by normalised description instead (D16): "Base
    salary", "Performance commission" and "Promotion arrears payment" all sit in
    category 'salary' but are different money.

    `notes` is an optional sink for human-readable decisions (terminated streams,
    stale streams, scheduled-salary anchoring). The contract's four positional
    arguments are unchanged.
    """
    log = notes if notes is not None else []
    min_occ = max(1, int(getattr(policy, "min_occurrences", 3)))
    # D11: a confirmed recurring credit needs fewer occurrences than a spend
    # pattern -- two payrolls on the same day-of-month are income, not noise.
    min_income = max(1, int(getattr(policy, "min_income_occurrences", 2) or 2))
    estimator = getattr(policy, "amount_estimator", "mean") or "mean"
    user_id = profile.user_id if profile is not None else (events[0].user_id if events else "")

    debit_groups: dict[str, list[Event]] = {}
    credit_groups: dict[str, list[Event]] = {}
    future_income: dict[str, list[Event]] = {}
    for e in events:
        if (
            e.status == "scheduled"
            and e.event_date > request_date
            and e.amount is not None
            and _is_income_event(e)
        ):
            future_income.setdefault(e.category, []).append(e)
        if e.event_date > request_date or not _is_candidate(e):
            continue
        if e.direction == "debit":
            debit_groups.setdefault(e.category, []).append(e)
        else:
            credit_groups.setdefault(e.category, []).append(e)

    streams: list[RecurringStream] = []
    counters: dict[str, int] = {}

    def _next_k(category: str) -> int:
        k = counters.get(category, 0)
        counters[category] = k + 1
        return k

    # ---- expenses -----------------------------------------------------------
    for category in sorted(debit_groups):
        occ = sorted(
            debit_groups[category], key=lambda e: (e.event_date, _numeric_id(e.event_id))
        )
        if len(occ) < min_occ:
            continue

        subs: dict[str, list[Event]] = {}
        for e in occ:
            subs.setdefault(_norm_desc(e.description), []).append(e)
        qualified = [
            subs[d]
            for d in sorted(subs)
            if len(subs[d]) >= min_occ and _is_regular(subs[d]) and _amount_stable(subs[d])
        ]
        whole_regular = _is_regular(occ)
        # Split rule: two or more descriptions each recur on their own cadence for
        # their own stable amount (two subscriptions inside one category), or the
        # category as a whole is irregular but exactly one description inside it
        # recurs cleanly. Categories of varying everyday spend -- groceries bought
        # at half a dozen differently named shops -- fail the stability test and
        # stay a single stream.
        if len(qualified) >= 2 or (len(qualified) == 1 and not whole_regular):
            chosen = qualified
        else:
            chosen = [occ]

        for members in chosen:
            if len(members) < min_occ:
                continue
            streams.append(_build(members, user_id, estimator, _next_k(category)))

    # ---- income -------------------------------------------------------------
    for category in sorted(set(credit_groups) | set(future_income)):
        occ = sorted(
            credit_groups.get(category, []),
            key=lambda e: (e.event_date, _numeric_id(e.event_id)),
        )
        scheduled = sorted(
            future_income.get(category, []),
            key=lambda e: (e.event_date, _numeric_id(e.event_id)),
        )
        if not occ:
            continue

        # D18: a "Final employer payroll" ends the relationship -- nothing after it.
        newest_overall = max(
            occ + scheduled, key=lambda e: (e.event_date, _numeric_id(e.event_id))
        )
        if _TERMINAL.search(newest_overall.description or ""):
            log.append(
                f"{user_id}: income category '{category}' terminated by "
                f"{newest_overall.event_id} ({newest_overall.description!r}); no stream built"
            )
            continue

        subs: dict[str, list[Event]] = {}
        for e in occ:
            subs.setdefault(_norm_desc(e.description), []).append(e)

        # D16 splits income by description -- but only when the description labels
        # really are separate commitments. A label is a commitment when it recurs
        # monthly on its own; the split must cover almost the whole category, and
        # two labels landing on the SAME day-of-month are one stream renamed
        # (payroll before/after unpaid leave), never two salaries.
        qualified = [
            key
            for key in sorted(subs)
            if len(subs[key]) >= min_income
            and _is_monthly_series([e.event_date for e in subs[key]])
        ]
        doms = [
            _dom_mode([e.event_date for e in subs[key]], subs[key][-1].event_date)
            for key in qualified
        ]
        if qualified and len(set(doms)) == len(doms):
            keys = qualified
            by_dom = dict(zip(doms, qualified))
            grouped = {key: list(subs[key]) for key in qualified}
            for key in sorted(subs):
                if key in grouped:
                    continue
                for e in subs[key]:
                    # An odd-label credit on the stream's own day-of-month is that
                    # stream renamed ("Payroll after returning from leave"); on any
                    # other day it is a one-off (arrears, a bonus) and is not
                    # projected at all.
                    owner = by_dom.get(e.event_date.day)
                    if owner is not None:
                        grouped[owner].append(e)
            subs = {
                key: sorted(v, key=lambda e: (e.event_date, _numeric_id(e.event_id)))
                for key, v in grouped.items()
            }
        else:
            # Rotating labels on one income ("Website project payment", "Client
            # retainer payment", ...) are not separate salaries: keep the category.
            subs = {"": occ}
            keys = [""]

        # The scheduled "Next confirmed salary" restates the principal income, not
        # whichever side income shares the category, so it anchors the largest group.
        principal = max(keys, key=lambda d: (len(subs[d]), subs[d][-1].event_date))
        anchor = scheduled[-1] if scheduled else None

        for key in keys:
            members = subs[key]
            fa = anchor if key == principal else None
            if len(members) + (1 if fa is not None else 0) < min_income:
                continue
            stream = _build(
                members, user_id, estimator, _next_k(category), future_anchor=fa, income=True
            )
            if fa is not None:
                log.append(
                    f"{stream.stream_id}: amount and day-of-month taken from scheduled "
                    f"{fa.event_id} ({fa.event_date}); not counted as an occurrence"
                )
            streams.append(stream)

    # ---- staleness (D17) ----------------------------------------------------
    fresh: list[RecurringStream] = []
    for s in streams:
        period = MONTHLY_CADENCE if s.monthly else s.cadence_days
        age = (request_date - s.last_date).days
        if age > STALENESS_FACTOR * period:
            log.append(
                f"{s.stream_id}: dropped as stale (last occurrence {s.last_date}, "
                f"{age} days before the request, cadence {period})"
            )
            continue
        fresh.append(s)
    return fresh



def _clamp_day(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def next_occurrences(stream: RecurringStream, start: date, end: date) -> list[date]:
    """Projected future dates for `stream`, strictly after `last_date` and in [start, end]."""
    out: list[date] = []
    if end < start:
        return out
    if stream.monthly:
        day = stream.last_date.day
        y, m = stream.last_date.year, stream.last_date.month
        for _ in range(400):  # bounded: 400 months is far beyond any horizon
            m += 1
            if m > 12:
                m = 1
                y += 1
            d = _clamp_day(y, m, day)
            if d > end:
                break
            if d > stream.last_date and d >= start:
                out.append(d)
    else:
        cadence = int(stream.cadence_days or 0)
        if cadence <= 0:
            return out
        k = 1
        while True:
            d = stream.last_date + timedelta(days=k * cadence)
            if d > end:
                break
            if d >= start:
                out.append(d)
            k += 1
    return out
