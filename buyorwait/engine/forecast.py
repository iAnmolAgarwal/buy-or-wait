"""Cash-flow projection and the day-by-day balance path.

docs/CONTRACT.md Section 2, engine/forecast.py.

The engine consumes already-built streams / scheduled events / amendments; it
never imports from ingest/ or perception/. Two small private helpers mirror
rules that also live in ingest (`_occurrences` mirrors
`ingest.recurrence.next_occurrences`, `_convert` mirrors `ingest.fx.convert`)
so that this package stays standalone.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Iterable, Optional, Sequence

from buyorwait.types import CashFlow, Event, Profile, RecurringStream, Request
from buyorwait.engine.policy import Policy

# ---------------------------------------------------------------------------
# Occurrence projection (mirror of ingest.recurrence.next_occurrences)
# ---------------------------------------------------------------------------


def _add_months(anchor: date, k: int) -> date:
    """`anchor` shifted by k months, keeping the anchor's day-of-month clamped
    to the length of the target month (31 Jan + 1 month -> 28/29 Feb)."""
    m0 = anchor.month - 1 + k
    year = anchor.year + m0 // 12
    month = m0 % 12 + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _monthly_from(first: date, start: date, end: date) -> list[date]:
    """`first`, then the same day-of-month each month (clamped), kept inside
    ``[start, end]``. Used when an amendment has to *create* an income stream."""
    out: list[date] = []
    k = 0
    while True:
        d = _add_months(first, k)
        if d > end:
            break
        if d >= start:
            out.append(d)
        k += 1
    return out


def _occurrences(stream: RecurringStream, start: date, end: date) -> list[date]:
    """Projected occurrence dates for `stream` strictly after ``stream.last_date``
    and inside ``[start, end]``.

    monthly -> same day-of-month as last_date, clamped to month length;
    otherwise -> last_date + k * cadence_days.
    """
    if end < start:
        return []
    last = stream.last_date
    out: list[date] = []
    if stream.monthly:
        # bound: at most one occurrence per month plus slack
        k_max = (end.year - last.year) * 12 + (end.month - last.month) + 2
        for k in range(1, max(k_max, 0) + 1):
            d = _add_months(last, k)
            if d > end:
                break
            if d > last and d >= start:
                out.append(d)
    else:
        step = int(stream.cadence_days or 0)
        if step <= 0:
            return []
        k = 1
        while True:
            d = last + timedelta(days=step * k)
            if d > end:
                break
            if d > last and d >= start:
                out.append(d)
            k += 1
    return out


# ---------------------------------------------------------------------------
# FX (mirror of ingest.fx.convert) -- engine-local so there is no ingest import
# ---------------------------------------------------------------------------


def _rate_rows(rates) -> Sequence[tuple]:
    return getattr(rates, "rows", None) or ()


def _pick(rows: list[tuple], on: date) -> Optional[tuple]:
    if not rows:
        return None
    le = [r for r in rows if r[0] <= on]
    if le:
        return max(le, key=lambda r: r[0])
    return min(rows, key=lambda r: r[0])


def _rate_lookup(from_ccy: str, to_ccy: str, on: date, rates) -> Optional[float]:
    rows = _rate_rows(rates)
    direct = _pick([r for r in rows if r[1] == from_ccy and r[2] == to_ccy], on)
    if direct is not None:
        return float(direct[3])
    inverse = _pick([r for r in rows if r[1] == to_ccy and r[2] == from_ccy], on)
    if inverse is not None and float(inverse[3]) != 0.0:
        return 1.0 / float(inverse[3])
    return None


def _convert(amount: float, from_ccy: str, to_ccy: str, on: date, rates) -> float:
    """Direct pair on the nearest rate_date <= `on`, else inverse as 1/rate, else
    routed through USD. Raises ValueError when unroutable."""
    if amount is None:
        raise ValueError("no amount to convert")
    if not from_ccy or not to_ccy or from_ccy == to_ccy:
        return float(amount)
    r = _rate_lookup(from_ccy, to_ccy, on, rates)
    if r is not None:
        return float(amount) * r
    a = _rate_lookup(from_ccy, "USD", on, rates)
    b = _rate_lookup("USD", to_ccy, on, rates)
    if a is not None and b is not None:
        return float(amount) * a * b
    raise ValueError(f"no route {from_ccy}->{to_ccy} on {on}")


# ---------------------------------------------------------------------------
# Amendments
# ---------------------------------------------------------------------------

# Deterministic application order (docs/CONTRACT.md Section 2 + PS:200-205).
_KIND_ORDER = [
    "cancel",
    "delay",
    "amend_amount",
    "amend_amount_pct",
    "income_change",
    "income_end",
    "new_income",
    "one_time_credit",
    "pending_ignore",
    "confirm",
    "none",
]
_INCOME_KINDS = {"income_change", "income_end", "new_income", "one_time_credit"}

# A confirmed event this many days either side of a projected occurrence of the
# same category and direction is taken to BE that occurrence (D19).
DEDUPE_WINDOW_DAYS = 3


def _evnum(s: Optional[str]) -> int:
    digits = "".join(ch for ch in (s or "") if ch.isdigit())
    return int(digits) if digits else 0


def _signed(magnitude: float, direction: str) -> float:
    return abs(magnitude) if direction == "credit" else -abs(magnitude)


def _match_flows(flows: list[CashFlow], streams: Sequence[RecurringStream], am) -> list[CashFlow]:
    """Flows targeted by an amendment: target_event_id first (a scheduled flow
    with that source_id, or every flow of the stream that contains it), then
    target_category (with 'salary' assumed for income kinds)."""
    if am.target_event_id:
        sids = {
            s.stream_id
            for s in streams
            if am.target_event_id in (s.event_ids or ()) or s.anchor_event_id == am.target_event_id
        }
        sel = [f for f in flows if f.source_id == am.target_event_id or (f.stream_id and f.stream_id in sids)]
        if sel:
            return sel
    cat = am.target_category or ("salary" if am.kind in _INCOME_KINDS else None)
    if cat:
        sids = {s.stream_id for s in streams if s.category == cat}
        return [f for f in flows if (f.stream_id in sids if f.stream_id else f.category == cat)]
    return []


def _gate(sel: list[CashFlow], am) -> list[CashFlow]:
    """`effective_from` gates which occurrences change."""
    if am.effective_from is None:
        return sel
    return [f for f in sel if f.flow_date >= am.effective_from]


def _amendment_amount(am, profile: Profile, rates, on: date) -> Optional[float]:
    if am.new_amount is None:
        return None
    ccy = am.currency or profile.home_currency
    return _convert(float(am.new_amount), ccy, profile.home_currency, on, rates)


def build_flows(
    streams: Sequence[RecurringStream],
    scheduled_events: Sequence[Event],
    amendments: Sequence,
    profile: Profile,
    request: Request,
    rates,
    policy: Policy,
) -> tuple[list[CashFlow], list[str]]:
    """Project the forecast horizon as signed CashFlows and apply amendments.

    Returns (flows sorted by (date, source_id), notes)."""
    notes: list[str] = []
    start = request.request_date
    end = start + timedelta(days=policy.horizon_days)
    flows: list[CashFlow] = []

    # --- recurring streams -------------------------------------------------
    for s in streams or ():
        for d in _occurrences(s, start, end):
            flows.append(
                CashFlow(
                    flow_date=d,
                    amount=_signed(s.amount, s.direction),
                    source_id=s.stream_id,
                    kind="recurring",
                    category=s.category,
                    label=s.description,
                    flexibility=s.flexibility,
                    minimum_allowed_amount=s.minimum_allowed_amount,
                    stream_id=s.stream_id,
                )
            )

    # --- confirmed future one-offs ----------------------------------------
    for ev in scheduled_events or ():
        eligible = ev.status in ("pending", "scheduled") or (
            ev.settlement_date is not None and ev.settlement_date >= start
        )
        if not eligible:
            continue
        if ev.direction not in ("debit", "credit"):
            continue
        if ev.amount is None:
            notes.append(f"scheduled event {ev.event_id} skipped: amount unknown")
            continue
        d = ev.settlement_date if (policy.pending_debit_on == "settlement_date" and ev.settlement_date) else ev.event_date
        if d is None:
            d = ev.event_date
        if d < start:
            d = start
        if d > end:
            continue
        flows.append(
            CashFlow(
                flow_date=d,
                amount=_signed(ev.amount, ev.direction),
                source_id=ev.event_id,
                kind="scheduled",
                category=ev.category,
                label=ev.description,
                flexibility=ev.flexibility,
                minimum_allowed_amount=ev.minimum_allowed_amount,
                stream_id=None,
            )
        )

    # --- D19 dedupe: a confirmed event supersedes the projection it duplicates
    # (PS:204 -- a settled/confirmed record beats an estimate or forecast).
    dropped: set[int] = set()
    recurring = [f for f in flows if f.kind == "recurring"]
    for sf in sorted((f for f in flows if f.kind == "scheduled"), key=lambda f: (f.flow_date, f.source_id)):
        best: Optional[tuple[int, CashFlow]] = None
        for rf in recurring:
            if id(rf) in dropped or rf.category != sf.category:
                continue
            if (rf.amount >= 0) != (sf.amount >= 0):      # same direction only
                continue
            # D23: debit projections from short-cadence streams (groceries every
            # 7 days) are never absorbed by a confirmed one-off -- that would be
            # the optimistic direction. Only credits or monthly streams dedupe.
            if rf.amount < 0:
                st = next((s for s in streams if s.stream_id == rf.stream_id), None)
                if st is not None and not st.monthly and st.cadence_days < 28:
                    continue
            gap = abs((rf.flow_date - sf.flow_date).days)
            if gap <= DEDUPE_WINDOW_DAYS and (best is None or gap < best[0]):
                best = (gap, rf)
        if best is not None:
            dropped.add(id(best[1]))
            notes.append(
                f"dedupe: dropped projected {best[1].category} occurrence on {best[1].flow_date} "
                f"superseded by confirmed event {sf.source_id} on {sf.flow_date}"
            )
    if dropped:
        flows = [f for f in flows if id(f) not in dropped]

    # --- amendments, in the contract order ---------------------------------
    ordered = sorted(
        enumerate(amendments or ()),
        key=lambda t: (
            _KIND_ORDER.index(t[1].kind) if t[1].kind in _KIND_ORDER else len(_KIND_ORDER),
            t[0],
        ),
    )
    for _, am in ordered:
        try:
            flows, note = _apply_amendment(flows, streams, am, profile, request, rates, policy, start, end)
        except ValueError as exc:  # unroutable fx etc. -> fail closed, keep forecast unchanged
            note = f"{am.kind} from {am.source_id} skipped: {exc}"
        if note:
            notes.append(note)

    flows.sort(key=lambda f: (f.flow_date, f.source_id, f.amount))
    return flows, notes


def _apply_amendment(
    flows: list[CashFlow],
    streams: Sequence[RecurringStream],
    am,
    profile: Profile,
    request: Request,
    rates,
    policy: Policy,
    start: date,
    end: date,
) -> tuple[list[CashFlow], str]:
    kind = am.kind
    if kind in ("confirm", "none"):
        return flows, f"{kind} from {am.source_id}: no forecast change"
    if kind not in _KIND_ORDER:
        return flows, f"unknown amendment kind {kind!r} from {am.source_id}: ignored"

    sel = _gate(_match_flows(flows, streams, am), am)

    if kind == "cancel":
        if not sel:
            return flows, f"cancel from {am.source_id}: no matching flow"
        drop = {id(f) for f in sel}
        return [f for f in flows if id(f) not in drop], (
            f"cancel from {am.source_id}: removed {len(drop)} flow(s) ({sel[0].label})"
        )

    if kind == "delay":
        if not sel or am.new_date is None:
            return flows, f"delay from {am.source_id}: no matching flow or no new_date"
        drop: set[int] = set()

        # A one-off named by event_id moves on its own.
        direct = [f for f in sel if f.stream_id is None and f.source_id == am.target_event_id]
        if direct:
            for f in direct:
                if am.new_date > end:
                    drop.add(id(f))
                else:
                    f.flow_date = max(am.new_date, start)
            if drop:
                flows = [f for f in flows if id(f) not in drop]
            return flows, f"delay from {am.source_id}: moved {len(direct)} flow(s) to {am.new_date}"

        # D21: delaying a recurring stream RE-ANCHORS it -- the next occurrence
        # moves to new_date and the rest follow monthly from that day-of-month.
        stream_flows = [f for f in sel if f.stream_id]
        if not stream_flows:
            return flows, f"delay from {am.source_id}: no matching flow"
        sids = sorted({f.stream_id for f in stream_flows})
        for sid in sids:
            grp = sorted((f for f in stream_flows if f.stream_id == sid), key=lambda f: f.flow_date)
            for k, f in enumerate(grp):
                nd = _add_months(am.new_date, k)
                if nd > end:
                    drop.add(id(f))
                else:
                    f.flow_date = max(nd, start)
        if drop:
            flows = [f for f in flows if id(f) not in drop]
        return flows, (
            f"delay from {am.source_id}: re-anchored {', '.join(sids)} to {am.new_date}"
        )

    if kind == "amend_amount":
        newv = _amendment_amount(am, profile, rates, am.effective_from or request.request_date)
        if not sel or newv is None:
            return flows, f"amend_amount from {am.source_id}: no matching flow or no amount"
        for f in sel:
            f.amount = abs(newv) if f.amount > 0 else -abs(newv)
        return flows, f"amend_amount from {am.source_id}: {len(sel)} flow(s) -> {abs(newv):.2f}"

    if kind == "amend_amount_pct":
        if not sel or am.pct_change is None:
            return flows, f"amend_amount_pct from {am.source_id}: no matching flow or no pct"
        factor = 1.0 + float(am.pct_change)
        for f in sel:
            f.amount = f.amount * factor
        return flows, f"amend_amount_pct from {am.source_id}: {len(sel)} flow(s) x {factor:.4f}"

    if kind == "income_change":
        newv = _amendment_amount(am, profile, rates, am.effective_from or request.request_date)
        credits = [f for f in sel if f.amount > 0]
        if credits and newv is not None:
            for f in credits:
                f.amount = abs(newv)
            return flows, f"income_change from {am.source_id}: {len(credits)} credit(s) -> {abs(newv):.2f}"
        # D13: a confirmed first salary. There is no stream to amend (no salary
        # history), so the income is created rather than dropped.
        if newv is not None and am.effective_from is not None:
            created = 0
            for d in _monthly_from(am.effective_from, start, end):
                flows.append(
                    CashFlow(
                        flow_date=d,
                        amount=abs(newv),
                        source_id=am.source_id,
                        kind="message",
                        category=am.target_category or "salary",
                        label=am.note or "confirmed salary",
                        flexibility="fixed",
                        minimum_allowed_amount=None,
                        stream_id=None,
                    )
                )
                created += 1
            if created:
                return flows, f"income_change created salary stream from {am.source_id}"
            return flows, (
                f"income_change from {am.source_id}: created salary starts {am.effective_from}, "
                f"outside the horizon"
            )
        return flows, f"income_change from {am.source_id}: no matching income flow or no amount"

    if kind == "income_end":
        credits = [f for f in sel if f.amount > 0]
        if not credits:
            return flows, f"income_end from {am.source_id}: no matching income flow"
        drop = {id(f) for f in credits}
        return [f for f in flows if id(f) not in drop], (
            f"income_end from {am.source_id}: removed {len(drop)} income flow(s)"
        )

    if kind == "new_income":
        when = am.new_date or am.effective_from or request.request_date
        newv = _amendment_amount(am, profile, rates, when)
        if newv is None:
            return flows, f"new_income from {am.source_id}: no amount"
        if not (start <= when <= end):
            return flows, f"new_income from {am.source_id}: {when} outside horizon, ignored"
        flows.append(
            CashFlow(
                flow_date=when,
                amount=abs(newv),
                source_id=am.source_id,
                kind="message",
                category=am.target_category or "salary",
                label=am.note or "confirmed income",
                flexibility="fixed",
                minimum_allowed_amount=None,
                stream_id=None,
            )
        )
        return flows, f"new_income from {am.source_id}: +{abs(newv):.2f} on {when}"

    if kind == "one_time_credit":
        newv = _amendment_amount(am, profile, rates, am.new_date or request.request_date)
        if newv is None:
            return flows, f"one_time_credit from {am.source_id}: no amount"
        when = am.new_date
        if when is None:
            floor_date = am.effective_from or request.request_date
            salary_dates = sorted(
                f.flow_date
                for f in flows
                if f.amount > 0 and f.flow_date >= floor_date
            )
            when = salary_dates[0] if salary_dates else floor_date
        if not (start <= when <= end):
            return flows, f"one_time_credit from {am.source_id}: {when} outside horizon, ignored"
        flows.append(
            CashFlow(
                flow_date=when,
                amount=abs(newv),
                source_id=am.source_id,
                kind="message",
                category=am.target_category or "salary",
                label=am.note or "one-off credit",
                flexibility="fixed",
                minimum_allowed_amount=None,
                stream_id=None,
            )
        )
        return flows, f"one_time_credit from {am.source_id}: +{abs(newv):.2f} on {when}"

    if kind == "pending_ignore":
        # D24: "this money is not yet available" can only ever describe a
        # one-off/scheduled credit. It must NEVER delete a confirmed recurring
        # payroll stream, or confirmed income silently becomes zero.
        if am.target_event_id:
            targets = [f for f in sel if f.source_id == am.target_event_id and f.kind != "recurring"]
        else:
            targets = [f for f in sel if f.kind != "recurring"]
        credits = [f for f in targets if f.amount > 0]
        if not credits:
            return flows, (
                f"pending_ignore from {am.source_id}: nothing to remove (confirmed stream untouched)"
            )
        drop = {id(f) for f in credits}
        return [f for f in flows if id(f) not in drop], (
            f"pending_ignore from {am.source_id}: removed {len(drop)} unconfirmed credit(s)"
        )

    return flows, ""


# ---------------------------------------------------------------------------
# Balance path
# ---------------------------------------------------------------------------


def balance_path(
    profile: Profile,
    flows: Iterable[CashFlow],
    start: date,
    horizon_days: int,
    extra_payments: Sequence[tuple[date, float]] = (),
) -> list[tuple[date, float]]:
    """Day-by-day closing balance for ``start .. start + horizon_days`` inclusive.

    Day 0 already applies flows dated `start` (policy.include_request_day).
    Flows outside the window are ignored; `extra_payments` are always debits."""
    end = start + timedelta(days=horizon_days)
    delta: dict[date, float] = {}
    for f in flows or ():
        if f.flow_date < start or f.flow_date > end:
            continue
        delta[f.flow_date] = delta.get(f.flow_date, 0.0) + f.amount
    for d, a in (extra_payments or ()):
        if d < start or d > end:
            continue
        delta[d] = delta.get(d, 0.0) - abs(float(a))

    bal = float(profile.current_available_balance)
    out: list[tuple[date, float]] = []
    for i in range(horizon_days + 1):
        d = start + timedelta(days=i)
        bal += delta.get(d, 0.0)
        out.append((d, bal))
    return out
