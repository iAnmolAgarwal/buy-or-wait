"""`run_perception` - called once per request. The only catcher of Fallback."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from buyorwait.perception.client import CallStats, ModelClient, parse_iso_date
from buyorwait.perception.extract import LAST_STATS, extract_image, interpret_messages
from buyorwait.perception.fallback import Fallback
from buyorwait.perception.tools import ToolBox
from buyorwait.types import (Amendment, Event, ImageExtraction, ImageRef, Message,
                             PerceptionResult, Profile, RateTable, Request)

LOGGER = logging.getLogger("perception")

# Amendment fields that must end up as datetime.date (or None).
_DATE_FIELDS = ("new_date", "effective_from")
# Kinds that are meaningless without the date they carry.
_DATE_REQUIRED = {"delay": ("new_date",), "new_income": ("new_date",)}


@dataclass
class PerceptionContext:
    request: Request
    profile: Profile
    messages: list[Message] = field(default_factory=list)
    images: list[ImageRef] = field(default_factory=list)
    events_by_id: dict[str, Event] = field(default_factory=dict)
    rates: Optional[RateTable] = None
    client: Optional[ModelClient] = None
    cache: Any = None


def _empty(request_id: str, reason: Optional[str] = None) -> PerceptionResult:
    return PerceptionResult(
        request_id=request_id, amendments=[], image_extractions={}, tool_steps=0,
        model_calls=0, input_tokens=0, output_tokens=0, cached=False,
        fallbacks=[reason] if reason else [],
    )


def sanitise_amendments(amendments: list[Amendment], request_id: str
                        ) -> tuple[list[Amendment], list[str]]:
    """Parse the model's ISO date strings into `datetime.date`.

    THE ONE ALLOWED SANITISATION: an amendment carrying an unparseable date is
    dropped and logged, and the reason is added to PerceptionResult.fallbacks.
    """
    kept: list[Amendment] = []
    notes: list[str] = []
    for amendment in amendments:
        bad = ""
        parsed: dict[str, Optional[date]] = {}
        for name in _DATE_FIELDS:
            raw = getattr(amendment, name)
            if raw is None or raw == "":
                parsed[name] = None
                continue
            value = parse_iso_date(raw)
            if value is None:
                bad = f"{name}={raw!r}"
                break
            parsed[name] = value
        if not bad:
            for name in _DATE_REQUIRED.get(amendment.kind, ()):
                if parsed.get(name) is None:
                    bad = f"{amendment.kind} without {name}"
                    break
        if bad:
            note = (f"dropped amendment {amendment.kind} from {amendment.source_id}: "
                    f"unusable date ({bad})")
            LOGGER.warning("perception [%s]: %s", request_id, note)
            notes.append(note)
            continue
        for name, value in parsed.items():
            setattr(amendment, name, value)
        kept.append(amendment)
    return kept, notes


def run_perception(ctx: PerceptionContext) -> PerceptionResult:
    """Images first (cached), then one bounded message call. Fail-safe, never raises."""
    request_id = ctx.request.request_id
    if not ctx.messages and not ctx.images:
        return _empty(request_id)
    if ctx.client is None:
        return _empty(request_id, "no model client configured")

    calls = tool_steps = in_tok = out_tok = 0
    cached_any = False
    extractions: dict[str, ImageExtraction] = {}
    amendments: list[Amendment] = []
    fallbacks: list[str] = []

    def absorb(stats: Optional[CallStats]) -> None:
        nonlocal calls, tool_steps, in_tok, out_tok, cached_any
        if stats is None:
            return
        calls += stats.calls
        tool_steps += stats.tool_steps
        in_tok += stats.input_tokens
        out_tok += stats.output_tokens
        cached_any = cached_any or stats.cached

    try:
        images_by_id = {img.image_id: img for img in ctx.images}
        toolbox = ToolBox(client=ctx.client, profile=ctx.profile,
                          events_by_id=ctx.events_by_id, images_by_id=images_by_id,
                          rates=ctx.rates or RateTable(rows=[]))

        for image in ctx.images:
            event = ctx.events_by_id.get(image.related_event_id or "")
            extraction = extract_image(ctx.client, image, event, ctx.profile)
            extractions[image.image_id] = extraction
            toolbox.extractions[image.image_id] = extraction
            absorb(LAST_STATS.get(f"image:{image.image_id}"))

        if ctx.messages:
            candidates = sorted(ctx.events_by_id.values(),
                                key=lambda e: (e.event_date, e.event_id))
            amendments = interpret_messages(ctx.client, ctx.request, ctx.profile,
                                            ctx.messages, candidates, toolbox)
            absorb(LAST_STATS.get(f"messages:{request_id}"))
    except Fallback as exc:
        LOGGER.warning("perception [%s] falling back: %s", request_id, exc.reason)
        return _empty(request_id, exc.reason)

    amendments, notes = sanitise_amendments(amendments, request_id)
    fallbacks.extend(notes)

    return PerceptionResult(
        request_id=request_id,
        amendments=amendments,
        image_extractions=extractions,
        tool_steps=tool_steps,
        model_calls=calls,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cached=cached_any,
        fallbacks=fallbacks,
    )
