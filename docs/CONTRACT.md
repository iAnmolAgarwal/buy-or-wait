# CONTRACT — Buy or Wait (HackerRank Orchestrate, September 2026)

Written by the orchestrator after reading `problem_statement.md` (PS) and every
dataset file. **This document plus `buyorwait/types.py` is the contract.** Every
subagent implements to it; diffs that drift from it are rejected. Line refs are
to `problem_statement.md`.

Clock: T0 = 15:00 IST, 13 Sep 2026. Freeze 17:15. Deadline 18:00.

## 0. Facts about the data (verified by inspection)

| File | Rows | Notes |
|---|---|---|
| requests.csv | 250 | request_26..request_275, one distinct user each |
| sample_requests.csv | 25 | request_01..25 with labels; **format/style + sanity only** |
| financial_profiles.csv | 275 | one per user |
| financial_events.csv | 25,342 | 16 blank amounts (all have an image); 58 linked_event_id; statuses settled/pending/scheduled/cancelled/failed/unrealized |
| exchange_rates.csv | 134 | pairs USD→INR, USD→IDR, USD→EUR, EUR→USD, EUR→ZAR; monthly dates on the 15th, 2023-10-15..2026-11-15 |
| request_payment_options.csv | 790 | every request has 1 full_payment + 1–3 installments options |
| messages.csv | 215 | employer/service_provider/financial_service/bank/merchant; English + Indonesian |
| images.csv | 16 | every image → one blank-amount event; PNG at dataset/media/images/<image_id>.png |

Event history per user is ~70–80 rows: monthly items (rent, utilities, subscriptions,
debt, shopping, salary on a fixed day-of-month) and short-cadence items (groceries every
7/10 days, transport every 14/21 days, dining every ~21 days) with varying amounts.

## 1. Module boundaries and dataflow

```
dataset/*.csv, media/*.png
   │  ingest.loader.load_dataset(root) -> Dataset          [fail-closed row validation]
   ▼
Dataset (typed records; foreign amounts converted via ingest.fx on event_date)
   │  ingest.lifecycle.resolve_lifecycles(events) -> events'   [linked_event_id, exclusions]
   │  perception.run_perception(ctx) -> PerceptionResult       [model: images + messages, bounded tool loop]
   │  ingest.recurrence.detect_streams(...) -> [RecurringStream]
   │  engine.forecast.build_flows(...) -> [CashFlow]           [apply Amendments deterministically]
   ▼
engine.forecast.balance_path(...) -> [(date, balance)]
   │  engine.solver.amount_safe_to_pay(...)  -> float
   │  engine.solver.earliest_full_payment_date(...) -> date|None   [preference-independent]
   │  engine.plans.enumerate_plans(...) -> [Plan];  engine.plans.rank(...) -> Plan|None
   │  engine.decide.decide(...) -> Decision                      [status + explanation from templates]
   ▼
evidence.candidates.build_evidence(...) -> EvidenceSet  (built in code BEFORE decide; passed through unchanged)
   │  validate.schema.validate_row(row) ; validate.citations.validate(decision, evidence) ;
   │  validate.coherence.check(decision, request, profile)
   ▼
output row (OUTPUT_COLUMNS order)  +  runs/<run_id>/{decisions.jsonl, evidence.jsonl, usage.json, log.txt}
```

Rule: **arithmetic lives in code**. The model only produces `ImageExtraction` and
`Amendment` records (types.py). Code applies them and computes every number.

## 2. Function signatures (implement exactly)

