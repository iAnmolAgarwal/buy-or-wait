"""A Decision may only cite ids that its EvidenceSet contains."""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "fixtures" / "validate"))

import builders  # noqa: E402
from buyorwait.types import SpendingChange  # noqa: E402
from buyorwait.validate.citations import validate_citations  # noqa: E402


def test_valid_decisions_cite_only_evidence():
    for name, factory in builders.VALID_DECISIONS.items():
        d = factory()
        assert validate_citations(d, d.evidence) == [], name


def test_spending_change_on_an_unknown_event_is_caught():
    d = builders.decision_full_payment_with_changes()
    d.spending_changes_needed = [
        SpendingChange("stop", "event_777", None, "user_900:subscription:0")
    ]
    ps = validate_citations(d, d.evidence)
    assert any("event_777" in p and "not in the evidence" in p for p in ps), ps


def test_option_id_outside_the_evidence_is_caught():
    d = builders.decision_installments()
    d.chosen_plan.option_id = "option_77"
    ps = validate_citations(d, d.evidence)
    assert any("option_77" in p and "not in the evidence" in p for p in ps), ps


def test_not_affordable_decision_must_not_carry_spending_changes():
    d = builders.decision_not_recommended()
    d.spending_changes_needed = [
        SpendingChange("stop", "event_12", None, "user_900:subscription:0")
    ]
    ps = validate_citations(d, d.evidence)
    assert any("not_affordable decisions must not request changes" in p for p in ps), ps


def test_event_id_mentioned_in_the_explanation_must_be_in_the_evidence():
    d = builders.decision_wait()
    d.decision_explanation = (
        "Pay INR 1,000 in full on 10 February 2026, after event_4242 settles."
    )
    ps = validate_citations(d, d.evidence)
    assert any("event_4242" in p for p in ps), ps


def test_event_id_mentioned_in_a_note_must_be_in_the_evidence():
    d = builders.decision_wait()
    d.notes = ["ranked wait above installments because event_888 lands first"]
    ps = validate_citations(d, d.evidence)
    assert any("event_888" in p for p in ps), ps


def test_event_id_already_in_the_evidence_may_be_mentioned():
    d = builders.decision_wait()
    d.notes = ["anchor event_105 defines the dining stream"]
    assert validate_citations(d, d.evidence) == []


def test_evidence_built_for_another_request_is_caught():
    d = builders.decision_wait()
    other = builders.make_evidence(request_id="request_901")
    ps = validate_citations(d, other)
    assert any("built for 'request_901'" in p for p in ps), ps
