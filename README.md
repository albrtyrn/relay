# Relay

Relay is a learning project: a backend service where an AI proposes a Postgres schema change, a human approves it over a REST API, and a pool of workers executes it.

**Status: work in progress.** It is under active development, and nothing beyond a local Postgres database exists yet. This README makes no reliability claims; each one will be added only once a test demonstrates it.

## Running Postgres locally

Requires Docker.

```sh
cp .env.example .env        # then edit the values
docker compose up -d
docker compose exec postgres psql -U relay -d relay_db
```

Stop it with `docker compose down`. Your data is kept in a named volume. Use `docker compose down -v` to delete it.
