"""output.py: Decision -> row formatting, and CSV round-trips."""
from __future__ import annotations

import pathlib
import sys
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "fixtures" / "validate"))

import builders  # noqa: E402
from buyorwait.output import (  # noqa: E402
    decision_to_row,
    read_output_csv,
    refusal_row,
    write_output_csv,
)
from buyorwait.types import OUTPUT_COLUMNS, SpendingChange  # noqa: E402


def test_row_has_exactly_the_output_columns_in_order():
    row = decision_to_row(builders.decision_installments(), builders.make_request())
    assert list(row.keys()) == list(OUTPUT_COLUMNS)
    assert all(isinstance(v, str) for v in row.values())


def test_sample_style_amount_formatting():
    """request_06 style: 620.4 -> '620.40' in the plan, '603.3' in amount_safe_to_pay."""
    request = builders.make_request(requested_amount=620.4)
    d = builders.decision_full_payment()
    d.amount_safe_to_pay = 603.3
    d.payment_plan = [(date(2026, 1, 3), 620.4)]
    row = decision_to_row(d, request)
    assert row["amount_safe_to_pay"] == "603.3"
    assert row["payment_plan"] == "2026-01-03:620.40"


def test_whole_amounts_keep_no_decimals():
    """request_01 style: 25256 -> '25256' everywhere."""
    request = builders.make_request(requested_amount=25256)
    d = builders.decision_full_payment()
    d.amount_safe_to_pay = 25256
    d.payment_plan = [(date(2024, 3, 3), 25256)]
    row = decision_to_row(d, request)
    assert row["amount_safe_to_pay"] == "25256"
    assert row["payment_plan"] == "2024-03-03:25256"


def test_multi_payment_plan_and_empty_earliest_and_none_changes():
    d = builders.decision_not_recommended()
    row = decision_to_row(d, builders.make_request())
    assert row["payment_plan"] == "none"
    assert row["earliest_date_for_full_payment"] == ""
    assert row["spending_changes_needed"] == "none"

    inst = decision_to_row(builders.decision_installments(), builders.make_request())
    assert inst["payment_plan"] == "2026-01-12:350|2026-02-11:350|2026-03-13:350"
    assert inst["earliest_date_for_full_payment"] == "2026-02-10"


def test_spending_changes_render_like_the_samples():
    """request_21 style: 'stop:event_1815|reduce_to:event_1816:23.50'."""
    d = builders.decision_full_payment_with_changes()
    d.spending_changes_needed = [
        SpendingChange("stop", "event_9", None, "s:1"),
        SpendingChange("reduce_to", "event_12", 23.5, "s:2"),
    ]
    row = decision_to_row(d, builders.make_request())
    assert row["spending_changes_needed"] == "stop:event_9|reduce_to:event_12:23.50"


def test_explanation_is_flattened_to_one_line_and_capped():
    d = builders.decision_wait()
    d.decision_explanation = "Pay INR 1,000\n in full on 10 February 2026." + " x" * 400
    row = decision_to_row(d, builders.make_request())
    assert "\n" not in row["decision_explanation"]
    assert len(row["decision_explanation"]) <= 400


def test_write_and_read_output_csv_round_trips(tmp_path):
    request = builders.make_request()
    rows = [
        decision_to_row(factory(), request)
        for _, factory in sorted(builders.VALID_DECISIONS.items())
    ]
    rows.append(refusal_row(request, "profile missing"))
    path = tmp_path / "output.csv"
    write_output_csv(rows, str(path))

    raw = path.read_bytes().decode("utf-8")
    assert "\r\n" not in raw
    assert raw.splitlines()[0] == ",".join(OUTPUT_COLUMNS)
    assert raw.endswith("\n")

    back = read_output_csv(str(path))
    assert back == rows


def test_quote_minimal_only_quotes_fields_that_need_it(tmp_path):
    request = builders.make_request()
    row = decision_to_row(builders.decision_full_payment(), request)
    path = tmp_path / "output.csv"
    write_output_csv([row], str(path))
    line = path.read_text(encoding="utf-8").splitlines()[1]
    # request_id is never quoted; the explanation contains a comma, so it is.
    assert line.startswith("request_900,")
    assert '"Pay INR 1,000 today.' in line
