# AegisTest

Autonomous API Testing & Bug Discovery Platform.

AegisTest takes an OpenAPI/Swagger specification, generates and executes API test
cases against a target API, analyses the responses, and reports bugs, validation
gaps, inconsistencies and security issues. An LLM is used as a reasoning layer
over the collected evidence — never as the sole source of truth.

**Status: Phase 4 — test-case generation.** AegisTest reads an OpenAPI 3.x or
Swagger 2.0 document, turns it into endpoints, and generates a deterministic
suite of test cases from their schemas. Execution and LLM analysis come in later
phases: **nothing here sends an HTTP request to a target API.** Phase 4 writes
`TestCase` rows and nothing else.

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
| POST   | `/api/v1/test-runs/<id>/generate-tests/`        | Generate the run's test cases              |
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

## Generating test cases

A test run starts empty. Generation reads the endpoints of the run's
specification and writes one `TestCase` per rule worth testing:

```bash
curl -X POST http://localhost:8000/api/v1/test-runs/1/generate-tests/
```

```json
{"test_run_id": 1, "endpoints": 4, "created": 48,
 "updated": 0, "unchanged": 0, "skipped": 0, "warnings": []}
```

**This does not call the API under test.** Generation only writes rows; sending
the requests they describe is a later phase.

### What a generated case looks like

For `POST /users` with this schema:

```json
{"type": "object",
 "required": ["name", "email", "age"],
 "properties": {"name":  {"type": "string", "minLength": 3},
                "email": {"type": "string", "format": "email"},
                "age":   {"type": "integer", "minimum": 18}}}
```

generation produces `happy_path`, `invalid_type:body`, and for each field the
rules it declares — `missing_required:name`, `empty_value:name`,
`below_min_length:name`, `invalid_type:name`, `invalid_format:email`,
`below_minimum:age`, and so on. One of them:

```json
{"name": "below_minimum:age",
 "test_type": "below_minimum",
 "description": "POST /users: a value below the minimum 18 for 'age' in the request body.",
 "expected_status_code": 400,
 "input_data": {"path": {}, "query": {}, "headers": {},
                "body": {"name": "example", "email": "user@example.com", "age": 17}}}
```

Every case breaks **exactly one** rule and is otherwise a valid request. A body
that is missing a field *and* has a malformed email cannot tell you which of the
two the API rejected.

### `input_data`

Always these four keys, so a consumer never has to check for their presence:

```json
{"path": {"userId": 1}, "query": {"page": 1}, "headers": {}, "body": null}
```

`body` is `null` when the endpoint declares no request body.

### Naming

`happy_path`, or `<test_type>:<target>`. A body field is named by its dotted
path (`below_minimum:age`, `missing_required:profile.displayName`); a parameter
is prefixed with its location (`invalid_type:path.userId`,
`missing_required:query.limit`). Names are deterministic, which is what makes
re-generation safe.

### Supported constraints

| Kind | Keywords | Cases |
| --- | --- | --- |
| Presence | `required` | `missing_required`, `empty_value` (null) |
| Type | `type` | `invalid_type` |
| String | `minLength`, `maxLength`, `format` | `below_min_length`, `above_max_length`, `invalid_format` |
| Number | `minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum` | `below_minimum`, `above_maximum` |
| Enum | `enum` | `invalid_enum` |
| Array | `items`, `minItems`, `maxItems` | `invalid_type`, `below_min_length`, `above_max_length` |

Both exclusive-bound spellings are honoured: OpenAPI 3.0's
`exclusiveMinimum: true` beside `minimum`, and JSON Schema 2020-12's
`exclusiveMinimum: 5`. Reading only one would produce a "violating" value that
is in fact valid.

Values are deterministic — `"example"`, `1`, `1.0`, `true`, `[]`,
`user@example.com`, `2026-01-01`, a fixed UUID — so the same schema always
yields the same suite and a failure reproduces exactly. `const`, `enum`,
`example` and `default` from the document win over a synthesised value.

### Expected status codes

Taken from what the document actually declares, never guessed:

- **happy path** — the lowest documented `2xx`.
- **invalid cases** — `400` if documented, else `422`, else the lowest other
  `4xx`.
- **neither documented** — `null`, meaning AegisTest asserts nothing. Assuming
  every API answers an invalid body with `400` would manufacture failures
  against one that chose `422`, or that documents nothing at all.

### Re-generating

A case is identified by its run, its endpoint and its generated name. A second
pass updates those rows in place and never duplicates them, so `created: 0,
unchanged: 48` is the normal result of running it twice. Only generated fields
are rewritten; anything set by other means survives.

Generation is refused for a run that is not `pending` — adding cases to a run
that has already executed would silently change what its results mean.

### Limitations

- `pattern` produces no violation case. Any value chosen might happen to match
  the regex, and a case that is secretly valid reports a bug that is not there.
  A warning is emitted instead.
- `oneOf`/`anyOf` take the first branch, with a warning; `allOf` is
  shallow-merged; `not` is unsupported.
- A schema still holding a `$ref` — Phase 3 leaves circular and external ones in
  place — cannot be realised. Such a property is omitted; such a request body
  skips the endpoint entirely rather than sending a body that is wrong for a
  reason AegisTest did not intend.
- Nested objects are followed four levels deep. Array *items* are not descended
  into; a wrong item type is already covered by the array's own case.
- Path parameters get no `missing_required` case: the URL would no longer
  address the operation, so the result would say nothing about it.
- `multipleOf`, `additionalProperties` and cookie parameters are not covered.
