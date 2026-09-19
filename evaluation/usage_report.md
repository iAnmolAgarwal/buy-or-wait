# Usage Report — Buy or Wait

This report describes the final full-dataset run that produced output.csv (run id `final3`). Every number below comes from `final3/usage.json` and the `output.csv` written beside it; no other run contributes to it.

## Run

| | |
|---|---|
| Run id | `final3` |
| Started | 2026-09-13T11:14:33+00:00 |
| Finished | 2026-09-13T11:16:08+00:00 |
| Wall time | 94.5 s (1.6 min) |
| Requests processed | 250 |
| Dataset rows in output.csv | 250 |
| Model provider | Anthropic |
| Model(s) | `claude-sonnet-5` |
| Code fingerprint (sha256 of `buyorwait/**/*.py`) | `3d448f54d0e643d46f8e1149c5d4bd589e4e09e52f700c65105d72977fa3572b` |

## Model usage

| Model | Calls | Input tokens | Output tokens | Cache hits | Price /MTok (in / out) | Cost |
|---|---:|---:|---:|---:|---:|---:|
| `claude-sonnet-5` | 224 | 1,505,499 | 45,217 | 0 | $2.00 / $10.00 | $3.46 |
| **Total** | **224** | **1,505,499** | **45,217** | **0** | | **$3.46** |

- Total tokens: **1,550,716** (1,505,499 in + 45,217 out)
- Average tokens per request: **6,203**
- Estimated total cost: **$3.46**
- Estimated cost per request: **$0.0139**
- Requests that made a live call: **200**; served from cache: **0** (cache file hits 0, misses 209)

Prices are Anthropic first-party API list prices per million tokens (see `PRICES` in `buyorwait/report.py`). Cost is an estimate: it prices input and output tokens only and does not model cache-write discounts.

## Reliability

| | |
|---|---:|
| Perception fallbacks | 0 |
| Gate failures (re-decided) | 0 |
| Refusal rows | 0 |
| Loader row errors | 0 |

## Output spread

From `output.csv` (250 rows).

| affordability_status | rows | share |
|---|---:|---:|
| affordable_with_plan | 76 | 30.4% |
| affordable_later | 75 | 30.0% |
| affordable_now | 62 | 24.8% |
| not_affordable | 37 | 14.8% |

| recommended_payment_method | rows | share |
|---|---:|---:|
| wait | 69 | 27.6% |
| full_payment | 67 | 26.8% |
| installments | 61 | 24.4% |
| not_recommended | 43 | 17.2% |
| partial_payment | 10 | 4.0% |

