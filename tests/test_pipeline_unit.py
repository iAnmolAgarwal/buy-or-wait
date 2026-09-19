"""Offline tests for pipeline.py / report.py. No network: the ModelClient gets a
fake `transport` that returns canned emit_result payloads.
"""
from __future__ import annotations

import copy
import csv
import json
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from buyorwait import report as report_mod
from buyorwait.engine.policy import Policy
from buyorwait.ingest.loader import load_dataset
from buyorwait.perception.cache import ResponseCache
from buyorwait.pipeline import (REFUSAL_NOTE, ThreadSafeModelClient,
                                code_fingerprint, process_request, run_pipeline)
from buyorwait.types import (Dataset, Event, ImageRef, Profile, RateTable,
                             Request)
from buyorwait.validate.gate import gate
from conftest import requires_dataset  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATASET = os.path.join(ROOT, "dataset")
REAL_IDS = ["request_26", "request_27", "request_33"]   # request_33 has image_06

pytestmark = requires_dataset


# ---------------------------------------------------------------------------
# fake transport
# ---------------------------------------------------------------------------

def _emit(payload, tool_id="t1"):
    return {"content": [{"type": "tool_use", "id": tool_id, "name": "emit_result",
                         "input": payload}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 120, "output_tokens": 40}}


def fake_transport(**kw):
    """Image calls get an amount; message calls get an empty amendment list."""
    content = kw["messages"][0]["content"]
    is_image = isinstance(content, list) and any(
        b.get("type") == "image" for b in content if isinstance(b, dict))
    if is_image:
        return _emit({"amount": 4820.0, "currency": "INR", "doc_date": "2026-01-06",
                      "due_date": None, "description": "Grocery tax invoice",
                      "confidence": 0.95})
    return _emit({"amendments": []})


@pytest.fixture(scope="module")
def dataset():
    return load_dataset(DATASET)


@pytest.fixture
def client(tmp_path):
    return ThreadSafeModelClient(model="fake-model",
                                 cache_dir=str(tmp_path / "cache"),
                                 transport=fake_transport, sleep=lambda _s: None)


def _bare_dataset_with_pending_blank_debit() -> Dataset:
    """One user, one request, one PENDING debit with a blank amount and an image.

    There is no settled history at all, so the (a)-(c) fallback ladder in
    `pipeline.fallback_amount` finds nothing to estimate from.
    """
    event = Event(
        event_id="event_x1", user_id="user_x", event_type="expense",
        description="Unreadable invoice", category="healthcare", direction="debit",
        amount=None, amount_raw=None, currency="INR",
        event_date=date(2026, 3, 10), settlement_date=date(2026, 3, 20),
        status="pending", linked_event_id=None, flexibility="fixed",
        minimum_allowed_amount=None, amount_source="missing")
    profile = Profile(
        user_id="user_x", home_currency="INR", current_available_balance=100000.0,
        minimum_balance_to_keep=10000.0, financial_priorities=[],
        protect_categories=[], reduce_categories=[], stop_categories=[],
        methods={"full_payment"}, max_installment_months=None)
    request = Request(
        request_id="request_x1", user_id="user_x", request_date=date(2026, 3, 1),
        request_type="purchase", requested_amount=5000.0,
        desired_completion_date=date(2026, 4, 30), allows_partial_payment=False,
        request_text="Can I pay INR 5,000 now?")
    image = ImageRef(image_id="image_x1", user_id="user_x", request_id="request_x1",
                     related_event_id="event_x1",
                     path=os.path.join(DATASET, "media", "images", "image_06.png"))
    return Dataset(
        requests={"request_x1": request},
        profiles={"user_x": profile},
        events_by_user={"user_x": [event]},
        events_by_id={"event_x1": event},
        options_by_request={},
        messages_by_user={},
        images_by_user={"user_x": [image]},
        rates=RateTable(rows=[]),
        load_errors=[],
    )


def _logs():
    lines: list[str] = []
    return lines, lines.append


# ---------------------------------------------------------------------------
# process_request on real request ids
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("request_id", REAL_IDS)
def test_process_request_passes_gate(dataset, client, request_id):
    ds = copy.deepcopy(dataset)
    lines, log = _logs()
    row, decision, perception, evidence, notes = process_request(
        ds, request_id, client, client.cache, Policy(), log)

    assert row["request_id"] == request_id
    assert decision is not None, f"{request_id} fell through to a refusal: {notes[-3:]}"
    assert perception is not None and evidence is not None
    ok, problems = gate(decision, ds.requests[request_id],
                        ds.profiles[ds.requests[request_id].user_id], evidence, row)
    assert ok, problems
    assert row["affordability_status"] in {
        "affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"}