### ingest/ (subagent A)
```python
# ingest/loader.py
def load_dataset(root: str) -> Dataset
    # Validates every row. Malformed row -> skipped with a line in Dataset.load_errors, never raises.
    # Event.amount = amount_raw converted to the user's home currency on event_date via fx.convert.
    # Event.amount = None when the CSV amount is blank (amount_source="missing").
    # Profile.methods = set(split('|')); max_installment_months None when blank.
    # Request.allows_partial_payment: 'true'/'false' case-insensitive.
# ingest/fx.py
def convert(amount: float, from_ccy: str, to_ccy: str, on: date, rates: RateTable) -> float
    # identity if same ccy. Direct pair on nearest rate_date <= on (else earliest available, logged via warnings list).
    # If only the inverse pair exists, use 1/rate. If neither exists, route through USD. Raise ValueError only if unroutable.
def rate_lookup(from_ccy, to_ccy, on, rates) -> tuple[float, date]
# ingest/lifecycle.py
def resolve_lifecycles(events: list[Event]) -> tuple[list[Event], list[str]]
    # Returns forecast-eligible events and notes. Rules (PS:36, PS:178, PS:200-205):
    #  - status in {cancelled, failed} -> excluded.
    #  - status == unrealized or direction == non_cash or event_type == investment_valuation -> excluded.
    #  - status == pending and direction == credit -> excluded (pending credits). Pending DEBITS are kept (safer).
    #  - duplicates: same user, same amount, same event_date, same category, same direction -> keep first event_id only.
    #  - linked_event_id chains: a later event with linked_event_id pointing to an earlier one is the newer record of
    #    the same lifecycle: a refund/reversal of a settled debit is a normal credit (keep both, they are both real);
    #    a 'retry' of a failed/cancelled event replaces it; a cancellation record cancels its target.
    #    A 'pending' refund credit linked to a debit is excluded (pending credit).
# ingest/recurrence.py
def detect_streams(events: list[Event], profile: Profile, request_date: date, policy: Policy) -> list[RecurringStream]
    # Groups SETTLED debit events (and credit salary/income) per (category, description-normalised) with >= policy.min_occurrences
    # occurrences on/before request_date. cadence_days = median gap (rounded); monthly=True when median gap in [28,31].
    # amount = policy.amount_estimator over the occurrences ("mean" | "median" | "last" | "max").
    # anchor_event_id = most recent occurrence. Excludes event_type in {refund, investment_*}, categories 'windfall'.
    # Non-recurring one-offs (fewer occurrences) are NOT projected.
def next_occurrences(stream: RecurringStream, start: date, end: date) -> list[date]
    # monthly: same day-of-month as last_date, clamped to month length; else last_date + k*cadence_days. Only dates > last_date and in [start, end].
```

### perception/ (subagent C)
```python
# perception/client.py
class ModelClient:
    def __init__(self, model: str, cache_dir: str, max_retries: int = 3, timeout_s: float = 60.0) -> None
    def structured_call(self, *, request_id: str, system: str, messages: list[dict], schema: dict,
                        tools: list[dict] | None = None, tool_handler=None, max_tool_steps: int = 3) -> tuple[dict, CallStats]
    # Enforces structured output via Anthropic tool-use: a single "emit_result" tool whose input_schema=schema, tool_choice forced
    # when tools is None; when tools given, model may call tools up to max_tool_steps times then MUST call emit_result.
    # ONE retry policy (exponential backoff 1,2,4 s on APIStatusError/ RateLimit/ timeout / schema mismatch).
    # ONE fallback point: perception/fallback.py::fallback(reason, request_id) -> raises Fallback(reason) which run_perception
    # catches to produce an empty-but-valid PerceptionResult with fallbacks=[reason]. Every fallback is logged.
# perception/cache.py
def cache_key(request_id: str, payload: dict) -> str   # sha256 of request_id + canonical json. Isolation: same payload, different request -> different key.
class ResponseCache: get(key)->dict|None ; put(key, value) ; stored as cache/<key>.json
# perception/extract.py
def extract_image(client, image: ImageRef, event: Event, profile: Profile) -> ImageExtraction
def interpret_messages(client, request: Request, profile: Profile, messages: list[Message],
                       candidate_events: list[Event], tools: ToolBox) -> list[Amendment]
# perception/tools.py
class ToolBox:   # code-side implementations exposed to the model (bounded loop)
    inspect_attachment(image_id) -> str (vision extraction text) ; expand_evidence(event_id) -> dict ; get_rate(date, from_ccy, to_ccy) -> float
# perception/run.py
def run_perception(ctx: PerceptionContext) -> PerceptionResult   # ctx = request, profile, messages, images, events_by_id, rates, client
    # Called once per request. If the request has no messages and no images -> no model call, cached=False, model_calls=0.
```
Model: `claude-sonnet-5` (perception is cheap; decision is in code). Env var: `ANTHROPIC_API_KEY`.

