"""Tests for evidence/candidates.py::build_evidence."""
from __future__ import annotations

import pathlib
import sys
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "fixtures" / "validate"))

import builders  # noqa: E402
from buyorwait.evidence.candidates import build_evidence, natural_key  # noqa: E402
from buyorwait.types import Event, ImageRef, Message, RecurringStream  # noqa: E402


def make_stream(stream_id: str, event_ids: list[str], anchor: str) -> RecurringStream:
    return RecurringStream(
        stream_id=stream_id,
        category="dining",
        description="weekend food delivery",
        direction="debit",
        cadence_days=21,
        monthly=False,
        amount=500.0,
        anchor_event_id=anchor,
        last_date=date(2026, 1, 1),
        flexibility="reducible",
        minimum_allowed_amount=100.0,
        event_ids=list(event_ids),
    )


def make_event(event_id: str, user_id: str = "user_900") -> Event:
    return Event(
        event_id=event_id,
        user_id=user_id,
        event_type="expense",
        description="rent",
        category="housing",
        direction="debit",
        amount=100.0,
        amount_raw=100.0,
        currency="INR",
        event_date=date(2026, 1, 20),
        settlement_date=date(2026, 1, 20),
        status="scheduled",
        linked_event_id=None,
        flexibility="fixed",
        minimum_allowed_amount=None,
    )


def make_message(mid: str, user_id="user_900", request_id=None) -> Message:
    return Message(
        message_id=mid,
        user_id=user_id,
        request_id=request_id,
        related_event_id=None,
        sent_at=date(2026, 1, 2),
        source_type="employer",
        message_text="Your salary increases next month.",
    )


def make_image(iid: str, user_id="user_900", request_id=None) -> ImageRef:
    return ImageRef(
        image_id=iid,
        user_id=user_id,
        request_id=request_id,
        related_event_id="event_9",
        path=f"/tmp/{iid}.png",
    )


def test_natural_key_orders_event_ids_numerically():
    ids = ["event_105", "event_9", "event_12", "event_2"]
    assert sorted(ids, key=natural_key) == [
        "event_2",
        "event_9",
        "event_12",
        "event_105",
    ]


def test_event_ids_are_the_union_of_streams_anchors_and_scheduled_events():
    request = builders.make_request()
    streams = [
        make_stream("s:1", ["event_12", "event_2"], "event_12"),
        make_stream("s:2", ["event_105"], "event_9"),
    ]
    scheduled = [make_event("event_40"), make_event("event_7")]
    ev = build_evidence(
        request, builders.make_profile(), streams, scheduled, [], [], []
    )
    assert ev.event_ids == [
        "event_2",
        "event_7",
        "event_9",
        "event_12",
        "event_40",
        "event_105",
    ]
    assert ev.stream_ids == ["s:1", "s:2"]
    assert ev.request_id == request.request_id


def test_duplicate_ids_are_collapsed_and_output_is_deterministic():
    request = builders.make_request()
    streams = [
        make_stream("s:1", ["event_3", "event_3", "event_1"], "event_3"),
        make_stream("s:1", ["event_1"], "event_1"),
    ]
    scheduled = [make_event("event_3")]
    first = build_evidence(request, None, streams, scheduled, [], [], [])
    second = build_evidence(
        request, None, list(reversed(streams)), scheduled, [], [], []
    )
    assert first.event_ids == ["event_1", "event_3"]
    assert first.event_ids == second.event_ids
    assert first.stream_ids == ["s:1"]


def test_messages_and_images_are_filtered_by_user_and_request_scope():
    request = builders.make_request()  # user_900 / request_900
    messages = [
        make_message("message_3"),                                  # unscoped, kept
        make_message("message_10", request_id="request_900"),       # same request, kept
        make_message("message_11", request_id="request_901"),       # other request, dropped
        make_message("message_12", user_id="user_901"),             # other user, dropped
    ]
    images = [
        make_image("image_2"),
        make_image("image_9", request_id="request_901"),
        make_image("image_8", user_id="user_555"),
    ]
    ev = build_evidence(request, None, [], [], messages, images, [])
    assert ev.message_ids == ["message_3", "message_10"]
    assert ev.image_ids == ["image_2"]


def test_option_ids_come_from_this_requests_options_only():
    request = builders.make_request()
    options = [
        builders.make_option(payment_option_id="option_12"),
        builders.make_option(payment_option_id="option_3"),
        builders.make_option(payment_option_id="option_99", request_id="request_901"),
    ]
    ev = build_evidence(request, None, [], [], [], [], options)
    assert ev.option_ids == ["option_3", "option_12"]


def test_empty_inputs_give_an_empty_but_valid_evidence_set():
    request = builders.make_request()
    ev = build_evidence(request, None, [], [], [], [], [])
    assert ev.request_id == "request_900"
    assert ev.event_ids == []
    assert ev.stream_ids == []
    assert ev.message_ids == []
    assert ev.image_ids == []
    assert ev.option_ids == []
