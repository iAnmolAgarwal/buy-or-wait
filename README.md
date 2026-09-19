# Buy or Wait?

An affordability agent that decides whether someone can safely afford a purchase now, on a plan,
later, or not at all — where the model reads the documents and the code makes the decision.

**#6 of 3,062 globally — HackerRank Orchestrate, September 2026** (24-hour hackathon).

## The problem

Each request is a person asking whether they can afford something: a purchase amount, a date they
want it done by, and their financial history. The history is messy — recurring salary and expenses
of different cadences, five currencies, pending and cancelled and duplicated transactions, bank and
employer messages that change the picture, and events whose amount is blank because it only exists
on a receipt image. The answer has to be a 90-day cash-flow forecast: a plan is safe only if the
balance never falls below the minimum the user wants to keep, and the payment finishes by their
deadline. The output is one row per request — a safe amount, a status, a payment method, a plan, the
earliest date full payment becomes safe, spending changes, and an explanation.

## How it works

The model never emits a number that reaches the output. It returns two kinds of typed record: an
`ImageExtraction` (the amount on a receipt or payslip) and an `Amendment` (a salary change, a
cancelled charge — an enum plus fields). Everything else — the forecast, the safe amount, the plan
ranking, the status, the dates — is arithmetic in `buyorwait/engine/`.

```
dataset  ->  ingest  ->  perception (model)  ->  engine  ->  evidence  ->  validate  ->  output.csv
              |            |                      |           |            |
      typed records,   forced tool use,     90-day balance   citable    schema +
      FX, lifecycle    bounded 3-step       path, solver,    facts      citations +
      resolution       tool loop            plans, decide    built      coherence
                                                             in code    behind one gate
```

Consequences of that split:

- Structured output is forced, not requested: `tool_choice={"type": "tool", "name": "emit_result"}`,
  and the payload is re-validated against the caller's schema in code. A mismatch raises
  `SchemaMismatch` and is retried.
- Message and image content is treated as untrusted data in both prompts. The structural backstop is
  stronger than the prompt: even a fully compromised model can only return an extracted amount or an
  amendment enum. It cannot emit a status, a plan or a safe amount.
- Three validators (schema, citations, coherence) sit behind one gate. Every `event_id` cited in an
  explanation must exist in an evidence set that was built in code before the decision. A gate
  failure re-decides once with every stream forced to fixed; if it still fails, the row becomes a
  logged refusal rather than a guess.
- Responses are cached on `sha256(request_id + canonical_json(payload))`, so the same payload under
  two request ids can never cross-hit.

## Results of the final run

The locked run that produced the submission, against an empty cache so every call was live:

| | |
|---|---|
| Requests processed | 250 / 250 rows written |
| Model | `claude-sonnet-5`, single model |
| Cost | $3.46 total, $0.0139 per request |
| Fallbacks | 0 |

Full numbers in [`evaluation/usage_report.md`](evaluation/usage_report.md).

## How it was built

Contract first, then eight Claude subagents working to narrow briefs, plus an independent verifier
kept away from the implementation. 2h22m of session turns. The interpretation calls are numbered and
timestamped in [`docs/DECISIONS.md`](docs/DECISIONS.md), superseded ones included; the process is in
[`docs/WORKFLOW.md`](docs/WORKFLOW.md).

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then put your own ANTHROPIC_API_KEY in it

python -m pytest -q           # no network, no API key needed
python run.py --root dataset --out runs/demo --limit 20 --workers 4
python -m buyorwait.report runs/demo
```

`python -m pytest -q` gives 233 passed, 32 skipped on a checkout without the contest data. The
skipped ones are the tests that read `dataset/` and one live API smoke test behind `LIVE=1`.

`run.py` needs the data (see below). `--root fixtures/ingest` runs the pipeline over the synthetic
fixtures instead, which exercises the shape but not the real task.

## Repo layout

```
buyorwait/
  ingest/      loading, FX, event lifecycle, recurring-stream detection
  perception/  the model layer: prompts, forced tool use, tool loop, cache, fallback
  engine/      forecast, solver, plan enumeration and ranking, decide, explain
  evidence/    the citable fact set, built in code before the decision
  validate/    schema, citations, coherence — behind one gate
docs/          CONTRACT.md (written before the code), DECISIONS.md, WORKFLOW.md
tests/         unit, integration and Hypothesis property tests
fixtures/      synthetic data mirroring the real layout
evaluation/    usage report for the final run
```

## Notes on data

The contest data — financial events, profiles, messages, receipt and payslip images, FX rates — was
provided by HackerRank. I don't have redistribution rights, so it isn't here, and neither is the
problem statement. [`dataset/README.md`](dataset/README.md) records the layout and columns the code
expects.

## License

MIT — see [LICENSE](LICENSE).
