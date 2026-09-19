# dataset/ — not included

The data for this task was provided by HackerRank for the contest. I don't have
redistribution rights for it, so it isn't in this repo. This directory only records
what the code expects to find here.

Everything else runs without it: the ingest, engine, evidence, validate and perception
tests use the synthetic fixtures in `fixtures/`, and the tests that do need real data
skip themselves (`tests/conftest.py`).

```
dataset/
  requests.csv                  request_id, user_id, request_date, request_type,
                                requested_amount, desired_completion_date,
                                allows_partial_payment, request_text
  sample_requests.csv           the same columns plus the seven labelled output columns
  financial_profiles.csv        user_id, home_currency, current_available_balance,
                                minimum_balance_to_keep, financial_priorities,
                                expense_categories_to_protect,
                                expense_categories_user_is_willing_to_reduce,
                                expense_categories_user_is_willing_to_stop,
                                payment_methods_user_will_consider, max_installment_months
  financial_events.csv          event_id, user_id, event_type, description, category,
                                direction, amount, currency, event_date, settlement_date,
                                status, linked_event_id, flexibility,
                                minimum_allowed_amount
  request_payment_options.csv   payment_option_id, request_id, payment_method,
                                payment_amount, number_of_payments, first_payment_date,
                                payment_frequency_days, financing_fee,
                                total_payable_amount
  messages.csv                  message_id, user_id, request_id, related_event_id,
                                sent_at, source_type, message_text
  images.csv                    image_id, user_id, request_id, related_event_id
  exchange_rates.csv            rate_date, from_currency, to_currency, rate
  media/images/<image_id>.png   receipts, payslips and bills for events whose amount
                                is blank in financial_events.csv
```

`fixtures/ingest/` mirrors this layout with a handful of hand-written rows, so
`python run.py --root fixtures/ingest` is a working shape check for the loader.
