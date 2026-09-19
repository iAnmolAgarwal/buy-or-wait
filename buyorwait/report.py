"""buyorwait/report.py — render `runs/<id>/usage_report.md` from that run's usage.json.

    python -m buyorwait.report runs/smoke20

The report is built from usage.json (and the output.csv sitting beside it) and
from NOTHING else: no other run's numbers ever leak in.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from collections import Counter
from typing import Optional

# Per-million-token list prices, Anthropic first-party API.
# Source: the bundled `claude-api` skill's "Current Models" table
# (cached 2026-06-24; https://www.anthropic.com/pricing#api).
#   Claude Sonnet 5   $2.00 in / $10.00 out per 1M tokens
#   Claude Opus 5     $5.00 in / $25.00 out per 1M tokens
#   Claude Haiku 4.5  $1.00 in / $5.00  out per 1M tokens
PRICES: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
DEFAULT_PRICE = (2.00, 10.00)


def price_for(model: str) -> tuple[float, float]:
    return PRICES.get(model, DEFAULT_PRICE)


def _fmt_money(x: float) -> str:
    return f"${x:,.4f}" if x and abs(x) < 1 else f"${x:,.2f}"


def _read_output_spread(path: str) -> tuple[int, Counter, Counter]:
    if not os.path.isfile(path):
        return 0, Counter(), Counter()
    with open(path, "r", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return (len(rows),
            Counter(r.get("affordability_status", "") for r in rows),
            Counter(r.get("recommended_payment_method", "") for r in rows))


def render(usage: dict, run_dir: str) -> str:
    run_id = usage.get("run_id") or os.path.basename(os.path.normpath(run_dir))
    per_model: dict = usage.get("per_model") or {}
    processed = int(usage.get("requests_processed") or 0)

    total_calls = sum(int(m.get("calls", 0)) for m in per_model.values())
    total_in = sum(int(m.get("input_tokens", 0)) for m in per_model.values())
    total_out = sum(int(m.get("output_tokens", 0)) for m in per_model.values())
    total_hits = sum(int(m.get("cache_hits", 0)) for m in per_model.values())
    total_tokens = total_in + total_out

    cost = 0.0
    model_lines = []
    for name in sorted(per_model):
        m = per_model[name]
        p_in, p_out = price_for(name)
        c = (int(m.get("input_tokens", 0)) / 1e6) * p_in + \
            (int(m.get("output_tokens", 0)) / 1e6) * p_out
        cost += c
        model_lines.append(
            f"| `{name}` | {int(m.get('calls', 0)):,} | {int(m.get('input_tokens', 0)):,} "
            f"| {int(m.get('output_tokens', 0)):,} | {int(m.get('cache_hits', 0)):,} "
            f"| ${p_in:.2f} / ${p_out:.2f} | {_fmt_money(c)} |")
    if not model_lines:
        model_lines.append("| _(no model calls — everything served from cache)_ "
                           "| 0 | 0 | 0 | 0 | – | $0.00 |")

    csv_path = usage.get("output_csv") or os.path.join(run_dir, "output.csv")
    if not os.path.isabs(csv_path) and not os.path.isfile(csv_path):
        csv_path = os.path.join(run_dir, "output.csv")
    n_rows, statuses, methods = _read_output_spread(csv_path)

    per_req_tokens = (total_tokens / processed) if processed else 0.0
    per_req_cost = (cost / processed) if processed else 0.0
    wall = float(usage.get("wall_seconds") or 0.0)

    out: list[str] = []
    A = out.append
    A("# Usage Report — Buy or Wait")
    A("")
    A(f"This report describes the final full-dataset run that produced output.csv "
      f"(run id `{run_id}`). Every number below comes from `{run_id}/usage.json` and the "
      f"`output.csv` written beside it; no other run contributes to it.")
    A("")
    A("## Run")
    A("")
    A("| | |")
    A("|---|---|")
    A(f"| Run id | `{run_id}` |")
    A(f"| Started | {usage.get('started_at', '—')} |")
    A(f"| Finished | {usage.get('finished_at', '—')} |")
    A(f"| Wall time | {wall:.1f} s ({wall / 60:.1f} min) |")
    A(f"| Requests processed | {processed} |")
    A(f"| Dataset rows in output.csv | {n_rows} |")
    A(f"| Model provider | {usage.get('provider', 'Anthropic')} |")
    names = ", ".join(f"`{m}`" for m in sorted(per_model)) or f"`{usage.get('model', '-')}`"
    A(f"| Model(s) | {names} |")
    A(f"| Code fingerprint (sha256 of `buyorwait/**/*.py`) | `{usage.get('code_fingerprint', '—')}` |")
    A("")
    A("## Model usage")
    A("")
    A("| Model | Calls | Input tokens | Output tokens | Cache hits | Price /MTok (in / out) | Cost |")
    A("|---|---:|---:|---:|---:|---:|---:|")
    out.extend(model_lines)
    A(f"| **Total** | **{total_calls:,}** | **{total_in:,}** | **{total_out:,}** "
      f"| **{total_hits:,}** | | **{_fmt_money(cost)}** |")
    A("")
    A(f"- Total tokens: **{total_tokens:,}** ({total_in:,} in + {total_out:,} out)")
    A(f"- Average tokens per request: **{per_req_tokens:,.0f}**")
    A(f"- Estimated total cost: **{_fmt_money(cost)}**")
    A(f"- Estimated cost per request: **{_fmt_money(per_req_cost)}**")
    A(f"- Requests that made a live call: **{usage.get('requests_with_live_calls', 0)}**; "
      f"served from cache: **{usage.get('requests_served_from_cache', 0)}** "
      f"(cache file hits {usage.get('cache_hits', 0)}, misses {usage.get('cache_misses', 0)})")
    A("")
    A("Prices are Anthropic first-party API list prices per million tokens "
      "(see `PRICES` in `buyorwait/report.py`). Cost is an estimate: it prices "
      "input and output tokens only and does not model cache-write discounts.")
    A("")
    A("## Reliability")
    A("")
    A("| | |")
    A("|---|---:|")
    A(f"| Perception fallbacks | {usage.get('fallbacks', 0)} |")
    A(f"| Gate failures (re-decided) | {usage.get('gate_failures', 0)} |")
    A(f"| Refusal rows | {usage.get('refusal_rows', 0)} |")
    A(f"| Loader row errors | {usage.get('load_errors', 0)} |")
    reasons = usage.get("fallback_reasons") or []
    if reasons:
        A("")
        A("Fallback reasons:")
        for r in reasons:
            A(f"- {r}")
    A("")
    A("## Output spread")
    A("")
    A(f"From `{os.path.basename(csv_path)}` ({n_rows} rows).")
    A("")
    A("| affordability_status | rows | share |")
    A("|---|---:|---:|")
    for name, count in statuses.most_common():
        A(f"| {name or '(blank)'} | {count} | {100 * count / n_rows:.1f}% |"
          if n_rows else f"| {name or '(blank)'} | {count} | – |")
    A("")
    A("| recommended_payment_method | rows | share |")
    A("|---|---:|---:|")
    for name, count in methods.most_common():
        A(f"| {name or '(blank)'} | {count} | {100 * count / n_rows:.1f}% |"
          if n_rows else f"| {name or '(blank)'} | {count} | – |")
    A("")
    return "\n".join(out) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m buyorwait.report runs/<id>", file=sys.stderr)
        return 2
    run_dir = argv[0]
    usage_path = os.path.join(run_dir, "usage.json")
    if not os.path.isfile(usage_path):
        print(f"no usage.json in {run_dir}", file=sys.stderr)
        return 1
    with open(usage_path, "r", encoding="utf-8") as fh:
        usage = json.load(fh)
    text = render(usage, run_dir)
    out_path = os.path.join(run_dir, "usage_report.md")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
