"""Shared data types for the Buy-or-Wait pipeline.

This file IS the contract. Subagents implement functions that consume and
produce exactly these shapes. Do not add fields without updating
docs/CONTRACT.md. All amounts are floats in the user's home currency unless
the field name ends in `_raw`. All dates are `datetime.date`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Optional

# ----------------------------------------------------------------------------
# Raw / parsed records (ingest output)
# ----------------------------------------------------------------------------

Status = Literal["settled", "pending", "scheduled", "cancelled", "failed", "unrealized"]
Direction = Literal["debit", "credit", "non_cash"]
Flexibility = Literal["fixed", "reducible", "stoppable", "reducible_or_stoppable"]


@dataclass
class Event:
    event_id: str
    user_id: str
    event_type: str            # expense|subscription|income|debt_payment|investment_purchase|refund|investment_valuation|investment_sale
    description: str
    category: str
    direction: str             # Direction
    amount: Optional[float]    # in HOME currency; None when blank in the CSV (must be filled from image)
    amount_raw: Optional[float]  # as written in the CSV, original currency; None when blank
    currency: str              # original currency code
    event_date: date
    settlement_date: Optional[date]
    status: str                # Status
    linked_event_id: Optional[str]
    flexibility: str           # Flexibility
    minimum_allowed_amount: Optional[float]  # home currency
    amount_source: str = "csv"  # "csv" | "image:<image_id>" | "missing"


@dataclass
class Profile:
    user_id: str
    home_currency: str
    current_available_balance: float
    minimum_balance_to_keep: float
    financial_priorities: list[str]
    protect_categories: list[str]
    reduce_categories: list[str]
    stop_categories: list[str]
    methods: set[str]          # subset of {full_payment, partial_payment, installments}
    max_installment_months: Optional[int]


@dataclass
class Request:
    request_id: str
    user_id: str
    request_date: date
    request_type: str
    requested_amount: float
    desired_completion_date: date
    allows_partial_payment: bool
    request_text: str


@dataclass
class PaymentOption:
    payment_option_id: str
    request_id: str
    payment_method: str        # full_payment | installments
    payment_amount: float
    number_of_payments: int
    first_payment_date: date
    payment_frequency_days: Optional[int]
    financing_fee: float
    total_payable_amount: float

    def schedule(self) -> list[tuple[date, float]]:
        """Chronological (date, amount) pairs implied by this option."""
        from datetime import timedelta
        step = self.payment_frequency_days or 0
        return [
            (self.first_payment_date + timedelta(days=i * step), self.payment_amount)
            for i in range(self.number_of_payments)
        ]


@dataclass
class Message:
    message_id: str
    user_id: str
    request_id: Optional[str]
    related_event_id: Optional[str]
    sent_at: date               # date part of sent_at
    source_type: str            # employer|service_provider|financial_service|bank|merchant
    message_text: str


@dataclass
class ImageRef:
    image_id: str
    user_id: str
    request_id: Optional[str]
    related_event_id: Optional[str]
    path: str                   # absolute path to the PNG


@dataclass
class Dataset:
    requests: dict[str, Request]
    profiles: dict[str, Profile]
    events_by_user: dict[str, list[Event]]       # sorted by event_date, then event_id numeric
    events_by_id: dict[str, Event]
    options_by_request: dict[str, list[PaymentOption]]  # sorted by payment_option_id numeric
    messages_by_user: dict[str, list[Message]]   # sorted by sent_at
    images_by_user: dict[str, list[ImageRef]]
    rates: "RateTable"
    load_errors: list[str] = field(default_factory=list)   # fail-closed notes, never raise


@dataclass
class RateTable:
    """Fixed dated rates. Lookup rule is in ingest/fx.py: exact date first, else the
    nearest rate_date on or before `on`, else the earliest available (logged)."""
    rows: list[tuple[date, str, str, float]]   # (rate_date, from_ccy, to_ccy, rate)


# ----------------------------------------------------------------------------
# Perception output (model -> code). The model NEVER produces numbers that go
# straight to output; it produces these structured facts which code applies.
# ----------------------------------------------------------------------------

AmendmentKind = Literal[
    "none",               # informational only, no change to the forecast
    "amend_amount",       # target event/stream amount becomes new_amount from effective_from
    "amend_amount_pct",   # target stream amount scaled by (1 + pct_change) from effective_from
    "cancel",             # target event will not occur (remove from forecast)
    "delay",              # target event moves to new_date
    "confirm",            # target event confirmed as-is (no change)
    "new_income",         # a confirmed one-time credit of new_amount on new_date (currency may differ)
    "income_change",      # recurring salary stream becomes new_amount from effective_from
    "income_end",         # recurring salary stream stops from effective_from
    "one_time_credit",    # extra one-off credit on next salary date (arrears, etc.)
    "pending_ignore",     # explicitly says money is NOT yet available -> exclude related credit
]


@dataclass
class Amendment:
    kind: str                      # AmendmentKind
    source_id: str                 # message_id or image_id that supports it
    target_event_id: Optional[str] = None   # exact event_id when the message names one
    target_category: Optional[str] = None   # e.g. "salary", "rent" when no event id
    new_amount: Optional[float] = None      # in `currency`
    currency: Optional[str] = None
    new_date: Optional[date] = None
    effective_from: Optional[date] = None
    pct_change: Optional[float] = None      # +0.12 for "+12%"
    confidence: float = 1.0
    note: str = ""


@dataclass
class ImageExtraction:
    image_id: str
    amount: Optional[float]        # None if unreadable
    currency: Optional[str]
    doc_date: Optional[date]
    due_date: Optional[date]
    description: str
    confidence: float
    fallback_used: bool = False


@dataclass
class PerceptionResult:
    request_id: str
    amendments: list[Amendment]
    image_extractions: dict[str, ImageExtraction]   # by image_id
    tool_steps: int
    model_calls: int
    input_tokens: int
    output_tokens: int
    cached: bool
    fallbacks: list[str]           # human-readable fallback reasons (empty if none)


# ----------------------------------------------------------------------------
# Derived signals (ingest + engine)
# ----------------------------------------------------------------------------

@dataclass
class RecurringStream:
    stream_id: str                 # f"{user_id}:{category}:{k}"
    category: str
    description: str               # description of the anchor event
    direction: str                 # debit|credit
    cadence_days: int              # e.g. 7, 10, 14, 21, 30/31 (month = 30 means "monthly, same day-of-month")
    monthly: bool                  # True -> repeat on same day-of-month; False -> every cadence_days
    amount: float                  # projected amount per occurrence (home ccy)
    anchor_event_id: str           # most recent settled occurrence (used in spending_changes ids)
    last_date: date
    flexibility: str
    minimum_allowed_amount: Optional[float]
    event_ids: list[str]           # all occurrences that define the stream


@dataclass
class CashFlow:
    flow_date: date
    amount: float                  # signed: +credit, -debit (home ccy)
    source_id: str                 # event_id, stream_id, message_id or image_id
    kind: str                      # "recurring" | "scheduled" | "message" | "image"
    category: str
    label: str
    flexibility: str = "fixed"
    minimum_allowed_amount: Optional[float] = None
    stream_id: Optional[str] = None


@dataclass
class SpendingChange:
    kind: str                      # "stop" | "reduce_to"
    event_id: str                  # anchor_event_id of the stream
    new_amount: Optional[float]    # for reduce_to
    stream_id: str

    def render(self) -> str:
        if self.kind == "stop":
            return f"stop:{self.event_id}"
        from buyorwait.formatting import fmt_amount
        return f"reduce_to:{self.event_id}:{fmt_amount(self.new_amount)}"


@dataclass
class Plan:
    method: str                    # full_payment|partial_payment|installments|wait|not_recommended
    payments: list[tuple[date, float]]
    changes: list[SpendingChange]
    option_id: Optional[str]       # for installments
    total_paid: float
    completes_by_deadline: bool
    min_balance_after: float       # lowest forecast balance with this plan applied

    @property
    def start(self) -> Optional[date]:
        return self.payments[0][0] if self.payments else None


@dataclass
class EvidenceSet:
    request_id: str
    event_ids: list[str]           # all events considered in the forecast (recurring anchors + scheduled)
    stream_ids: list[str]
    message_ids: list[str]
    image_ids: list[str]
    option_ids: list[str]


@dataclass
class Decision:
    request_id: str
    amount_safe_to_pay: float
    affordability_status: str
    recommended_payment_method: str
    payment_plan: list[tuple[date, float]]
    earliest_date_for_full_payment: Optional[date]
    spending_changes_needed: list[SpendingChange]
    decision_explanation: str
    evidence: EvidenceSet
    chosen_plan: Optional[Plan]
    notes: list[str] = field(default_factory=list)   # decision log lines


OUTPUT_COLUMNS = [
    "request_id",
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
]
STATUSES = {"affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"}
METHODS = {"full_payment", "partial_payment", "installments", "wait", "not_recommended"}
HORIZON_DAYS = 90