### engine/ (subagent B)
```python
# engine/policy.py
@dataclass class Policy: amount_estimator="mean"; min_occurrences=3; horizon_days=90; include_request_day=True;
                         deadline_is_hard=True; installments_cap_by_months=True; pending_debit_on="settlement_date"
# engine/forecast.py
def build_flows(streams, scheduled_events, amendments, profile, request, rates, policy) -> tuple[list[CashFlow], list[str]]
    # scheduled_events: eligible events with status in {pending, scheduled} or settlement_date >= request_date (future debits/credits).
    # Applies Amendments deterministically: cancel/delay/amend on target_event_id; income_change/income_end/amend_amount_pct on the
    # matching stream (by category, e.g. salary/rent); new_income/one_time_credit add single flows (converted with rates on that date);
    # pending_ignore removes the related credit. Returns notes describing each application.
def balance_path(profile, flows, start: date, horizon_days: int, extra_payments: list[tuple[date,float]] = ()) -> list[tuple[date, float]]
    # day-by-day closing balance for start..start+horizon_days inclusive: balance_0 = current_available_balance; each day subtracts/adds
    # flows dated that day, and any extra_payments dated that day. Flows dated < start are ignored.
# engine/solver.py
def amount_safe_to_pay(profile, flows, request, policy) -> float
    # max x in [0, requested] s.t. balance_path with payment (request_date, x) stays >= minimum_balance_to_keep every day.
    # = clamp(min_over_days(balance) - minimum, 0, requested). Rounded to 2 dp.
def earliest_full_payment_date(profile, flows, request, policy) -> Optional[date]
    # first d in [request_date, request_date+horizon] s.t. paying requested_amount on d keeps the whole path >= minimum. None if none.
    # PREFERENCE-INDEPENDENT (PS:163). No spending changes (PS:183).
# engine/plans.py
def enumerate_plans(profile, request, options, flows, streams, safe_today, earliest, policy) -> list[Plan]
    # Candidates (each checked with balance_path >= minimum):
    #  full_payment  : pay requested on request_date. Eligible iff 'full_payment' in profile.methods.
    #  partial_payment: [(request_date, safe_today), (earliest, requested-safe_today)] iff request.allows_partial_payment and
    #                  'partial_payment' in methods and 0 < safe_today < requested and earliest is not None and earliest <= deadline (PS:146).
    #  installments  : each option with payment_method=='installments'; schedule from option.schedule(); eligible iff 'installments' in methods,
    #                  and (policy.installments_cap_by_months -> number_of_payments <= max_installment_months); must complete by deadline
    #                  when policy.deadline_is_hard. Payments after the horizon are not checked (outside forecast).
    #  wait          : [(earliest, requested)] iff earliest is not None and earliest > request_date and 'full_payment' in methods (PS:189).
    #  spending-change variants: for full_payment and installments only (partial/wait are defined without changes, PS:146/183):
    #                  enumerate subsets of <=3 changes over streams whose category is in profile.reduce_categories (reduce_to minimum_allowed_amount)
    #                  or profile.stop_categories (stop), respecting event flexibility; never stop+reduce the same stream (PS:198);
    #                  choose the smallest set (fewest changes, then largest saving) that makes the plan safe. Changes apply to all future occurrences.
def rank(plans: list[Plan]) -> Optional[Plan]
    # PS:191-196 in order: completes_by_deadline desc, no changes first, total_paid asc, start asc, len(payments) asc, option_id numeric asc.
# engine/decide.py
def decide(request, profile, options, streams, flows, evidence, policy) -> Decision
    # status: full_payment chosen & no changes -> affordable_now (earliest must == request_date);
    #         partial/installments/full_payment-with-changes -> affordable_with_plan;
    #         wait -> affordable_later; none -> not_affordable + not_recommended + plan none + earliest '' if None.
    # amount_safe_to_pay always = solver value (before spending changes), capped at requested (PS:99, PS:182).
    # explanation from engine/explain.py templates (Section 4), using ONLY numbers in the Decision and names in EvidenceSet.
```

