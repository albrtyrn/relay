# Relay: progress

One entry per milestone. Scope and the exact exit tests are in DESIGN.md §8; this file records status only. A milestone is done when all its tests pass via `docker compose run --rm tests pytest`.

Status values: `not started`, `in progress`, `done`.

## Current state (updated 2026-11-16)

- **Done:** DESIGN.md written. All 25 §1 resolutions accepted. D8 (retry limit in M3/M4), D9 (DDL timeouts are terminal `FAILED`; marker-insert timeouts retry), and D13 (thread-based concurrency tests) accepted. Python fixed at 3.12. Late-apply gap kept as a documented non-guarantee, with the fencing fix logged as future work (D23). README wording rules added (DESIGN.md §10).
- **Next:** M1 scaffolding. Not started; waiting for Albert's go.
- **Open questions:**
  1. `psycopg_pool` is in CLAUDE.md's approved stack, but D16 opens one connection per API request and uses no pool. Keep D16, or use the pool?
  2. `pytest-repeat` (for `--count=20`) is not in the approved stack. Approve it, or use a shell loop?
  3. CLAUDE.md says "short leases or an injectable clock"; the accepted §1 #20 has tests backdate `lease_expires_at` instead. Confirm backdating is fine, or CLAUDE.md should be reworded.
  4. PLAN.md line 60 still says "e.g. `python:3.13-slim`". Not edited; update when PLAN is next touched.
- **Untested:** everything. No application code exists yet. The pglast wheel check was read from PyPI, not an image build.

## M1: Scaffolding

- Status: not started
- Started: / Finished:
- Scope: Dockerfile (Python 3.12), Compose services, `dbinit`, relay schema (`migrations`, `migration_events`), create/get/list/health endpoints, test fixtures
- Exit checks:
  - [ ] `test_health.py`
  - [ ] `test_schema.py`
  - [ ] `test_create_get.py`
  - [ ] `test_isolation.py`
  - [ ] Manual: record survives `docker compose down && docker compose up -d`
  - [ ] Image builds and suite passes on both machines (ARM64 and x64)
- Last test run (command, result):
- Not yet tested:
- Notes:

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
