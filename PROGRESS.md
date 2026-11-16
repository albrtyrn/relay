# Relay: progress

One entry per milestone. Scope and the exact exit tests are in DESIGN.md §8; this file records status only. A milestone is done when all its tests pass via `docker compose run --rm tests pytest`.

Status values: `not started`, `in progress`, `done`.

## Current state (updated 2026-11-16)

- **Done:** M1 built and its automated checks pass (28 tests); the manual restart gate passed. Earlier: DESIGN.md written, all 25 §1 resolutions accepted, D8, D9, D13 accepted, Python fixed at 3.12.
- **Done since:** M2 built (94 tests at that point). M3 built; the full suite passes (214 tests). Both wait for Albert's review and commit.
- **Next:** M4 (marker-first execution). It touches target_db, locks, and leases, so it needs a plan and Albert's OK first. M4 must widen the claim `WHERE` to `APPROVED` and the reclaim to `EXECUTING` (D32).
- **Answered (Albert, 2026-11-16):**
  - Keep D16: one connection per request, no `psycopg_pool`.
  - No `pytest-repeat`. Repeat race tests with a shell loop (DESIGN §7.1 and §7.7 updated).
  - M2 plan choices accepted as D27, D28, D29, plus the READ COMMITTED assumption recorded with D27 and tested.
  - Pin READ COMMITTED explicitly in `db.connect` (done; the level no longer depends on the server default).
  - M3: claim follows D3 (select-and-lock, then the helper); the single-`UPDATE` claim in the M3 prompt predates DESIGN.md and DESIGN wins. Claim only `PENDING` in M3 (D32). Scope is the full DESIGN §8 M3 list. Retry limit counts per phase (D30).
  - Do not install `httpx2`. The test run prints `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead.` This is known and ignored; `httpx` works today. Revisit only if `TestClient` stops working.
- **Open questions:**
  1. CLAUDE.md says "short leases or an injectable clock"; the accepted §1 #20 has tests backdate `lease_expires_at` instead. Confirm backdating is fine, or CLAUDE.md should be reworded.
  2. PLAN.md line 60 still says "e.g. `python:3.13-slim`". Not edited; update when PLAN is next touched.
  3. This session read DESIGN §8 as the M1 scope (14 listed tests), which is wider than the five tests in the prompt, and left `target_schema.sql` and the role to M4. Say so if you wanted them in M1.- **Untested:** see M1, M2, and M3 below. Everything from M4 on has no code. The pglast wheel check was read from PyPI, not an image build (pglast is not installed until M3).

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

- Status: not started
- Started: / Finished:
- Scope: target schema and `relay_migrator` role, marker transaction (core operation), worker execute path, reclaim for `EXECUTING`, execution retry limit (D8)
- Exit checks:
  - [ ] `test_target_role.py`
  - [ ] `test_execute.py`
  - [ ] `test_marker_contention.py`
  - [ ] `test_reclaim.py::test_expired_executing_returns_to_approved`
  - [ ] `test_stale_worker.py::test_stale_executor_write_matches_zero_rows`
  - [ ] `test_crash_after_target_commit.py`
  - [ ] `test_worker_contention.py::test_execution_50_jobs_4_workers`
  - [ ] `test_retry_limit.py` (execution cases)
- Last test run (command, result):
- Not yet tested:
- Notes:

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
- PLAN's Phase 2 list: Groq adapter, CI, structured logs, `demo_crash.py`, GIF.
- Future work beyond Phase 2: target-side fencing (DESIGN.md D23).
