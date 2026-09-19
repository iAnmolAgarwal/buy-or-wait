"""ingest/lifecycle.py — exclusions and linked-event lifecycles (PS:178, PS:200-205)."""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from buyorwait.ingest.lifecycle import exclusion_reason, resolve_lifecycles  # noqa: E402
from buyorwait.ingest.loader import load_dataset  # noqa: E402

FIXTURE = os.path.join(ROOT, "fixtures", "ingest")


@pytest.fixture(scope="module")
def resolved():
    ds = load_dataset(FIXTURE)
    events = ds.events_by_user["user_a"]
    eligible, notes = resolve_lifecycles(events)
    return ds, {e.event_id for e in eligible}, notes


@pytest.mark.parametrize(
    "event_id,reason",
    [
        ("ev_60", "cancelled"),            # cancelled authorization
        ("ev_61", "failed"),               # failed bill payment
        ("ev_63", "unrealized"),           # unrealized non_cash valuation
        ("ev_66", "pending_credit"),       # pending refund credit
    ],
)
def test_exclusion_reason_per_category(event_id, reason, resolved):
    ds, _, _ = resolved
    assert exclusion_reason(ds.events_by_id[event_id]) == reason


def test_each_ps178_category_is_excluded(resolved):
    _, kept, _ = resolved
    for event_id in ("ev_60", "ev_61", "ev_63", "ev_66"):
        assert event_id not in kept


def test_settled_and_pending_debits_are_kept(resolved):
    _, kept, _ = resolved
    assert "ev_69" in kept    # pending DEBIT is kept (safer)
    assert "ev_62" in kept    # retry of the failed payment replaces it
    assert "ev_01" in kept


def test_retry_replaces_its_failed_target(resolved):
    _, kept, notes = resolved
    assert "ev_61" not in kept and "ev_62" in kept
    assert any("ev_62" in n and "replaces ev_61" in n for n in notes)


def test_linked_duplicate_charge_is_dropped(resolved):
    _, kept, notes = resolved
    assert "ev_68" not in kept        # "Possible duplicate card charge"
    assert "ev_67" in kept            # the original survives
    assert any("ev_68" in n and "duplicate of ev_67" in n for n in notes)


def test_exact_duplicate_rows_keep_the_first_event_id(resolved):
    _, kept, notes = resolved
    assert "ev_80" in kept and "ev_81" not in kept
    assert any("ev_81" in n and "duplicate row of ev_80" in n for n in notes)


def test_settled_refund_of_a_settled_debit_keeps_both(resolved):
    _, kept, _ = resolved
    assert "ev_71" in kept and "ev_72" in kept


def test_notes_are_returned_and_nothing_raises(resolved):
    _, _, notes = resolved
    assert notes and all(isinstance(n, str) for n in notes)


def test_empty_input():
    kept, notes = resolve_lifecycles([])
    assert kept == [] and notes == []
