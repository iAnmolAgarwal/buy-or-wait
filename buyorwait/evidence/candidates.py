"""evidence/candidates.py — build the EvidenceSet for one request.

Deterministic by construction: every id list is de-duplicated and sorted with a
natural (numeric-aware) key so that `event_9` sorts before `event_12`.

This module depends only on `buyorwait.types` — never on ingest/, engine/ or
perception/ (CONTRACT Section 1).
"""
from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence

from buyorwait.types import (
    EvidenceSet,
    ImageRef,
    Message,
    PaymentOption,
    Profile,
    RecurringStream,
    Request,
)

_NUM_SPLIT = re.compile(r"(\d+)")


def natural_key(value: str) -> tuple:
    """Sort key that orders `event_9` before `event_12`.

    Splits the string into alternating text/number runs; numbers compare
    numerically, text compares lexicographically. A leading (0, ...) /
    (1, ...) discriminator per part keeps the tuple type-homogeneous so
    Python never compares int against str.
    """
    parts = _NUM_SPLIT.split(str(value))
    key: list[tuple[int, int, str]] = []
    for part in parts:
        if part == "":
            continue
        if part.isdigit():
            key.append((0, int(part), ""))
        else:
            key.append((1, 0, part))
    return tuple(key)


def _sorted_unique(ids: Iterable[Optional[str]]) -> list[str]:
    seen = {i for i in ids if i}
    return sorted(seen, key=natural_key)


def build_evidence(
    request: Request,
    profile: Optional[Profile],
    streams: Sequence[RecurringStream],
    scheduled_events: Sequence[object],
    messages: Sequence[Message],
    images: Sequence[ImageRef],
    options: Sequence[PaymentOption],
) -> EvidenceSet:
    """Assemble the set of ids a Decision for `request` may cite.

    event_ids  : union of every stream's occurrence ids, every stream anchor id,
                 and every scheduled/future event id.
    stream_ids : the ids of the streams that were forecast.
    message_ids: messages belonging to this user that are either unscoped
                 (request_id is None) or scoped to this request.
    image_ids  : same rule as messages.
    option_ids : payment options attached to this request.
    """
    streams = list(streams or ())
    scheduled_events = list(scheduled_events or ())
    messages = list(messages or ())
    images = list(images or ())
    options = list(options or ())

    event_ids: list[Optional[str]] = []
    for stream in streams:
        event_ids.extend(getattr(stream, "event_ids", ()) or ())
        event_ids.append(getattr(stream, "anchor_event_id", None))
    for event in scheduled_events:
        event_ids.append(getattr(event, "event_id", None))

    stream_ids = [getattr(s, "stream_id", None) for s in streams]

    user_id = request.user_id
    request_id = request.request_id

    def _belongs(record: object) -> bool:
        if getattr(record, "user_id", None) != user_id:
            return False
        scope = getattr(record, "request_id", None)
        return scope is None or scope == request_id

    message_ids = [m.message_id for m in messages if _belongs(m)]
    image_ids = [i.image_id for i in images if _belongs(i)]
    option_ids = [
        o.payment_option_id
        for o in options
        if getattr(o, "request_id", request_id) == request_id
    ]

    return EvidenceSet(
        request_id=request_id,
        event_ids=_sorted_unique(event_ids),
        stream_ids=_sorted_unique(stream_ids),
        message_ids=_sorted_unique(message_ids),
        image_ids=_sorted_unique(image_ids),
        option_ids=_sorted_unique(option_ids),
    )
