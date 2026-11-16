# Relay: progress

One entry per milestone. Scope and the exact exit tests are in DESIGN.md §8; this file records status only. A milestone is done when all its tests pass via `docker compose run --rm tests pytest`.

Status values: `not started`, `in progress`, `done`.

## Current state (updated 2026-11-16)

- **Done:** M1 built and its automated checks pass (28 tests); the manual restart gate passed. Earlier: DESIGN.md written, all 25 §1 resolutions accepted, D8, D9, D13 accepted, Python fixed at 3.12.
- **Done since:** M2 built (94 tests at that point). M3 built; the full suite passes (214 tests). Both wait for Albert's review and commit.
- **Done since:** M4 built, plan approved by Albert first; the full suite passes (268 tests). Waits for Albert's review and commit.
- **Before the next `docker compose` command:** add `TARGET_MIGRATOR_PASSWORD=<letters and digits>` to `.env`. Compose now refuses to start without it. During the M4 session the value was passed from the shell as `localtestonly1`, so the `relay_migrator` role and the running containers currently use that; the next `docker compose up -d` with your `.env` value resets the role's password and recreates the containers.
- **Next:** M5 (idempotency keys). It touches transactions, so it needs a plan and Albert's OK first.
- **Answered (Albert, 2026-11-16):**
  - Keep D16: one connection per request, no `psycopg_pool`.
  - No `pytest-repeat`. Repeat race tests with a shell loop (DESIGN §7.1 and §7.7 updated).
  - M2 plan choices accepted as D27, D28, D29, plus the READ COMMITTED assumption recorded with D27 and tested.
  - Pin READ COMMITTED explicitly in `db.connect` (done; the level no longer depends on the server default).
  - M3: claim follows D3 (select-and-lock, then the helper); the single-`UPDATE` claim in the M3 prompt predates DESIGN.md and DESIGN wins. Claim only `PENDING` in M3 (D32). Scope is the full DESIGN §8 M3 list. Retry limit counts per phase (D30).
  - M4: where the M4 prompt and DESIGN.md differ, DESIGN wins (policy re-check, idle timeout, the full §8 test list). `demo_crash.py` is built in M4 (D36). Five plan decisions accepted, now D33-D37. Added by Albert: a worker must refuse to start unless `LEASE_SECONDS > LOCK_TIMEOUT + STATEMENT_TIMEOUT + margin` (D33, recorded with D23).
  - Do not install `httpx2`. The test run prints `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead.` This is known and ignored; `httpx` works today. Revisit only if `TestClient` stops working.
- **Open questions:**
  1. CLAUDE.md says "short leases or an injectable clock"; the accepted §1 #20 has tests backdate `lease_expires_at` instead. Confirm backdating is fine, or CLAUDE.md should be reworded.
  2. PLAN.md line 60 still says "e.g. `python:3.13-slim`". Not edited; update when PLAN is next touched.
  3. This session read DESIGN §8 as the M1 scope (14 listed tests), which is wider than the five tests in the prompt, and left `target_schema.sql` and the role to M4. Say so if you wanted them in M1.
  4. M4: the lease re-check requires only "not yet expired". It could require "enough lease left for the whole target transaction" (remaining time > lock + statement timeouts). Not built; say so if you want it.
  5. M4: the 10-second lease margin is a constant in `relay/config.py`, not a setting (D33). Confirm that is what you want.
- **Untested:** see M1 to M4 below. M5 has no code. The pglast wheel check was read from PyPI, not an image build (pglast is not installed until M3).

## M1: Scaffolding