def test_image_backed_event_gets_an_amount(dataset, client):
    """request_33 / image_06 / event_3051 has a blank CSV amount."""
    ds = copy.deepcopy(dataset)
    assert ds.events_by_id["event_3051"].amount is None
    lines, log = _logs()
    row, decision, perception, evidence, notes = process_request(
        ds, "request_33", client, client.cache, Policy(), log)

    event = {e.event_id: e for e in ds.events_by_user["user_33"]}["event_3051"]
    assert event.amount == pytest.approx(4820.0)
    assert event.amount_source == "image:image_06"
    assert "image_06" in perception.image_extractions
    assert perception.model_calls >= 1
    assert any("event_3051 amount filled from image_06" in n for n in notes)


def test_unreadable_image_leaves_amount_none(dataset, tmp_path):
    """Fail closed per CONTRACT 5.1: amount stays None and the run still decides."""
    def transport(**kw):
        content = kw["messages"][0]["content"]
        if isinstance(content, list) and any(
                b.get("type") == "image" for b in content if isinstance(b, dict)):
            return _emit({"amount": None, "currency": None, "doc_date": None,
                          "due_date": None, "description": "unreadable",
                          "confidence": 0.0})
        return _emit({"amendments": []})

    cl = ThreadSafeModelClient(model="fake-model", cache_dir=str(tmp_path / "c"),
                              transport=transport, sleep=lambda _s: None)
    ds = copy.deepcopy(load_dataset(DATASET))
    lines, log = _logs()
    row, decision, _p, _e, notes = process_request(ds, "request_33", cl, cl.cache,
                                                   Policy(), log)
    event = {e.event_id: e for e in ds.events_by_user["user_33"]}["event_3051"]
    assert event.amount is None
    assert any("unreadable" in n for n in notes)
    assert decision is not None          # uncertainty is not a refusal


# ---------------------------------------------------------------------------
# CONTRACT 5.1 — fail-closed estimates for unresolved future debits (D32)
# ---------------------------------------------------------------------------

# request_64 / image_10 / event_6033: a PENDING grocery debit whose CSV amount is
# blank and whose settlement (2024-06-10) is after the request date (2024-06-04).
FUTURE_DEBIT_REQUEST = "request_64"
FUTURE_DEBIT_USER = "user_64"
FUTURE_DEBIT_EVENT = "event_6033"


def _image_transport(amount, confidence):
    def transport(**kw):
        content = kw["messages"][0]["content"]
        if isinstance(content, list) and any(
                b.get("type") == "image" for b in content if isinstance(b, dict)):
            return _emit({"amount": amount, "currency": "IDR", "doc_date": None,
                          "due_date": None, "description": "receipt",
                          "confidence": confidence})
        return _emit({"amendments": []})
    return transport


def _run_future_debit(tmp_path, amount, confidence, drop_event=False):
    ds = copy.deepcopy(load_dataset(DATASET))
    if drop_event:
        ds.events_by_user[FUTURE_DEBIT_USER] = [
            e for e in ds.events_by_user[FUTURE_DEBIT_USER]
            if e.event_id != FUTURE_DEBIT_EVENT]
        ds.images_by_user[FUTURE_DEBIT_USER] = []
    cl = ThreadSafeModelClient(model="fake-model", cache_dir=str(tmp_path),
                               transport=_image_transport(amount, confidence),
                               sleep=lambda _s: None)
    lines, log = _logs()
    row, decision, perception, evidence, notes = process_request(
        ds, FUTURE_DEBIT_REQUEST, cl, cl.cache, Policy(), log)
    event = {e.event_id: e for e in ds.events_by_user[FUTURE_DEBIT_USER]}.get(
        FUTURE_DEBIT_EVENT)
    return row, notes, event


def test_unreadable_future_debit_uses_a_fail_closed_estimate(tmp_path):
    """An unreadable image on a FUTURE debit must not drop the event."""
    row, notes, event = _run_future_debit(tmp_path / "a", None, 0.0)

    assert event.amount is not None, "the future debit was dropped instead of estimated"
    assert event.amount_source.startswith("fallback:")
    assert any(f"event {FUTURE_DEBIT_EVENT}: unresolved amount, fail-closed estimate"
               in n for n in notes), notes
    assert not any("dropped: amount still unknown" in n for n in notes)

    # Carrying the estimate is strictly safer than pretending the debit is absent.
    dropped_row, _n, _e = _run_future_debit(tmp_path / "b", None, 0.0, drop_event=True)
    assert float(row["amount_safe_to_pay"]) < float(dropped_row["amount_safe_to_pay"])


