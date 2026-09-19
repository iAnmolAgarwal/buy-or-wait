"""The gate must pass a hand-built valid Decision for every payment method."""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "fixtures" / "validate"))

import builders  # noqa: E402
from buyorwait.output import decision_to_row, refusal_row  # noqa: E402
from buyorwait.validate.gate import gate  # noqa: E402


@pytest.mark.parametrize("name", sorted(builders.VALID_DECISIONS))
def test_valid_decision_passes_the_gate(name):
    decision = builders.VALID_DECISIONS[name]()
    request = builders.make_request()
    profile = builders.make_profile()
    row = decision_to_row(decision, request)
    ok, problems = gate(decision, request, profile, decision.evidence, row)
    assert ok, f"{name} should pass the gate but got: {problems}"
    assert problems == []


def test_gate_reports_problems_from_all_three_validators():
    decision = builders.decision_full_payment()
    decision.affordability_status = "affordable_later"       # coherence
    decision.spending_changes_needed = [
        builders.SpendingChange("stop", "event_999", None, "s:x")  # citations
    ]
    request = builders.make_request()
    row = decision_to_row(decision, request)
    row["payment_plan"] = "2026-01-05:0"                     # schema
    ok, problems = gate(decision, request, builders.make_profile(), decision.evidence, row)
    assert not ok
    assert any(p.startswith("schema:") for p in problems)
    assert any(p.startswith("citations:") for p in problems)
    assert any(p.startswith("coherence:") for p in problems)


def test_refusal_row_is_schema_valid():
    from buyorwait.validate.schema import validate_row

    request = builders.make_request()
    row = refusal_row(request, "profile missing for user_900")
    assert validate_row(row, request) == []
    assert row["amount_safe_to_pay"] == "0"
    assert row["affordability_status"] == "not_affordable"
    assert row["recommended_payment_method"] == "not_recommended"
    assert row["payment_plan"] == "none"
    assert row["earliest_date_for_full_payment"] == ""
    assert row["spending_changes_needed"] == "none"
    assert row["decision_explanation"].startswith("Insufficient verified data")
