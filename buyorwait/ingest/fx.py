"""Dated foreign-exchange conversion (ingest/fx.py).

Contract (docs/CONTRACT.md Section 2):
  convert(amount, from_ccy, to_ccy, on, rates) -> float
  rate_lookup(from_ccy, to_ccy, on, rates) -> (rate, rate_date)

Lookup rule, in order:
  1. same currency                      -> (1.0, `on`)
  2. direct pair, nearest rate_date <= on (else the earliest available row)
  3. inverse pair, same date rule        -> 1 / rate
  4. route through USD (leg 1: from->USD, leg 2: USD->to; each leg direct-or-inverse)
  5. shortest path through any currency  (generalisation of 4; kept so that e.g.
     ZAR->INR would resolve if the table ever grows an intermediate hop)
  6. ValueError - unroutable.

The returned rate_date is the effective "as of" date actually used. For a routed
conversion it is the EARLIER of the two legs' dates, i.e. the staler of the two,
so that callers can detect a stale conversion with `used_date < on`.
No warnings list is threaded through: RateTable has no such field and the
contract forbids adding one, so `loader` compares the returned date with the
event date and writes any staleness note into `Dataset.load_errors`.
"""
from __future__ import annotations

from collections import deque
from datetime import date
from typing import Optional

from buyorwait.types import RateTable

# id(rows) -> (rows_ref, index).  rows_ref is retained so the id cannot be reused.
_INDEX_CACHE: dict[int, tuple[list, dict[tuple[str, str], list[tuple[date, float]]]]] = {}


def _index(rates: RateTable) -> dict[tuple[str, str], list[tuple[date, float]]]:
    """(from, to) -> [(rate_date, rate)] sorted ascending by date."""
    key = id(rates.rows)
    hit = _INDEX_CACHE.get(key)
    if hit is not None and hit[0] is rates.rows:
        return hit[1]
    idx: dict[tuple[str, str], list[tuple[date, float]]] = {}
    for rate_date, f, t, rate in rates.rows:
        idx.setdefault((f.upper(), t.upper()), []).append((rate_date, float(rate)))
    for v in idx.values():
        v.sort(key=lambda p: p[0])
    _INDEX_CACHE[key] = (rates.rows, idx)
    return idx


def _pick(series: list[tuple[date, float]], on: date) -> tuple[float, date]:
    """Nearest rate_date <= on; else the earliest available row."""
    best: Optional[tuple[date, float]] = None
    for d, r in series:
        if d <= on:
            best = (d, r)
        else:
            break
    if best is None:
        best = series[0]
    return best[1], best[0]


def _direct_or_inverse(
    f: str, t: str, on: date, idx: dict[tuple[str, str], list[tuple[date, float]]]
) -> Optional[tuple[float, date]]:
    series = idx.get((f, t))
    if series:
        return _pick(series, on)
    series = idx.get((t, f))
    if series:
        rate, d = _pick(series, on)
        if rate == 0:
            return None
        return 1.0 / rate, d
    return None


def _neighbours(f: str, idx) -> set[str]:
    out = set()
    for (a, b) in idx:
        if a == f:
            out.add(b)
        elif b == f:
            out.add(a)
    return out


def rate_lookup(
    from_ccy: str, to_ccy: str, on: date, rates: RateTable
) -> tuple[float, date]:
    """Return (rate, effective rate_date) to convert 1 `from_ccy` into `to_ccy`."""
    f = (from_ccy or "").strip().upper()
    t = (to_ccy or "").strip().upper()
    if not f or not t:
        raise ValueError(f"fx: blank currency code ({from_ccy!r} -> {to_ccy!r})")
    if f == t:
        return 1.0, on

    idx = _index(rates)

    hit = _direct_or_inverse(f, t, on, idx)
    if hit is not None:
        return hit

    # Route through USD explicitly (contract Section 2).
    if f != "USD" and t != "USD":
        leg1 = _direct_or_inverse(f, "USD", on, idx)
        leg2 = _direct_or_inverse("USD", t, on, idx)
        if leg1 is not None and leg2 is not None:
            return leg1[0] * leg2[0], min(leg1[1], leg2[1])

    # Generalised routing: shortest currency path using direct/inverse edges.
    prev: dict[str, str] = {f: ""}
    queue = deque([f])
    while queue:
        cur = queue.popleft()
        if cur == t:
            break
        for nxt in sorted(_neighbours(cur, idx)):
            if nxt not in prev:
                prev[nxt] = cur
                queue.append(nxt)
    if t in prev:
        path = [t]
        while prev[path[-1]]:
            path.append(prev[path[-1]])
        path.reverse()
        rate = 1.0
        used = on
        for a, b in zip(path, path[1:]):
            leg = _direct_or_inverse(a, b, on, idx)
            if leg is None:  # pragma: no cover - BFS only walks existing edges
                raise ValueError(f"fx: no rate for {a}->{b} on {on}")
            rate *= leg[0]
            used = min(used, leg[1])
        return rate, used

    raise ValueError(f"fx: no route from {f} to {t} on {on.isoformat()}")


def convert(
    amount: float, from_ccy: str, to_ccy: str, on: date, rates: RateTable
) -> float:
    """Convert `amount` from `from_ccy` to `to_ccy` using the rate effective on `on`."""
    if amount is None:
        raise ValueError("fx.convert: amount is None")
    f = (from_ccy or "").strip().upper()
    t = (to_ccy or "").strip().upper()
    if f == t:
        return float(amount)
    rate, _ = rate_lookup(f, t, on, rates)
    return float(amount) * rate
