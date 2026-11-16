# Relay: progress

One entry per milestone. Scope and the exact exit tests are in DESIGN.md §8; this file records status only. A milestone is done when all its tests pass via `docker compose run --rm tests pytest`.

Status values: `not started`, `in progress`, `done`.

## Current state (updated 2026-11-16)

- **Done:** M1 built and its automated checks pass (28 tests); the manual restart gate passed. Earlier: DESIGN.md written, all 25 §1 resolutions accepted, D8, D9, D13 accepted, Python fixed at 3.12.
- **Done since:** M2 built; all its tests pass (93 in the suite). Waiting for Albert's review and commit.
- **Next:** M3 (claim, leases, generation). It touches locks and leases, so it needs a plan and Albert's OK first.
- **Answered (Albert, 2026-11-16):**
  - Keep D16: one connection per request, no `psycopg_pool`.
  - No `pytest-repeat`. Repeat race tests with a shell loop (DESIGN §7.1 and §7.7 updated).
  - M2 plan choices accepted as D27, D28, D29, plus the READ COMMITTED assumption recorded with D27 and tested.
  - Pin READ COMMITTED explicitly in `db.connect` (done; the level no longer depends on the server default).
  - Do not install `httpx2`. The test run prints `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead.` This is known and ignored; `httpx` works today. Revisit only if `TestClient` stops working.
- **Open questions:**
  1. CLAUDE.md says "short leases or an injectable clock"; the accepted §1 #20 has tests backdate `lease_expires_at` instead. Confirm backdating is fine, or CLAUDE.md should be reworded.
  2. PLAN.md line 60 still says "e.g. `python:3.13-slim`". Not edited; update when PLAN is next touched.
  3. This session read DESIGN §8 as the M1 scope (14 listed tests), which is wider than the five tests in the prompt, and left `target_schema.sql` and the role to M4. Say so if you wanted them in M1.- **Untested:** see M1 and M2 below. Everything from M3 on has no code. The pglast wheel check was read from PyPI, not an image build (pglast is not installed until M3).

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

- Status: not started
- Started: / Finished:
- Scope: claim query (core operation), reclaim for `DRAFTING`, hooks, fake LLM, pglast policy, worker draft path, generation retry limit (D8)
- Exit checks:
  - [ ] `test_policy.py`
  - [ ] `test_fake_llm.py`
  - [ ] `test_claim.py`
  - [ ] `test_generation.py`
  - [ ] `test_reclaim.py` (drafting cases)
  - [ ] `test_stale_worker.py::test_stale_drafter_write_matches_zero_rows`
  - [ ] `test_crash_generation.py`
  - [ ] `test_worker_contention.py::test_generation_50_jobs_4_workers`
  - [ ] `test_retry_limit.py::test_generation_fails_after_max_attempts`
- Last test run (command, result):
- Not yet tested:
- Notes:

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