def test_low_confidence_extraction_takes_the_max(tmp_path):
    """confidence < 0.5 on a future debit -> max(extracted, fallback estimate)."""
    row, notes, event = _run_future_debit(tmp_path / "c", 1.0, 0.2)

    assert event.amount_source.startswith("fallback:")
    assert event.amount > 1.0, "the tiny low-confidence amount was trusted"
    assert any("low-confidence extraction" in n and "raised to the fail-closed estimate"
               in n for n in notes), notes


def test_high_confidence_extraction_is_trusted(tmp_path):
    """The max rule applies only below the confidence floor."""
    _row, notes, event = _run_future_debit(tmp_path / "d", 1.0, 0.95)
    assert event.amount_source == "image:image_10"
    assert not any("low-confidence extraction" in n for n in notes)


def test_past_settled_unresolved_amount_is_still_excluded(dataset, tmp_path):
    """event_3051 is a PAST settled debit: it only feeds stream means, so an
    unreadable image leaves it blank (unchanged behaviour)."""
    ds = copy.deepcopy(dataset)
    cl = ThreadSafeModelClient(model="fake-model", cache_dir=str(tmp_path / "e"),
                               transport=_image_transport(None, 0.0),
                               sleep=lambda _s: None)
    lines, log = _logs()
    _row, decision, _p, _e, notes = process_request(ds, "request_33", cl, cl.cache,
                                                    Policy(), log)
    event = {e.event_id: e for e in ds.events_by_user["user_33"]}["event_3051"]
    assert event.amount is None
    assert any("past settled event, excluded from recurrence estimation" in n
               for n in notes), notes
    assert decision is not None


def test_unestimable_future_debit_fails_closed_to_a_refusal(tmp_path):
    """D33b: no history to borrow from -> refusal row, never `affordable_now`."""
    ds = _bare_dataset_with_pending_blank_debit()
    cl = ThreadSafeModelClient(model="fake-model", cache_dir=str(tmp_path / "f"),
                               transport=_image_transport(None, 0.0),
                               sleep=lambda _s: None)
    lines, log = _logs()
    row, decision, perception, evidence, notes = process_request(
        ds, "request_x1", cl, cl.cache, Policy(), log)

    assert decision is None and evidence is None
    assert perception is not None                 # the image call did happen
    assert row == {
        "request_id": "request_x1",
        "amount_safe_to_pay": "0",
        "affordability_status": "not_affordable",
        "recommended_payment_method": "not_recommended",
        "payment_plan": "none",
        "earliest_date_for_full_payment": "",
        "spending_changes_needed": "none",
        "decision_explanation": (
            "Insufficient verified data to assess this request safely. unresolved "
            "future debit event_x1 with no estimable amount."),
    }
    assert any(n.startswith(REFUSAL_NOTE)
               and "unresolved future debit event_x1 with no estimable amount" in n
               for n in notes), notes
    assert any("unresolved future debit event_x1" in line for line in lines)


def test_unestimable_refusal_is_counted_in_refusal_rows(tmp_path, monkeypatch):
    """The D33b path must count like the missing-profile refusal."""
    ds = _bare_dataset_with_pending_blank_debit()
    monkeypatch.setattr("buyorwait.pipeline.load_dataset", lambda _root: ds)
    cl = ThreadSafeModelClient(model="fake-model", cache_dir=str(tmp_path / "g"),
                               transport=_image_transport(None, 0.0),
                               sleep=lambda _s: None)
    summary = run_pipeline(root=DATASET, out_dir=str(tmp_path / "run"), workers=1,
                           model="fake-model", client=cl,
                           cache_dir=str(tmp_path / "g"))
    assert summary.refusal_rows == 1
    assert summary.dataset_rows == 1
    usage = json.loads((tmp_path / "run" / "usage.json").read_text())
    assert usage["refusal_rows"] == 1


def test_zero_calls_when_no_messages_or_images(dataset, client):
    """A request with neither messages nor images must not touch the model."""
    ds = copy.deepcopy(dataset)
    before = dict(client.usage_snapshot())
    picked = next(r for r in ds.requests
                  if not ds.messages_by_user.get(ds.requests[r].user_id)
                  and not ds.images_by_user.get(ds.requests[r].user_id))
    lines, log = _logs()
    row, _d, perception, _e, _n = process_request(ds, picked, client, client.cache,
                                                  Policy(), log)
    assert perception.model_calls == 0
    assert client.usage_snapshot() == before


# ---------------------------------------------------------------------------
# refusal path
# ---------------------------------------------------------------------------

def test_missing_profile_is_the_blanket_refusal(dataset, client):
    ds = copy.deepcopy(dataset)
    user = ds.requests["request_26"].user_id
    del ds.profiles[user]
    lines, log = _logs()
    row, decision, perception, evidence, notes = process_request(
        ds, "request_26", client, client.cache, Policy(), log)

    assert decision is None and perception is None and evidence is None
    assert row == {
        "request_id": "request_26",
        "amount_safe_to_pay": "0",
        "affordability_status": "not_affordable",
        "recommended_payment_method": "not_recommended",
        "payment_plan": "none",
        "earliest_date_for_full_payment": "",
        "spending_changes_needed": "none",
        "decision_explanation": ("Insufficient verified data to assess this request "
                                 "safely. Profile unavailable."),
    }
    assert any("no profile" in line for line in lines)


