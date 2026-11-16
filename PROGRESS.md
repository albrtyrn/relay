# Relay: progress

One entry per milestone. Scope and the exact exit tests are in DESIGN.md §8; this file records status only. A milestone is done when all its tests pass via `docker compose run --rm tests pytest` and Albert can explain every line kept.

Status values: `not started`, `in progress`, `done`.

## M1: Scaffolding

- Status: not started
- Started: / Finished:
- Scope: Dockerfile, Compose services, `dbinit`, relay schema (`migrations`, `migration_events`), create/get/list/health endpoints, test fixtures
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
- Scope: `transitions.py` (Albert), `service.py`, approve and reject endpoints bound to the SQL hash
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
- Scope: claim query (Albert), reclaim for `DRAFTING`, hooks, fake LLM, pglast policy, worker draft path, generation retry limit
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
- Scope: target schema and `relay_migrator` role, marker transaction (Albert), worker execute path, reclaim for `EXECUTING`, execution retry limit
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
- Scope: `idempotency_keys` table, reservation and replay (Albert), `Idempotency-Key` required on approve and reject
- Exit checks:
  - [ ] `test_idempotency.py`
  - [ ] `test_concurrent_approval.py` still passes with required keys
- Last test run (command, result):
- Not yet tested:
- Notes:
