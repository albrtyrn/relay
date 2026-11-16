# Relay: notes for Claude Code (build mode)

## What this is
Relay is a backend that gates AI-generated Postgres schema migrations behind human approval and executes them with a pool of workers. It must handle concurrent requests, worker crashes, retries, and uncertain outcomes without applying a migration twice.

We are in a time-boxed BUILD sprint. You write the code, including the four core operations. I will study the finished project later with an AI tutor, so the code, tests, and docs must be easy to learn from.

## Sources of truth
- PLAN.md: requirements and scope. v1 = the core (Weeks 1-3). Phase 2 only when I say so.
- DESIGN.md: how we build it (schema, transaction boundaries, layout, decisions). Follow it. If code must differ, update DESIGN.md in the same change and say why.
- PROGRESS.md: where we are. Read it at session start; update it before you stop.
- If PLAN.md, DESIGN.md, and the code disagree, stop and tell me instead of picking one.

## Architecture
- FastAPI: REST API for the migration lifecycle
- relay_db (Postgres): workflow state, leases, idempotency keys, audit events
- Workers (2+ processes): claim jobs, generate proposals, execute, recover expired leases
- target_db (Postgres): disposable demo database where approved migrations run
- LLM: fake adapter until Phase 2, then Groq
- Docker Compose locally; pytest against real Postgres

relay_db and target_db are separate: no transaction spans both.

States: PENDING -> DRAFTING -> WAITING_FOR_APPROVAL -> APPROVED -> EXECUTING -> SUCCEEDED, plus REJECTED and FAILED. Use exactly these names.

## Approved stack
Python 3.12, FastAPI, Pydantic v2, psycopg 3 (+ psycopg_pool), pglast, pytest, httpx, uvicorn. Raw SQL, no ORM. Schema in plain .sql files. Ask before adding anything else, and say what the no-library alternative is.

## How to work
- Work only on the milestone named in my prompt. Don't build beyond it.
- Anything touching transactions, locks, leases, or target_db: plan first. Name the invariant, the transaction boundaries, the failure cases, and the alternatives you rejected. Wait for my OK.
- Write the test first, watch it fail, then implement.
- After a change: run the relevant tests and report the exact commands and results. Never say tests passed if you didn't run them. List what is still untested.
- When something fails, state the cause before changing code. If two fix attempts fail, stop and summarize what you tried, the evidence, and your best hypothesis.
- Don't commit, push, tag, or merge. I do that.

## Make it teachable
- The four core operations (transition helper, claim query, target marker transaction, idempotency reservation) each get a header comment: precondition, the atomic operation, how it fails, the invariant it protects.
- Every test has a docstring naming the guarantee and invariant it proves.
- Every real decision goes in DESIGN.md's decision log: what we chose, what we rejected, why.
- Prefer boring, explicit code over clever code.

## Invariants (never break these)
1. Every status change goes through the transition helper.
2. Never hold a transaction or row lock across an LLM call or other slow work.
3. Every worker write to Relay that depends on its claim checks the current lease_token and expected status, and commits atomically with its audit event.
4. On the target, insert the relay_applied marker before the DDL, in the same transaction. An existing marker counts as success only if its sql_hash matches the approved hash.
5. Integration tests use real Postgres, never mocks.
6. The README claims no guarantee that a test doesn't demonstrate.

## Repo practices
- Secrets in .env (gitignored); commit only .env.example.
- Tests reset database state; no test depends on another.
- Concurrency tests force their races with barriers or events, never sleeps. Use short leases or an injectable clock.
- Before you stop: update PROGRESS.md (done, next, open questions, untested) and draft a commit message for me.