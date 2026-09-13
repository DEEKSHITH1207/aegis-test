# AegisTest

Autonomous API Testing & Bug Discovery Platform.

AegisTest takes an OpenAPI/Swagger specification, generates and executes API test
cases against a target API, analyses the responses, and reports bugs, validation
gaps, inconsistencies and security issues. An LLM is used as a reasoning layer
over the collected evidence — never as the sole source of truth.

**Status: Phase 2 — data model and REST foundation.** On top of the Phase 1
foundation, the domain is now modelled and persisted: specifications, endpoints,
test runs, test cases, request/response evidence and findings, with a read/write
REST API under `/api/v1/`. Spec parsing, test generation, execution and LLM
analysis come in later phases — nothing calls a target API yet.

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
ignored if a client sends them. Endpoints, test cases and evidence rows are
written by the generator and executor in later phases, so they are read-only
over HTTP for now.

## Configuration

All configuration is read from environment variables; see `.env.example` for the
full list. `.env` is git-ignored and must never be committed.
`DJANGO_SECRET_KEY` is mandatory whenever `DJANGO_DEBUG` is not true — a missing
key aborts startup instead of falling back to a guessable default.
