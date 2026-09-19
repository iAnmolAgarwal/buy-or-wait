"""Dataset loading with fail-closed row validation (ingest/loader.py).

`load_dataset(root)` never raises on bad data: every unparseable or invalid row is
skipped and described in `Dataset.load_errors`.
"""
from __future__ import annotations

import csv
import os
import re
from datetime import date, datetime
from typing import Iterable, Optional

from buyorwait.types import (
    Dataset,
    Event,
    ImageRef,
    Message,
    PaymentOption,
    Profile,
    RateTable,
    Request,
)
from buyorwait.ingest import fx

VALID_STATUS = {"settled", "pending", "scheduled", "cancelled", "failed", "unrealized"}
VALID_DIRECTION = {"debit", "credit", "non_cash"}
VALID_FLEXIBILITY = {"fixed", "reducible", "stoppable", "reducible_or_stoppable"}
VALID_METHODS = {"full_payment", "partial_payment", "installments"}

_NUM_RE = re.compile(r"(\d+)")


def _numeric_id(s: str) -> tuple[int, str]:
    """Sort key: trailing integer of an id like 'event_1042', then the raw string."""
    m = _NUM_RE.findall(s or "")
    return (int(m[-1]) if m else -1, s or "")


def _parse_date(s: str, field: str) -> date:
    s = (s or "").strip()
    if not s:
        raise ValueError(f"{field} is blank")
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field}={s!r} is not YYYY-MM-DD") from exc


def _parse_opt_date(s: str, field: str) -> Optional[date]:
    return _parse_date(s, field) if (s or "").strip() else None


def _parse_float(s: str, field: str) -> float:
    s = (s or "").strip().replace(",", "")
    if not s:
        raise ValueError(f"{field} is blank")
    try:
        return float(s)
    except ValueError as exc:
        raise ValueError(f"{field}={s!r} is not a number") from exc


def _parse_opt_float(s: str, field: str) -> Optional[float]:
    return _parse_float(s, field) if (s or "").strip() else None


def _parse_opt_int(s: str, field: str) -> Optional[int]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError as exc:
        raise ValueError(f"{field}={s!r} is not an integer") from exc


def _parse_bool(s: str, field: str) -> bool:
    v = (s or "").strip().lower()
    if v in ("true", "1", "yes"):
        return True
    if v in ("false", "0", "no"):
        return False
    raise ValueError(f"{field}={s!r} is not true/false")


def _split_list(s: str) -> list[str]:
    return [p.strip() for p in (s or "").split("|") if p.strip()]


def _rows(path: str, errors: list[str]) -> Iterable[tuple[int, dict]]:
    """Yield (line_number, row) for a CSV, logging a note when the file is missing."""
    if not os.path.exists(path):
        errors.append(f"{os.path.basename(path)}: file not found; skipped")
        return
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for i, row in enumerate(reader, start=2):
            yield i, row


def _require(row: dict, *fields: str) -> None:
    for f in fields:
        if not (row.get(f) or "").strip():
            raise ValueError(f"{f} is blank")