def test_unknown_request_id_refuses(dataset, client):
    lines, log = _logs()
    row, decision, _p, _e, _n = process_request(copy.deepcopy(dataset), "request_9999",
                                                client, client.cache, Policy(), log)
    assert decision is None
    assert row["request_id"] == "request_9999"
    assert row["affordability_status"] == "not_affordable"


# ---------------------------------------------------------------------------
# run_pipeline
# ---------------------------------------------------------------------------

def test_run_pipeline_limit_5_writes_everything(tmp_path, client):
    out = tmp_path / "run5"
    summary = run_pipeline(root=DATASET, out_dir=str(out), limit=5, workers=4,
                           model="fake-model", client=client,
                           cache_dir=str(tmp_path / "cache"))

    for name in ("output.csv", "decisions.jsonl", "usage.json", "log.txt"):
        assert (out / name).is_file(), name

    with open(out / "output.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 5
    expected = list(load_dataset(DATASET).requests.keys())[:5]
    assert [r["request_id"] for r in rows] == expected     # requests.csv order

    lines = [json.loads(l) for l in (out / "decisions.jsonl").read_text().splitlines()]
    assert [l["request_id"] for l in lines] == expected
    assert all("row" in l and "notes" in l and "evidence" in l and "perception" in l
               for l in lines)

    usage = json.loads((out / "usage.json").read_text())
    assert usage["requests_processed"] == 5
    assert usage["provider"] == "Anthropic"
    # A 64-char sha256 over buyorwait/**/*.py. Compared by shape, not recomputed:
    # recomputing races with anyone editing the package while the suite runs.
    assert len(usage["code_fingerprint"]) == 64
    assert set(usage["code_fingerprint"]) <= set("0123456789abcdef")
    assert usage["code_fingerprint"] == summary.code_fingerprint
    assert code_fingerprint(os.path.join(ROOT, "buyorwait")) is not None
    assert summary.dataset_rows == 5
    assert summary.refusal_rows == 0


def test_resume_skips_done_ids(tmp_path, client):
    out = tmp_path / "resume"
    run_pipeline(root=DATASET, out_dir=str(out), limit=3, workers=1,
                 model="fake-model", client=client, cache_dir=str(tmp_path / "c"))
    first = [json.loads(l)["request_id"]
             for l in (out / "decisions.jsonl").read_text().splitlines()]
    assert len(first) == 3

    second = run_pipeline(root=DATASET, out_dir=str(out), limit=5, workers=1,
                          resume=True, model="fake-model", client=client,
                          cache_dir=str(tmp_path / "c"))
    assert second.requests_processed == 2          # only the two new ids

    all_ids = [json.loads(l)["request_id"]
               for l in (out / "decisions.jsonl").read_text().splitlines()]
    assert len(all_ids) == 5 and len(set(all_ids)) == 5
    with open(out / "output.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["request_id"] for r in rows] == list(
        load_dataset(DATASET).requests.keys())[:5]


def test_ids_selection(tmp_path, client):
    out = tmp_path / "ids"
    run_pipeline(root=DATASET, out_dir=str(out), ids=["request_27", "request_26"],
                 workers=2, model="fake-model", client=client,
                 cache_dir=str(tmp_path / "c"))
    with open(out / "output.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["request_id"] for r in rows] == ["request_26", "request_27"]


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def test_report_renders_from_usage_json(tmp_path, client):
    out = tmp_path / "rep"
    run_pipeline(root=DATASET, out_dir=str(out), limit=3, workers=2,
                 model="fake-model", client=client, cache_dir=str(tmp_path / "c"))
    assert report_mod.main([str(out)]) == 0

    text = (out / "usage_report.md").read_text()
    assert "This report describes the final full-dataset run that produced output.csv" in text
    assert f"run id `{out.name}`" in text
    assert "Anthropic" in text
    assert "fake-model" in text
    assert "affordability_status" in text and "recommended_payment_method" in text
    usage = json.loads((out / "usage.json").read_text())
    assert usage["code_fingerprint"] in text


def test_report_prices_are_current():
    assert report_mod.price_for("claude-sonnet-5") == (2.00, 10.00)
    assert report_mod.price_for("something-unknown") == report_mod.DEFAULT_PRICE


def test_report_needs_a_usage_json(tmp_path):
    assert report_mod.main([str(tmp_path)]) == 1
