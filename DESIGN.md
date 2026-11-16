# Relay: build blueprint

This file says how to build what PLAN.md describes. PLAN.md stays the source for *what* and *why*; where the two disagree, §1 lists the disagreement and the resolution. All §1 resolutions were accepted by Albert on 2026-11-16.

Code marked **[core]** is one of the four core operations. Each gets a header comment in the code stating its precondition, its atomic operation, how it fails, and the invariant it protects (CLAUDE.md, "Make it teachable"). The SQL here is the spec those implementations must match.

Conventions used everywhere below:

- **Autocommit connections, explicit transactions.** Every psycopg connection is opened with `autocommit=True`, and every transaction is a visible `with conn.transaction():` block. Outside such a block, no transaction is open. (psycopg's default silently opens a transaction on the first statement and keeps it open, which would break invariant 2 during an LLM call.)
- **Isolation level:** READ COMMITTED, the Postgres default. The compare-and-set logic depends on it (§4.1).
- **Time:** every lease comparison uses the Postgres clock (`now()`), never a worker's clock.
- **Hash:** `sha256(sql.encode("utf-8")).hexdigest()` over the exact stored text. No whitespace or case normalization.
- **Actors** in the audit log: `user:<X-User>`, `worker:<worker_id>`, `reclaimer:<worker_id>`.

---

## 1. Spec issues

**Status: all 25 resolutions accepted (Albert, 2026-11-16).** They are now part of the spec.

| # | Issue in PLAN.md | Resolution (accepted) |
| --- | --- | --- |
| 1 | **Idempotency-Key timing.** The endpoint table says approve/reject *require* the key, but the roadmap builds approve on Day 12 and idempotency on Days 20–21. The same table says `POST /migrations` gets a key "in Phase 2", but the Phase 2 list does not mention it. | Approve/reject: header is accepted and ignored in M2–M4, required from M5 (missing → `422`). `POST /migrations`: no key in v1 or Phase 2; a duplicate create only makes a second `PENDING` row that a reviewer rejects. Remove "added in Phase 2" from the table. |
| 2 | **No retry limit in v1.** A job that crashes its worker every time is reclaimed forever. | Pull a minimal limit forward: `MAX_ATTEMPTS` (default 5), enforced by the worker that claims the job (§5), in M3 for generation and M4 for execution, although PLAN lists it under Phase 2 (D8). |
| 3 | **Invariant 1 vs. the claim query.** "Nothing else writes `status`", yet PLAN's claim is its own `UPDATE ... RETURNING` that changes status. | Claim = `SELECT ... FOR UPDATE SKIP LOCKED` to pick and lock the row, then the transition helper in the same transaction (§4.2). The helper stays the only `UPDATE` of `status`. The initial `INSERT` (status `PENDING`) is the one allowed non-helper write. |
| 4 | **Invariant 3 vs. reclaim.** The "lease expires" edges are written by a worker that does not own the lease. | Reclaim goes through the helper, guarded by the *expired* token it just read plus `lease_expires_at < now()` under a row lock (§4.5). |
| 5 | **`FAILED` does not prove the target is unchanged.** A stale worker frozen before its target transaction can wake up and apply the DDL after another worker marked the job `FAILED`. The marker stops a double apply, not a late one. | Document as a non-guarantee. Narrow it with `idle_in_transaction_session_timeout` on the target transaction and a lease re-check just before it. Closing it fully needs a fencing check inside target_db (D23); future work, not built. |
| 6 | **`lock_timeout` also applies to the marker insert.** A second worker waiting on the first worker's uncommitted marker can time out. That is contention, not a failed migration. | Errors are classified by step: marker step → retry; DDL step → `FAILED` (§4.3). |
| 7 | **Timeout outcome unspecified.** PLAN says "SQL error → FAILED" but not whether `lock_timeout` / `statement_timeout` on the DDL retries. | `FAILED` in v1. Simple and deterministic; a human resubmits. |
| 8 | **Lost commit acknowledgement.** If the connection drops during the target `COMMIT`, the worker cannot know the outcome. | Never mark `FAILED` on an unknown outcome. Abandon the lease; the next worker's marker insert reveals what happened. |
| 9 | **No lease renewal.** Nothing says what happens when work outlasts the lease. | No heartbeat in v1. Defaults keep the lease longer than the work: `LEASE_SECONDS=60` > LLM timeout 20 s + `lock_timeout` 5 s + `statement_timeout` 15 s. A worker that overruns loses its final write and the work is redone, which is safe. |
| 10 | **`attempts` semantics.** Not defined; unclear if generation retries eat the execution budget. | `attempts` = claims in the current phase. Reset to 0 when generation succeeds. Full history is in `migration_events`. |
| 11 | **`422` for "policy violation".** The API never receives SQL in v1 (no editing), so it can never return this. | Policy violations happen in the worker and end in `FAILED` with `error` set. `422` means malformed input only. |
| 12 | **"Submit allowed SQL that errors" (failed-DDL test).** There is no way to submit SQL; the API takes a description. | The fake LLM maps descriptions to SQL deterministically (§6). Failed DDL = add a column that already exists. |
| 13 | **Reject reason has no column.** | `migration_events.detail` (nullable text). Also holds the error text on `FAILED` events. |
| 14 | **`risk_level` values and source undefined.** | The LLM returns it; allowed values `LOW`, `MEDIUM`, `HIGH`. Informational only: no code path branches on it. An invalid value is an LLM permanent error. |
| 15 | **Missing `X-User`.** | Required on every `POST`; missing → `422`. Still an untrusted label. |
| 16 | **Idempotency scope.** Is `endpoint` the route or the full path? Are error responses saved? Do keys expire? | `endpoint` is `approve` or `reject`. `request_hash` covers the migration id and the body, so reusing a key on another migration is `422`. Every response the handler returns normally is saved (`200`, `404`, `409`); exceptions roll the key back. Keys never expire in v1. |
| 17 | **Creation is not a transition** but the audit log should show it. | The create transaction also inserts one event with `from_status = NULL`, `to_status = 'PENDING'`. |
| 18 | **`GET /migrations` has no paging.** | `?status=<state>&limit=<1..200, default 50>`, newest first. No cursor. |
| 19 | **`/health` failure code** is not in the status list. | `200 {"status":"ok"}` when `SELECT 1` works on relay_db, else `503`. |
| 20 | **"Short test lease (e.g. 2 seconds)"** contradicts "never sleep past a real lease". | Tests backdate `lease_expires_at` instead of waiting (§7). |
| 21 | **Two Phase 2 tests are already core behavior:** stale hash cannot be approved; terminal states reject every transition. | Both land in M2, since the helper and approve endpoint provide them. |
| 22 | **Roadmap order.** PLAN builds claim (Days 8–10) before approve (Day 12); this blueprint's M2 is approve/reject and M3 is claim. | Follow M1–M5. Tests in M2 seed rows directly in `WAITING_FOR_APPROVAL`. |
| 23 | **Compose only creates `relay_db`.** PLAN wants `target_db` in the same container. | A `dbinit` step creates databases, the role, and the schema (§2, §3). |
| 24 | **Code vs. docs:** README hard-codes `psql -U relay`, but the user comes from `.env`. | Fix the README line at M1. |
| 25 | **Python version.** PLAN says "e.g. `python:3.13-slim`"; CLAUDE.md's approved stack says Python 3.12. | Python 3.12 (`python:3.12-slim`, exact tag pinned at M1). pglast 8.8 ships `cp312` manylinux wheels for both x86_64 and aarch64 (checked on PyPI, 2026-11-16). PLAN's line is an example, not a requirement; update it when PLAN is next edited. |

---

## 2. Repo layout

```
relay/
├── CLAUDE.md               working rules for Claude Code
├── PLAN.md                 what to build and why
├── DESIGN.md               this file
├── PROGRESS.md             milestone status, one entry per milestone
├── README.md               setup and (later) tested guarantees only
├── LICENSE
├── .env.example            fake values for every variable in §7.6
├── .gitattributes          forces LF line endings
├── .gitignore
├── docker-compose.yml      postgres, dbinit, api, worker, tests
├── Dockerfile              one pinned Python 3.12 image for api, worker, dbinit, tests
├── requirements.txt        exact pinned versions
├── pytest.ini              test paths and markers
├── db/
│   ├── relay_schema.sql    relay_db tables, constraints, indexes (§3.1)
│   └── target_schema.sql   demo tables, relay_applied, grants (§3.2); drops first, so it doubles as the test reset
├── relay/                  the Python package
│   ├── __init__.py
│   ├── config.py           reads env vars once into a frozen Settings dataclass
│   ├── db.py               connect helpers (autocommit on) for relay_db and target_db
│   ├── dbinit.py           `python -m relay.dbinit`: create databases and role, apply schema; `--reset` drops first
│   ├── sqlhash.py          the one sha256 function
│   ├── transitions.py      [core] transition helper and the table of legal (from, to) pairs
│   ├── repo.py             create_migration insert and read queries (get, list, events)
│   ├── service.py          approve/reject decisions (404/409) with no HTTP in it
│   ├── idempotency.py      [core] key reservation and replay (M5)
│   ├── api.py              FastAPI app, routes, Pydantic models, error mapping
│   ├── claim.py            [core] claim query; also the reclaim sweep
│   ├── target.py           [core] marker-first target transaction (M4)
│   ├── policy.py           pglast allow-list (§6.2)
│   ├── hooks.py            named pause/crash points for tests and the demo (§7.5)
│   ├── worker.py           worker loop, draft step, execute step; `python -m relay.worker`
│   └── llm/
│       ├── __init__.py     Proposal, the two error types, LLMClient protocol, get_llm()
│       └── fake.py         deterministic fake (groq.py arrives in Phase 2)
├── scripts/
│   └── demo_crash.py       Phase 2; uses the same hook as the crash test
└── tests/
    ├── conftest.py         database creation, per-test reset, shared fixtures
    ├── helpers.py          seed rows, expire_lease, run threads behind a Barrier, spawn a worker process
    └── test_*.py           one file per behavior, listed in §8
```

Schema files grow with the milestones (M1: `migrations`, `migration_events`; M4: target; M5: `idempotency_keys`). Data is disposable, so a schema change means `dbinit --reset`, not a migration tool.

---

## 3. Schema

### 3.1 relay_db (`db/relay_schema.sql`)

```sql
CREATE TABLE migrations (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    description        text NOT NULL CHECK (length(description) BETWEEN 1 AND 2000),
    status             text NOT NULL DEFAULT 'PENDING',
    proposed_sql       text,
    proposed_sql_hash  text CHECK (proposed_sql_hash ~ '^[0-9a-f]{64}$'),
    approved_sql_hash  text CHECK (approved_sql_hash ~ '^[0-9a-f]{64}$'),
    risk_level         text CHECK (risk_level IN ('LOW', 'MEDIUM', 'HIGH')),
    risk_reason        text,
    lease_token        uuid,
    lease_expires_at   timestamptz,
    attempts           integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    error              text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT status_valid CHECK (status IN (
        'PENDING', 'DRAFTING', 'WAITING_FOR_APPROVAL', 'APPROVED',
        'EXECUTING', 'SUCCEEDED', 'REJECTED', 'FAILED')),

    -- A lease exists exactly while a worker owns the row.
    CONSTRAINT lease_matches_status CHECK (
        (status IN ('DRAFTING', 'EXECUTING')) = (lease_token IS NOT NULL)),
    CONSTRAINT lease_columns_together CHECK (
        (lease_token IS NULL) = (lease_expires_at IS NULL)),

    -- A proposal exists from WAITING_FOR_APPROVAL onward.
    CONSTRAINT proposal_present CHECK (
        status NOT IN ('WAITING_FOR_APPROVAL', 'APPROVED', 'EXECUTING', 'SUCCEEDED', 'REJECTED')
        OR (proposed_sql IS NOT NULL AND proposed_sql_hash IS NOT NULL)),

    -- An approval exists from APPROVED onward.
    CONSTRAINT approval_present CHECK (
        status NOT IN ('APPROVED', 'EXECUTING', 'SUCCEEDED')
        OR approved_sql_hash IS NOT NULL)
);

-- The queue: what the claim query scans, oldest first.
CREATE INDEX migrations_claimable ON migrations (created_at)
    WHERE status IN ('PENDING', 'APPROVED');

-- What the reclaim sweep scans.
CREATE INDEX migrations_leased ON migrations (lease_expires_at)
    WHERE status IN ('DRAFTING', 'EXECUTING');

-- GET /migrations?status=...
CREATE INDEX migrations_status_created ON migrations (status, created_at DESC);

CREATE TABLE migration_events (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    migration_id  uuid NOT NULL REFERENCES migrations (id),
    from_status   text,            -- NULL only for the creation event
    to_status     text NOT NULL,
    actor         text NOT NULL CHECK (length(actor) BETWEEN 1 AND 200),
    detail        text,            -- reject reason or error text
    created_at    timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT from_status_valid CHECK (from_status IN (
        'PENDING', 'DRAFTING', 'WAITING_FOR_APPROVAL', 'APPROVED',
        'EXECUTING', 'SUCCEEDED', 'REJECTED', 'FAILED')),
    CONSTRAINT to_status_valid CHECK (to_status IN (
        'PENDING', 'DRAFTING', 'WAITING_FOR_APPROVAL', 'APPROVED',
        'EXECUTING', 'SUCCEEDED', 'REJECTED', 'FAILED'))
);

CREATE INDEX migration_events_by_migration ON migration_events (migration_id, id);

-- M5
CREATE TABLE idempotency_keys (
    endpoint       text NOT NULL CHECK (endpoint IN ('approve', 'reject')),
    key            text NOT NULL CHECK (length(key) BETWEEN 1 AND 200),
    request_hash   text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    status_code    integer,        -- filled in before the reserving transaction commits
    response_body  jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (endpoint, key)
);

-- The target role must not reach Relay's state.
REVOKE CONNECT ON DATABASE relay_db FROM PUBLIC;
```

Notes:

- The CHECK constraints are a second line of defense. A helper bug that leaves a lease on a `PENDING` row fails loudly instead of corrupting state.
- `migration_events` is append-only by convention: no code updates or deletes it. Not enforced by grants, because Relay connects as the admin role (decision D15).
- `status_code` is nullable only because the row is inserted before the work and completed in the same transaction. A committed row with `NULL` is a bug and replays as `500`.

### 3.2 target_db

Role, created by `dbinit` (the password comes from `TARGET_MIGRATOR_PASSWORD`; roles are cluster-wide, so this runs once):

```sql
CREATE ROLE relay_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    PASSWORD '<TARGET_MIGRATOR_PASSWORD>';
```

`db/target_schema.sql`, run as the admin role:

```sql
DROP TABLE IF EXISTS orders, customers, relay_applied;

-- Explicit, though it is already the default for new databases since Postgres 15.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

CREATE TABLE customers (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email       text NOT NULL UNIQUE,
    full_name   text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE orders (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id  bigint NOT NULL REFERENCES customers (id),
    total_cents  integer NOT NULL CHECK (total_cents >= 0),
    status       text NOT NULL DEFAULT 'new',
    created_at   timestamptz NOT NULL DEFAULT now()
);

INSERT INTO customers (email, full_name) VALUES
    ('ada@example.com', 'Ada Lovelace'),
    ('alan@example.com', 'Alan Turing');
INSERT INTO orders (customer_id, total_cents) VALUES (1, 1250), (2, 990);

-- Ownership is what permits ALTER TABLE; there is no narrower grant.
ALTER TABLE customers OWNER TO relay_migrator;
ALTER TABLE orders    OWNER TO relay_migrator;

-- Owned by the admin role. The migrator can add and read markers, never change or remove them.
CREATE TABLE relay_applied (
    migration_id  uuid PRIMARY KEY,
    sql_hash      text NOT NULL CHECK (sql_hash ~ '^[0-9a-f]{64}$'),
    applied_at    timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT ON relay_applied TO relay_migrator;
```

What `relay_migrator` can do: alter, read, and write `customers` and `orders`; insert into and read `relay_applied`. What it cannot: create tables, touch `relay_applied` otherwise, connect to `relay_db`.

---

## 4. The four core operations and reclaim

### 4.1 Transition helper **[core]** (M2)

`transition(conn, migration_id, from_status, to_status, actor, *, lease_token=None, detail=None, set_fields=None) -> bool`

**The helper never opens or commits a transaction. The caller owns it.** That is what lets the claim, the idempotency key, and the status change share one commit.

SQL (two statements, inside the caller's transaction):

```sql
UPDATE migrations
SET status = %(to_status)s,
    updated_at = now()
    -- plus ", <col> = %(<col>)s" for each key in set_fields
WHERE id = %(id)s
  AND status = %(from_status)s
  -- plus "AND lease_token = %(lease_token)s" when lease_token is given
RETURNING id;

-- only if the UPDATE returned a row:
INSERT INTO migration_events (migration_id, from_status, to_status, actor, detail)
VALUES (%(id)s, %(from_status)s, %(to_status)s, %(actor)s, %(detail)s);
```

Rules checked in Python before any SQL (a violation is a bug, so raise `ValueError`):

- `(from_status, to_status)` must be in the legal table below.
- `from_status IN ('DRAFTING', 'EXECUTING')` requires `lease_token`. This makes invariant 3 impossible to skip.
- `set_fields` keys must be in a fixed allow-list of column names: `proposed_sql`, `proposed_sql_hash`, `risk_level`, `risk_reason`, `approved_sql_hash`, `lease_token`, `lease_expires_at`, `attempts`, `error`. Column names are composed with `psycopg.sql.Identifier`, values are always parameters.

Legal transitions and what each sets:

| From → To | Caller | Token guard | `set_fields` |
| --- | --- | --- | --- |
| `PENDING` → `DRAFTING` | claim | none (row is locked by the claim's `SELECT`) | `lease_token`, `lease_expires_at`, `attempts` |
| `DRAFTING` → `WAITING_FOR_APPROVAL` | worker | own token | `proposed_sql`, `proposed_sql_hash`, `risk_level`, `risk_reason`, `attempts = 0`, lease columns = `NULL` |
| `DRAFTING` → `FAILED` | worker | own token | `error`, lease columns = `NULL` |
| `DRAFTING` → `PENDING` | reclaim | expired token | lease columns = `NULL` |
| `WAITING_FOR_APPROVAL` → `APPROVED` | API | none | `approved_sql_hash` |
| `WAITING_FOR_APPROVAL` → `REJECTED` | API | none | none (reason goes in `detail`) |
| `APPROVED` → `EXECUTING` | claim | none (row locked) | `lease_token`, `lease_expires_at`, `attempts` |
| `EXECUTING` → `SUCCEEDED` | worker | own token | lease columns = `NULL` |
| `EXECUTING` → `FAILED` | worker | own token | `error`, lease columns = `NULL` |
| `EXECUTING` → `APPROVED` | reclaim | expired token | lease columns = `NULL` |

| | |
| --- | --- |
| **Transaction** | The caller's. `UPDATE` and `INSERT` commit together or not at all. |
| **Precondition** | An open transaction on an autocommit connection at READ COMMITTED. |
| **0 rows** | Return `False`, insert no event. The row is in another state or the token is not the caller's. API maps it to `409`; a worker logs "lost the job" and stops. Never retried. |
| **Conflict** | Two transactions update the same row: the second waits on the row lock. When the first commits, Postgres re-checks the second's `WHERE` against the new row, it no longer matches, 0 rows. If the first rolls back, the second proceeds. This re-check is READ COMMITTED behavior; under REPEATABLE READ it would be a serialization error instead. |
| **Crash before commit** | Postgres rolls back both statements. Nothing happened. |
| **Crash after commit** | Both are durable. The caller may never see the answer: the API client retries and gets `409` (a replay after M5); for a worker, the lease or the next state handles it. |
| **Invariant** | 1 (single writer of `status`), 3 (token check, atomic with the audit event). |

### 4.2 Claim query **[core]** (M3)

`claim_one(conn, worker_id, lease_seconds) -> Claim | None`. One transaction:

```sql
-- BEGIN
SELECT id, status, attempts, description, proposed_sql, approved_sql_hash,
       now() + make_interval(secs => %(lease_seconds)s) AS lease_expires_at
FROM migrations
WHERE status IN ('PENDING', 'APPROVED')
ORDER BY created_at
LIMIT 1
FOR UPDATE SKIP LOCKED;

-- if a row came back, in the same transaction:
--   transition(id, 'PENDING' -> 'DRAFTING'  or  'APPROVED' -> 'EXECUTING',
--              actor = 'worker:<worker_id>',
--              set_fields = {lease_token: <fresh uuid4>,
--                            lease_expires_at: <value selected above>,
--                            attempts: <attempts + 1>})
-- COMMIT
```

`FOR UPDATE` locks the chosen row until commit. `SKIP LOCKED` makes other workers pass over a locked row instead of waiting for it.

Two properties this operation must have, both tested in `test_claim.py`:

- **The lease is set by the helper, in the same `UPDATE` as the status change.** `lease_token`, `lease_expires_at`, and `attempts` travel in `set_fields`, so status, lease, and audit event commit together. There is never a committed `DRAFTING`/`EXECUTING` row without a lease (the `lease_matches_status` CHECK would reject it anyway).
- **The claim commits before any LLM or target work.** `claim_one` returns only after `COMMIT`. The caller holds no transaction and no row lock when it calls the LLM or opens the target connection.

| | |
| --- | --- |
| **Transaction** | One short Relay transaction: select, transition, commit. |
| **Precondition** | No transaction open on the connection. |
| **0 rows** | Queue is empty or every candidate is locked by another claimer. Return `None`; the loop sleeps `POLL_SECONDS` and tries again. |
| **Conflict** | None by construction: a competitor's row is skipped, not waited on. The helper's `UPDATE` cannot hit 0 rows because this transaction holds the row lock; if it ever does, raise. |
| **Crash before commit** | Lock released, row still `PENDING`/`APPROVED`, another worker takes it. |
| **Crash after commit** | Row is `DRAFTING`/`EXECUTING` with a live lease and a dead owner. Reclaim (§4.5) returns it to the queue after expiry. |
| **Invariant** | 2 (commit before work) and 3 (the fresh token is the proof of ownership for later writes). |
| **Does not guarantee** | Strict oldest-first order under contention, or that a job is processed once. `SKIP LOCKED` only prevents two *simultaneous* claims; the lease and the marker cover everything after the commit. |

### 4.3 Target marker transaction **[core]** (M4)

`apply_on_target(target_conn, migration_id, approved_sql, approved_hash, settings) -> APPLIED | ALREADY_APPLIED | HASH_MISMATCH`, or raises `DDLFailed`.

Checks before any target work, using the row returned by the claim (no lock held): `sha256(proposed_sql) == approved_sql_hash`, and `policy.validate(proposed_sql)` passes. Either failing → `EXECUTING → FAILED`, target untouched.

One transaction on `target_db`, connected as `relay_migrator`:

```sql
-- BEGIN
SELECT set_config('lock_timeout', %(lock_timeout)s, true);        -- e.g. '5s';  true = this transaction only
SELECT set_config('statement_timeout', %(statement_timeout)s, true);            -- e.g. '15s'
SELECT set_config('idle_in_transaction_session_timeout', %(idle_timeout)s, true); -- e.g. '30s'

-- step "marker"
INSERT INTO relay_applied (migration_id, sql_hash)
VALUES (%(id)s, %(approved_hash)s)
ON CONFLICT (migration_id) DO NOTHING
RETURNING migration_id;

-- step "ddl": only if the INSERT returned a row
<the exact approved SQL, as stored>

-- only if the INSERT returned no row
SELECT sql_hash FROM relay_applied WHERE migration_id = %(id)s;
-- COMMIT
```

Then `hooks.fire("execute.after_target_commit")`, then a separate Relay transaction: `transition(EXECUTING → SUCCEEDED, lease_token = mine)`.

`set_config(..., true)` is used instead of `SET LOCAL` because `SET` cannot take bound parameters.

Outcomes:

| What happened | Result | Relay write (own token) |
| --- | --- | --- |
| Marker inserted, DDL ran, commit acknowledged | `APPLIED` | → `SUCCEEDED` |
| Marker already there, hash equal | `ALREADY_APPLIED`, no DDL | → `SUCCEEDED` |
| Marker already there, hash differs | `HASH_MISMATCH`, no DDL | → `FAILED` |
| Server rejects the DDL statement (error has a SQLSTATE, connection still healthy) **and** the rollback is confirmed | `DDLFailed(sqlstate, message)`; marker rolled back with it | → `FAILED`, `error` saved |
| Anything else: error on the marker step (including `lock_timeout` while waiting on another worker's marker), connection lost at any point, error during `COMMIT` | exception propagates, outcome unknown | none. Abandon the lease; the next attempt's marker insert shows what really happened |

| | |
| --- | --- |
| **Transaction** | One target transaction. No Relay transaction is open while it runs. |
| **Precondition** | Worker holds a committed `EXECUTING` claim; hash and policy checks passed. |
| **0 rows (marker insert)** | A marker exists. Read its hash: equal → skip the DDL; different → `HASH_MISMATCH`. |
| **Conflict** | Two workers insert the same `migration_id`: the second waits on the primary key until the first finishes. First commits → second gets 0 rows and skips. First rolls back → second inserts and runs the DDL. Wait longer than `lock_timeout` → second errors on the marker step and retries later. |
| **Crash before `BEGIN`** | Nothing on the target. Lease expires, job retried. |
| **Crash after marker, before or during DDL** | Transaction never committed; Postgres rolls back marker and DDL together. Retry starts clean. |
| **Crash during `COMMIT`** | Unknown to the worker, but atomic on the target: both or neither. The next attempt finds out. |
| **Crash after target commit, before the Relay write** | Target has marker + DDL, Relay still says `EXECUTING`. Lease expires → reclaim → next worker's insert conflicts, hash matches → `SUCCEEDED` with no second DDL. |
| **0 rows on the final Relay write** | Lease was lost. Stop. Whoever holds the job now will find the marker. |
| **Invariant** | 4 (marker first, same transaction, hash must match), and the "no transaction spans both databases" rule. |

### 4.4 Idempotency reservation **[core]** (M5)

Wraps approve and reject. `request_hash = sha256(canonical JSON of {"migration_id": ..., "body": ...})` with sorted keys.

One Relay transaction per request:

```sql
-- BEGIN
INSERT INTO idempotency_keys (endpoint, key, request_hash)
VALUES (%(endpoint)s, %(key)s, %(request_hash)s)
ON CONFLICT (endpoint, key) DO NOTHING
RETURNING key;

-- Row returned: this request owns the key.
--   Run the real work (service.approve / service.reject, which calls the transition helper),
--   then save its answer:
UPDATE idempotency_keys
SET status_code = %(status_code)s, response_body = %(response_body)s
WHERE endpoint = %(endpoint)s AND key = %(key)s;

-- No row returned: the key already exists and is committed.
SELECT request_hash, status_code, response_body
FROM idempotency_keys
WHERE endpoint = %(endpoint)s AND key = %(key)s;
--   request_hash differs -> 422, nothing else happens
--   otherwise            -> return the saved status_code and response_body
-- COMMIT
```

The service layer returns `(status_code, body)` instead of raising for `404`/`409`, so those answers are saved and replayed too.

| | |
| --- | --- |
| **Transaction** | One Relay transaction holding the key insert, the status change, its audit event, and the saved response. Database work only, so invariant 2 holds. |
| **Precondition** | `Idempotency-Key` header present (else `422` before any SQL). |
| **0 rows (insert)** | Key already used. Compare hashes: same → replay the saved response; different → `422`. |
| **Conflict** | Same key at the same moment: the second `INSERT` waits on the unique index. Winner commits → second gets 0 rows and replays. Winner rolls back → second inserts and does the work itself. The second holds no locks while it waits, so no deadlock. |
| **Crash or exception before commit** | Key, status change, event, and response all roll back. A retry starts fresh. |
| **Crash after commit, before the HTTP response** | Everything is durable. The retry replays the saved response. This is the case idempotency exists for. |
| **Invariant** | "The same request counts once." Composes with 4.1: the helper stops two *different* requests from both winning; this stops one request from acting twice. |

### 4.5 Lease-expiry reclaim (M3 for `DRAFTING`, M4 for `EXECUTING`)

Every worker runs this at the top of each loop. No separate reaper process. One transaction:

```sql
-- BEGIN
SELECT id, status, lease_token
FROM migrations
WHERE status IN ('DRAFTING', 'EXECUTING')
  AND lease_expires_at < now()
ORDER BY lease_expires_at
LIMIT 10
FOR UPDATE SKIP LOCKED;

-- for each row, in the same transaction:
--   transition(id, 'DRAFTING' -> 'PENDING'  or  'EXECUTING' -> 'APPROVED',
--              actor = 'reclaimer:<worker_id>',
--              lease_token = <the expired token just read>,
--              set_fields = {lease_token: NULL, lease_expires_at: NULL})
-- COMMIT
```

- **Why the token guard is safe here:** the row is locked and the token is the one read under that lock, so the guard states "I am ending exactly this expired lease."
- **0 rows:** nothing expired, or another reclaimer has those rows locked.
- **Race with the slow owner:** the owner's final write and the reclaim both update the same row. Whichever commits first wins; the other matches 0 rows (owner) or skips the locked row (reclaimer). Either way one consistent outcome.
- **An expired lease that nobody has reclaimed yet still belongs to its owner.** If the owner finishes first, its write succeeds. Expiry gives others permission to take the job; it does not by itself invalidate the owner.
- **Reclaim never judges the earlier attempt.** `DRAFTING → PENDING` just redoes the draft (the LLM call has no side effect on Relay). `EXECUTING → APPROVED` sends the job back through §4.3, where the marker decides whether the DDL already ran.
- **Crash mid-sweep:** the transaction rolls back; the next loop repeats it.

---

## 5. Worker loop

`Worker(settings, llm, worker_id)` exposes `run_once() -> bool` (did it do anything) and `run_forever()`. Tests call `run_once`; the container runs `run_forever`. One long-lived Relay connection; a target connection is opened per execution.

```
run_once():
  R1  reclaim_expired()                         # Relay txn, §4.5
  R2  claim = claim_one()                       # Relay txn, §4.2; commits before returning
      hooks.fire("claim.after_commit")
      if claim is None: return False

      if claim.attempts > MAX_ATTEMPTS: exhausted(claim); return True
      if claim.status == DRAFTING:  draft(claim)
      if claim.status == EXECUTING: execute(claim)
      return True

draft(claim):                                   # acts on PENDING jobs
      proposal = llm.propose(description, schema_summary)     # no transaction open
      policy.validate(proposal.sql)                           # pure function
      hooks.fire("draft.before_save")
  R3  transition(DRAFTING -> WAITING_FOR_APPROVAL, token)     # Relay txn

execute(claim):                                 # acts on APPROVED jobs
      check sha256(proposed_sql) == approved_sql_hash; policy.validate(proposed_sql)
  T1  apply_on_target(...)                      # target txn, §4.3; no Relay txn open
      hooks.fire("execute.after_target_commit")
  R4  transition(EXECUTING -> SUCCEEDED or FAILED, token)     # Relay txn

exhausted(claim):                               # retry limit reached
      DRAFTING:  R3' transition(DRAFTING -> FAILED, token, error = 'retry limit reached')
      EXECUTING: read the marker on the target (read-only);
                 matching hash -> R4' transition(EXECUTING -> SUCCEEDED, token)
                 otherwise     -> R4' transition(EXECUTING -> FAILED, token, error = 'retry limit reached')
```

`run_forever` sleeps `POLL_SECONDS` (plus a little random jitter so workers do not poll in step) when `run_once` returns `False`, and finishes the current job before exiting on `SIGTERM`.

**There is one retry mechanism: abandon the lease.** A worker that cannot finish does nothing further; the lease expires, reclaim returns the job to the queue, and `attempts` counts up to the limit.

| Situation | Action |
| --- | --- |
| `LLMTransientError` (timeout, rate limit, 5xx) | Retry: abandon the lease |
| `LLMPermanentError` (unusable output, invalid `risk_level`) | `DRAFTING → FAILED` |
| Policy rejects the generated SQL | `DRAFTING → FAILED`, `error = 'policy: <rule>'` |
| Hash or policy check fails before execution | `EXECUTING → FAILED`, target untouched |
| `DDLFailed`, including `lock_timeout` / `statement_timeout` on the DDL | `EXECUTING → FAILED`, `error` saved |
| `HASH_MISMATCH` on an existing marker | `EXECUTING → FAILED` |
| Marker-step error, target connection lost, commit outcome unknown | Retry: abandon the lease |
| Relay connection lost | Reconnect with backoff; the lease covers the gap |
| Any worker write returns 0 rows | Stop this job, no further writes; it belongs to someone else now |
| Unexpected exception (a bug) | Log it, abandon the lease; the limit stops a crash loop |
| `attempts > MAX_ATTEMPTS` on claim | `exhausted()` above |

---

## 6. Fake LLM and SQL policy

### 6.1 LLM interface (`relay/llm/__init__.py`)

```python
@dataclass(frozen=True)
class Proposal:
    sql: str
    risk_level: str      # 'LOW' | 'MEDIUM' | 'HIGH'
    risk_reason: str

class LLMTransientError(Exception): ...   # worth retrying
class LLMPermanentError(Exception): ...   # retrying will not help

class LLMClient(Protocol):
    def propose(self, description: str, schema_summary: str) -> Proposal: ...

def get_llm(settings) -> LLMClient: ...   # LLM_PROVIDER = 'fake' now, 'groq' in Phase 2
```

- `schema_summary` is a fixed text description of the demo tables from `config.py`. The fake ignores it; Groq will need it.
- The adapter owns its timeout (`LLM_TIMEOUT_SECONDS`, default 20) and maps provider errors onto the two exception types. The worker never sees provider-specific errors, so Groq drops in without touching `worker.py`.
- Whatever an adapter returns is untrusted: it always passes through `policy.validate`.

Fake behavior (stateless, deterministic), applied to the trimmed, lower-cased description:

| Description | Result |
| --- | --- |
| `add column <col> <type> to <table>` | `Proposal("ALTER TABLE public.<table> ADD COLUMN <col> <type>", "LOW", "adds a nullable column; no table rewrite")` |
| `fake:transient` | raises `LLMTransientError` |
| `fake:permanent` | raises `LLMPermanentError` |
| anything else | raises `LLMPermanentError("fake LLM cannot parse description")` |

The fake does not check the table or type. `add column x text to secrets` produces SQL that the policy rejects, and `add column email text to customers` produces allowed SQL that fails on the target (duplicate column): those are the policy-violation and failed-DDL test inputs.

### 6.2 SQL policy (`relay/policy.py`)

`validate(sql) -> None`, raising `PolicyViolation(rule)`. Pure function, no database. Runs twice: on LLM output before it is saved, and again just before execution.

Uses `pglast`, a Python wrapper around the real Postgres parser, so the check sees the statement the way the server will. Rules on the parse tree, all required:

1. Input is at most 1000 characters and parses.
2. Exactly one statement.
3. It is an `AlterTableStmt` on a table (`objtype = OBJECT_TABLE`), without `IF EXISTS`.
4. Target: no catalog name; schema is absent or `public`; table name is `customers` or `orders`.
5. Exactly one command, of subtype `AT_AddColumn`, without `IF NOT EXISTS` (a silent no-op would be recorded as applied).
6. Column name matches `^[a-z_][a-z0-9_]{0,62}$`.
7. Column type, after dropping the `pg_catalog` prefix the parser adds, is one of: `text`, `varchar`, `int4`, `int8`, `bool`, `numeric`, `timestamptz`, `date`, `uuid`. Type modifiers only on `varchar` and `numeric`, integer constants only. No arrays, no `SETOF`.
8. Nothing else on the column: no constraints (`NOT NULL`, `DEFAULT`, `CHECK`, `REFERENCES`, `UNIQUE`, `PRIMARY KEY`), no `COLLATE`, no identity or generated clause, no storage or compression options.
9. Belt and braces: rebuild the canonical statement from the extracted (table, column, type, modifiers), parse it, and require its tree to equal the input's tree ignoring source positions. This catches any field rules 3–8 forgot.

Net effect: v1 allows only "add one nullable column with no default, of a plain type, to a demo table". That change is metadata-only (no table rewrite) and cannot call a function.

To verify at M3 before writing the rules: print the parse tree for each allowed type and confirm the node and field names for the pinned pglast version.

---

## 7. Test harness

### 7.1 Running

- `tests` is a Compose service built from the same image, with the repo mounted, `depends_on` postgres being healthy (`pg_isready` healthcheck), in a `test` profile so `docker compose up` does not start it.
- **Run the suite:** `docker compose run --rm tests pytest`
- Repeat one race test: `docker compose run --rm tests pytest tests/test_concurrent_approval.py --count=20`
- Tests use their own databases, `relay_test` and `target_test`, in the same Postgres container, so they never touch dev data.

### 7.2 Database reset

- **Once per session** (`conftest.py`): connect with `ADMIN_DSN`, drop and recreate `relay_test` and `target_test`, ensure `relay_migrator` exists, apply both schema files. Uses the same functions as `relay.dbinit`.
- **Before every test** (autouse fixture): `TRUNCATE migrations, migration_events, idempotency_keys;` on `relay_test`, and re-run `db/target_schema.sql` on `target_test`. Truncating is not enough on the target because tests change table definitions.
- Reset happens before, not after, so a failed test's rows are still there to inspect and no test depends on leftovers.
- Tests may `INSERT` a row in any state as setup (`helpers.seed_migration(status=...)`). They never `UPDATE` `status`.

### 7.3 Forcing races

Two tools, no sleeps:

- **Start together:** N threads, each with its own connection, wait on a `threading.Barrier(N)` and then act. Used for the 20-approval test and the 4-worker contention tests. Threads are real concurrency here: Python releases its global lock while waiting on the network, and Postgres sees N independent sessions.
- **Stop at an exact point:** a hook (§7.5) makes thread A wait on a `threading.Event` at a named line, e.g. holding its transaction open. The test then runs B against that known state and releases A. Used for "two transactions, one row", the stale worker, marker contention, and the same-key idempotency race.
- To prove B is really blocked (not just slow), the test polls `pg_stat_activity` for B's session with `wait_event_type = 'Lock'`, with a deadline.
- Every `Barrier.wait`, `Event.wait`, and `Thread.join` has a timeout and the test fails if it expires. No hung test runs.

The 20-way and 4-worker tests call the service and worker functions directly from threads. HTTP mapping (`409`, `422`, ...) is tested separately, single-threaded, with FastAPI's `TestClient`. See decision D13.

### 7.4 Lease time

- `claim_one` takes `lease_seconds`; the worker passes `settings.LEASE_SECONDS` (default 60).
- Tests never wait for expiry. `helpers.expire_lease(migration_id)` runs `UPDATE migrations SET lease_expires_at = now() - interval '1 second' WHERE id = %s`. It does not touch `status`.

### 7.5 Hooks and the crash hook (`relay/hooks.py`)

`hooks.fire(name)` is called at the fixed points below and does nothing by default.

| Hook | Location | Used by |
| --- | --- | --- |
| `claim.after_commit` | after the claim commits | crash during generation; claim-commits-before-work |
| `draft.before_save` | after the LLM returns, before R3 | stale worker |
| `execute.after_marker_insert` | inside the target transaction, after the marker | marker contention |
| `execute.after_target_commit` | after T1 commits, before R4 | crash after target commit; `demo_crash.py` |
| `idempotency.after_reserve` (M5) | after the key insert, before the work | same-key race |

Two ways to arm a hook:

- **In-process** (thread tests): `hooks.register(name, fn)`, where `fn` typically sets one Event and waits on another. Cleared by a fixture after each test.
- **By environment variable** (separate worker process): `RELAY_HOOK=<name>:crash` makes `fire` call `os._exit(137)`: the process dies on the spot with no cleanup, like `kill -9`. `RELAY_HOOK=<name>:pause` prints `PAUSED at <name>` and blocks forever, so something else can kill it.

Crash tests: `helpers.spawn_worker(hook="execute.after_target_commit:crash")` starts `python -m relay.worker` as a subprocess, waits for it to exit with code 137 (`proc.wait(timeout=...)`), asserts the mid-crash state (marker present, Relay still `EXECUTING`), expires the lease, then runs a second worker's `run_once()` in-process and asserts the recovery.

`demo_crash.py` (Phase 2) starts a worker with `...:pause`, waits for the marker to appear, kills the container, and shows a second worker finishing.

### 7.6 Configuration (all from environment; `.env.example` lists each)

`RELAY_DB_DSN`, `TARGET_DB_DSN` (as `relay_migrator`), `ADMIN_DSN` (dbinit and tests only), `TARGET_MIGRATOR_PASSWORD`, `WORKER_ID` (default hostname + pid), `LEASE_SECONDS=60`, `POLL_SECONDS=1`, `MAX_ATTEMPTS=5`, `LOCK_TIMEOUT=5s`, `STATEMENT_TIMEOUT=15s`, `TARGET_IDLE_TIMEOUT=30s`, `LLM_PROVIDER=fake`, `LLM_TIMEOUT_SECONDS=20`, `RELAY_HOOK` (unset outside tests and the demo).

### 7.7 Dependencies

| Package | Why | Alternative |
| --- | --- | --- |
| `fastapi`, `uvicorn` | API and its server | (stack) |
| `psycopg[binary]` | Postgres driver with prebuilt wheels for x64 and ARM64 Linux | building from source needs a compiler in the image |
| `pglast` | real Postgres parser for the policy. 8.8 has `cp312` manylinux wheels for x86_64 and aarch64 (checked on PyPI, 2026-11-16) | regex on SQL, which is exactly what the policy must not be |
| `pytest` | test runner | (stack) |
| `httpx` | required by FastAPI's `TestClient` | running a live server for every HTTP test |
| `pytest-repeat` | `--count=20` to hunt rare interleavings. **Not in CLAUDE.md's approved stack: do not install until Albert approves.** | a shell loop running pytest 20 times |

Not added: an ORM, a migration tool, `pytest-timeout`, `pydantic-settings`. `psycopg_pool` is approved in CLAUDE.md but unused under D16 (open question). Runtime is Python 3.12. Exact versions are pinned when M1 builds the image.

---

## 8. Build order

A milestone is done when every listed test passes via `docker compose run --rm tests pytest`, every test has a docstring naming the guarantee it proves, and PROGRESS.md is updated.

### M1: Scaffolding

Dockerfile, Compose (postgres healthcheck, `dbinit`, `api`, `tests`), `config.py`, `db.py`, `dbinit.py`, relay schema (`migrations`, `migration_events`), `POST /migrations`, `GET /migrations/{id}`, `GET /migrations`, `GET /health`, test fixtures.

- `test_health.py::test_health_returns_200_when_db_up`
- `test_schema.py::test_unknown_status_rejected_by_check`
- `test_schema.py::test_lease_on_pending_row_rejected_by_check`
- `test_schema.py::test_event_requires_existing_migration`
- `test_create_get.py::test_create_returns_202_and_row_is_pending`
- `test_create_get.py::test_create_writes_one_creation_event`
- `test_create_get.py::test_create_empty_description_422`
- `test_create_get.py::test_create_missing_x_user_422`
- `test_create_get.py::test_get_returns_row_and_events`
- `test_create_get.py::test_get_unknown_id_404`
- `test_create_get.py::test_get_malformed_id_422`
- `test_create_get.py::test_list_filters_by_status`
- `test_create_get.py::test_list_unknown_status_422`
- `test_isolation.py::test_database_is_empty_at_start_of_each_test` (two tests that each insert and each expect one row)
- Manual gate (PLAN Week 1): create a migration, `docker compose down && docker compose up -d`, read the same record back. Also: the image builds and the suite passes on both machines.

### M2: Transition helper, approve, reject

`transitions.py` [core], `service.py`, approve and reject endpoints (`Idempotency-Key` accepted, ignored).

- `test_transitions.py::test_success_updates_status_and_writes_one_event`
- `test_transitions.py::test_wrong_from_status_returns_false_and_writes_no_event`
- `test_transitions.py::test_wrong_lease_token_returns_false`
- `test_transitions.py::test_leaving_leased_state_without_token_raises`
- `test_transitions.py::test_illegal_pair_raises`
- `test_transitions.py::test_every_transition_out_of_terminal_states_raises` (`SUCCEEDED`, `REJECTED`, `FAILED` × all targets)
- `test_transitions.py::test_unknown_set_field_raises`
- `test_transitions.py::test_caller_rollback_undoes_status_and_event`
- `test_transitions.py::test_second_transaction_waits_then_matches_zero_rows` (A holds its update open; B is seen blocked; A commits; B returns `False`)
- `test_approve_reject.py::test_approve_200_sets_approved_hash_and_event`
- `test_approve_reject.py::test_approve_stale_hash_409_and_state_unchanged`
- `test_approve_reject.py::test_approve_unknown_id_404`
- `test_approve_reject.py::test_approve_when_not_waiting_409` (parametrized over the other seven states)
- `test_approve_reject.py::test_reject_200_records_reason_in_event`
- `test_approve_reject.py::test_reject_after_approve_409`
- `test_approve_reject.py::test_missing_x_user_422`
- `test_concurrent_approval.py::test_20_concurrent_approvals_exactly_one_wins` (1 success, 19 conflicts, exactly one `→ APPROVED` event)
- `test_concurrent_approval.py::test_concurrent_approve_and_reject_exactly_one_wins`

### M3: Claim, leases, generation

`claim.py` [core] with the reclaim sweep, `hooks.py`, `llm/`, `policy.py`, `worker.py` draft path, retry limit for generation, `worker` Compose service.

- `test_policy.py::test_accepts_add_column` (parametrized: both tables, every allowed type, with and without `public.`)
- `test_policy.py::test_rejects` (parametrized: empty, unparseable, two statements, `SELECT`, `DROP TABLE`, `CREATE TABLE`, other table, other schema, `IF EXISTS`, `IF NOT EXISTS`, two commands, `DROP COLUMN`, `DEFAULT`, `NOT NULL`, `REFERENCES`, disallowed type, array type, over-long input)
- `test_fake_llm.py::test_add_column_description_maps_to_expected_sql`
- `test_fake_llm.py::test_directives_raise_transient_and_permanent`
- `test_claim.py::test_claim_sets_drafting_token_expiry_attempts_and_event`
- `test_claim.py::test_claim_on_empty_queue_returns_none`
- `test_claim.py::test_two_claimers_one_row_second_gets_none` (A holds its claim transaction open; B claims and gets nothing, without waiting)
- `test_claim.py::test_claim_is_committed_before_llm_runs` (inside the fake LLM call: another connection sees `DRAFTING`, and the worker's connection is idle, not in a transaction)
- `test_generation.py::test_happy_path_reaches_waiting_with_sql_hash_and_risk`
- `test_generation.py::test_policy_violation_marks_failed_with_error`
- `test_generation.py::test_llm_permanent_error_marks_failed`
- `test_generation.py::test_llm_transient_error_leaves_lease_then_retry_succeeds`
- `test_reclaim.py::test_expired_drafting_returns_to_pending_with_reclaimer_event`
- `test_reclaim.py::test_unexpired_lease_is_not_reclaimed`
- `test_reclaim.py::test_owner_can_finish_after_expiry_if_not_yet_reclaimed`
- `test_stale_worker.py::test_stale_drafter_write_matches_zero_rows` (A paused at `draft.before_save`; lease expired; B reclaims, claims, finishes; A released; A's write returns `False`; row and events show only B's result)
- `test_crash_generation.py::test_worker_killed_after_claim_is_reclaimed_and_job_completes` (subprocess, `claim.after_commit:crash`)
- `test_worker_contention.py::test_generation_50_jobs_4_workers` (all 50 `WAITING_FOR_APPROVAL`; each id has exactly one `PENDING → DRAFTING` event and one LLM call)
- `test_retry_limit.py::test_generation_fails_after_max_attempts`

### M4: Marker-first execution

Target schema and role, `target.py` [core], `worker.py` execute path, reclaim for `EXECUTING`, retry limit for execution.

- `test_target_role.py::test_migrator_is_not_superuser`
- `test_target_role.py::test_migrator_can_alter_demo_tables`
- `test_target_role.py::test_migrator_cannot_create_table`
- `test_target_role.py::test_migrator_cannot_update_or_delete_markers`
- `test_target_role.py::test_migrator_cannot_connect_to_relay_db`
- `test_execute.py::test_happy_path_adds_column_writes_marker_marks_succeeded`
- `test_execute.py::test_failed_ddl_leaves_schema_unchanged_no_marker_status_failed_with_error`
- `test_execute.py::test_sql_not_matching_approved_hash_fails_without_touching_target`
- `test_execute.py::test_policy_recheck_failure_fails_without_touching_target`
- `test_execute.py::test_existing_marker_with_matching_hash_skips_ddl_and_succeeds`
- `test_execute.py::test_existing_marker_with_different_hash_fails`
- `test_execute.py::test_lost_target_connection_does_not_mark_failed`
- `test_marker_contention.py::test_second_inserter_waits_then_skips_when_first_commits`
- `test_marker_contention.py::test_second_inserter_applies_when_first_rolls_back`
- `test_reclaim.py::test_expired_executing_returns_to_approved`
- `test_stale_worker.py::test_stale_executor_write_matches_zero_rows`
- `test_crash_after_target_commit.py::test_next_worker_finds_marker_skips_ddl_and_marks_succeeded` (subprocess, `execute.after_target_commit:crash`)
- `test_worker_contention.py::test_execution_50_jobs_4_workers` (all 50 `SUCCEEDED`; 50 markers; 50 new columns)
- `test_retry_limit.py::test_execution_at_limit_with_marker_succeeds`
- `test_retry_limit.py::test_execution_at_limit_without_marker_fails`

### M5: Idempotency keys

`idempotency_keys` table, `idempotency.py` [core], header required on approve and reject.

- `test_idempotency.py::test_missing_key_422`
- `test_idempotency.py::test_same_key_same_body_replays_identical_response_one_event`
- `test_idempotency.py::test_same_key_different_body_422`
- `test_idempotency.py::test_same_key_different_migration_422`
- `test_idempotency.py::test_saved_409_is_replayed`
- `test_idempotency.py::test_exception_rolls_back_key_and_retry_executes`
- `test_idempotency.py::test_two_simultaneous_requests_same_key_one_mutation_identical_responses` (A paused at `idempotency.after_reserve`; B seen blocked; A released; B replays)
- `test_idempotency.py::test_second_request_proceeds_when_first_rolls_back`
- `test_concurrent_approval.py::test_20_concurrent_approvals_exactly_one_wins` still passes, now with 20 distinct required keys

### Phase 2 backlog (not part of M1–M5; only when Albert says so)

- **Live-HTTP concurrency test:** 20 real approve requests against a running `api` container. Until it exists, the README rule in §10 applies.
- Retry `lock_timeout` automatically? Revisit D9 with the Phase 2 `lock_timeout` test.
- PLAN's own Phase 2 list: Groq adapter, CI, structured logs, `demo_crash.py`, GIF.
- Future work beyond Phase 2: target-side fencing (D23).

---

## 9. Decision log

| # | Decision | Rejected alternatives | Why |
| --- | --- | --- | --- |
| D1 | Autocommit connections with explicit `with conn.transaction():` blocks | psycopg's default implicit transactions | Implicit mode leaves a transaction open after any `SELECT`; one forgotten commit would hold it across an LLM call. Explicit blocks make every boundary visible. |
| D2 | The transition helper never commits; the caller owns the transaction | Helper opens and commits its own | Claim, reclaim, and idempotency each need the status change to commit together with something else. |
| D3 | Claim = `SELECT ... FOR UPDATE SKIP LOCKED`, then the helper | PLAN's single `UPDATE ... WHERE id = (SELECT ...) RETURNING` | Keeps invariant 1 literally true and puts the audit event in the helper. Costs one extra round trip. |
| D4 | Reclaim is an explicit transition back to the queue state | Let the claim query pick up expired `DRAFTING`/`EXECUTING` rows directly | Matches PLAN's state diagram, keeps the claim query simple, and the audit log shows every recovery. |
| D5 | No lease renewal | Heartbeat thread extending the lease | A second thread and connection per worker for work bounded at ~40 s. Timeouts are set under the lease instead. |
| D6 | A worker's write checks only token and status, not `lease_expires_at > now()` | Also require an unexpired lease | Until someone reclaims the row, nobody else can be working on it, so accepting the late write is safe and avoids redoing finished work. |
| D7 | Retry = abandon the lease | A worker-initiated "release" transition | One recovery path to build, test, and explain. Cost: a transient failure waits up to `LEASE_SECONDS` before retry. |
| D8 | **Accepted 2026-11-16.** Retry limit pulled into M3/M4, enforced by the claiming worker; execution at the limit reads the marker before deciding | Defer to Phase 2 (PLAN); or have the reclaim sweep mark `FAILED` | Without it a poison job loops forever through the whole core build. The claimer holds a lease, so the write satisfies invariant 3, and reading the marker avoids marking `FAILED` a migration that actually applied. |
| D9 | **Accepted 2026-11-16 for v1.** DDL errors, including both timeouts, are terminal `FAILED`; unknown outcomes retry. A timeout on the marker insert is classified separately as contention and retries (§4.3) | Retry `lock_timeout` automatically | Deterministic and simple; avoids a retry storm against a locked table. Revisit in Phase 2 alongside the `lock_timeout` test. |
| D10 | Lease times use the database clock; tests backdate `lease_expires_at` | Inject a Python clock; real short leases with sleeps | One clock shared by all workers, no drift. Backdating is instant and deterministic. |
| D11 | Hash is sha256 of the exact stored text | Normalize whitespace/case, or hash the parse tree | "Approved this exact statement" should mean byte-for-byte. Nothing edits the SQL in v1, so normalization buys nothing. |
| D12 | Policy allows only nullable, default-less `ADD COLUMN` of listed types; `IF NOT EXISTS` banned | Allow `DEFAULT`/`NOT NULL`; allow `IF NOT EXISTS` | Defaults can call functions and rewrite tables. `IF NOT EXISTS` could record a no-op as applied. |
| D13 | **Accepted 2026-11-16**, with the README wording rule in §10 and a live-HTTP test logged as Phase 2 work. Many-way concurrency tests call service and worker functions from threads; HTTP mapping is tested single-threaded | 20 real HTTP requests against a running `api` container | The race lives in Postgres, and threads with separate connections exercise it exactly. A live server adds startup and reset coordination without testing more of the guarantee. Cost: the HTTP layer is not itself under concurrent load in tests. |
| D14 | Crash tests kill a real subprocess with `os._exit`; pause-style tests use in-process hooks | Raise a fake exception in-process for crashes | A real process death proves no cleanup code runs. In-process hooks are simpler where nothing needs to die. |
| D15 | Relay connects to `relay_db` as the admin role from `.env` | A dedicated least-privilege Relay role | PLAN only requires least privilege on the target. One more role and grant set to maintain for no tested guarantee. |
| D16 | One connection per API request, no pool | `psycopg_pool` | No extra dependency or pool lifecycle to learn. Connecting costs a few ms locally. Switch when request rate makes that matter. |
| D17 | Schema as SQL files applied by `dbinit`; changes mean `--reset` | Alembic; `docker-entrypoint-initdb.d` scripts | Data is disposable. Init scripts only run on an empty volume and cannot build the test databases. |
| D18 | Separate `relay_test` / `target_test` databases; truncate plus re-run of the target schema before each test | Reuse dev databases; a rolled-back transaction per test; template databases | Concurrency tests need real commits across several connections, so per-test rollback cannot work. Template cloning is faster but one more concept. |
| D19 | Idempotency saves every normal response, including `404` and `409`; exceptions roll back the key | Save only `2xx` | "Same request, same answer" holds for conflicts too, and the 20-key test stays simple. |
| D20 | `risk_level` comes from the LLM and gates nothing | Compute risk in code; block on `HIGH` | The policy already decides what may run. Risk is reviewer context only. |
| D21 | Reject reason and failure text live in `migration_events.detail` (failure text also in `migrations.error`) | A `reject_reason` column | One generic field on the audit row instead of a column per outcome. |
| D22 | Python 3.12 (`python:3.12-slim`) | Python 3.13, which PLAN gives as an example | CLAUDE.md's approved stack names 3.12, and pglast 8.8 has `cp312` wheels for x86_64 and aarch64 Linux, so neither machine needs a compiler in the image. |
| D23 | **Future work, not built.** The late-apply gap (§1 #5) stays a documented non-guarantee in v1. The real fix is a fencing check enforced inside target_db: every claim gets a number that only ever increases (a new counter, since `attempts` resets); target_db keeps the highest number seen per migration in a table the migrator can only raise; the target transaction's first statement raises it to its own number and aborts if a higher one is already there. A worker about to mark `FAILED` first raises the fence, so an older attempt that wakes up later is refused by the target itself. | Build it now; rely on the lease token alone; narrow the window with timeouts only (what v1 does) | The lease token is checked by Relay, not by the target, so it cannot stop a stale worker there. Fencing needs a second target table, a new counter, and its own crash tests: too much for the v1 sprint. The README must list late apply as a non-guarantee. |

---

## 10. README rules

The README may claim only what a test demonstrates (invariant 6). Specific wording rules:

- Say **"concurrent approvals"**, never "concurrent requests", until the live-HTTP concurrency test exists (§8 Phase 2 backlog). The current test drives 20 threads through the service layer, not 20 HTTP requests (D13).
- Never say "exactly-once". Say "at-least-once delivery with once-only effects on the target, for the tested workflow".
- List as non-guarantees: a migration marked `FAILED` may still be applied later by a stale worker (§1 #5, D23); no atomic commit across the two databases; no safety for SQL the policy allows but is still wrong; `X-User` is not authentication.
