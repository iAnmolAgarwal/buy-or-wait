# How this was built

The hackathon ran for 24 hours; this build took 2h22m of session turns on 13 September 2026.
I worked as the orchestrator: I wrote the contract first, then handed narrow briefs to eight
Claude subagents and reviewed everything that came back.

**Contract first.** `docs/CONTRACT.md` and `buyorwait/types.py` were written before any
implementation. They fix the module boundaries, the function signatures and the record types.
A diff that drifted from the contract got rejected rather than merged.

**Eight subagents, narrow briefs.** Each brief said which files the agent could touch, which
signatures it had to implement, what the acceptance criteria were, and asked for a diff plus a
first-person diagnosis of what it was unsure about. The split was: ingest, engine, perception,
evidence + validate, the `run.py` glue, a verifier, a spec probe, and the README.

**An independent verifier.** One agent was kept away from the implementation entirely and
re-derived rows from the raw data with its own script, so a disagreement meant something. It
found one bug that I audited and falsified (its script was missing the currency conversion) —
recorded as D29/D31 in `docs/DECISIONS.md`.

**A decision ledger instead of vibes.** Every interpretation call is numbered and timestamped in
`docs/DECISIONS.md`, with the reason and the experiment that would confirm or kill it. Some are
marked superseded; that revision trail is left in on purpose.

**The test suite after every merge.** Nothing merged on a red suite. The code was frozen three
times and reopened twice, each time because an external review pass found something real.

Model roles: `claude-sonnet-5` for the perception layer at inference time, Claude Opus 5 for the
subagents, and Claude Fable 5.1 as the orchestrator session. The subagent transcripts are not
published here.
