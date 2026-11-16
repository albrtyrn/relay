"""The SQL policy (DESIGN.md section 6.2, M3). Pure functions: no database in this file.

The policy is the code that decides what may run. The LLM never decides, and a human
approval establishes intent, not safety (PLAN.md).
"""

import pytest

from relay.policy import PolicyViolation, validate

ALLOWED_TYPES = [
    "text", "varchar", "varchar(50)", "integer", "int", "bigint", "boolean", "numeric",
    "numeric(10, 2)", "timestamptz", "timestamp with time zone", "date", "uuid",
]


@pytest.mark.parametrize("column_type", ALLOWED_TYPES)
@pytest.mark.parametrize("table", ["customers", "orders", "public.customers", "public.orders"])
def test_accepts_add_column(table, column_type):
    """Guarantee: adding one nullable, default-less column of an allowed type to a demo table
    passes the policy, with or without the `public.` schema prefix.

    Invariant: none of the six directly; this is the "allowed" half of the allow-list. If
    the policy rejected everything, every reject test below would pass for the wrong reason.
    """
    validate(f"ALTER TABLE {table} ADD COLUMN nickname {column_type}")


REJECTED = {
    "empty": "",
    "whitespace-only": "   ",
    "unparseable": "ALTER TABLE customers ADD COLUMN",
    "not-sql": "please add a nickname column",
    "two-statements": "ALTER TABLE customers ADD COLUMN a text; ALTER TABLE customers ADD COLUMN b text",
    "piggybacked-drop": "ALTER TABLE customers ADD COLUMN a text; DROP TABLE orders",
    "select": "SELECT * FROM customers",
    "drop-table": "DROP TABLE customers",
    "create-table": "CREATE TABLE evil (id int)",
    "delete": "DELETE FROM customers",
    "other-table": "ALTER TABLE secrets ADD COLUMN x text",
    "marker-table": "ALTER TABLE relay_applied ADD COLUMN x text",
    "other-schema": "ALTER TABLE pg_catalog.customers ADD COLUMN x text",
    "catalog-name": "ALTER TABLE target_db.public.customers ADD COLUMN x text",
    "if-exists": "ALTER TABLE IF EXISTS customers ADD COLUMN x text",
    "if-not-exists": "ALTER TABLE customers ADD COLUMN IF NOT EXISTS x text",
    "only": "ALTER TABLE ONLY customers ADD COLUMN x text",
    "foreign-table": "ALTER FOREIGN TABLE customers ADD COLUMN x text",
    "two-commands": "ALTER TABLE customers ADD COLUMN x text, ADD COLUMN y text",
    "drop-column": "ALTER TABLE customers DROP COLUMN email",
    "alter-column-type": "ALTER TABLE customers ALTER COLUMN email TYPE varchar(5)",
    "rename-column": "ALTER TABLE customers RENAME COLUMN email TO mail",
    "default": "ALTER TABLE customers ADD COLUMN x text DEFAULT 'a'",
    "default-function": "ALTER TABLE customers ADD COLUMN x timestamptz DEFAULT now()",
    "not-null": "ALTER TABLE customers ADD COLUMN x text NOT NULL",
    "references": "ALTER TABLE orders ADD COLUMN x bigint REFERENCES customers (id)",
    "unique": "ALTER TABLE customers ADD COLUMN x text UNIQUE",
    "primary-key": "ALTER TABLE customers ADD COLUMN x bigint PRIMARY KEY",
    "check": "ALTER TABLE customers ADD COLUMN x integer CHECK (x > 0)",
    "collate": 'ALTER TABLE customers ADD COLUMN x text COLLATE "C"',
    "identity": "ALTER TABLE customers ADD COLUMN x bigint GENERATED ALWAYS AS IDENTITY",
    "generated": "ALTER TABLE customers ADD COLUMN x integer GENERATED ALWAYS AS (1) STORED",
    "disallowed-type": "ALTER TABLE customers ADD COLUMN x json",
    "serial-type": "ALTER TABLE customers ADD COLUMN x serial",
    "schema-qualified-type": "ALTER TABLE customers ADD COLUMN x public.text",
    "array-type": "ALTER TABLE customers ADD COLUMN x text[]",
    "modifier-on-plain-type": "ALTER TABLE customers ADD COLUMN x timestamptz(3)",
    "quoted-mixed-case-column": 'ALTER TABLE customers ADD COLUMN "Nick Name" text',
    "over-long": "ALTER TABLE customers ADD COLUMN x text" + " " * 1000,
}


@pytest.mark.parametrize("sql", REJECTED.values(), ids=REJECTED.keys())
def test_rejects(sql):
    """Guarantee: everything outside "add one plain nullable column to a demo table" raises
    PolicyViolation with a rule name.

    Invariant: none of the six directly; it is the safety boundary they all sit behind. The
    check runs on the real Postgres parse tree (pglast), so the policy sees the statement the
    way the server will, not the way a regex would guess.
    """
    with pytest.raises(PolicyViolation) as error:
        validate(sql)

    assert error.value.rule  # every violation names the rule that was broken


def test_violation_names_the_rule():
    """Guarantee: the violation says which rule failed, so the worker can store a useful error.

    Invariant: none of the six; this is what ends up in `migrations.error` as 'policy: <rule>'.
    """
    with pytest.raises(PolicyViolation) as error:
        validate("ALTER TABLE secrets ADD COLUMN x text")

    assert "table" in error.value.rule