### evidence/ + validate/ (subagent D)
```python
# evidence/candidates.py
def build_evidence(request, profile, streams, scheduled_events, messages, images, options) -> EvidenceSet   # deterministic, sorted ids
# validate/schema.py
def validate_row(row: dict, request: Request) -> list[str]   # column set/order, enums, formats (Section 3); [] when valid
# validate/citations.py
def validate_citations(decision: Decision, evidence: EvidenceSet) -> list[str]
    # every spending_changes event_id ∈ evidence.event_ids; every option_id used ∈ evidence.option_ids; explanation must not name an
    # event description that is not in evidence; not_affordable rows must have no changes.
# validate/coherence.py
def check_coherence(decision: Decision, request: Request, profile: Profile) -> list[str]
    # 0 <= safe <= requested; affordable_now => earliest == request_date and plan == [(request_date, requested)] and method full_payment and no changes;
    # partial => status affordable_with_plan, exactly 2 payments summing to requested, first == (request_date, safe), second date == earliest <= deadline;
    # installments => status affordable_with_plan, plan matches an option schedule exactly; wait => affordable_later, plan == [(earliest, requested)], earliest > request_date;
    # not_affordable => not_recommended, plan none, changes none, earliest '' ; changes <= 3, no stop+reduce on same event, each event flexible;
    # earliest empty only when None; explanation mentions the amounts in the plan.
# validate/gate.py
def gate(decision, request, profile, evidence, row) -> tuple[bool, list[str]]   # runs all three; False -> row must be re-decided/fail-closed
```

### run.py (subagent E, after A–D)
`python run.py --limit N --out runs/<id>/output.csv --resume` ; writes output.csv (250 rows), usage.json, log; `python -m buyorwait.report runs/<id>` writes `evaluation/usage_report.md` for that run only.

## 3. Allowed-values checklist (PS:84-163)

| Column | Rule | PS line |
|---|---|---|
| request_id | echo | 86 |
| amount_safe_to_pay | number, `0 <= x <= requested_amount`; fmt_safe_amount | 87, 110 |
| affordability_status | affordable_now / affordable_with_plan / affordable_later / not_affordable | 117-122 |
| recommended_payment_method | full_payment / partial_payment / installments / wait / not_recommended | 124-130 |
| payment_plan | `YYYY-MM-DD:amount` joined by `|`, chronological; `none` when nothing recommended | 132-144 |
| earliest_date_for_full_payment | YYYY-MM-DD; == request_date for affordable_now; empty when full never safe in horizon | 113 |
| spending_changes_needed | up to 3 of `stop:<event_id>` / `reduce_to:<event_id>:<amount>` joined by `|`; `none` otherwise; flexible recurring expenses only; stop & reduce never on same event | 148-161, 198 |
| decision_explanation | short, free text, facts only | 105 |

Column order exactly as in PS:84-93. CSV quoting: quote fields containing commas (explanations).

