# Relay

Relay is a learning project: a backend service where an AI proposes a Postgres schema change, a human approves it over a REST API, and a pool of workers executes it.

**Status: work in progress (milestone M4 of 5).** The API can create, read, list, approve, and reject migration records. Two worker processes draft a SQL proposal for each new record using a fake, deterministic LLM, and run each approved proposal against a separate, disposable target database. Each claim below is added only once a test demonstrates it.

## What is tested so far

- **Concurrent approvals:** when 20 approvals of the same migration are made at the same moment, exactly one succeeds, the other 19 are refused as conflicts, and exactly one approval event is recorded (`tests/test_concurrent_approval.py`). This test drives the decision code from 20 database connections; it does not send 20 HTTP requests.
- **Approve or reject, never both:** concurrent approvals and rejections of one migration produce exactly one decision.
- **Decisions are bound to the SQL:** approve and reject must carry the hash of the proposal the reviewer saw; a different hash is refused with `409` (`tests/test_approve_reject.py`).
- **A job is claimed by one worker at a time:** with 4 workers draining 50 queued jobs at once, every job is claimed exactly once, drafted exactly once, and reaches the approval stage (`tests/test_claim.py`, `tests/test_worker_contention.py`). These tests run the 4 workers as threads with separate database connections.
- **A crashed worker's job is finished by another:** a real worker process is killed right after claiming a job; once its lease expires, a second worker reclaims the job and completes the draft (`tests/test_crash_generation.py`).
- **A worker that lost its job cannot overwrite the new owner:** after its lease is reclaimed, the old worker's save changes nothing, whether the new owner has finished or is still working, for drafts and for executions (`tests/test_stale_worker.py`).
- **Reclaiming an expired lease is safe under contention:** two workers reclaiming the same job at the same moment produce one reclaim, and a reclaim racing the slow owner's final save ends with exactly one of the two taking effect (`tests/test_reclaim_races.py`).
- **Only allow-listed SQL becomes a proposal:** the policy accepts only "add one nullable column, with no default, of a plain type, to a demo table", checked on the real Postgres parse tree; anything else marks the job failed (`tests/test_policy.py`, `tests/test_generation.py`).
- **A job that never succeeds stops:** after the retry limit it is marked failed instead of being retried forever. For an execution, the worker first checks whether the migration was in fact applied, and records success if it was. If an earlier attempt is still in the middle of applying it, the check waits for that attempt instead of deciding around it (`tests/test_retry_limit.py`).
- **A migration's SQL takes effect on the target once:** the worker records a marker row on the target in the same transaction as the schema change, before the change. With 4 workers running 50 approved migrations at once, all 50 succeed with exactly one marker each (`tests/test_worker_contention.py`; the 4 workers are threads with separate database connections). When two workers run the same migration at the same moment, the second waits and then skips it (`tests/test_marker_contention.py`). This is at-least-once delivery with once-only effects on the target, for the tested workflow.
- **A worker that dies after changing the target does not cause a second run:** a real worker process is killed right after the target commit, before it can tell Relay. The next worker finds the marker, skips the SQL, and records success (`tests/test_crash_after_target_commit.py`).
- **A failed schema change leaves nothing behind:** when the target rejects the SQL, the schema is unchanged, no marker remains, and the job is marked failed with the database's error (`tests/test_execute.py`).
- **Only the approved SQL runs:** if the stored SQL no longer matches the approved hash, or fails the policy on the second check, the job is marked failed and the target is not touched. A marker with a different hash never counts as success (`tests/test_execute.py`).
- **A lost target connection is not reported as a failure:** the job stays claimed and is retried later, because the worker cannot know whether the target committed (`tests/test_execute.py`). This includes a connection cut during the commit itself, in both directions: if the target did commit, the next worker finds the marker and records success without running the SQL again; if it did not, the next worker applies it (`tests/test_commit_cut.py`).
- **The target timeouts take effect:** a schema change that waits too long for a lock, or runs too long, is marked failed and leaves nothing behind (it is not retried automatically). Waiting too long behind another worker's attempt at the same migration is not a failure: the job is retried. A worker that stalls inside its target transaction has that transaction ended by the target, and the job is retried (`tests/test_timeouts_fire.py`).
- **A frozen process cannot block the others indefinitely:** if a worker or request freezes in the middle of one of Relay's own short database transactions, the database ends that session after a timeout and whoever was waiting behind it carries on. A request that waits too long for a migration another request is changing gets `503` with `Retry-After` and changes nothing; a worker in the same position gives the job up to be retried. A healthy worker waiting on slow work is not affected (`tests/test_relay_timeouts.py`).
- **The target login has limited rights:** it is not a superuser, cannot create tables, cannot change or delete markers, and cannot connect to Relay's own database (`tests/test_target_role.py`).
- **A worker refuses to start with a lease that is too short:** the lease must be longer than the target lock timeout plus the statement timeout plus a 10-second margin (`tests/test_config.py`).

Not yet true: a retried approve is not recognized as a retry (it gets `409`); idempotency keys arrive in M5.

## What is not guaranteed

- **A migration marked failed may still be applied later.** A worker that freezes just before its target transaction, long enough for its lease to expire and for another worker to mark the job failed, can still apply the SQL when it wakes up. The lease checks make this unlikely; they do not make it impossible.
- **No atomic commit across the two databases.** The target and Relay commit separately. In between, the target can already be changed while Relay still says the job is executing; the marker is what lets the next worker settle it.
- **No safety for SQL that the policy allows but is still wrong.** The policy limits the kind of statement, not whether it is a good idea.
- **`X-User` is not authentication.** It is an unverified label for the audit log.

## Running it locally

Requires Docker.

```sh
cp .env.example .env        # then edit the values
docker compose up -d --build
curl http://localhost:8000/health
```

`.env` needs three values: `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `TARGET_MIGRATOR_PASSWORD` (the password of the limited role the workers use on the target).

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

## Watching a crash recovery

```sh
docker compose run --rm tests python -m scripts.demo_crash
```

The script approves one migration, freezes a worker right after it has changed the target, kills it, and shows a second worker finishing the job without running the SQL again. It takes about 20 seconds (it waits out a real 15-second lease) and uses its own databases (`relay_demo`, `target_demo`).

On Windows, run Docker commands from PowerShell. Git Bash's `/c/...` paths make `docker compose build` fail with "additional privileges requested".
