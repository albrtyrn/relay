-- relay_db schema (DESIGN.md section 3.1).
-- M1 holds `migrations` and `migration_events`. `idempotency_keys` arrives in M5 and the
-- REVOKE CONNECT line with the target role in M4.
-- Data is disposable, so a schema change means `python -m relay.dbinit --reset` (decision D17).

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