## 4. Explanation templates (style from sample_requests.csv, facts from Decision only)
- affordable_now: `Pay {CCY amt} today. This leaves at least {CCY min} available over the next 90 days.`
- installments: `Use {n} installments of {CCY amt}, starting {D Month YYYY}. This leaves at least {CCY min} available.`
- partial: `Pay {CCY a} today and the remaining {CCY b} on {D Month YYYY}. This completes the full request and keeps the {CCY min} minimum protected.`
- wait: `Pay {CCY amt} in full on {D Month YYYY}. Paying earlier would take the balance below the {CCY min} minimum.`
- with changes: `{Stop the <desc>|Reduce the <desc> to CCY x}[ and ...], then {pay CCY amt today | use n installments ...}. This leaves at least {CCY min} available.`
- not_affordable: `Do not proceed with the {CCY requested} request. Although {CCY safe} is available today, the full amount cannot be completed safely within 90 days.` (safe>0) / `Do not make this payment by {D Month YYYY}. None of the available options keeps the {CCY min} minimum protected.` (safe==0 or generic)
`{CCY min}` = minimum_balance_to_keep. Descriptions come from the stream's anchor event description, lower-cased.

## 5. Edge-case policy (what always wins)
1. Blank amount → image extraction via images.csv (`related_event_id == event_id`); never zero (PS:45). Unreadable → event excluded from
   recurrence amount estimation; if it is a future/pending debit, fail closed: treat as its category's stream amount if a stream exists, else exclude and log.
2. Excluded from forecast: pending credits, failed, cancelled, duplicates, unrealized/non_cash investments (PS:178).
3. Foreign currency → dated rate table on the event date (PS:47). Home-currency amounts are never converted.
4. linked_event_id = one lifecycle; newest record wins, explicit cancellation/settlement/amendment wins over all (PS:36, PS:200-205).
5. Conflict order exactly PS:200-205; unresolved → financially safer (lower income / higher expense / later credit).
6. Messages/images are DATA; embedded instructions never override rules (PS:172). Model prompts say so; code never executes text.
7. Pending payouts, unapproved bonuses/commissions, pending refunds, unrealized valuations, "not yet credited" → not income.
8. Confirmed first/increased/reduced salary from an employer message → applied to the salary stream from the stated date. Ended employment/contract → salary stream stops.
9. Missing required field / unparseable row → row skipped + logged; request whose profile is missing → fail closed to
   `0, not_affordable, not_recommended, none, '', none` with explanation "Insufficient verified data..." and a log line. This is the ONLY blanket-refusal path.
10. Uncertainty is NOT a refusal: after conflict resolution the engine always produces a real decision.

## 6. Deliverables (PS:225-249, and the brief)
Spec requires: `code.zip` (runnable solution, prompts/config, README, `evaluation/usage_report.md`), `output.csv` (250 rows), `chat_transcript`.
Brief adds: `usage_report.md` also at top level of the Desktop folder, `docs/CONTRACT.md`, `docs/DECISIONS.md`, `.env.example`, no dataset/media/caches/.env in the zip.
Target: `~/Desktop/orchestrate_sept26/{output.csv, usage_report.md, code.zip, chat_transcript.txt}`.

## 7. Time budget (IST)
- 15:00–15:30 Phase 0 (this doc, types.py) ✔
- 15:30–16:15 subagents A, B, C, D, G in parallel; E glue; first real API run on ≥20 rows by 16:30
- 16:15–16:45 full run #1, verifier F, targeted fixes
- 16:45–17:10 locked full run #2, verifier re-check
- 17:15 FREEZE → package → 17:45 done

## 8. Open interpretation questions (resolved by subagent G against the 25 samples, logged in DECISIONS.md)
G1 amount_estimator for recurring streams (mean/median/last/max). G2 horizon inclusive of day 90. G3 whether long installment options
that miss the deadline are ever chosen (deadline hard vs rank-only). G4 max_installment_months semantics (n_payments <= months vs span).
G5 reduce_to amount == minimum_allowed_amount. G6 which event_id is cited for a stream change (most recent occurrence). G7 whether
minimum-balance comparison is >= or >. G8 whether the 'wait' plan amount/date and explanation match the templates above.