def load_dataset(root: str) -> Dataset:
    """Load every CSV under `root`. Malformed rows are skipped and logged."""
    root = os.path.abspath(os.path.expanduser(root))
    errors: list[str] = []

    # ---- exchange rates ----------------------------------------------------
    rate_rows: list[tuple[date, str, str, float]] = []
    for ln, row in _rows(os.path.join(root, "exchange_rates.csv"), errors):
        try:
            _require(row, "from_currency", "to_currency")
            rate_rows.append(
                (
                    _parse_date(row.get("rate_date"), "rate_date"),
                    row["from_currency"].strip().upper(),
                    row["to_currency"].strip().upper(),
                    _parse_float(row.get("rate"), "rate"),
                )
            )
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"exchange_rates.csv:{ln}: skipped ({exc})")
    rates = RateTable(rows=rate_rows)

    # ---- profiles ----------------------------------------------------------
    profiles: dict[str, Profile] = {}
    for ln, row in _rows(os.path.join(root, "financial_profiles.csv"), errors):
        try:
            _require(row, "user_id", "home_currency")
            methods = set(_split_list(row.get("payment_methods_user_will_consider", "")))
            unknown = methods - VALID_METHODS
            if unknown:
                raise ValueError(f"unknown payment methods {sorted(unknown)}")
            p = Profile(
                user_id=row["user_id"].strip(),
                home_currency=row["home_currency"].strip().upper(),
                current_available_balance=_parse_float(
                    row.get("current_available_balance"), "current_available_balance"
                ),
                minimum_balance_to_keep=_parse_float(
                    row.get("minimum_balance_to_keep"), "minimum_balance_to_keep"
                ),
                financial_priorities=_split_list(row.get("financial_priorities", "")),
                protect_categories=_split_list(row.get("expense_categories_to_protect", "")),
                reduce_categories=_split_list(
                    row.get("expense_categories_user_is_willing_to_reduce", "")
                ),
                stop_categories=_split_list(
                    row.get("expense_categories_user_is_willing_to_stop", "")
                ),
                methods=methods,
                max_installment_months=_parse_opt_int(
                    row.get("max_installment_months"), "max_installment_months"
                ),
            )
            if p.user_id in profiles:
                raise ValueError(f"duplicate profile for {p.user_id}")
            profiles[p.user_id] = p
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"financial_profiles.csv:{ln}: skipped ({exc})")

    # ---- requests ----------------------------------------------------------
    requests: dict[str, Request] = {}
    for ln, row in _rows(os.path.join(root, "requests.csv"), errors):
        try:
            _require(row, "request_id", "user_id")
            r = Request(
                request_id=row["request_id"].strip(),
                user_id=row["user_id"].strip(),
                request_date=_parse_date(row.get("request_date"), "request_date"),
                request_type=(row.get("request_type") or "").strip(),
                requested_amount=_parse_float(row.get("requested_amount"), "requested_amount"),
                desired_completion_date=_parse_date(
                    row.get("desired_completion_date"), "desired_completion_date"
                ),
                allows_partial_payment=_parse_bool(
                    row.get("allows_partial_payment"), "allows_partial_payment"
                ),
                request_text=(row.get("request_text") or "").strip(),
            )
            requests[r.request_id] = r
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"requests.csv:{ln}: skipped ({exc})")

    # ---- events ------------------------------------------------------------
    events_by_id: dict[str, Event] = {}
    for ln, row in _rows(os.path.join(root, "financial_events.csv"), errors):
        try:
            _require(row, "event_id", "user_id", "currency")
            direction = (row.get("direction") or "").strip()
            status = (row.get("status") or "").strip()
            flexibility = (row.get("flexibility") or "").strip() or "fixed"
            if direction not in VALID_DIRECTION:
                raise ValueError(f"direction={direction!r} not allowed")
            if status not in VALID_STATUS:
                raise ValueError(f"status={status!r} not allowed")
            if flexibility not in VALID_FLEXIBILITY:
                raise ValueError(f"flexibility={flexibility!r} not allowed")

            event_date = _parse_date(row.get("event_date"), "event_date")
            amount_raw = _parse_opt_float(row.get("amount"), "amount")
            currency = row["currency"].strip().upper()
            user_id = row["user_id"].strip()

            amount: Optional[float]
            amount_source = "csv"
            if amount_raw is None:
                amount = None
                amount_source = "missing"
            else:
                home = profiles[user_id].home_currency if user_id in profiles else None
                if home is None:
                    amount = amount_raw
                    errors.append(
                        f"financial_events.csv:{ln}: no profile for {user_id}; "
                        f"amount left in {currency}"
                    )
                elif home == currency:
                    amount = amount_raw
                else:
                    rate, used = fx.rate_lookup(currency, home, event_date, rates)
                    amount = round(amount_raw * rate, 2)
                    if used > event_date:
                        errors.append(
                            f"financial_events.csv:{ln}: no {currency}->{home} rate on or "
                            f"before {event_date}; used earliest ({used})"
                        )

            ev = Event(
                event_id=row["event_id"].strip(),
                user_id=user_id,
                event_type=(row.get("event_type") or "").strip(),
                description=(row.get("description") or "").strip(),
                category=(row.get("category") or "").strip(),
                direction=direction,
                amount=amount,
                amount_raw=amount_raw,
                currency=currency,
                event_date=event_date,
                settlement_date=_parse_opt_date(row.get("settlement_date"), "settlement_date"),
                status=status,
                linked_event_id=(row.get("linked_event_id") or "").strip() or None,
                flexibility=flexibility,
                minimum_allowed_amount=_parse_opt_float(
                    row.get("minimum_allowed_amount"), "minimum_allowed_amount"
                ),
                amount_source=amount_source,
            )
            if ev.event_id in events_by_id:
                raise ValueError(f"duplicate event_id {ev.event_id}")
            events_by_id[ev.event_id] = ev
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"financial_events.csv:{ln}: skipped ({exc})")

    events_by_user: dict[str, list[Event]] = {}
    for ev in events_by_id.values():
        events_by_user.setdefault(ev.user_id, []).append(ev)
    for evs in events_by_user.values():
        evs.sort(key=lambda e: (e.event_date, _numeric_id(e.event_id)))

    # ---- payment options ---------------------------------------------------
    options_by_request: dict[str, list[PaymentOption]] = {}
    for ln, row in _rows(os.path.join(root, "request_payment_options.csv"), errors):
        try:
            _require(row, "payment_option_id", "request_id", "payment_method")
            n = _parse_opt_int(row.get("number_of_payments"), "number_of_payments")
            if n is None or n < 1:
                raise ValueError("number_of_payments must be >= 1")
            opt = PaymentOption(
                payment_option_id=row["payment_option_id"].strip(),
                request_id=row["request_id"].strip(),
                payment_method=row["payment_method"].strip(),
                payment_amount=_parse_float(row.get("payment_amount"), "payment_amount"),
                number_of_payments=n,
                first_payment_date=_parse_date(
                    row.get("first_payment_date"), "first_payment_date"
                ),
                payment_frequency_days=_parse_opt_int(
                    row.get("payment_frequency_days"), "payment_frequency_days"
                ),
                financing_fee=_parse_opt_float(row.get("financing_fee"), "financing_fee") or 0.0,
                total_payable_amount=_parse_float(
                    row.get("total_payable_amount"), "total_payable_amount"
                ),
            )
            options_by_request.setdefault(opt.request_id, []).append(opt)
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"request_payment_options.csv:{ln}: skipped ({exc})")
    for opts in options_by_request.values():
        opts.sort(key=lambda o: _numeric_id(o.payment_option_id))

    # ---- messages ----------------------------------------------------------
    messages_by_user: dict[str, list[Message]] = {}
    for ln, row in _rows(os.path.join(root, "messages.csv"), errors):
        try:
            _require(row, "message_id", "user_id", "sent_at")
            m = Message(
                message_id=row["message_id"].strip(),
                user_id=row["user_id"].strip(),
                request_id=(row.get("request_id") or "").strip() or None,
                related_event_id=(row.get("related_event_id") or "").strip() or None,
                sent_at=_parse_date(row["sent_at"], "sent_at"),  # ISO datetime -> date part
                source_type=(row.get("source_type") or "").strip(),
                message_text=(row.get("message_text") or "").strip(),
            )
            messages_by_user.setdefault(m.user_id, []).append(m)
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"messages.csv:{ln}: skipped ({exc})")
    for msgs in messages_by_user.values():
        msgs.sort(key=lambda m: (m.sent_at, _numeric_id(m.message_id)))

    # ---- images ------------------------------------------------------------
    images_by_user: dict[str, list[ImageRef]] = {}
    media_dir = os.path.join(root, "media", "images")
    for ln, row in _rows(os.path.join(root, "images.csv"), errors):
        try:
            _require(row, "image_id", "user_id")
            image_id = row["image_id"].strip()
            path = os.path.join(media_dir, f"{image_id}.png")
            if not os.path.exists(path):
                errors.append(f"images.csv:{ln}: {path} not found (reference kept)")
            img = ImageRef(
                image_id=image_id,
                user_id=row["user_id"].strip(),
                request_id=(row.get("request_id") or "").strip() or None,
                related_event_id=(row.get("related_event_id") or "").strip() or None,
                path=path,
            )
            images_by_user.setdefault(img.user_id, []).append(img)
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"images.csv:{ln}: skipped ({exc})")
    for imgs in images_by_user.values():
        imgs.sort(key=lambda i: _numeric_id(i.image_id))

    return Dataset(
        requests=requests,
        profiles=profiles,
        events_by_user=events_by_user,
        events_by_id=events_by_id,
        options_by_request=options_by_request,
        messages_by_user=messages_by_user,
        images_by_user=images_by_user,
        rates=rates,
        load_errors=errors,
    )
