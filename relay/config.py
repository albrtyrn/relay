"""Configuration, read from environment variables once (DESIGN.md section 7.6).

Each milestone adds the variables it first uses; M1 needs only the relay_db connection string.
`ADMIN_DSN` is not part of Settings: only dbinit and the tests use it, never the running API.
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    relay_db_dsn: str


def _require(env: dict, name: str) -> str:
    try:
        return env[name]
    except KeyError:
        raise RuntimeError(f"environment variable {name} is not set (see .env.example)") from None


def load_settings(env: dict | None = None) -> Settings:
    env = os.environ if env is None else env
    return Settings(relay_db_dsn=_require(env, "RELAY_DB_DSN"))


def load_admin_dsn(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    return _require(env, "ADMIN_DSN")
