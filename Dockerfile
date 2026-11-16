# One image for every Relay process: api, worker, dbinit, tests (DESIGN.md section 2).
# The command is chosen per service in docker-compose.yml.
FROM python:3.12.16-slim

# No .pyc files in the container; print logs immediately instead of buffering them.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so editing code does not re-install them (Docker caches each step).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pytest.ini .
COPY db/ db/
COPY relay/ relay/
COPY tests/ tests/
COPY scripts/ scripts/

# Default: the API. `--factory` tells uvicorn to call create_app() to get the app.
CMD ["uvicorn", "relay.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
