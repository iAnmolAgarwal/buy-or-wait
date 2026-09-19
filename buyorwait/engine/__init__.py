"""Deterministic decision engine: forecast -> solver -> plans -> decision.

All arithmetic for the output row lives here; the model never produces a number
that reaches the CSV (docs/CONTRACT.md Section 1).
"""
from __future__ import annotations

from buyorwait.engine.policy import Policy
from buyorwait.engine.forecast import balance_path, build_flows
from buyorwait.engine.solver import (
    amount_safe_to_pay,
    check_horizon_for,
    earliest_full_payment_date,
    flows_for_candidate,
    min_balance,
    path_is_safe,
    tail_trim_from,
    trim_tail_recurring,
)
from buyorwait.engine.plans import apply_changes, candidate_changes, enumerate_plans, rank, rank_key
from buyorwait.engine.explain import explain, render_changes
from buyorwait.engine.decide import decide

__all__ = [
    "Policy",
    "build_flows",
    "balance_path",
    "amount_safe_to_pay",
    "earliest_full_payment_date",
    "check_horizon_for",
    "flows_for_candidate",
    "tail_trim_from",
    "trim_tail_recurring",
    "min_balance",
    "path_is_safe",
    "enumerate_plans",
    "candidate_changes",
    "apply_changes",
    "rank",
    "rank_key",
    "explain",
    "render_changes",
    "decide",
]
