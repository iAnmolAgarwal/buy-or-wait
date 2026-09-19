"""Schema validation of output rows, including every labelled sample request."""
from __future__ import annotations

import csv
import pathlib
import sys
from datetime import datetime

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "fixtures" / "validate"))

import builders  # noqa: E402
from buyorwait.output import decision_to_row  # noqa: E402
from buyorwait.types import OUTPUT_COLUMNS, Request  # noqa: E402
from buyorwait.validate.schema import validate_row  # noqa: E402

from conftest import requires_dataset  # noqa: E402

SAMPLE_CSV = ROOT / "dataset" / "sample_requests.csv"


def valid_row():
    return decision_to_row(builders.decision_installments(), builders.make_request())


def test_a_well_formed_row_has_no_problems():
    assert validate_row(valid_row(), builders.make_request()) == []


def test_wrong_column_order_is_caught():
    row = valid_row()
    reordered = {k: row[k] for k in reversed(OUTPUT_COLUMNS)}
    ps = validate_row(reordered, builders.make_request())
    assert any("wrong order" in p for p in ps), ps


def test_missing_column_is_caught():
    row = valid_row()
    del row["spending_changes_needed"]
    ps = validate_row(row, builders.make_request())
    assert any("missing" in p for p in ps), ps


def test_request_id_must_echo_the_request():
    row = valid_row()
    row["request_id"] = "request_001"
    ps = validate_row(row, builders.make_request())
    assert any("does not echo" in p for p in ps), ps


def test_amount_safe_to_pay_must_be_numeric():
    row = valid_row()
    row["amount_safe_to_pay"] = "n/a"
    ps = validate_row(row, builders.make_request())
    assert any("is not a plain number" in p for p in ps), ps


def test_amount_safe_to_pay_must_not_exceed_requested_amount():
    row = valid_row()
    row["amount_safe_to_pay"] = "2000"
    ps = validate_row(row, builders.make_request())
    assert any("exceeds requested_amount" in p for p in ps), ps


def test_unknown_status_and_method_are_caught():
    row = valid_row()
    row["affordability_status"] = "maybe"
    row["recommended_payment_method"] = "barter"
    ps = validate_row(row, builders.make_request())
    assert any("affordability_status" in p for p in ps), ps
    assert any("recommended_payment_method" in p for p in ps), ps


@pytest.mark.parametrize(
    "plan,needle",
    [
        ("2026-01-05", "is not '<YYYY-MM-DD>:<amount>'"),
        ("2026-13-45:100", "bad date"),
        ("2026-01-05:abc", "bad amount"),
        ("2026-01-05:100.123", "bad amount"),
        ("2026-01-05:0", "amount must be > 0"),
        ("2026-02-05:100|2026-01-05:100", "not in chronological order"),
        ("", "empty"),
    ],
)
def test_payment_plan_grammar(plan, needle):
    row = valid_row()
    row["payment_plan"] = plan
    ps = validate_row(row, builders.make_request())
    assert any(needle in p for p in ps), (plan, ps)


def test_payment_plan_none_is_accepted():
    row = decision_to_row(builders.decision_not_recommended(), builders.make_request())
    assert row["payment_plan"] == "none"
    assert validate_row(row, builders.make_request()) == []


def test_earliest_date_must_be_iso_or_empty():
    row = valid_row()
    row["earliest_date_for_full_payment"] = "10 Feb 2026"
    ps = validate_row(row, builders.make_request())
    assert any("not YYYY-MM-DD or empty" in p for p in ps), ps


@pytest.mark.parametrize(
    "changes,needle",
    [
        ("pause:event_12", "unknown kind"),
        ("stop:event_12:5", "is not 'stop:<event_id>'"),
        ("reduce_to:event_12", "is not 'reduce_to:<event_id>:<amount>'"),
        ("reduce_to:event_12:abc", "bad amount"),
        ("stop:event_1|stop:event_2|stop:event_3|stop:event_4", "at most 3 allowed"),
        ("stop:event_12|reduce_to:event_12:50", "more than one change"),
        ("", "empty"),
    ],
)
def test_spending_changes_grammar(changes, needle):
    row = valid_row()
    row["spending_changes_needed"] = changes
    ps = validate_row(row, builders.make_request())
    assert any(needle in p for p in ps), (changes, ps)


@pytest.mark.parametrize(
    "explanation,needle",
    [
        ("", "must not be empty"),
        ("Pay INR 350\nand wait.", "must not contain a newline"),
        ("x" * 401, "at most 400 allowed"),
    ],
)
def test_explanation_rules(explanation, needle):
    row = valid_row()
    row["decision_explanation"] = explanation
    ps = validate_row(row, builders.make_request())
    assert any(needle in p for p in ps), (explanation[:20], ps)


# ---------------------------------------------------------------------------
# The official examples must pass our grammar unchanged.
# ---------------------------------------------------------------------------

def _sample_rows():
    # Called at COLLECTION time by the parametrize below, so it must not
    # raise when dataset/ is absent - the tests carry @requires_dataset.
    if not SAMPLE_CSV.is_file():
        return []
    with open(SAMPLE_CSV, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _request_from_sample(raw: dict) -> Request:
    return Request(
        request_id=raw["request_id"],
        user_id=raw["user_id"],
        request_date=datetime.strptime(raw["request_date"], "%Y-%m-%d").date(),
        request_type=raw["request_type"],
        requested_amount=float(raw["requested_amount"]),
        desired_completion_date=datetime.strptime(
            raw["desired_completion_date"], "%Y-%m-%d"
        ).date(),
        allows_partial_payment=raw["allows_partial_payment"].strip().lower() == "true",
        request_text=raw["request_text"],
    )


@requires_dataset
def test_sample_csv_has_all_25_labelled_rows():
    assert len(_sample_rows()) == 25


@requires_dataset
@pytest.mark.parametrize("raw", _sample_rows(), ids=lambda r: r["request_id"])
def test_every_sample_request_row_passes_schema_validation(raw):
    row = {c: raw[c] for c in OUTPUT_COLUMNS}
    assert validate_row(row, _request_from_sample(raw)) == []
