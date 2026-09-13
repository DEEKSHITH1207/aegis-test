# AegisTest

Autonomous API Testing & Bug Discovery Platform.

AegisTest takes an OpenAPI/Swagger specification, generates and executes API test
cases against a target API, analyses the responses, and reports bugs, validation
gaps, inconsistencies and security issues. An LLM is used as a reasoning layer
over the collected evidence — never as the sole source of truth.

**Status: Phase 3 — specification import.** The domain is modelled and
persisted (specifications, endpoints, test runs, test cases, request/response
evidence, findings) behind a REST API under `/api/v1/`, and AegisTest can now
read a real OpenAPI 3.x or Swagger 2.0 document and turn it into endpoints. Test
generation, execution and LLM analysis come in later phases — nothing calls a
target API yet.

## Architecture

```
React frontend (later)
        |
        v
  Django REST API  ----> PostgreSQL
        |
        +------------->  Redis  ---->  Celery worker (later phases)
```

See [docs/architecture.md](docs/architecture.md) for the request flow and the
reasoning behind the layout.

## Layout

```
aegis-test/
├── backend/
│   ├── config/        Django project: settings, URLs, Celery, WSGI/ASGI
│   ├── apps/core/     cross-cutting concerns (health check, error translation)
│   ├── apps/testing/  the domain: models, services, serializers, views, URLs
│   ├── manage.py
│   ├── pytest.ini     pytest-django configuration
│   └── Dockerfile
├── docs/
├── docker-compose.yml
├── .env.example
└── README.md
```

## Stack

Python 3.12 · Django 5.2 LTS · Django REST Framework · PostgreSQL 16 · Redis 7 ·
Celery · pytest. Exact versions are pinned in `backend/requirements.txt`; stay on
the Django 5.2 LTS line.

## Requirements

Either Docker (with Compose v2), or Python 3.12 with a local PostgreSQL 16 and
Redis 7.

## Running with Docker (recommended)

```bash
cp .env.example .env
# Set a real secret key:
python -c "import secrets; print('DJANGO_SECRET_KEY=' + secrets.token_urlsafe(50))"
# ...then paste the value into .env

docker compose up --build -d
docker compose exec backend python manage.py migrate
curl http://localhost:8000/api/health/          # {"status": "ok"}
```

`docker-compose.yml` overrides `POSTGRES_HOST=postgres` and
`REDIS_URL=redis://redis:6379/0`, so the same `.env` works on the host and in
containers.

Useful commands:

```bash
docker compose logs -f backend
docker compose exec backend pytest
docker compose down          # add -v to also drop the database volume
```

## Running on the host

```bash
cp .env.example .env          # keep POSTGRES_HOST=localhost

cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

python manage.py migrate
python manage.py runserver
```

The Postgres role and database in `.env` must exist:

```bash
sudo -u postgres createuser --createdb --pwprompt aegis
sudo -u postgres createdb --owner aegis aegis
```

`--createdb` is required for the test suite, not for the application: pytest
creates and drops a separate `test_aegis` database on every run.

## Tests

```bash
cd backend
pytest
```

Tests run against `config/settings_test.py`, which supplies the required
environment variables before importing the real settings — so the suite needs
neither a `.env` file nor a running PostgreSQL (the health check does not use the
database).

## API

| Method | Path                                            | Purpose                                    |
| ------ | ----------------------------------------------- | ------------------------------------------ |
| GET    | `/api/health/`                                  | Liveness probe. Returns `{"status": "ok"}` |
| POST   | `/api/v1/specifications/`                       | Register a specification                   |
| GET    | `/api/v1/specifications/`                       | List specifications                        |
| GET    | `/api/v1/specifications/<id>/`                  | Retrieve one specification                 |
| GET    | `/api/v1/specifications/<id>/endpoints/`        | Endpoints of a specification               |
| POST   | `/api/v1/specifications/<id>/import/`           | Parse the document into endpoints          |
| POST   | `/api/v1/test-runs/`                            | Open a test run (starts `pending`)         |
| GET    | `/api/v1/test-runs/`                            | List runs; `?specification=<id>` filters   |
| GET    | `/api/v1/test-runs/<id>/`                       | Retrieve one run                           |
| GET    | `/api/v1/test-runs/<id>/test-cases/`            | Test cases of a run                        |
| GET    | `/api/v1/test-runs/<id>/findings/`              | Findings produced by a run                 |

`/api/health/` is intentionally unversioned: it is an infrastructure probe, not
part of the product contract.

List responses are paginated (`{"count", "next", "previous", "results"}`); the
page size comes from `API_PAGE_SIZE` and defaults to 50.

A run's `status`, `started_at` and `completed_at` are owned by the system and
ignored if a client sends them. Test cases and evidence rows are written by the
generator and executor in later phases, so they are read-only over HTTP for now.

## Importing a specification

Registering a specification stores the document; it does not read it. Import is
a separate, explicit step:

```bash
curl -X POST http://localhost:8000/api/v1/specifications/ \
     -H 'Content-Type: application/json' \
     -d '{"name": "Example API", "version": "1.0.0", "raw_specification": '"$(cat openapi.json)"'}'

curl -X POST http://localhost:8000/api/v1/specifications/1/import/
```

The import returns a summary rather than the endpoints themselves:

```json
{
  "source_type": "openapi", "title": "Example API", "api_version": "1.0.0",
  "created": 4, "updated": 0, "unchanged": 0, "imported_count": 4,
  "stale": [], "skipped": [], "warnings": []
}
```

Re-importing is safe. Endpoints are matched on path and method, so an edited
operation is updated in place and keeps the test history attached to it.

- `skipped` — operations the parser found but cannot import (a `trace`
  operation, a path not starting with `/`), each with a reason.
- `stale` — endpoints still in the database that the document no longer lists.
  They are **reported, not deleted**: removing one would cascade to its test
  cases and every request, response and finding recorded against them.
- `warnings` — a `$ref` that could not be inlined, or text truncated to fit.

Local `$ref` pointers are inlined so each stored schema stands on its own. A
reference that is circular, dangling or external is left as a `$ref`; external
references are never fetched, so importing a document cannot make the server
issue outbound requests.

## Configuration

All configuration is read from environment variables; see `.env.example` for the
full list. `.env` is git-ignored and must never be committed.
`DJANGO_SECRET_KEY` is mandatory whenever `DJANGO_DEBUG` is not true — a missing
key aborts startup instead of falling back to a guessable default.
