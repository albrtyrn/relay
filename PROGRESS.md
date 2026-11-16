# Relay: progress

One entry per milestone. Scope and the exact exit tests are in DESIGN.md §8; this file records status only. A milestone is done when all its tests pass via `docker compose run --rm tests pytest`.

Status values: `not started`, `in progress`, `done`.

## Current state (updated 2026-11-16)

- **Done:** M1 built and its automated checks pass (28 tests); the manual restart gate passed. Earlier: DESIGN.md written, all 25 §1 resolutions accepted, D8, D9, D13 accepted, Python fixed at 3.12.
- **Next:** M2 (transition helper, approve, reject). It touches transactions and row locks, so CLAUDE.md requires a plan and Albert's OK first. Waiting for Albert's go.
- **Open questions:**
  1. `psycopg_pool` is in CLAUDE.md's approved stack, but D16 opens one connection per API request and uses no pool. M1 followed D16. Keep it, or use the pool?
  2. `pytest-repeat` (for `--count=20`, needed from M2) is not in the approved stack. Approve it, or use a shell loop?
  3. CLAUDE.md says "short leases or an injectable clock"; the accepted §1 #20 has tests backdate `lease_expires_at` instead. Confirm backdating is fine, or CLAUDE.md should be reworded.
  4. PLAN.md line 60 still says "e.g. `python:3.13-slim`". Not edited; update when PLAN is next touched.
  5. The test run prints `StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead.` `httpx` is in the approved stack and works today. Ask before adding `httpx2`? (No-library alternative: ignore the warning until it breaks.)
  6. This session read DESIGN §8 as the M1 scope (14 listed tests), which is wider than the five tests in the prompt, and left `target_schema.sql` and the role to M4. Say so if you wanted them in M1.
- **Untested:** see M1 below. Everything from M2 on has no code. The pglast wheel check was read from PyPI, not an image build (pglast is not installed until M3).

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

- Status: not started
- Started: / Finished:
- Scope: `transitions.py` (core operation), `service.py`, approve and reject endpoints bound to the SQL hash
- Exit checks:
  - [ ] `test_transitions.py`
  - [ ] `test_approve_reject.py`
  - [ ] `test_concurrent_approval.py`
- Last test run (command, result):
- Not yet tested:
- Notes:

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
