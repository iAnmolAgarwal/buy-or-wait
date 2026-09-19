"""Code-side tools exposed to the model inside the bounded tool loop.

Everything here is deterministic code: the model may ask for a fact, it never
computes one. `get_rate` uses a small local lookup over `RateTable.rows` so
perception never imports from ingest/.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from buyorwait.perception.client import parse_iso_date
from buyorwait.types import Event, ImageRef, Profile, RateTable

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "inspect_attachment",
        "description": ("Read the receipt/payslip/invoice image attached to this "
                        "request and return its final payable/paid total, currency "
                        "and dates. Use it when a message refers to an attached "
                        "document or when a candidate event has no amount."),
        "input_schema": {
            "type": "object",
            "properties": {"image_id": {"type": "string"}},
            "required": ["image_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "expand_evidence",
        "description": ("Return one financial event as a dict, plus any events linked "
                        "to it through linked_event_id. Use it to check what a message "
                        "is actually talking about before amending it."),
        "input_schema": {
            "type": "object",
            "properties": {"event_id": {"type": "string"}},
            "required": ["event_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_rate",
        "description": ("Dated FX rate from the supplied rate table: the nearest rate "
                        "on or before `date` for from_ccy->to_ccy. For reference only "
                        "- never convert amounts yourself, code does that."),
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "from_ccy": {"type": "string"},
                "to_ccy": {"type": "string"},
            },
            "required": ["date", "from_ccy", "to_ccy"],
            "additionalProperties": False,
        },
    },
]


def _direct_rate(rates: RateTable, frm: str, to: str, on: date) -> Optional[float]:
    """Nearest rate_date <= on for the exact pair; else the earliest available."""
    matches = [r for r in rates.rows if r[1] == frm and r[2] == to]
    if not matches:
        return None
    on_or_before = [r for r in matches if r[0] <= on]
    if on_or_before:
        return max(on_or_before, key=lambda r: r[0])[3]
    return min(matches, key=lambda r: r[0])[3]


def lookup_rate(rates: RateTable, frm: str, to: str, on: date) -> Optional[float]:
    """Direct pair, else inverse as 1/rate, else routed through USD. None if unroutable."""
    frm, to = frm.upper(), to.upper()
    if frm == to:
        return 1.0
    direct = _direct_rate(rates, frm, to, on)
    if direct:
        return direct
    inverse = _direct_rate(rates, to, frm, on)
    if inverse:
        return 1.0 / inverse
    for hub in ("USD", "EUR"):
        if hub in (frm, to):
            continue
        left = _direct_rate(rates, frm, hub, on)
        if not left:
            right_inv = _direct_rate(rates, hub, frm, on)
            left = (1.0 / right_inv) if right_inv else None
        right = _direct_rate(rates, hub, to, on)
        if not right:
            left_inv = _direct_rate(rates, to, hub, on)
            right = (1.0 / left_inv) if left_inv else None
        if left and right:
            return left * right
    return None


def event_to_dict(event: Event) -> dict:
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "description": event.description,
        "category": event.category,
        "direction": event.direction,
        "amount": event.amount,
        "amount_raw": event.amount_raw,
        "currency": event.currency,
        "event_date": event.event_date.isoformat() if event.event_date else None,
        "settlement_date": (event.settlement_date.isoformat()
                            if event.settlement_date else None),
        "status": event.status,
        "linked_event_id": event.linked_event_id,
        "flexibility": event.flexibility,
        "minimum_allowed_amount": event.minimum_allowed_amount,
        "amount_source": event.amount_source,
    }


@dataclass
class ToolBox:
    """Bound to one request. `handle` is the tool_handler passed to ModelClient."""

    client: Any
    profile: Profile
    events_by_id: dict[str, Event]
    images_by_id: dict[str, ImageRef]
    rates: RateTable
    extractions: dict[str, Any] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    @property
    def definitions(self) -> list[dict]:
        return TOOL_DEFINITIONS

    # -- individual tools ---------------------------------------------------

    def inspect_attachment(self, image_id: str) -> dict:
        from buyorwait.perception.extract import extract_image  # local: avoids a cycle

        image = self.images_by_id.get(image_id)
        if image is None:
            return {"error": f"unknown image_id {image_id!r}",
                    "known": sorted(self.images_by_id)}
        if image_id not in self.extractions:
            event = self.events_by_id.get(image.related_event_id or "")
            self.extractions[image_id] = extract_image(self.client, image, event,
                                                       self.profile)
        ex = self.extractions[image_id]
        return {
            "image_id": ex.image_id,
            "amount": ex.amount,
            "currency": ex.currency,
            "doc_date": ex.doc_date.isoformat() if ex.doc_date else None,
            "due_date": ex.due_date.isoformat() if ex.due_date else None,
            "description": ex.description,
            "confidence": ex.confidence,
            "related_event_id": image.related_event_id,
        }

    def expand_evidence(self, event_id: str) -> dict:
        event = self.events_by_id.get(event_id)
        if event is None:
            return {"error": f"unknown event_id {event_id!r}"}
        linked: list[dict] = []
        if event.linked_event_id and event.linked_event_id in self.events_by_id:
            linked.append(event_to_dict(self.events_by_id[event.linked_event_id]))
        for other in self.events_by_id.values():
            if other.linked_event_id == event_id:
                linked.append(event_to_dict(other))
        return {"event": event_to_dict(event), "linked_events": linked}

    def get_rate(self, date_str: str, from_ccy: str, to_ccy: str) -> dict:
        on = parse_iso_date(date_str)
        if on is None:
            return {"error": f"date {date_str!r} is not YYYY-MM-DD"}
        rate = lookup_rate(self.rates, from_ccy, to_ccy, on)
        if rate is None:
            return {"error": f"no route from {from_ccy} to {to_ccy}"}
        return {"date": on.isoformat(), "from_ccy": from_ccy.upper(),
                "to_ccy": to_ccy.upper(), "rate": rate}

    # -- dispatcher ---------------------------------------------------------

    def handle(self, name: str, payload: dict) -> str:
        self.calls.append(name)
        if name == "inspect_attachment":
            out: Any = self.inspect_attachment(str(payload.get("image_id", "")))
        elif name == "expand_evidence":
            out = self.expand_evidence(str(payload.get("event_id", "")))
        elif name == "get_rate":
            out = self.get_rate(str(payload.get("date", "")),
                                str(payload.get("from_ccy", "")),
                                str(payload.get("to_ccy", "")))
        else:
            out = {"error": f"unknown tool {name!r}"}
        return json.dumps(out, default=str)
