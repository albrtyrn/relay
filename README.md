# Relay

Relay is a learning project: a backend service where an AI proposes a Postgres schema change, a human approves it over a REST API, and a pool of workers executes it.

**Status: work in progress (milestone M2 of 5).** The API can create, read, list, approve, and reject migration records. There is no worker and no execution yet, so nothing reaches the approval stage on its own and no SQL is ever run. Each claim below is added only once a test demonstrates it.

## What is tested so far

- **Concurrent approvals:** when 20 approvals of the same migration are made at the same moment, exactly one succeeds, the other 19 are refused as conflicts, and exactly one approval event is recorded (`tests/test_concurrent_approval.py`). This test drives the decision code from 20 database connections; it does not send 20 HTTP requests.
- **Approve or reject, never both:** concurrent approvals and rejections of one migration produce exactly one decision.
- **Decisions are bound to the SQL:** approve and reject must carry the hash of the proposal the reviewer saw; a different hash is refused with `409` (`tests/test_approve_reject.py`).

Not yet true: a retried approve is not recognized as a retry (it gets `409`); idempotency keys arrive in M5.

## Running it locally

Requires Docker.

```sh
cp .env.example .env        # then edit the values
docker compose up -d --build
curl http://localhost:8000/health
```

`docker compose up` starts Postgres (which holds both `relay_db` and `target_db`), a one-shot `dbinit` that creates what is missing, the API on port 8000, and a placeholder worker. Interactive API docs are at http://localhost:8000/docs.

Open a SQL shell on the Relay database:

```sh
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d relay_db'
```

Stop it with `docker compose down`. Your data is kept in a named volume. Use `docker compose down -v` to delete it.

## Running the tests

```sh
docker compose run --rm tests pytest
```

The tests use real Postgres in their own databases (`relay_test`, `target_test`) on the same server, so they never touch the data above.

On Windows, run Docker commands from PowerShell. Git Bash's `/c/...` paths make `docker compose build` fail with "additional privileges requested".
