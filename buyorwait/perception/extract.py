"""The two model-facing extractions: images and messages.

Both go through `ModelClient.structured_call`, so both are schema-enforced,
retried and cached by exactly one mechanism.
"""
from __future__ import annotations

import base64
import hashlib
import os
from typing import Any, Optional

from buyorwait.perception import prompts
from buyorwait.perception.client import CallStats, ModelClient, parse_iso_date
from buyorwait.perception.fallback import give_up
from buyorwait.types import (Amendment, Event, ImageExtraction, ImageRef, Message,
                             Profile, Request)

# Per-call stats of the most recent extraction, so run_perception can aggregate
# without changing the contract's return types.
LAST_STATS: dict[str, CallStats] = {}


def _read_png(path: str) -> tuple[str, str]:
    """Return (base64 data, sha256 hex) for the PNG at `path`."""
    if not os.path.isfile(path):
        give_up(f"image file not found: {path}", os.path.basename(path))
    with open(path, "rb") as fh:
        raw = fh.read()
    return base64.standard_b64encode(raw).decode("ascii"), hashlib.sha256(raw).hexdigest()


def extract_image(client: ModelClient, image: ImageRef, event: Optional[Event],
                  profile: Optional[Profile]) -> ImageExtraction:
    """Read one receipt/payslip/invoice PNG into an ImageExtraction."""
    data, digest = _read_png(image.path)
    home_ccy = profile.home_currency if profile else ""
    user_text = prompts.image_user_text(
        image_id=image.image_id,
        event_description=event.description if event else "",
        event_category=event.category if event else "",
        event_currency=event.currency if event else home_ccy,
        event_date=event.event_date.isoformat() if event and event.event_date else "",
        home_currency=home_ccy,
        event_settlement_date=(event.settlement_date.isoformat()
                               if event and event.settlement_date else ""),
        event_status=event.status if event else "",
    )
    messages = [{
        "role": "user",
        "content": [
            {"type": "image",
             "source": {"type": "base64", "media_type": "image/png", "data": data}},
            {"type": "text", "text": user_text},
        ],
    }]
    # The cache key uses the file digest rather than the base64 blob.
    cache_payload = {"kind": "image", "image_id": image.image_id, "sha256": digest,
                     "prompt": user_text, "system": prompts.IMAGE_SYSTEM,
                     "schema": prompts.IMAGE_SCHEMA, "model": client.model}
    result, stats = client.structured_call(
        request_id=image.request_id or image.user_id or image.image_id,
        system=prompts.IMAGE_SYSTEM,
        messages=messages,
        schema=prompts.IMAGE_SCHEMA,
        cache_payload=cache_payload,
        max_tokens=1024,
    )
    LAST_STATS[f"image:{image.image_id}"] = stats

    amount = result.get("amount")
    return ImageExtraction(
        image_id=image.image_id,
        amount=float(amount) if isinstance(amount, (int, float)) else None,
        currency=(result.get("currency") or None),
        doc_date=parse_iso_date(result.get("doc_date")),
        due_date=parse_iso_date(result.get("due_date")),
        description=str(result.get("description") or ""),
        confidence=float(result.get("confidence") or 0.0),
        fallback_used=False,
    )


def _events_block(events: list[Event], limit: int = 60) -> str:
    lines = []
    for e in events[:limit]:
        amount = "?" if e.amount is None else f"{e.amount:.2f}"
        lines.append(f"{e.event_id} | {e.event_date} | {e.event_type} | {e.category} | "
                     f"{e.direction} | {amount} {e.currency} | {e.status}")
    return "\n".join(lines)


def _messages_block(messages: list[Message]) -> str:
    lines = []
    for m in messages:
        scope = m.request_id or "user-level"
        rel = m.related_event_id or "-"
        lines.append(
            f"--- {m.message_id} | sent_at {m.sent_at} | from {m.source_type} | "
            f"scope {scope} | related_event_id {rel}\n{m.message_text}")
    return "\n".join(lines)


def interpret_messages(client: ModelClient, request: Request, profile: Profile,
                       messages: list[Message], candidate_events: list[Event],
                       tools: Any) -> list[Amendment]:
    """One call per request covering every message that applies to it.

    Date fields come back as the model's raw ISO strings; run_perception parses
    them (and drops any amendment whose dates are unusable).
    """
    if not messages:
        return []

    request_line = (f"request_id {request.request_id} | user {request.user_id} | "
                    f"request_date {request.request_date} | type {request.request_type} | "
                    f"requested {request.requested_amount} {profile.home_currency} | "
                    f"deadline {request.desired_completion_date}")
    profile_line = (f"home_currency {profile.home_currency} | balance "
                    f"{profile.current_available_balance} | minimum "
                    f"{profile.minimum_balance_to_keep}")
    user_text = prompts.messages_user_text(
        request_line=request_line,
        profile_line=profile_line,
        messages_block=_messages_block(messages),
        events_block=_events_block(candidate_events),
    )
    tool_defs = tools.definitions if tools is not None else None
    handler = tools.handle if tools is not None else None
    result, stats = client.structured_call(
        request_id=request.request_id,
        system=prompts.MESSAGES_SYSTEM,
        messages=[{"role": "user", "content": user_text}],
        schema=prompts.AMENDMENTS_SCHEMA,
        tools=tool_defs,
        tool_handler=handler,
        max_tool_steps=3,
        cache_payload={"kind": "messages", "prompt": user_text,
                       "system": prompts.MESSAGES_SYSTEM,
                       "schema": prompts.AMENDMENTS_SCHEMA,
                       "tools": [t["name"] for t in (tool_defs or [])],
                       "model": client.model},
        max_tokens=4096,
    )
    LAST_STATS[f"messages:{request.request_id}"] = stats

    known = {m.message_id for m in messages}
    out: list[Amendment] = []
    for raw in result.get("amendments", []):
        source_id = str(raw.get("source_id") or "")
        if source_id not in known:
            # Keep the record but pin it to a real message when there is only one.
            source_id = messages[0].message_id if len(messages) == 1 else source_id
        amount = raw.get("new_amount")
        pct = raw.get("pct_change")
        out.append(Amendment(
            kind=str(raw.get("kind")),
            source_id=source_id,
            target_event_id=raw.get("target_event_id") or None,
            target_category=(raw.get("target_category") or None),
            new_amount=float(amount) if isinstance(amount, (int, float)) else None,
            currency=(raw.get("currency") or None),
            new_date=raw.get("new_date") or None,           # ISO string, parsed in run.py
            effective_from=raw.get("effective_from") or None,  # ISO string
            pct_change=float(pct) if isinstance(pct, (int, float)) else None,
            confidence=float(raw.get("confidence") or 0.0),
            note=str(raw.get("note") or ""),
        ))
    return out
