# Relay

Relay is a learning project: a backend service where an AI proposes a Postgres schema change, a human approves it over a REST API, and a pool of workers executes it.

**Status: work in progress (milestone M1 of 5).** Only the scaffolding exists: the API can create, read, and list migration records, and they survive a restart. There is no approval, no worker, and no execution yet. This README makes no reliability claims; each one will be added only once a test demonstrates it.

## Running it locally

Requires Docker.

```sh
cp .env.example .env        # then edit the values
docker compose up -d --build
curl http://localhost:8000/health
```

`docker compose up` starts Postgres (which holds both `relay_db` and `target_db`), a one-shot `dbinit` that creates what is missing, the API on port 8000, and a placeholder worker. Interactive API docs are at http://localhost:8000/docs.

Open a SQL shell on the Relay database:

```sh
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d relay_db'
```

Stop it with `docker compose down`. Your data is kept in a named volume. Use `docker compose down -v` to delete it.

## Running the tests

```sh
docker compose run --rm tests pytest
```

The tests use real Postgres in their own databases (`relay_test`, `target_test`) on the same server, so they never touch the data above.

On Windows, run Docker commands from PowerShell. Git Bash's `/c/...` paths make `docker compose build` fail with "additional privileges requested".
