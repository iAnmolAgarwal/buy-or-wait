"""Engine policy knobs (docs/CONTRACT.md Section 2, engine/policy.py).

Every tunable interpretation lives here so the rest of the engine has no magic
numbers. Defaults are the contract defaults.
"""
from __future__ import annotations

from dataclasses import dataclass

# Float noise tolerance for the minimum-balance comparison. Balances are money
# amounts built by summing floats; without this a path that is mathematically
# exactly at the minimum can read as 1e-12 below it.
EPS = 1e-9


@dataclass
class Policy:
    amount_estimator: str = "mean"          # mean | median | last | max
    min_occurrences: int = 3
    horizon_days: int = 90
    include_request_day: bool = True        # day 0 of the path applies flows dated request_date
    deadline_is_hard: bool = True           # plans finishing after desired_completion_date are ineligible
    installments_cap_by_months: bool = True  # number_of_payments <= max_installment_months
    pending_debit_on: str = "settlement_date"  # settlement_date | event_date
    strict_minimum: bool = False            # False -> `balance >= minimum` passes (G7)
    # D35 (refines D34, supersedes D25): how the earliest_date_for_full_payment
    # check treats the end of the horizon. "tail" checks all 90 days but ignores
    # *projected* stream occurrences in the last `tail_days` -- far-out
    # projections are the least reliable part of the forecast. Confirmed facts
    # (scheduled events, message- and image-derived flows) are honoured on every
    # day. "deadline" is the old D25 rule, "full" trims nothing.
    earliest_window: str = "tail"           # tail | deadline | full
    tail_days: int = 4                      # projected-only blind spot at the end

    # ------------------------------------------------------------------
    def meets_minimum(self, balance: float, minimum: float) -> bool:
        """The single place the 90-day safety comparison is made."""
        if self.strict_minimum:
            return balance > minimum + EPS
        return balance >= minimum - EPS
