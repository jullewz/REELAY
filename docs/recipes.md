# Recipes

## Front-end development without a backend

```bash
reelay serve --mode hybrid --target https://staging.example.com --tape tapes/staging.tape --cors
```

Point the dev server's API base URL at `http://127.0.0.1:8080`. The first visit records, everything after works on
a plane.

## Deterministic CI for an API client

```python
@pytest.mark.reelay(tape="tests/tapes/payments.tape", target="https://api.payments.example")
def test_refund_flow(reelay):
    client = PaymentsClient(base_url=reelay.url, token="anything")
    ...
```

Record locally with `pytest --reelay-record`, commit the tape, and CI never needs credentials.

## Testing a polling loop

Record a job that reports `pending`, `pending`, `done`. The default `sequential` strategy replays that exact sequence
and then keeps answering `done`. Reset with the dashboard's *Reset sequence* button or
`curl -X POST -H 'X-Reelay-Control: 1' http://127.0.0.1:8080/__reelay/api/reset`.

## Resilience testing

```toml
[chaos]
enabled = true
seed = 1

[[chaos.rule]]
route = "POST /checkout"
error_rate = 0.3
error_status = 503

[[chaos.rule]]
route = "GET /catalog/**"
latency = "normal(800ms, 200ms)"
bandwidth = "32KiB/s"
```

Same seed, same faults, so a failure found once is reproducible.

## Catching breaking API changes in CI

```bash
reelay verify tests/tapes/public-api.tape --target https://staging.example.com \
  --ignore '$..updated_at' --ignore '$..request_id' -f markdown -o drift.md
```

Exit code `1` fails the job, and `drift.md` makes a ready-to-post review comment.

## Bootstrapping API documentation

```bash
reelay har-import exported-from-devtools.har -o tapes/app.tape
reelay infer tapes/app.tape -o openapi.yaml --title "Internal API"
```
