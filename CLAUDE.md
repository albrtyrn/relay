# Relay: notes for Claude Code

## What this is
Relay is a learning project: a backend that gates AI-generated Postgres schema migrations behind human approval and executes them with a pool of workers. It must handle concurrent requests, worker crashes, retries, and uncertain outcomes without applying a migration twice.

I'm learning backend and distributed systems by building it. I must be able to explain every line I keep.

## Architecture
- FastAPI: REST API for the migration lifecycle
- relay_db (Postgres): workflow state, leases, idempotency keys, audit events
- Workers (2+ processes): claim jobs, generate proposals, execute, recover expired leases
- target_db (Postgres): disposable demo database where approved migrations run
- LLM: fake adapter until Phase 2, then Groq
- Docker Compose locally; pytest against real Postgres
- Environment: API, workers, and tests run in Docker Linux containers from one pinned image (e.g. python:3.13-slim) with exact versions in requirements.txt. Nothing depends on host Python (dev machines: Windows 11 ARM64 and x64).
- DB layer: psycopg 3, synchronous, plain SQL. No SQLAlchemy or asyncpg. FastAPI endpoints are plain `def`.

relay_db and target_db are separate: no transaction spans both.

States: PENDING -> DRAFTING -> WAITING_FOR_APPROVAL -> APPROVED -> EXECUTING -> SUCCEEDED, plus REJECTED and FAILED. Use exactly these names.

## Scope
- PLAN.md defines milestones and requirements. Read the relevant section and the existing code and tests before proposing anything.
- Don't build beyond the current milestone without asking.
- SQL: narrow allow-list, validated with a Postgres parser. LLM output is never trusted. Execute only the exact approved SQL (hash-checked).
- Don't claim exactly-once execution. Guarantees cover only the supported, tested workflow.

## How to work with me
- Before editing: explain the approach, transaction boundaries, alternatives, and failure cases, and name the invariant involved. Wait for my OK.
- I'm learning these topics: explain new terms in plain language the first time you use them, and say why something exists before how it works.
- One small, reviewable change at a time.
- No new dependency without saying why and what the alternative is.
- When I report an error, explain the cause first. Don't fix it unless I ask.
- I write the transition helper, claim query, target marker transaction, and idempotency reservation myself. Review them; don't rewrite them. For everything else, complete worked implementations with explanations are fine.
- After a change: summarize it, run the relevant tests, and report the exact commands and results. Never say tests passed if you didn't run them. Note what's still untested.
- Don't commit, push, or merge unless I ask.
- If code and docs disagree, point it out instead of picking one.

## Invariants (never break these)
1. Every status change goes through the transition helper.
2. Never hold a transaction or row lock across an LLM call or other slow work.
3. Every worker write to Relay that depends on its claim checks the current lease_token and expected status, and commits atomically with its audit event. The lease_token is an ownership token, not a fencing token: it stops a stale worker from changing Relay's state; the relay_applied marker protects the target.
4. On the target, insert the relay_applied marker before the DDL, in the same transaction. An existing marker counts as success only if its sql_hash matches the approved hash.
5. Integration tests use real Postgres, never mocks.
6. The README claims no guarantee that a test doesn't demonstrate.

## Repo practices
- Secrets in .env (gitignored); commit only .env.example.
- Tests don't depend on leftover database state.
- Concurrency tests force their races with explicit synchronization, not sleeps.
- At session end, help me write one entry for my learning notes (I keep them in Notion): concept, my explanation, one failure mode.