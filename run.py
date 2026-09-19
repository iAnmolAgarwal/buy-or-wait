#!/usr/bin/env python3
"""run.py — CLI entry point for the Buy-or-Wait pipeline.

    python run.py --root dataset --out runs/smoke20 --limit 20 --workers 4
    python run.py --ids request_26,request_27 --resume
    python -m buyorwait.report runs/smoke20
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

from dotenv import load_dotenv

from buyorwait.engine.policy import Policy
from buyorwait.pipeline import run_pipeline


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run.py", description="Buy-or-Wait pipeline")
    p.add_argument("--root", default="dataset", help="dataset directory")
    p.add_argument("--out", default=None,
                   help="output directory (default runs/<YYYYmmdd-HHMMSS>)")
    p.add_argument("--limit", type=int, default=None,
                   help="process only the first N requests in requests.csv order")
    p.add_argument("--ids", default=None,
                   help="comma-separated request ids, e.g. request_26,request_27")
    p.add_argument("--resume", action="store_true",
                   help="skip request ids already present in out/decisions.jsonl")
    p.add_argument("--workers", type=int, default=4, help="thread pool size")
    p.add_argument("--model", default="claude-sonnet-5", help="perception model id")
    p.add_argument("--estimator", default="mean",
                   choices=["mean", "median", "last", "max"],
                   help="recurring-stream amount estimator")
    p.add_argument("--cache-dir", default="cache", help="response cache directory")
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()  # ANTHROPIC_API_KEY; never printed
    args = build_parser().parse_args(argv)

    out = args.out or os.path.join("runs", datetime.now().strftime("%Y%m%d-%H%M%S"))
    ids = [i for i in (args.ids or "").split(",") if i.strip()] or None

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("warning: ANTHROPIC_API_KEY is not set; perception will fall back.",
              file=sys.stderr)

    summary = run_pipeline(
        root=args.root,
        out_dir=out,
        limit=args.limit,
        ids=ids,
        resume=args.resume,
        workers=args.workers,
        model=args.model,
        policy=Policy(amount_estimator=args.estimator),
        cache_dir=args.cache_dir,
    )

    print(
        f"run {summary.run_id}: {summary.requests_processed} processed, "
        f"{summary.dataset_rows} rows in {summary.wall_seconds:.1f}s | "
        f"gate_failures={summary.gate_failures} refusals={summary.refusal_rows} "
        f"fallbacks={summary.fallbacks}\n"
        f"  {out}/output.csv  {out}/decisions.jsonl  {out}/usage.json  {out}/log.txt",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