- Status: in progress (all checks below pass except the x64 build; waiting on that and Albert's review)
- Started: 2026-11-16 / Finished:
- Scope: Dockerfile (Python 3.12), Compose services, `dbinit`, relay schema (`migrations`, `migration_events`), create/get/list/health endpoints, test fixtures
- Exit checks:
  - [x] `test_health.py` (200 when up; plus a 503 test for DESIGN §1 #19)
  - [x] `test_schema.py` (three DESIGN tests, plus the reverse lease check and a seed-helper check)
  - [x] `test_create_get.py` (DESIGN's tests, plus bad-body, lease-hidden, ordering/limit, bad-limit)
  - [x] `test_isolation.py`
  - [x] Manual: record survives `docker compose down && docker compose up -d` (id `f00e1c68-...`, same body and event before and after; the volume was kept)
  - [ ] Image builds and suite passes on both machines. **Done on ARM64 only** (this machine, Docker `aarch64`); the x64 machine is not tried.
- Last test run: `docker compose run --rm tests pytest` (PowerShell) -> `28 passed, 1 warning in 2.13s`. Red run first: same command, exit 4, `ImportError: cannot import name 'db' from 'relay'`. Mutation check: removing the creation-event INSERT failed 2 tests (`test_create_writes_one_creation_event`, `test_get_returns_row_and_events`); the code was then restored.
- Not yet tested:
  - x64 image build.
  - `docker compose up` from a completely empty volume was only exercised through the test suite's own `dbinit --reset` path and the first `up`; `down -v` then `up` was not repeated.
  - `dbinit --reset` against a running API (the `WITH (FORCE)` disconnect).
  - The API under a real mid-request database outage (only `/health` is tested for that; other routes would return 500).
  - Concurrent creates.
  - The worker placeholder container does nothing and has no test.
- Notes:
  - No core operation exists in M1. `create_migration` writes a row and its event in one transaction; its header comment names the invariant (1: the one allowed non-helper write of `status`).
  - Decisions D24-D26 added to DESIGN.md.
  - On Windows, `docker compose build` fails from Git Bash ("additional privileges requested: pass --allow=fs.read=Dockerfile") and works from PowerShell. Noted in README.
  - Docker Desktop was not running at session start; it was started from this session.
  - Requirements pin direct dependencies only; transitive packages float.

## M2: Transition helper, approve, reject

- Status: in progress (all exit checks pass; waiting on Albert's review)
- Started: 2026-11-16 / Finished:
- Scope: `transitions.py` (core operation), `service.py`, approve and reject endpoints bound to the SQL hash
- Exit checks:
  - [x] `test_transitions.py` (DESIGN's nine, plus READ COMMITTED, no-open-transaction, right-token, unknown-id, detail, and legal-table tests)
  - [x] `test_approve_reject.py` (DESIGN's seven, plus reject stale hash, approve after reject, bad body, malformed id, ignored Idempotency-Key)
  - [x] `test_concurrent_approval.py` (20 approvals; 10 approvals vs 10 rejections)
- Last test run (PowerShell):
  - Red run first: `docker compose run --rm tests pytest -q` -> exit 2, `ModuleNotFoundError: No module named 'relay.transitions'` and `cannot import name 'service'`.
  - `docker compose run --rm tests pytest -q` -> `94 passed, 1 warning` (after the READ COMMITTED pin; 93 before it).
  - Repeat: `docker compose run --rm tests sh -c 'for i in $(seq 20); do pytest tests/test_concurrent_approval.py tests/test_transitions.py -q -p no:cacheprovider > /tmp/out.txt 2>&1 || { cat /tmp/out.txt; echo FAILED on run $i; exit 1; }; tail -1 /tmp/out.txt; done; echo all 20 runs passed'` -> `31 passed` 20 times out of 20, exit code 0. (Avoid double quotes inside the `sh -c '...'` string: PowerShell 5.1 mangles them.)
  - Pin check: with the `conn.isolation_level` line removed from `db.connect`, `test_read_committed_is_pinned_not_inherited` failed; the line was then restored.
  - Mutation check: with `AND status = <from>` removed from the helper's `WHERE`, 4 tests failed (both concurrency tests, `test_wrong_from_status_returns_false_and_writes_no_event`, `test_second_transaction_waits_then_matches_zero_rows`); the code was then restored and the suite re-run.
- Not yet tested:
  - Concurrent approvals over real HTTP (D13; Phase 2 backlog). The 20-way tests call the service from threads.
  - Behavior at any isolation level other than READ COMMITTED (the test only asserts the level is READ COMMITTED).
  - A database error or lost connection in the middle of an approve (expected: rollback and a 500; not exercised).
  - The lease-token paths of the helper are tested only with seeded rows; real worker callers arrive in M3.
  - A retried approve after a lost response gets 409, by design until M5; pinned by `test_idempotency_key_header_is_accepted_and_ignored`.
- Notes:
  - M5 seam: the route handlers open the transaction (`with conn.transaction():`, marked `M5 SEAM` in `relay/api.py`), and `service.approve`/`reject` return `(status_code, body)` without raising, which is the pair M5 will save and replay.
  - Decisions D27-D29 added to DESIGN.md; §4.1 now documents the approve/reject flow; §7.1 and §7.7 no longer mention `pytest-repeat`.
  - README updated to M2, claiming only what the tests above show.

## M3: Claim, leases, generation

- Status: in progress (all exit checks pass; waiting on Albert's review)
- Started: 2026-11-16 / Finished:
- Scope: claim query (core operation), reclaim for `DRAFTING`, hooks, fake LLM, pglast policy, worker draft path, generation retry limit (D8), real `worker` Compose service (2 replicas)
- Exit checks:
  - [x] `test_policy.py` (52 accepted cases, 39 rejected cases)
  - [x] `test_fake_llm.py`
  - [x] `test_claim.py` (includes 4 claimers over 50 rows: 50 claims, all distinct)
  - [x] `test_generation.py` (includes `test_claim_is_committed_before_llm_runs`, which DESIGN first listed under `test_claim.py`)
  - [x] `test_reclaim.py` (drafting cases)
  - [x] `test_stale_worker.py` (B finished; and B re-claimed but not finished)
  - [x] `test_crash_generation.py` (real subprocess, exit code 137)
  - [x] `test_worker_contention.py::test_generation_50_jobs_4_workers`
  - [x] `test_retry_limit.py` (limit reached; attempts reset per phase)
- Last test run (PowerShell):
  - Red run first: `docker compose run --rm tests pytest -q` -> exit 4, `ImportError: cannot import name 'hooks' from 'relay'`.
  - First green attempt: 1 failed, 212 passed. Cause: a bug in the test, not the worker (`test_generation_fails_after_max_attempts` backdated the lease of a row that no longer had one; the `lease_columns_together` CHECK refused it). The test was fixed; no worker code changed.
  - `docker compose run --rm tests pytest -q` -> `214 passed, 1 warning`.
  - Repeat loop over claim, contention, stale, crash, reclaim, retry-limit, concurrent-approval, and transitions tests (same loop shape as M2) -> `48 passed` 20 times out of 20, exit code 0.
  - Mutation 1: the helper's `AND lease_token = ...` removed -> 2 failed (`test_stale_drafter_cannot_overwrite_a_new_owner_mid_draft`, `test_wrong_lease_token_returns_false`). The original stale-worker test did NOT fail, because there B has finished and the status check alone stops A; that is why the mid-draft test was added.
  - Mutation 2: `SKIP LOCKED` removed from the claim -> 2 failed (`test_two_claimers_one_row_second_gets_none`, `test_second_claimer_skips_the_locked_row_and_takes_the_next`). The first try of this mutation HUNG, because only one of those tests set a `lock_timeout`; the container was killed, the timeout was added to both, and the mutation was re-run. Both mutations were restored before the final runs.
  - Live check: `docker compose up -d --build`, then two `POST /migrations`. The allowed one reached `WAITING_FOR_APPROVAL` with a claim event and a save event from one worker; `add column x text to secrets` reached `FAILED` with `policy: table 'secrets' is not an allowed table`. Those two rows are still in the dev `relay_db`.
- Not yet tested:
  - `run_forever`: the polling loop, jitter, SIGTERM shutdown, reconnect after a lost Relay connection, and the catch-all for unexpected exceptions. Only `run_once` is tested; the live check exercised the loop's happy path once.
  - `RELAY_HOOK=<name>:pause` (for the Phase 2 demo). Only `:crash` is tested.
  - Two reclaimers racing for the same expired row, and a reclaimer racing the slow owner's save at the same instant. Both rely on the row lock and `SKIP LOCKED`; only the one-after-the-other versions are tested.
  - More than `batch_size` (10) expired leases in one sweep.
  - The 50-job test ran 4 workers as threads in one process, not 4 processes. Only the crash test uses a real worker process.
  - Reading `WORKER_ID`, `LEASE_SECONDS`, `POLL_SECONDS`, `MAX_ATTEMPTS`, `LLM_PROVIDER` from the environment (`load_settings`); tests build `Settings` directly.
  - x64 image build with pglast (ARM64 only, as in M1).
- Notes:
  - `pglast==8.8` added to `requirements.txt`; the image must be rebuilt (`docker compose build`, from PowerShell).
  - Policy field names were checked against the pglast 8.8 parse tree before writing the rules (DESIGN §6.2).
  - Decisions D30 (attempts per phase), D31 (`run_once` returns an outcome string; `claim.after_commit` fires only after a real claim), D32 (PENDING only until M4) added to DESIGN.md.
  - The new settings have defaults and are not in `.env.example` or passed by Compose; add them to the worker's `environment` if a non-default is ever needed.
  - An approved migration now waits forever: nothing claims `APPROVED` until M4.

## M4: Marker-first execution

- Status: in progress (all exit checks pass; waiting on Albert's review)
- Started: 2026-11-16 / Finished:
- Scope: target schema and `relay_migrator` role, marker transaction (core operation), worker execute path, claim widened to `APPROVED`, reclaim for `EXECUTING`, execution retry limit (D8), lease startup check and lease re-check (D33), `scripts/demo_crash.py` (D36)
- Exit checks:
  - [x] `test_target_role.py` (5 guarantees, 6 tests)
  - [x] `test_execute.py` (DESIGN's 7, plus 5 added: no Relay transaction during target work, expired lease before target work, owner finishes after expiry, timeouts scoped to the transaction, `DDLFailed` leaves the connection usable)
  - [x] `test_marker_contention.py` (first commits; first rolls back)
  - [x] `test_reclaim.py::test_expired_executing_returns_to_approved`
  - [x] `test_stale_worker.py::test_stale_executor_write_matches_zero_rows`
  - [x] `test_crash_after_target_commit.py` (real subprocess: `:crash`, and `:pause` then SIGKILL)
  - [x] `test_worker_contention.py::test_execution_50_jobs_4_workers`
  - [x] `test_retry_limit.py` (execution: with marker, without marker)
  - [x] Added: `test_config.py` (D33), `test_dbinit_target.py` (D34), three claim tests for the widened queue
- Last test run (PowerShell; `TARGET_MIGRATOR_PASSWORD` was set in the shell because `.env` does not have it yet):
  - Red run first: `docker compose run --rm tests pytest -q` -> exit 4, `ImportError: cannot import name 'MIGRATOR_ROLE' from 'relay.dbinit'`.
  - `docker compose run --rm tests pytest -q` -> `268 passed, 1 warning` on the first green attempt, and again after the mutation checks and the live check.
  - Repeat loop (same shape as M2/M3) over execute, marker contention, crash after target commit, worker contention, stale worker, reclaim, retry limit, claim, crash generation, concurrent approval, transitions -> `71 passed` 20 times out of 20, exit code 0.
  - Mutation checks, one at a time, full suite each, file restored after each:
    1. Always run the DDL, whatever the marker insert returned -> 6 failed (both crash tests, matching-marker skip, different-hash, first-commits contention, stale executor).
    2. Any existing marker counts as success -> 1 failed (`test_existing_marker_with_different_hash_fails`).
    3. No lease re-check before target work -> 1 failed (`test_expired_lease_before_target_work_leaves_target_untouched`).
    4. `DDLFailed` raised without confirming the rollback -> 1 failed (`test_lost_target_connection_does_not_mark_failed`: the killed session's error does carry a SQLSTATE, so the connection check is what keeps it "unknown").
    5. `dbinit` re-runs the target schema every time -> 1 failed (`test_dbinit_does_not_rebuild_a_target_that_already_has_its_schema`).
    6. Lease check skipped at worker startup -> 1 failed (`test_worker_refuses_to_start_with_a_lease_that_is_too_short`).
  - Demo: `docker compose run --rm tests python -m scripts.demo_crash` -> exit 0; frozen state `EXECUTING` with 1 marker and the column; final state `SUCCEEDED`, 1 marker, 8 audit events.
  - Live check: `docker compose up -d --build`, then over HTTP: create `add column m4_live_check text to orders`, approve, -> `SUCCEEDED`; `target_db` has the marker and `orders.m4_live_check`. `dbinit` upgraded the existing volume in place (role, REVOKE, target schema) without `--reset`. That row, column, and marker are still in the dev databases. (My first try of this check posted before the API was up and failed; the script was fixed to wait for `/health`.)
- Not yet tested:
  - The late-apply gap itself (a worker frozen after the lease re-check that commits on the target after the job was marked `FAILED`). It is a documented non-guarantee (D23), not a tested behavior.
  - `lock_timeout` or `statement_timeout` actually firing: on the marker step (expected: abandon) and on the DDL step (expected: `FAILED`). Only the classification rule is tested, with a duplicate-column error and a killed session. This is the Phase 2 `lock_timeout` test (D9).
  - `idle_in_transaction_session_timeout` actually ending a stalled target transaction. Only that the setting is in force is tested.
  - A connection lost during the target `COMMIT` itself (outcome unknown). The lost-connection test kills the session before the DDL.
  - Target unreachable at connect time, and target unreachable while reading the marker at the retry limit (both expected: `abandoned`).
  - A marker conflict where the marker then cannot be read (an administrator deleted it mid-transaction).
  - Two workers colliding through the full worker path at the marker step; the contention tests call `apply_on_target` directly on two connections.
  - The 50-job test ran 4 workers as threads, not 4 processes. Only the crash tests use a real worker process.
  - `python -m relay.worker` exiting at startup with a bad lease setting (the `Worker(...)` constructor is tested; the process exit is not).
  - `dbinit --reset` on the dev databases, and `dbinit` changing the role's password when `.env` changes.
  - The demo has no automated test of its own; a test asserts the same sequence.
  - x64 image build (ARM64 only, as before). The image now copies `scripts/`.
- Notes:
  - Core operation 3 of 4 is `relay/target.py::apply_on_target`, with the header comment (precondition, atomic operation, failures, invariant).
  - New environment variable `TARGET_MIGRATOR_PASSWORD` (in `.env.example`); Compose builds `TARGET_DB_DSN` from it. `LOCK_TIMEOUT`, `STATEMENT_TIMEOUT`, `TARGET_IDLE_TIMEOUT` have defaults and are not passed by Compose.
  - `Settings.target_db_dsn` is required, so the API also needs `TARGET_DB_DSN` in its environment although it never connects to the target. Compose gives every service the same variables.
  - M3 tests changed because the behavior changed: `test_claim_ignores_rows_that_are_not_pending` became `..._not_queued`; `test_unexpired_lease_is_not_reclaimed` no longer expects an expired `EXECUTING` row to be left alone.
  - Decisions D33-D37 added to DESIGN.md; D23 now points at D33. §3, §4.2, §4.3, §5, §7 and §8 updated to match the code.
  - The demo leaves `relay_demo` and `target_demo` behind; each run rebuilds them.
  - README updated to M4, with the non-guarantees section DESIGN §10 requires.

## M5: Idempotency keys

- Status: not started
- Started: / Finished:
- Scope: `idempotency_keys` table, reservation and replay (core operation), `Idempotency-Key` required on approve and reject
- Exit checks:
  - [ ] `test_idempotency.py`
  - [ ] `test_concurrent_approval.py` still passes with required keys
- Last test run (command, result):
- Not yet tested:
- Notes:

## Phase 2 backlog (only when Albert says so)

- Live-HTTP concurrency test: 20 real approve requests against a running `api` container. Until then the README says "concurrent approvals", not "concurrent requests".
- Revisit D9 (auto-retry on `lock_timeout`) with the Phase 2 `lock_timeout` test.
- PLAN's Phase 2 list: Groq adapter, CI, structured logs, GIF. (`demo_crash.py` was built in M4.)
- Future work beyond Phase 2: target-side fencing (DESIGN.md D23).
