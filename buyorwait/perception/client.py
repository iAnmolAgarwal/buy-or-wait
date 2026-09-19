"""The only module in the project that talks to the Anthropic API.

Structured output is enforced AT THE API BOUNDARY: every call declares a single
tool named ``emit_result`` whose ``input_schema`` is the caller's JSON schema.
When no other tools are offered the call is forced with
``tool_choice={"type": "tool", "name": "emit_result"}``. When tools are offered
the model may use them for at most ``max_tool_steps`` rounds; after that
``emit_result`` is forced.

The returned dict is validated against the schema by a small local validator
(no jsonschema dependency); a mismatch is a retryable error.

ONE retry policy: 3 attempts, backoff 1/2/4 s, on RateLimitError,
APIStatusError 5xx/529, APITimeoutError, APIConnectionError and schema mismatch.
Exhausting it goes through the ONE fallback point (`fallback.give_up`).
"""
from __future__ import annotations

import calendar
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Optional

from buyorwait.perception.cache import ResponseCache, cache_key
from buyorwait.perception.fallback import LOGGER, give_up

DEFAULT_MODEL = "claude-sonnet-5"
EMIT_TOOL_NAME = "emit_result"
BACKOFF_SECONDS = (1.0, 2.0, 4.0)

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def parse_iso_date(value: Any) -> Optional[date]:
    """Parse 'YYYY-MM-DD' without raising. Returns None for anything else.

    Also accepts a full ISO timestamp by taking the date part, and passes a
    `datetime.date` straight through. Deliberately exception-free: perception
    must never catch-and-continue.
    """
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) > 10 and (text[10] == "T" or text[10] == " "):
        text = text[:10]
    m = _ISO_DATE_RE.match(text)
    if not m:
        return None
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= month <= 12):
        return None
    if not (1 <= day <= calendar.monthrange(year, month)[1]):
        return None
    return date(year, month, day)


# ---------------------------------------------------------------------------
# Schema validation (required keys, types, enums). No third-party dependency.
# ---------------------------------------------------------------------------

_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate_against_schema(value: Any, schema: dict, path: str = "$") -> list[str]:
    """Return a list of human-readable problems; [] means the value conforms."""
    problems: list[str] = []
    declared = schema.get("type")
    if declared is not None:
        types = declared if isinstance(declared, list) else [declared]
        if not any(_TYPE_CHECKS.get(t, lambda _v: True)(value) for t in types):
            problems.append(f"{path}: expected type {types}, got {type(value).__name__}")
            return problems

    enum = schema.get("enum")
    if enum is not None and value not in enum:
        problems.append(f"{path}: {value!r} not in enum {enum}")

    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                problems.append(f"{path}: missing required key {key!r}")
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                problems.extend(validate_against_schema(value[key], sub, f"{path}.{key}"))

    if isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(value):
                problems.extend(validate_against_schema(item, item_schema, f"{path}[{i}]"))

    return problems


class SchemaMismatch(Exception):
    """Retryable: the model's emit_result payload did not match the schema."""


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@dataclass
class CallStats:
    model: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_steps: int = 0
    cached: bool = False


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from either an SDK object or a plain dict (test transports)."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, SchemaMismatch):
        return True
    try:  # pragma: no cover - import guard only
        import anthropic
    except ImportError:  # pragma: no cover
        return False
    if isinstance(exc, (anthropic.RateLimitError, anthropic.APITimeoutError,
                        anthropic.APIConnectionError)):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        status = getattr(exc, "status_code", 0) or 0
        return status >= 500 or status == 529
    return False


