-- target_db schema (DESIGN.md section 3.2). Run as the admin role, after dbinit has created
-- the relay_migrator role.
--
-- This file DROPS the tables first, so it doubles as the per-test reset. For the same reason
-- dbinit runs it only when `relay_applied` does not exist yet, or on --reset (decision D34):
-- running it against a live target would erase applied migrations and their markers.

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

-- The marker table: one row per migration whose DDL has been applied here. The worker inserts
-- the row BEFORE the DDL, in the same transaction, so marker and DDL commit together
-- (invariant 4). The primary key is what makes a second worker's insert wait and then do nothing.
-- Owned by the admin role. The migrator can add and read markers, never change or remove them.
CREATE TABLE relay_applied (
    migration_id  uuid PRIMARY KEY,
    sql_hash      text NOT NULL CHECK (sql_hash ~ '^[0-9a-f]{64}$'),
    applied_at    timestamptz NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT ON relay_applied TO relay_migrator;
