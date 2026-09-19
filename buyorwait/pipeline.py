"""pipeline.py — the glue that turns a Dataset into output.csv rows.

Implements CONTRACT Section 1 dataflow for one request (`process_request`) and
for a whole run (`run_pipeline`). This module owns no financial arithmetic: it
only calls the packages A-D expose and fails closed exactly where the contract
says it may (a missing profile, or a row that will not pass the gate).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from buyorwait.engine.decide import decide
from buyorwait.engine.forecast import build_flows
from buyorwait.engine.policy import Policy
from buyorwait.evidence.candidates import build_evidence
from buyorwait.ingest.fx import convert
from buyorwait.ingest.lifecycle import resolve_lifecycles
from buyorwait.ingest.loader import load_dataset
from buyorwait.ingest.recurrence import detect_streams
from buyorwait.output import (REFUSAL_EXPLANATION, decision_to_row, refusal_row,
                              write_output_csv)
from buyorwait.perception.cache import ResponseCache
from buyorwait.perception.client import ModelClient
from buyorwait.perception.run import PerceptionContext, run_perception
from buyorwait.types import (Dataset, Decision, EvidenceSet, PerceptionResult,
                             Request)
from buyorwait.validate.gate import gate

# Note prefixes used by run_pipeline to count outcomes without changing the
# 5-tuple that process_request must return.
GATE_NOTE = "gate: "
REFUSAL_NOTE = "refusal: "


# ---------------------------------------------------------------------------
# Thread-safety wrappers
#
# ModelClient._bump and ResponseCache.hits/misses are read-modify-write on
# shared mutable counters; under a ThreadPoolExecutor those lose increments.
# Subclassing (rather than editing perception/) keeps package C untouched.
# perception.extract.LAST_STATS is a module global but every key it writes is
# unique per image_id / request_id, so parallel requests never collide there.
# ---------------------------------------------------------------------------

class ThreadSafeCache(ResponseCache):
    def __init__(self, dir: str) -> None:
        super().__init__(dir)
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            return super().get(key)

    def put(self, key: str, value: dict) -> None:
        with self._lock:
            super().put(key, value)


class ThreadSafeModelClient(ModelClient):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._usage_lock = threading.Lock()

    def _bump(self, *args, **kwargs) -> None:
        with self._usage_lock:
            super()._bump(*args, **kwargs)

    def usage_snapshot(self) -> dict:
        with self._usage_lock:
            return super().usage_snapshot()


# ---------------------------------------------------------------------------
# RunSummary
# ---------------------------------------------------------------------------

@dataclass
class RunSummary:
    run_id: str
    out_dir: str
    model: str
    started_at: str
    finished_at: str
    wall_seconds: float
    requests_processed: int
    requests_with_live_calls: int
    requests_served_from_cache: int
    fallbacks: int
    fallback_reasons: list[str]
    gate_failures: int
    refusal_rows: int
    dataset_rows: int
    usage: dict
    code_fingerprint: str
    rows: list[dict] = field(default_factory=list)
    load_errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# One request
# ---------------------------------------------------------------------------

# An extraction at or below this confidence is not trusted on its own for a
# future debit: CONTRACT 5.1 says fail closed, so we take the higher of the
# extracted amount and the category estimate.
LOW_CONFIDENCE = 0.5


def _is_future_debit(event, request_date: date) -> bool:
    """A debit that is still ahead of us: pending, scheduled, or dated forward."""
    if event.direction != "debit":
        return False
    if event.status in ("pending", "scheduled"):
        return True
    when = event.settlement_date or event.event_date
    return when is not None and when >= request_date


def fallback_amount(event, priced_events, streams, request, policy
                    ) -> tuple[Optional[float], str]:
    """CONTRACT 5.1 fail-closed estimate for a future debit with no usable amount.

    (a) the recurring stream in the same category, else
    (b) the mean of the user's settled debits in that category, else
    (c) the largest settled debit in the horizon-adjacent history.
    Returns (amount, source) or (None, "") when the user has no usable history.
    """
    for stream in streams or ():
        if stream.direction == "debit" and stream.category == event.category:
            return float(stream.amount), f"stream {stream.stream_id}"

    settled = [e for e in priced_events
               if e.direction == "debit" and e.status == "settled"
               and e.amount is not None]

    same_category = [float(e.amount) for e in settled if e.category == event.category]
    if same_category:
        return (sum(same_category) / len(same_category),
                f"mean of {len(same_category)} settled {event.category} debits")

    window_start = request.request_date - timedelta(days=int(policy.horizon_days))
    recent = [float(e.amount) for e in settled if e.event_date >= window_start]
    pool = recent or [float(e.amount) for e in settled]
    if pool:
        scope = "recent history" if recent else "full history"
        return max(pool), f"largest settled debit in {scope}"
    return None, ""


def _is_scheduled(event, request_date: date) -> bool:
    if event.status in ("pending", "scheduled"):
        return True
    when = event.settlement_date or event.event_date
    return when is not None and when >= request_date


def process_request(
    ds: Dataset,
    request_id: str,
    client,
    cache,
    policy: Policy,
    log: Callable[[str], None],
) -> tuple[dict, Optional[Decision], Optional[PerceptionResult], Optional[EvidenceSet], list[str]]:
    """CONTRACT Section 1 for a single request.

    Returns (row, decision, perception, evidence, notes). `decision` is None on
    the two fail-closed paths (missing profile, gate still failing after one
    re-decide); `perception` is None only when the request itself is malformed.
    """
    notes: list[str] = []
    request = ds.requests.get(request_id)
    if request is None:
        note = f"{REFUSAL_NOTE}{request_id}: request not present in requests.csv"
        log(note)
        notes.append(note)
        blank = Request(request_id=request_id, user_id="", request_date=date(1970, 1, 1),
                        request_type="", requested_amount=0.0,
                        desired_completion_date=date(1970, 1, 1),
                        allows_partial_payment=False, request_text="")
        return refusal_row(blank, "request record missing."), None, None, None, notes

    profile = ds.profiles.get(request.user_id)
    if profile is None:
        note = f"{REFUSAL_NOTE}{request_id}: no profile for {request.user_id}"
        log(note)
        notes.append(note)
        return refusal_row(request, "Profile unavailable."), None, None, None, notes

    # -- 2. lifecycle ------------------------------------------------------
    all_events = list(ds.events_by_user.get(request.user_id, []))
    eligible, lifecycle_notes = resolve_lifecycles(all_events)
    notes.extend(lifecycle_notes)
    eligible_by_id = {e.event_id: e for e in eligible}

    # -- 3. perception -----------------------------------------------------
    user_messages = [
        m for m in ds.messages_by_user.get(request.user_id, [])
        if m.request_id is None or m.request_id == request_id
    ]
    user_images = [
        i for i in ds.images_by_user.get(request.user_id, [])
        if i.request_id is None or i.request_id == request_id
    ]
    blank_images = [
        i for i in user_images
        if i.related_event_id in eligible_by_id
        and eligible_by_id[i.related_event_id].amount is None
    ]

    ctx = PerceptionContext(
        request=request,
        profile=profile,
        messages=user_messages,
        images=blank_images,
        events_by_id={e.event_id: e for e in all_events},
        rates=ds.rates,
        client=client,
        cache=cache,
    )
    perception = run_perception(ctx)
    for reason in perception.fallbacks:
        note = f"perception fallback: {reason}"
        log(f"{request_id}: {note}")
        notes.append(note)

    unresolved: list[tuple] = []          # (event, image) with no usable extraction
    low_confidence: list[tuple] = []      # (event, image, amount) below LOW_CONFIDENCE
    for image in blank_images:
        event = eligible_by_id[image.related_event_id]
        extraction = perception.image_extractions.get(image.image_id)
        if extraction is None or extraction.amount is None:
            note = (f"image {image.image_id} unreadable for event {event.event_id}: "
                    "no amount extracted")
            log(f"{request_id}: {note}")
            notes.append(note)
            unresolved.append((event, image))
            continue
        raw = float(extraction.amount)
        ccy = (extraction.currency or event.currency or profile.home_currency).upper()
        try:
            amount = convert(raw, ccy, profile.home_currency, event.event_date, ds.rates)
        except ValueError as exc:
            note = (f"image {image.image_id} amount {raw} {ccy} not convertible to "
                    f"{profile.home_currency}: {exc}")
            log(f"{request_id}: {note}")
            notes.append(note)
            unresolved.append((event, image))
            continue
        event.amount = amount
        event.amount_raw = raw
        event.currency = ccy
        event.amount_source = f"image:{image.image_id}"
        note = (f"event {event.event_id} amount filled from {image.image_id}: "
                f"{raw} {ccy} -> {amount:.2f} {profile.home_currency}")
        log(f"{request_id}: {note}")
        notes.append(note)
        if float(extraction.confidence or 0.0) < LOW_CONFIDENCE:
            low_confidence.append((event, image, amount))

    # -- 4. streams --------------------------------------------------------
    # Streams are detected BEFORE the fail-closed estimates below, from the
    # events that already carry an amount, so an estimate can borrow from them.
    priced = [e for e in eligible if e.amount is not None]
    streams = detect_streams(priced, profile, request.request_date, policy)

    # -- 4b. CONTRACT 5.1: fail closed on unresolved future debits ----------
    for event, image in unresolved:
        if not _is_future_debit(event, request.request_date):
            note = (f"event {event.event_id}: unresolved amount on a past settled "
                    "event, excluded from recurrence estimation")
            log(f"{request_id}: {note}")
            notes.append(note)
            continue
        estimate, source = fallback_amount(event, priced, streams, request, policy)
        if estimate is None:
            # D33b: the ladder is exhausted, so we cannot bound this debit. Dropping
            # it could still produce an `affordable_now` row, which would be the
            # unsafe direction -- fail closed to the refusal row instead.
            reason = (f"unresolved future debit {event.event_id} with no estimable "
                      "amount")
            note = f"{REFUSAL_NOTE}{request_id}: {reason}"
            log(note)
            notes.append(note)
            return refusal_row(request, f"{reason}."), None, perception, None, notes
        event.amount = estimate
        event.amount_source = f"fallback:{source}"
        note = (f"event {event.event_id}: unresolved amount, fail-closed estimate "
                f"{estimate:.2f} from {source}")
        log(f"{request_id}: {note}")
        notes.append(note)

    for event, image, extracted in low_confidence:
        if not _is_future_debit(event, request.request_date):
            continue
        estimate, source = fallback_amount(event, priced, streams, request, policy)
        if estimate is None or estimate <= extracted:
            continue
        event.amount = estimate
        event.amount_source = f"fallback:{source}"
        note = (f"event {event.event_id}: low-confidence extraction from "
                f"{image.image_id} ({extracted:.2f}) raised to the fail-closed "
                f"estimate {estimate:.2f} from {source}")
        log(f"{request_id}: {note}")
        notes.append(note)

    # -- 4c. scheduled events ----------------------------------------------
    scheduled_events = []
    for event in eligible:
        if not _is_scheduled(event, request.request_date):
            continue
        if event.amount is None:
            note = f"scheduled event {event.event_id} dropped: amount still unknown"
            log(f"{request_id}: {note}")
            notes.append(note)
            continue
        scheduled_events.append(event)

    # -- 5. flows ----------------------------------------------------------
    flows, flow_notes = build_flows(streams, scheduled_events, perception.amendments,
                                    profile, request, ds.rates, policy)
    notes.extend(flow_notes)

    # -- 6. evidence -------------------------------------------------------
    options = ds.options_by_request.get(request_id, [])
    evidence = build_evidence(request, profile, streams, scheduled_events,
                              ds.messages_by_user.get(request.user_id, []),
                              ds.images_by_user.get(request.user_id, []), options)

    # -- 7/8. decide + gate ------------------------------------------------
    decision = decide(request, profile, options, streams, flows, evidence, policy)
    notes.extend(decision.notes)
    row = decision_to_row(decision, request)
    ok, problems = gate(decision, request, profile, evidence, row)
    if ok:
        return row, decision, perception, evidence, notes

    for problem in problems:
        note = f"{GATE_NOTE}{problem}"
        log(f"{request_id}: {note}")
        notes.append(note)

    # ONE re-decide with spending changes disabled. Policy has no flag for it,
    # so every stream is made inflexible: plans.candidate_changes then yields
    # nothing and only change-free plans survive.
    fixed_streams = [replace(s, flexibility="fixed") for s in streams]
    retry = decide(request, profile, options, fixed_streams, flows, evidence, policy)
    notes.extend(f"re-decide: {n}" for n in retry.notes)
    retry_row = decision_to_row(retry, request)
    ok2, problems2 = gate(retry, request, profile, evidence, retry_row)
    if ok2:
        note = "re-decided without spending changes after a gate failure"
        log(f"{request_id}: {note}")
        notes.append(note)
        return retry_row, retry, perception, evidence, notes

    for problem in problems2:
        note = f"{GATE_NOTE}re-decide: {problem}"
        log(f"{request_id}: {note}")
        notes.append(note)
    reason = "; ".join(problems2[:3])
    note = f"{REFUSAL_NOTE}{request_id}: validation failed: {reason}"
    log(note)
    notes.append(note)
    return (refusal_row(request, f"validation failed: {reason}"), None, perception,
            evidence, notes)


# ---------------------------------------------------------------------------
# Whole run
# ---------------------------------------------------------------------------

def code_fingerprint(package_dir: str) -> str:
    """sha256 over every buyorwait/**/*.py file (path + bytes), sorted."""
    h = hashlib.sha256()
    paths = []
    for root, _dirs, files in os.walk(package_dir):
        if "__pycache__" in root:
            continue
        for name in sorted(files):
            if name.endswith(".py"):
                paths.append(os.path.join(root, name))
    for path in sorted(paths):
        h.update(os.path.relpath(path, package_dir).encode("utf-8"))
        h.update(b"\x00")
        with open(path, "rb") as fh:
            h.update(fh.read())
        h.update(b"\x00")
    return h.hexdigest()


def _done_ids(path: str) -> tuple[set[str], dict[str, dict]]:
    done: set[str] = set()
    rows: dict[str, dict] = {}
    if not os.path.isfile(path):
        return done, rows
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = record.get("request_id")
            if rid:
                done.add(rid)
                if isinstance(record.get("row"), dict):
                    rows[rid] = record["row"]
    return done, rows


def run_pipeline(
    root: str = "dataset",
    out_dir: str = "runs/run",
    limit: Optional[int] = None,
    ids: Optional[Iterable[str]] = None,
    resume: bool = False,
    workers: int = 4,
    model: str = "claude-sonnet-5",
    policy: Optional[Policy] = None,
    cache_dir: str = "cache",
    client: Optional[ModelClient] = None,
) -> RunSummary:
    """Load once, process every selected request, write the five run artefacts."""
    policy = policy or Policy()
    os.makedirs(out_dir, exist_ok=True)
    started = datetime.now(timezone.utc)
    t0 = time.time()

    log_path = os.path.join(out_dir, "log.txt")
    log_lock = threading.Lock()
    log_fh = open(log_path, "a" if resume else "w", encoding="utf-8")

    def log(message: str) -> None:
        line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {message}"
        with log_lock:
            log_fh.write(line + "\n")
            log_fh.flush()

    ds = load_dataset(root)
    for err in ds.load_errors:
        log(f"load: {err}")
    log(f"loaded {len(ds.requests)} requests, {len(ds.profiles)} profiles from {root}")

    order = list(ds.requests.keys())
    if ids:
        wanted = [i.strip() for i in ids if i and i.strip()]
        order = [r for r in order if r in set(wanted)]
    if limit is not None:
        order = order[: int(limit)]

    jsonl_path = os.path.join(out_dir, "decisions.jsonl")
    skip, resumed_rows = ((set(), {}) if not resume else _done_ids(jsonl_path))
    todo = [r for r in order if r not in skip]
    if skip:
        log(f"resume: skipping {len(skip & set(order))} already-recorded requests")

    shared_cache = ThreadSafeCache(cache_dir)
    if client is None:
        client = ThreadSafeModelClient(model=model, cache_dir=cache_dir,
                                       cache=shared_cache)
    model_name = getattr(client, "model", model)

    results: dict[str, tuple] = {}
    counter = {"n": 0}
    progress_lock = threading.Lock()
    total = len(todo)

    def work(request_id: str):
        try:
            out = process_request(ds, request_id, client, shared_cache, policy, log)
        except Exception as exc:  # noqa: BLE001 - one bad request must not kill the run
            log(f"{REFUSAL_NOTE}{request_id}: unhandled {type(exc).__name__}: {exc}")
            request = ds.requests.get(request_id)
            row = (refusal_row(request, f"internal error: {type(exc).__name__}.")
                   if request is not None
                   else {"request_id": request_id, "amount_safe_to_pay": "0",
                         "affordability_status": "not_affordable",
                         "recommended_payment_method": "not_recommended",
                         "payment_plan": "none",
                         "earliest_date_for_full_payment": "",
                         "spending_changes_needed": "none",
                         "decision_explanation": REFUSAL_EXPLANATION})
            out = (row, None, None, None,
                   [f"{REFUSAL_NOTE}unhandled {type(exc).__name__}: {exc}"])
        with progress_lock:
            counter["n"] += 1
            n = counter["n"]
        if n % 10 == 0 or n == total:
            print(f"[{n}/{total}] {request_id} ({time.time() - t0:.1f}s elapsed)",
                  file=sys.stderr, flush=True)
        return request_id, out

    if workers and workers > 1 and total > 1:
        with ThreadPoolExecutor(max_workers=int(workers)) as pool:
            for request_id, out in pool.map(work, todo):
                results[request_id] = out
    else:
        for request_id in todo:
            rid, out = work(request_id)
            results[rid] = out

    # -- write artefacts in requests.csv order -----------------------------
    gate_failures = refusals = live = cached = 0
    fallback_reasons: list[str] = []
    rows: list[dict] = []
    jsonl_lines: list[str] = []

    for request_id in order:
        if request_id in results:
            row, decision, perception, evidence, notes = results[request_id]
            rows.append(row)
            if any(n.startswith(GATE_NOTE) for n in notes):
                gate_failures += 1
            if any(n.startswith(REFUSAL_NOTE) for n in notes):
                refusals += 1
            if perception is not None:
                if perception.model_calls:
                    live += 1
                if perception.cached:
                    cached += 1
                fallback_reasons.extend(perception.fallbacks)
            record = {
                "request_id": request_id,
                "row": row,
                "notes": notes,
                "evidence": ({
                    "event_ids": evidence.event_ids,
                    "stream_ids": evidence.stream_ids,
                    "message_ids": evidence.message_ids,
                    "image_ids": evidence.image_ids,
                    "option_ids": evidence.option_ids,
                } if evidence is not None else None),
                "perception": ({
                    "model_calls": perception.model_calls,
                    "tool_steps": perception.tool_steps,
                    "input_tokens": perception.input_tokens,
                    "output_tokens": perception.output_tokens,
                    "cached": perception.cached,
                    "fallbacks": perception.fallbacks,
                    "amendments": len(perception.amendments),
                    "image_extractions": len(perception.image_extractions),
                } if perception is not None else None),
                "status": row.get("affordability_status"),
                "method": row.get("recommended_payment_method"),
            }
            jsonl_lines.append(json.dumps(record, default=str, ensure_ascii=False))
        elif request_id in resumed_rows:
            rows.append(resumed_rows[request_id])

    write_output_csv(rows, os.path.join(out_dir, "output.csv"))
    with open(jsonl_path, "a" if resume else "w", encoding="utf-8") as fh:
        for line in jsonl_lines:
            fh.write(line + "\n")

    finished = datetime.now(timezone.utc)
    summary = RunSummary(
        run_id=os.path.basename(os.path.normpath(out_dir)),
        out_dir=out_dir,
        model=model_name,
        started_at=started.isoformat(timespec="seconds"),
        finished_at=finished.isoformat(timespec="seconds"),
        wall_seconds=round(time.time() - t0, 3),
        requests_processed=len(results),
        requests_with_live_calls=live,
        requests_served_from_cache=cached,
        fallbacks=len(fallback_reasons),
        fallback_reasons=sorted(set(fallback_reasons)),
        gate_failures=gate_failures,
        refusal_rows=refusals,
        dataset_rows=len(rows),
        usage=client.usage_snapshot(),
        code_fingerprint=code_fingerprint(os.path.dirname(os.path.abspath(__file__))),
        rows=rows,
        load_errors=list(ds.load_errors),
    )

    usage = {
        "run_id": summary.run_id,
        "model": summary.model,
        "provider": "Anthropic",
        "started_at": summary.started_at,
        "finished_at": summary.finished_at,
        "wall_seconds": summary.wall_seconds,
        "per_model": summary.usage,
        "requests_processed": summary.requests_processed,
        "requests_with_live_calls": summary.requests_with_live_calls,
        "requests_served_from_cache": summary.requests_served_from_cache,
        "cache_hits": shared_cache.hits,
        "cache_misses": shared_cache.misses,
        "fallbacks": summary.fallbacks,
        "fallback_reasons": summary.fallback_reasons,
        "gate_failures": summary.gate_failures,
        "refusal_rows": summary.refusal_rows,
        "dataset_rows": summary.dataset_rows,
        "load_errors": len(summary.load_errors),
        "code_fingerprint": summary.code_fingerprint,
        "output_csv": os.path.join(out_dir, "output.csv"),
    }
    with open(os.path.join(out_dir, "usage.json"), "w", encoding="utf-8") as fh:
        json.dump(usage, fh, indent=2, sort_keys=True)
        fh.write("\n")

    log(f"done: {summary.requests_processed} processed, {summary.dataset_rows} rows, "
        f"{summary.wall_seconds:.1f}s, gate_failures={summary.gate_failures}, "
        f"refusals={summary.refusal_rows}")
    log_fh.close()
    return summary