class ModelClient:
    """Structured-output client with a bounded tool loop, retries and a cache."""

    def __init__(self, model: str = DEFAULT_MODEL, cache_dir: str = "cache",
                 max_retries: int = 3, timeout_s: float = 60.0,
                 transport: Optional[Callable[..., Any]] = None,
                 sleep: Optional[Callable[[float], None]] = None,
                 cache: Optional[ResponseCache] = None,
                 api_key: Optional[str] = None) -> None:
        self.model = model
        self.cache_dir = cache_dir
        self.max_retries = max_retries
        self.timeout_s = timeout_s
        self.cache = cache if cache is not None else ResponseCache(cache_dir)
        self._sleep = sleep if sleep is not None else time.sleep
        self._transport = transport
        self._api_key = api_key
        self._sdk_client: Any = None
        self.usage: dict[str, dict[str, int]] = {}

    # -- usage accumulator --------------------------------------------------

    def _bump(self, model: str, *, calls: int = 0, input_tokens: int = 0,
              output_tokens: int = 0, cache_hits: int = 0) -> None:
        row = self.usage.setdefault(
            model, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_hits": 0})
        row["calls"] += calls
        row["input_tokens"] += input_tokens
        row["output_tokens"] += output_tokens
        row["cache_hits"] += cache_hits

    def usage_snapshot(self) -> dict:
        """Per-model totals for the usage report."""
        return {m: dict(v) for m, v in sorted(self.usage.items())}

    # -- transport ----------------------------------------------------------

    def _call_api(self, **kwargs: Any) -> Any:
        if self._transport is not None:
            return self._transport(**kwargs)
        if self._sdk_client is None:  # pragma: no cover - exercised by the live test
            import anthropic
            key = self._api_key or os.environ.get("ANTHROPIC_API_KEY")
            self._sdk_client = anthropic.Anthropic(api_key=key, timeout=self.timeout_s,
                                                   max_retries=0)
        return self._sdk_client.messages.create(**kwargs)  # pragma: no cover

    # -- the one public entry point ----------------------------------------

    def structured_call(self, *, request_id: str, system: str, messages: list[dict],
                        schema: dict, tools: Optional[list[dict]] = None,
                        tool_handler: Optional[Callable[[str, dict], str]] = None,
                        max_tool_steps: int = 3,
                        cache_payload: Optional[dict] = None,
                        max_tokens: int = 4096) -> tuple[dict, CallStats]:
        payload = cache_payload if cache_payload is not None else {
            "model": self.model, "system": system, "messages": messages,
            "schema": schema, "tools": tools, "max_tool_steps": max_tool_steps,
        }
        key = cache_key(request_id, payload)
        hit = self.cache.get(key)
        if hit is not None:
            self._bump(self.model, cache_hits=1)
            return hit, CallStats(model=self.model, calls=0, cached=True)

        last_problem = ""
        for attempt in range(self.max_retries):
            try:
                result, stats = self._attempt(
                    system=system, messages=messages, schema=schema, tools=tools,
                    tool_handler=tool_handler, max_tool_steps=max_tool_steps,
                    max_tokens=max_tokens)
            except Exception as exc:  # noqa: BLE001 - re-raised unless retryable
                if not _is_retryable(exc) or attempt == self.max_retries - 1:
                    if not _is_retryable(exc):
                        raise
                    last_problem = f"{type(exc).__name__}: {exc}"
                    break
                last_problem = f"{type(exc).__name__}: {exc}"
                delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
                LOGGER.warning("perception retry %d/%d for %s after %s (sleep %.0fs)",
                               attempt + 1, self.max_retries, request_id, last_problem,
                               delay)
                self._sleep(delay)
                continue
            self.cache.put(key, result)
            return result, stats

        give_up(f"model call failed after {self.max_retries} attempts ({last_problem})",
                request_id)

    # -- bounded tool loop --------------------------------------------------

    def _attempt(self, *, system: str, messages: list[dict], schema: dict,
                 tools: Optional[list[dict]], tool_handler, max_tool_steps: int,
                 max_tokens: int) -> tuple[dict, CallStats]:
        emit_tool = {
            "name": EMIT_TOOL_NAME,
            "description": ("Return the final structured result. You MUST call this "
                            "exactly once, as your last action."),
            "input_schema": schema,
        }
        all_tools = list(tools or []) + [emit_tool]
        convo: list[dict] = list(messages)
        stats = CallStats(model=self.model)
        steps = 0
        # +2 guards against a model that neither emits nor calls a tool.
        for _ in range(max_tool_steps + 2):
            forced = not tools or steps >= max_tool_steps
            tool_choice = ({"type": "tool", "name": EMIT_TOOL_NAME} if forced
                           else {"type": "auto"})
            response = self._call_api(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=convo,
                tools=all_tools,
                tool_choice=tool_choice,
                thinking={"type": "disabled"},
            )
            stats.calls += 1
            usage = _field(response, "usage")
            in_tok = int(_field(usage, "input_tokens", 0) or 0)
            out_tok = int(_field(usage, "output_tokens", 0) or 0)
            stats.input_tokens += in_tok
            stats.output_tokens += out_tok
            self._bump(self.model, calls=1, input_tokens=in_tok, output_tokens=out_tok)

            content = list(_field(response, "content", []) or [])
            uses = [b for b in content if _field(b, "type") == "tool_use"]
            emit = next((b for b in uses if _field(b, "name") == EMIT_TOOL_NAME), None)
            if emit is not None:
                result = _field(emit, "input")
                if not isinstance(result, dict):
                    raise SchemaMismatch("emit_result input was not a JSON object")
                problems = validate_against_schema(result, schema)
                if problems:
                    raise SchemaMismatch("; ".join(problems[:5]))
                stats.tool_steps = steps
                return result, stats

            if forced or not uses or tool_handler is None:
                raise SchemaMismatch(
                    f"model did not call {EMIT_TOOL_NAME} (forced={forced}, "
                    f"tool_uses={[_field(b, 'name') for b in uses]})")

            convo.append({"role": "assistant", "content": content})
            results = []
            for block in uses:
                results.append({
                    "type": "tool_result",
                    "tool_use_id": _field(block, "id"),
                    "content": tool_handler(_field(block, "name"),
                                            _field(block, "input") or {}),
                })
            convo.append({"role": "user", "content": results})
            steps += 1

        raise SchemaMismatch("tool loop exhausted without an emit_result call")
