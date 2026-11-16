"""The SQL policy: an allow-list checked on the real Postgres parse tree (DESIGN.md section 6.2).

`validate(sql)` returns None if the SQL is allowed and raises PolicyViolation(rule) if not.
It is a pure function: no database, no network. It runs on LLM output before a proposal is
saved, and (from M4) again just before execution.

It uses pglast, a Python wrapper around Postgres's own parser, so it sees the statement
exactly as the server will. A regex would be fooled by comments, quoting, and odd spacing.

Net effect: v1 allows only "add ONE nullable column, with no default, of a plain type, to a
demo table". That change is metadata-only (no table rewrite) and cannot call a function.

Field names below were checked against the pinned pglast 8.8 parse tree.
"""

import re

import pglast
from pglast import ast, enums

MAX_LENGTH = 1000
ALLOWED_TABLES = frozenset({"customers", "orders"})
# Postgres's internal type names. The parser turns "integer" into pg_catalog.int4,
# "boolean" into pg_catalog.bool, "timestamp with time zone" into pg_catalog.timestamptz.
ALLOWED_TYPES = frozenset({
    "text", "varchar", "int4", "int8", "bool", "numeric", "timestamptz", "date", "uuid",
})
# Only these may carry a modifier such as varchar(50) or numeric(10, 2).
TYPES_WITH_MODIFIERS = frozenset({"varchar", "numeric"})
_COLUMN_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class PolicyViolation(Exception):
    """The SQL is not allowed. `rule` says which rule it broke."""

    def __init__(self, rule: str):
        super().__init__(rule)
        self.rule = rule


def validate(sql: str) -> None:
    # Rule 1: bounded size, and it parses.
    if len(sql) > MAX_LENGTH:
        raise PolicyViolation(f"statement is longer than {MAX_LENGTH} characters")
    try:
        statements = pglast.parse_sql(sql)
    except pglast.Error:
        raise PolicyViolation("statement does not parse as SQL") from None

    # Rule 2: exactly one statement.
    if len(statements) != 1:
        raise PolicyViolation(f"exactly one statement is allowed, found {len(statements)}")
    statement = statements[0].stmt

    # Rule 3: ALTER TABLE on a plain table, without IF EXISTS.
    if not isinstance(statement, ast.AlterTableStmt) or statement.objtype != enums.ObjectType.OBJECT_TABLE:
        raise PolicyViolation("only ALTER TABLE is allowed")
    if statement.missing_ok:
        raise PolicyViolation("IF EXISTS is not allowed")

    # Rule 4: the target is a demo table in the public schema of this database.
    relation = statement.relation
    if relation.catalogname is not None:
        raise PolicyViolation("a database name on the table is not allowed")
    if relation.schemaname not in (None, "public"):
        raise PolicyViolation("only the public schema is allowed")
    if relation.relname not in ALLOWED_TABLES:
        raise PolicyViolation(f"table {relation.relname!r} is not an allowed table")

    # Rule 5: exactly one command, ADD COLUMN, without IF NOT EXISTS.
    if len(statement.cmds) != 1:
        raise PolicyViolation("exactly one ALTER TABLE command is allowed")
    command = statement.cmds[0]
    if command.subtype != enums.AlterTableType.AT_AddColumn:
        raise PolicyViolation("only ADD COLUMN is allowed")
    if command.missing_ok:
        # A silent no-op would be recorded as "applied" (decision D12).
        raise PolicyViolation("IF NOT EXISTS is not allowed")

    # Rule 6: a plain lower-case column name.
    column = command.def_
    if not _COLUMN_NAME.match(column.colname or ""):
        raise PolicyViolation("column name must be lower-case letters, digits, and underscores")

    # Rule 7: an allow-listed type; modifiers only where they make sense.
    type_name = column.typeName
    names = [part.sval for part in type_name.names]
    has_catalog_prefix = len(names) == 2 and names[0] == "pg_catalog"
    if has_catalog_prefix:
        names = names[1:]
    if len(names) != 1 or names[0] not in ALLOWED_TYPES:
        raise PolicyViolation(f"column type {'.'.join(names)!r} is not an allowed type")
    base_type = names[0]
    if type_name.arrayBounds or type_name.setof or type_name.pct_type:
        raise PolicyViolation("array, SETOF, and %TYPE column types are not allowed")
    modifiers = list(type_name.typmods or ())
    if modifiers and base_type not in TYPES_WITH_MODIFIERS:
        raise PolicyViolation(f"type {base_type!r} may not have a modifier")
    for modifier in modifiers:
        if not (isinstance(modifier, ast.A_Const) and isinstance(modifier.val, ast.Integer)):
            raise PolicyViolation("type modifiers must be whole numbers")

    # Rule 8: nothing else on the column. Defaults can call functions and rewrite tables;
    # constraints can scan or lock more than the one table.
    if column.constraints:
        raise PolicyViolation("column constraints and defaults are not allowed")
    if column.collClause is not None:
        raise PolicyViolation("COLLATE is not allowed")

    # Rule 9, belt and braces: rebuild the statement from only the parts checked above and
    # require the input's parse tree to equal the rebuilt one. Anything rules 3-8 forgot to
    # look at (ONLY, identity, storage options, a future parser field) shows up as a difference.
    table_sql = f'"{relation.relname}"'
    if relation.schemaname is not None:
        table_sql = f'"{relation.schemaname}".{table_sql}'
    type_sql = ("pg_catalog." if has_catalog_prefix else "") + base_type
    if modifiers:
        type_sql += "(" + ", ".join(str(modifier.val.ival) for modifier in modifiers) + ")"
    canonical = f'ALTER TABLE {table_sql} ADD COLUMN "{column.colname}" {type_sql}'
    if _tree(statement) != _tree(pglast.parse_sql(canonical)[0].stmt):
        raise PolicyViolation("statement has parts beyond a plain ADD COLUMN")


def _tree(node) -> object:
    """The parse tree as plain dicts and lists, without source positions.

    Positions ("location") only say where in the text a piece was found; two statements that
    mean the same thing differ in them, so they are dropped before comparing.
    """
    return _without_locations(node(skip_none=True))


def _without_locations(value) -> object:
    if isinstance(value, dict):
        return {key: _without_locations(item) for key, item in value.items() if key != "location"}
    if isinstance(value, (list, tuple)):
        return [_without_locations(item) for item in value]
    return value
