# Relay

Relay is a learning project: a backend service where an AI proposes a Postgres schema change, a human approves it over a REST API, and a pool of workers executes it.

**Status: work in progress (milestone M3 of 5).** The API can create, read, list, approve, and reject migration records, and two worker processes draft a SQL proposal for each new record using a fake, deterministic LLM. There is no execution yet: no SQL is ever run against the target database, and an approved migration stays approved. Each claim below is added only once a test demonstrates it.

## What is tested so far

- **Concurrent approvals:** when 20 approvals of the same migration are made at the same moment, exactly one succeeds, the other 19 are refused as conflicts, and exactly one approval event is recorded (`tests/test_concurrent_approval.py`). This test drives the decision code from 20 database connections; it does not send 20 HTTP requests.
- **Approve or reject, never both:** concurrent approvals and rejections of one migration produce exactly one decision.
- **Decisions are bound to the SQL:** approve and reject must carry the hash of the proposal the reviewer saw; a different hash is refused with `409` (`tests/test_approve_reject.py`).
- **A job is claimed by one worker at a time:** with 4 workers draining 50 queued jobs at once, every job is claimed exactly once, drafted exactly once, and reaches the approval stage (`tests/test_claim.py`, `tests/test_worker_contention.py`). These tests run the 4 workers as threads with separate database connections.
- **A crashed worker's job is finished by another:** a real worker process is killed right after claiming a job; once its lease expires, a second worker reclaims the job and completes the draft (`tests/test_crash_generation.py`).
- **A worker that lost its job cannot overwrite the new owner:** after its lease is reclaimed, the old worker's save changes nothing (`tests/test_stale_worker.py`).
- **Only allow-listed SQL becomes a proposal:** the policy accepts only "add one nullable column, with no default, of a plain type, to a demo table", checked on the real Postgres parse tree; anything else marks the job failed (`tests/test_policy.py`, `tests/test_generation.py`).
- **A job that never succeeds stops:** after the retry limit it is marked failed instead of being retried forever (`tests/test_retry_limit.py`).

Not yet true: a retried approve is not recognized as a retry (it gets `409`); idempotency keys arrive in M5.

## Running it locally

Requires Docker.

```sh
cp .env.example .env        # then edit the values
docker compose up -d --build
curl http://localhost:8000/health
```

`docker compose up` starts Postgres (which holds both `relay_db` and `target_db`), a one-shot `dbinit` that creates what is missing, the API on port 8000, and two workers. Interactive API docs are at http://localhost:8000/docs.

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
