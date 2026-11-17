# Relay: progress

One entry per milestone. Scope and the exact exit tests are in DESIGN.md §8; this file records status only. A milestone is done when all its tests pass via `docker compose run --rm tests pytest`.

Status values: `not started`, `in progress`, `done`.

## Current state (updated 2026-11-16)

- **Done:** M1 built and its automated checks pass (28 tests); the manual restart gate passed. Earlier: DESIGN.md written, all 25 §1 resolutions accepted, D8, D9, D13 accepted, Python fixed at 3.12.
- **Done since:** M2 built (94 tests at that point). M3 built; the full suite passes (214 tests). Both wait for Albert's review and commit.
- **Done since:** M4 built, plan approved by Albert first; the full suite passes (270 tests). Waits for Albert's review and commit.
- **Run `docker compose up -d` once:** `TARGET_MIGRATOR_PASSWORD` is now in `.env`, and the last test runs set the `relay_migrator` role to that value. The api and worker containers were created earlier with a shell-supplied password (`localtestonly1`), so until they are recreated the running workers cannot log in to `target_db` (an approved job would be abandoned and retried, not failed).
- **Done since:** skeptical review of M1 to M4 (REVIEW.md, findings F1 to F10) and `scripts/break_guards.py` (mutation check). Nothing in `relay/` changed. The review's 27 probes are in `probes/` (not collected by default).
- **Done since:** review follow-up 1 (plan approved by Albert first): 13 probes became tests; no file in `relay/` changed; the full suite passes (283 tests). Details in "Review follow-ups" below. Waits for Albert's review and commit.
- **Done since:** review follow-up 2, F1 (plan approved by Albert first): the retry-limit check is now a waiting probe (D39); the full suite passes (287 tests). Details in "Review follow-ups" below. Waits for Albert's review and commit.
- **Done since:** review follow-up 3, F3 (plan approved by Albert first): Relay connections carry a lock timeout and an idle-in-transaction timeout (D40); the API answers a lock timeout with `503` (DESIGN §1 #26); a worker abandons its lease. The full suite passes (304 tests). Details in "Review follow-ups" below. Waits for Albert's review and commit.
- **Next:** the last review follow-up, waiting for Albert's OK on its plan:
  4. F4(b): target connection failures do not count toward `MAX_ATTEMPTS`. D9 stays as it is.
  Then M5 (idempotency keys), which also needs a plan and Albert's OK first.
- **Opus backlog (review findings left for later; see REVIEW.md):**
  - F5: no network-level timeout on the target connection (a silently dead network can hang a worker).
  - F6: `test_lost_target_connection_does_not_mark_failed` kills every `relay_migrator` session on the server; crash tests can leak a worker process.
  - F7: the policy accepts a column name ending in a newline (`$` instead of `\Z` / `fullmatch`).
  - F8: no test enforces "only the helper writes `status`".
  - F9: the README's lost-connection sentence was ahead of its test. Follow-up 1 added the test (`test_commit_cut.py`) and the README now cites it; nothing left unless the wording should change.
  - F10: `GET /migrations/{id}` reads the row and its events in two statements.
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
  (M4's two questions are answered: the lease re-check now requires remaining lease > lock + statement timeouts + margin, and the margin stays a constant. Both are in D33.)
- **Untested:** see M1 to M4 and "Review follow-ups" below. M5 has no code. The pglast wheel check was read from PyPI, not an image build (pglast is not installed until M3).

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
  - (Now tested, review follow-up 1: two reclaimers on one row, a reclaimer against the owner's save, and more than `batch_size` expired leases, in `test_reclaim_races.py`. `RELAY_HOOK=<name>:pause` has been tested since M4.)
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
  - [x] `test_execute.py` (DESIGN's 7, plus 7 added: no Relay transaction during target work, expired lease before target work, too little lease left, just enough lease left, owner finishes after expiry, timeouts scoped to the transaction, `DDLFailed` leaves the connection usable)
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
  - Follow-up (remaining-lease rule, D33): with the re-check reduced to "not yet expired", `pytest tests/test_execute.py -q` -> 1 failed (`test_lease_with_too_little_time_left_stops_before_target_work`), 13 passed; rule restored. Then `docker compose run --rm tests pytest -q` -> `270 passed, 1 warning`, and the repeat loop -> `73 passed` 20 times out of 20. These runs used the password from `.env`. The demo and the live check were NOT re-run after this change.
  - Demo: `docker compose run --rm tests python -m scripts.demo_crash` -> exit 0; frozen state `EXECUTING` with 1 marker and the column; final state `SUCCEEDED`, 1 marker, 8 audit events.
  - Live check: `docker compose up -d --build`, then over HTTP: create `add column m4_live_check text to orders`, approve, -> `SUCCEEDED`; `target_db` has the marker and `orders.m4_live_check`. `dbinit` upgraded the existing volume in place (role, REVOKE, target schema) without `--reset`. That row, column, and marker are still in the dev databases. (My first try of this check posted before the API was up and failed; the script was fixed to wait for `/health`.)
- Not yet tested:
  - The late-apply gap itself (a worker frozen after the lease re-check that commits on the target after the job was marked `FAILED`). It is a documented non-guarantee (D23), not a tested behavior.
  - (Now tested, review follow-up 1: `lock_timeout` and `statement_timeout` firing on the DDL, `lock_timeout` on the marker step, the idle timeout ending a stalled transaction, in `test_timeouts_fire.py`; a connection lost during the target `COMMIT`, both directions, in `test_commit_cut.py`.)
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

## Review follow-ups (from REVIEW.md)

### 1. Probes to tests

- Status: in progress (all checks pass; waiting on Albert's review)
- Started: 2026-11-16 / Finished:
- Scope: 13 review probes rewritten as tests with docstrings. No file in `relay/` changed.
- Added:
  - [x] `tests/test_reclaim_races.py` (6: two reclaimers; reclaimer against the owner's save in three forced variants; two start-together tests)
  - [x] `tests/test_commit_cut.py` (2: `COMMIT` processed but answer lost; `COMMIT` never arrived), with `CommitCuttingProxy` (D38)
  - [x] `tests/test_timeouts_fire.py` (4: `lock_timeout` on the DDL, `statement_timeout` on the DDL, `lock_timeout` on the marker step, idle timeout)
  - [x] `tests/test_stale_worker.py::test_stale_executor_cannot_overwrite_a_new_owner_mid_flight`
  - [x] `tests/helpers.py::wait_until_migrator_sessions_are_gone` (counts sessions in the test's own target database only)
  - [x] `scripts/break_guards.py`: new tests added to the `expected` lists of `skip-locked-reclaim` (had none), `rollback-confirmed`, `lease-token-check`. No guard pattern changed.
  - [x] `probes/test_review_probes.py` now holds only the `finding_`, `contrast_`, and `measure_` probes (14 collected, counting the 8 policy cases).
- Last test run (PowerShell):
  - Before converting: `docker compose run --rm tests python -m pytest probes -q -p no:cacheprovider -k "p1 or p2 or p3 or p4"` -> `12 passed, 15 deselected`.
  - `docker compose run --rm tests pytest tests/test_reclaim_races.py tests/test_commit_cut.py tests/test_timeouts_fire.py tests/test_stale_worker.py -q -p no:cacheprovider` -> `16 passed, 1 warning` (13 new, 3 existing).
  - `python scripts/break_guards.py --only skip-locked-reclaim rollback-confirmed lease-token-check --repeat 3` (host) -> baseline 7 passed; all three `CAUGHT` in 3 runs of 3. `lease-token-check`: 3 expected tests failed (the new mid-flight test among them). `skip-locked-reclaim`: 2 failed (both new). `rollback-confirmed`: 2 failed (the new idle-timeout test among them).
  - Repeat loop (same shape as M2 to M4) over the four files above -> `16 passed` 20 times out of 20.
  - `docker compose run --rm tests pytest -q -p no:cacheprovider` -> `283 passed, 1 warning`.
  - `docker compose run --rm tests python -m pytest probes -q -p no:cacheprovider` -> `14 passed, 1 warning`.
- Not yet tested:
  - No mutation shows that the two `COMMIT`-cut tests, the two DDL-timeout tests, or the marker-step timeout test can fail: there is no one-line guard to remove for them (they depend on the worker treating every non-`DDLFailed` target error as unknown, and on the timeouts being set at all). The `--full` mutation run over every guard was not repeated.
  - The two start-together tests do not force an order; the owner-against-sweepers one can pass with the owner winning all 30 rows.
  - The proxy cuts only at `COMMIT`. A cut during the marker insert or the DDL is covered only by the killed-session test.
  - Reclaimer against the owner's final write on the EXECUTING side is forced only in the "new owner has claimed" form (the mid-flight test); the open-transaction variants use a drafting row.
- Notes:
  - Plain `pytest probes` fails (`No module named 'tests'`); use `python -m pytest probes`.
  - Docker Desktop was not running at session start; it was started from this session, with `docker compose up -d postgres` only (api and worker were not recreated; the note above about `docker compose up -d` still applies).
  - Decision D38 added to DESIGN.md; §7.3 and §8 updated; README gained two bullets and extended two, each citing its test file.

### 2. F1: the retry-limit check waits

- Status: in progress (all checks pass; waiting on Albert's review)
- Started: 2026-11-16 / Finished:
- Scope: `relay/target.py::probe_marker` (replaces `read_marker_hash`), with the marker `INSERT`, the timeouts, and the hash comparison moved into three private functions shared with `apply_on_target`; `relay/worker.py::_exhausted` maps the probe's result. Albert's two choices: one shared marker `INSERT`; a hash mismatch at the limit says "retry limit reached; target marker exists with a different hash".
- Checks:
  - [x] `test_retry_limit.py`: four new tests (in flight then `abandoned`; waits then `SUCCEEDED`; waits then `FAILED`; different-hash message), and the existing no-marker test now also covers "the probe leaves no marker"
  - [x] `scripts/break_guards.py`: `marker-result-ignored` and `marker-hash-compare` patterns updated to the moved code; `marker-insert` pattern still matches exactly once (comment added); new mutation `probe-rollback`
  - [x] `probes/`: the F1 finding and its contrast probe removed (12 collected now)
- Last test run (PowerShell):
  - Red run first: `docker compose run --rm tests pytest tests/test_retry_limit.py -q -p no:cacheprovider` -> `4 failed, 4 passed`. Test 1: `assert 'failed' == 'abandoned'` (the worker wrote `FAILED` beside an uncommitted marker). Tests 2 and 3: `no relay_migrator session ever blocked on a lock` (the old read never waits). Test 4: the error was the bare `retry limit reached`.
  - After the change: same command -> `8 passed`.
  - `docker compose run --rm tests pytest -q -p no:cacheprovider` -> `287 passed, 1 warning`.
  - `python scripts/break_guards.py --only marker-insert marker-result-ignored marker-hash-compare probe-rollback rollback-confirmed --repeat 3` (host) -> baseline 13 passed; all five `CAUGHT` in 3 runs of 3 (6, 4, 2, 2, 2 expected tests failed).
  - Repeat loop over retry limit, execute, marker contention, timeouts, commit cut, crash after target commit, worker contention, stale worker -> `38 passed` 20 times out of 20.
  - `docker compose run --rm tests python -m pytest probes -q -p no:cacheprovider` -> `12 passed`.
  - `docker compose run --rm tests python -m scripts.demo_crash` -> exit 0, final state `SUCCEEDED`.
- Not yet tested:
  - What F1 does NOT fix, by design: a stale attempt that begins its target transaction *after* the probe returned `NOT_APPLIED` (including one whose marker insert was waiting on the probe's own marker). That is the late-apply gap (D23).
  - The probe losing its connection after its insert (expected: `abandoned`, nothing left on the target). Reasoned from "never sends `COMMIT`"; not forced.
  - A job that is abandoned at the limit repeatedly because the earlier attempt stays stuck; only one abandon-then-succeed cycle is tested.
  - Target unreachable at the retry limit (expected: `abandoned`), as before.
  - The `--full` mutation run over every guard was not repeated; the five mutations in `target.py` and the three from follow-up 1 were.
- Notes:
  - Decision D39 added; D8 points to it; DESIGN §4.3 gained "Retry-limit probe", §5 and §8 updated. README's retry-limit bullet extended.
  - Behavior change visible to users: a different-hash marker at the limit now has its own error text; the check can take up to `LOCK_TIMEOUT`.

### 3. F3: timeouts on Relay connections

- Status: in progress (all checks pass; waiting on Albert's review)
- Started: 2026-11-16 / Finished:
- Scope: `relay/db.py::connect_relay` (session `lock_timeout` and `idle_in_transaction_session_timeout`, sent as startup options); settings `RELAY_LOCK_TIMEOUT` (5s) and `RELAY_IDLE_TIMEOUT` (10s); `relay/api.py` answers `LockNotAvailable` with `503` + `Retry-After: 1`; `relay/worker.py::run_once` turns a Relay-side lock timeout after the claim into `abandoned`. Albert's choices: `503` (recorded as DESIGN §1 #26) and abandon the lease. Albert's addition: prove the idle timeout never hits a healthy worker.
- Checks:
  - [x] `tests/test_relay_timeouts.py` (17 tests: settings in force; autocommit; bad values refused, 6 cases; environment; frozen reclaimer; frozen claimer; draft save and `SUCCEEDED` write timing out; API `503`; three "healthy worker" tests with a 300 ms idle timeout against 1 s of LLM time, target time, and poll wait)
  - [x] Autocommit confirmed: `connect_relay` calls the same `connect` (always `autocommit=True`); the test asserts it and that a plain read leaves the session idle, not in a transaction. On the execute path the worker's only Relay statement before the final write is the lease re-check, a plain autocommit read.
  - [x] `scripts/break_guards.py`: new mutation `relay-session-timeouts`
  - [x] `probes/`: the two F3 probes removed; only `measure_` probes remain (10 collected)
- Last test run (PowerShell):
  - Red run 1 (nothing implemented): `docker compose run --rm tests pytest tests/test_relay_timeouts.py -q -p no:cacheprovider` -> `17 failed` (the settings and `connect_relay` did not exist).
  - Red run 2 (settings in force; no API handler, no worker catch): same command -> `3 failed, 14 passed`: the draft-save, `SUCCEEDED`-write, and API tests, each with a raw `psycopg.errors.LockNotAvailable: canceling statement due to lock timeout`.
  - After the change: same command -> `17 passed`.
  - `docker compose run --rm tests pytest -q -p no:cacheprovider` -> `304 passed, 1 warning`.
  - First mutation run HUNG and was stopped by hand. Cause: a bug in the new API test, not in Relay. The test sent its approve from the thread that also held the blocking transaction, so with the lock timeout removed nothing could ever release the lock. Fix: the approve now runs in a `Background` thread, whose join fails after 10 s. `break_guards.py`'s own 240 s limit did not end the run either (see Notes).
  - After that fix: `docker compose run --rm tests pytest tests/test_relay_timeouts.py -q -p no:cacheprovider` -> `17 passed`; `python scripts/break_guards.py --only relay-session-timeouts --repeat 3` (host) -> `CAUGHT` in 3 runs of 3, all 4 expected tests failed.
  - Repeat loop over relay timeouts, reclaim races, stale worker, concurrent approval, approve/reject -> `64 passed` 20 times out of 20.
  - `docker compose run --rm tests pytest -q -p no:cacheprovider` (again, after the test fix) -> `304 passed, 1 warning`.
  - `docker compose run --rm tests python -m pytest probes -q -p no:cacheprovider` -> `10 passed`.
  - `docker compose run --rm tests python -m scripts.demo_crash` -> exit 0, final state `SUCCEEDED`.
- Not yet tested:
  - `run_forever` after the server ends a frozen worker's session (expected: the next statement fails and the loop reconnects). `run_forever` is still untested as a whole; the error may arrive as a non-`OperationalError` first, in which case the catch-all logs it and the following pass reconnects.
  - The API request that was itself frozen and then wakes (expected: `500`, its transaction already rolled back). Only the request waiting behind it is tested.
  - A lock timeout inside the claim or the reclaim sweep. Both use `SKIP LOCKED` and should never wait; if one did, `run_once` would raise.
  - A lock timeout on a worker's `FAILED` write and at the retry limit (same `except` as the two tested writes).
  - `503` on reject (same handler as approve), and over real HTTP.
  - The live stack was not restarted with the new settings (`docker compose up -d --build` not run).
- Notes:
  - Decision D40 and spec resolution §1 #26 added; DESIGN conventions, §5, §7.6, §8 updated; README gained one bullet.
  - For M5: the same-key wait is also subject to `RELAY_LOCK_TIMEOUT` (noted in DESIGN §8).
  - Three of the new tests each take 1 s of real time by design, and the two frozen-holder tests about 1 s each.
  - **`break_guards.py` weakness found, not fixed:** on this Windows host its per-run time limit did not stop a hung test run (the container was still up after 11 minutes against a 240 s limit), so the `HUNG` verdict never appeared and the script had to be stopped by hand; its temporary checkout and container were then removed by hand. Likely cause (not confirmed): after the timeout, Python waits for the output pipes, which the `docker` child processes still hold. Open question for Albert: fix it now or add it to the Opus backlog.
  - The full suite was run once more on its own at the end -> `304 passed, 1 warning`.

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
