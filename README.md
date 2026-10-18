# AegisTest

Autonomous API Testing & Bug Discovery Platform.

AegisTest takes an OpenAPI/Swagger specification, generates and executes API test
cases against a target API, analyses the responses, and reports bugs, validation
gaps, inconsistencies and security issues. An LLM is used as a reasoning layer
over the collected evidence — never as the sole source of truth.

**Status: Phase 7 — stateful testing.** AegisTest reads an OpenAPI 3.x or
Swagger 2.0 document, turns it into endpoints, generates a deterministic suite of
test cases from their schemas, sends each one to the API under test, and records
what came back as evidence and findings. Cases can be [sequences](#sequences)
that create a resource and then read it back or create it again, every
response is checked against what was sent, and the resources a run creates are
[cleaned up](#cleaning-up) afterwards. A [business-rules file](#business-rules)
adds multi-step scenarios that catch wrong values in otherwise successful
responses. [LLM analysis](#llm-analysis) asks Claude for a second opinion on a
finished run's findings — likely real or likely noise, the probable cause, what
to do next — without ever changing a finding itself. It is off by default.

## Architecture

```
React dashboard (frontend/)
        |
        v
  Django REST API  ----> PostgreSQL <----+
        |                                |
        +------------->  Redis  ---->  Celery worker ----> API under test
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
docker compose logs -f worker      # watch test runs execute
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

## Deploying to a server

`docker-compose.prod.yml` runs everything on one Linux server with Docker,
behind [Caddy](https://caddyserver.com) (`deploy/Caddyfile`):

```
browser ──HTTPS──> Caddy ── shared login ──┬── /api/*  → Django (gunicorn) ─┬─ PostgreSQL
                                           └── else    → built dashboard    ├─ Redis
                                                                            └─ Celery worker
```

Only Caddy publishes ports (80 and 443). The code is built into the images
rather than mounted, Django runs with `DEBUG=False`, and a one-off `migrate`
service applies migrations before Django and the worker start.

```bash
cp .env.prod.example .env       # fill in every value
docker run --rm caddy:2.8-alpine caddy hash-password --plaintext 'team-password'
                                # paste the output into BASIC_AUTH_HASH, in single quotes
docker compose -f docker-compose.prod.yml up -d --build
```

- **Login.** AegisTest has no accounts, so Caddy asks for one shared username
  and password before anything is served — the dashboard and the API alike.
  Caddy drops the `Authorization` header before passing a request to Django,
  which would otherwise try the login as a Django user and refuse it.
- **HTTPS.** Set `SITE_ADDRESS` to a domain whose DNS points at the server, and
  Caddy obtains and renews a Let's Encrypt certificate. `SITE_ADDRESS=:80`
  serves plain HTTP on the server's IP for a first test; the password is then
  sent unencrypted.
- **Targets.** Runs are sent from the server, so the APIs under test must be
  reachable from it — its `localhost` is not yours. Set
  `EXECUTOR_ALLOWED_HOSTS` to your staging and test hosts.
- **Admin.** `/admin/` is not exposed; only `/api/` reaches Django.

To update by hand: `git pull`, then the same `up -d --build`.

### Deploying automatically

A systemd timer on the server checks `main` every five minutes and, when it
has moved, fast-forwards the checkout and runs the same `up -d --build`
(`deploy/autodeploy.sh`). The server pulls; nothing pushes to it, so SSH can
stay closed to everyone but you and GitHub holds no key to the server. It
fetches with the repository's read-only deploy key.

```bash
# On the server, with the repository cloned to /home/ubuntu/aegis-test
sudo cp deploy/aegis-autodeploy.service deploy/aegis-autodeploy.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aegis-autodeploy.timer

systemctl list-timers aegis-autodeploy.timer   # when it next runs
journalctl -u aegis-autodeploy -f              # what each run did
sudo systemctl start aegis-autodeploy          # check right now
```

- **What gets deployed.** Whatever reaches `main`. The timer does not wait
  for CI, so protect `main` on GitHub (require the CI checks to pass before
  merging) and only tested code arrives.
- **Another branch.** `sudo systemctl edit aegis-autodeploy.service`, then add
  `[Service]` and `Environment=AUTODEPLOY_BRANCH=the-branch`.
- **When it refuses.** A checkout that cannot fast-forward (a commit made on
  the server, a force push to `main`) is left untouched and the run fails in
  the journal; sort out the checkout by hand.
- **A forced rebuild** without a new commit: `deploy/autodeploy.sh --force`.
- **Pausing.** `sudo systemctl stop aegis-autodeploy.timer`; `start` resumes.

## Dashboard

`frontend/` is a React + TypeScript app (Vite). From it a user:

1. **Uploads** a Swagger 2.0 or OpenAPI 3.x file, JSON or YAML, by drag and
   drop. It is stored and its endpoints imported in one step.
2. **Starts a test run** from the API's page: the base URL is prefilled from
   the document's `servers` (or `host` + `basePath`), and headers such as
   `Authorization` can be added. It tests everything, or only the sections
   (Swagger tags) and endpoints picked — see [Testing part of an
   API](#testing-part-of-an-api). One click generates the cases and starts
   execution; **Run again** on a finished run repeats it.
3. **Watches it run**: progress, results and findings fill in live. Each
   finding opens to show the request, the response and, once analysed, the AI
   assessment.
4. **Asks for AI analysis** (when the server has it turned on).
5. **Downloads the PDF report**, or opens it in the browser.

```bash
# With docker compose: http://localhost:5173
docker compose up

# On the host, with Django on :8000
cd frontend
npm install
npm run dev          # http://localhost:5173, /api forwarded to :8000
npm test             # vitest
npm run build        # type-check and build to dist/
```

The dev server forwards `/api` to Django (`VITE_API_PROXY`, default
`http://localhost:8000`), so the browser talks to one origin and the backend
needs no CORS setup. Without a Celery worker, set `CELERY_TASK_ALWAYS_EAGER=True`
so a run executes inside the request that starts it.

### Testing part of an API

A large document is rarely tested whole every time. A run can be limited to
some of its endpoints:

- **By section.** Swagger `tags` are stored on each endpoint at import. In the
  dashboard, choose *Choose sections or endpoints*, tick a section to take all
  of its endpoints, or open it and tick single endpoints; search filters by
  path, method, summary or section.
- **Over the API**, pass `tags`, `endpoints` (IDs) or both when creating the
  run; the run tests their union. An unknown tag is a 400 that lists the tags
  the API has.

```bash
curl -X POST http://localhost:8000/api/v1/test-runs/ -H 'Content-Type: application/json' \
  -d '{"specification": 2, "base_url": "http://localhost:4000/sklc", "tags": ["Fleet", "Citizen"]}'
```

A run with no selection tests every endpoint. Each run reports its `scope`
(`{"all": false, "endpoints": 25, "total": 374, "tags": ["Fleet"]}`), and the
PDF states it on the cover.

`POST /test-runs/<id>/rerun/` — **Run again** in the dashboard — creates a new
run with the same endpoints, base URL and headers, generates its cases and
starts it. The headers are copied on the server, so a token does not have to
be typed in again.

Specifications imported before tags were stored had them filled in by
migration `0006` from the stored document; re-importing one does the same.

### The PDF report

`GET /api/v1/test-runs/<id>/report.pdf` renders, with ReportLab:

- the API, target, scope, status and timings;
- an **executive summary**: cases generated, executed, passed, failed, error
  (no verdict) and not run, with the same labels as the dashboard; how many
  operations, paths and methods executed a case; findings grouped as bugs,
  possible issues, documentation gaps and inconclusive, and by severity. Case
  counts and finding counts are kept apart: one case can produce several
  findings, and a note can sit on a case that passed;
- the latest completed AI analysis: summary, verdict counts and likely root
  causes, clearly marked as advisory;
- **test coverage**: every operation with its cases sent, passed, failed, error
  and not run, and the scenarios its cases cover (happy path, required-field
  validation, wrong type, boundary value, semantic rule, duplicate create,
  create and read back);
- **findings** (up to 300, most severe first), in four groups. *Bugs* are
  crashes, accepted invalid or suspicious input and state bugs with medium or
  high confidence; *possible issues* are the low-confidence or low-severity
  ones; *documentation gaps* are every `spec_gap`, such as an undocumented 4xx,
  and are never presented as bugs; *inconclusive* covers the rest. A finding
  marked a false positive during triage is listed under *dismissed*. Each
  finding shows its category, confidence, probable cause, why it matters, the
  request, an excerpt of the response, the steps to reproduce, and the AI
  assessment beside it;
- **test case details** (up to 500): per operation, each case's name, scenario,
  purpose, the request as sent, the expected and actual status, its result and
  the recorded verdict;
- **stateful testing and cleanup**: every case that sent more than one request,
  step by step, and every resource the run created with whether cleanup
  deleted it;
- **limitations**: cases never sent and cases without a verdict, grouped by
  reason; operations of the specification that executed no case; and what
  generated cases do not cover.

Run credentials never appear: the report is built from stored evidence, which
is redacted when it is written, and secret-named fields in request and
response bodies (`password`, `token`, `api_key`, ...) are masked again when
the report is built. Long bodies and verdicts are cut on the page; the full
text stays in the API. The built-in PDF fonts cover Western European text
only; other characters print as `?`.

## Tests

```bash
cd backend
pytest
```

Tests run against `config/settings_test.py`, which supplies the required
environment variables before importing the real settings — so the suite needs no
`.env` file. It does need a running PostgreSQL whose role may create databases
(see [Running on the host](#running-on-the-host)); Redis is not used.

```bash
cd frontend
npm test            # vitest
npm run build       # type check and production build
```

GitHub Actions (`.github/workflows/ci.yml`) runs all of this on every pull
request and every push to `main`: the backend suite against a PostgreSQL 16
service, `makemigrations --check`, and the frontend tests and build.

## API

| Method | Path                                            | Purpose                                    |
| ------ | ----------------------------------------------- | ------------------------------------------ |
| GET    | `/api/health/`                                  | Liveness probe. Returns `{"status": "ok"}` |
| POST   | `/api/v1/specifications/`                       | Register a specification                   |
| POST   | `/api/v1/specifications/upload/`                | Upload a JSON/YAML file and import it      |
| GET    | `/api/v1/specifications/`                       | List specifications                        |
| GET    | `/api/v1/specifications/<id>/`                  | Retrieve one specification                 |
| GET    | `/api/v1/specifications/<id>/endpoints/`        | Endpoints of a specification               |
| POST   | `/api/v1/specifications/<id>/import/`           | Parse the document into endpoints          |
| POST   | `/api/v1/test-runs/`                            | Open a test run; `endpoints`/`tags` limit it |
| POST   | `/api/v1/test-runs/<id>/rerun/`                 | Same scope, target and headers again (202) |
| GET    | `/api/v1/test-runs/`                            | List runs; `?specification=<id>` filters   |
| GET    | `/api/v1/test-runs/<id>/`                       | Retrieve one run                           |
| POST   | `/api/v1/test-runs/<id>/generate-tests/`        | Generate the run's test cases              |
| POST   | `/api/v1/test-runs/<id>/execute/`               | Send the run's requests (202, background)  |
| GET    | `/api/v1/test-runs/<id>/test-cases/`            | Test cases; `?status=failed` filters       |
| GET    | `/api/v1/test-runs/<id>/findings/`              | Findings produced by a run                 |
| GET    | `/api/v1/test-runs/<id>/resources/`             | What the run created in the target         |
| POST   | `/api/v1/test-runs/<id>/cleanup/`               | DELETE what a finished run created         |
| POST   | `/api/v1/rules-runs/`                           | Run a business-rules file (202)            |
| GET    | `/api/v1/rules-runs/`                           | List rules runs; `?specification=<id>`     |
| GET    | `/api/v1/rules-runs/<id>/`                      | One rules run, with result counts          |
| GET    | `/api/v1/rules-runs/<id>/scenarios/`            | Scenarios and every rule check             |
| GET    | `/api/v1/rules-runs/<id>/exchanges/`            | Every request/response, credentials removed|
| GET    | `/api/v1/rules-runs/<id>/findings/`             | Broken rules, most severe first            |
| POST   | `/api/v1/test-runs/<id>/analyse/`               | Analyse a finished run's findings (202)    |
| POST   | `/api/v1/rules-runs/<id>/analyse/`              | Analyse a finished rules run (202)         |
| GET    | `/api/v1/analyses/`                             | List; `?test_run=<id>` / `?rules_run=<id>` |
| GET    | `/api/v1/analyses/<id>/`                        | Summary, groups, verdict counts, usage     |
| GET    | `/api/v1/analyses/<id>/assessments/`            | One assessment per finding                 |
| GET    | `/api/v1/test-runs/<id>/report.pdf`             | PDF report; `?inline=1` to view in-browser |
| GET    | `/api/v1/features/`                             | Whether AI analysis is turned on           |

`/api/health/` is intentionally unversioned: it is an infrastructure probe, not
part of the product contract.

List responses are paginated (`{"count", "next", "previous", "results"}`); the
page size comes from `API_PAGE_SIZE` and defaults to 50.

A run's `status`, `status_detail`, `started_at` and `completed_at` are owned by
the system and ignored if a client sends them. Test cases and evidence rows are
written by the generator and the executor, so they are read-only over HTTP.

## Where the specification comes from

The specification is the API's OpenAPI or Swagger document — one JSON or YAML
file listing every endpoint, the fields it accepts with their types and limits,
and the responses it can return. The Swagger UI page is that file rendered.
Most frameworks generate it from code rather than having it written by hand:

| Framework | How to get the document |
| --- | --- |
| FastAPI | built in: `GET /openapi.json` |
| Django REST Framework | add `drf-spectacular`: `GET /api/schema/` |
| Flask | add `flask-smorest`, `apispec` or `flasgger` |
| NestJS | `@nestjs/swagger`: `GET /<path>-json`, e.g. `/api-json` after `SwaggerModule.setup('api', ...)` |

The rules come from the request models — Pydantic models, serializers, DTOs —
not from comments. This model:

```python
class FinancialYearIn(BaseModel):
    """Create a financial year."""
    startYear: int = Field(ge=2000, le=2100)
```

becomes:

```json
"FinancialYearIn": {
  "description": "Create a financial year.",
  "required": ["startYear"],
  "properties": {"startYear": {"type": "integer", "minimum": 2000, "maximum": 2100}}
}
```

The docstring is only a `description`, which AegisTest does not read. Test
cases are built from `type`, `required`, `minimum`/`maximum`,
`minLength`/`maxLength`, `format`, `enum` and the documented response codes.
In NestJS, `class-validator` decorators (`@IsInt()`, `@Min(2000)`) enforce a
rule at runtime but reach the document only through `@ApiProperty({minimum:
2000})` or the `@nestjs/swagger` CLI plugin.

How much is tested depends on how much the document says:

| | Endpoints only | Full schemas |
| --- | --- | --- |
| 5xx reported as a crash | yes | yes |
| Duplicate create, read back, cleanup | only for a POST documenting 201 | yes |
| Required, type and boundary cases | no | yes |
| Semantic cases (end before start, negative price, ...) | no | yes |
| Status codes judged against the document | only crashes, when no responses are listed | yes |

A document without field details still runs, but finds little. Rules a schema
cannot express — a value computed from other fields, a cross-field rule
enforced in a validator — belong in a [business-rules file](#business-rules).

With FastAPI in particular:

- give create endpoints `status_code=201`; FastAPI documents 200 by default,
  and duplicate-create and read-back cases are generated only for a 201;
- invalid input is answered with 422, which FastAPI documents, so invalid cases
  expect 422 (see [Expected status codes](#expected-status-codes));
- a `@model_validator` rule such as "end after start" is not in the document.
  The semantic cases still probe it when the field names look like a range.

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
the requests they describe is [execution](#executing-a-run), a separate step.

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

## Executing a run

Execution sends every generated case to the API under test, records the request
and response, and judges the result. Point a run at your API when you create it:

```bash
curl -X POST http://localhost:8000/api/v1/test-runs/ \
     -H 'Content-Type: application/json' \
     -d '{"specification": 1,
          "base_url": "http://host.docker.internal:3000",
          "headers": {"Authorization": "Bearer <token>"}}'

curl -X POST http://localhost:8000/api/v1/test-runs/1/generate-tests/
curl -X POST http://localhost:8000/api/v1/test-runs/1/execute/      # 202, runs on the worker
curl http://localhost:8000/api/v1/test-runs/1/                      # poll: status + results
curl http://localhost:8000/api/v1/test-runs/1/findings/
curl 'http://localhost:8000/api/v1/test-runs/1/test-cases/?status=failed'
```

`base_url` is joined with each endpoint's path, so include any prefix the paths
in your document leave out (`http://localhost:3000/api`). From inside Docker,
`localhost` is the container itself: an API running on your machine is
`http://host.docker.internal:<port>`.

`headers` are sent with every request. They are **write-only** — the API
returns only `header_names` — and credential-like headers (`Authorization`,
`Cookie`, anything containing `token`, `key`, `secret`, `auth`) are redacted in
the stored request records and findings. They are stored in the database in
plain text, so treat the database as holding your API credentials.

Without a worker — running Django on the host — execute in the foreground:

```bash
python manage.py execute_test_run 1
# Run 1 completed: 48 sent, 31 passed, 15 failed, 2 errors, 0 skipped, 15 findings.
```

or set `CELERY_TASK_ALWAYS_EAGER=True` to run the task inside the `execute/`
request itself.

### How a response is judged

Rules apply in this order:

| Response | Case | Finding |
| --- | --- | --- |
| 5xx | `failed` | **crash**, high — the input was not handled |
| 409, or a 4xx saying "already exists" / "duplicate" / "conflict" | `error` | none — the request collided with data already in the target (happy-path and semantic cases are retried first; see below) |
| 404 for a sample path value (`id: 1`) that nothing could replace with a real one | `error` | none — the ID probably names nothing, so the case is inconclusive |
| 2xx to an invalid case | `failed` | **validation_gap**, medium — forbidden input was accepted |
| 2xx to a semantic case | `failed` | **validation_gap** "suspicious acceptance", with the case's own severity |
| 401 / 403 / 429 not documented for the case | `error` | none — the request was refused before the API looked at it; usually missing credentials |
| 404 for a resource the case created a moment earlier | `failed` | **state_bug**, medium — the create said it worked |
| A 4xx other than the documented one, to an invalid case | `passed` | one **spec_gap** note per operation, low — the rule is enforced; the code differs from the document |
| A code other than the documented one | `failed` | **spec_gap**, low |
| Anything else | `passed` | none |

Every 2xx to a happy-path or semantic case is then looked inside:

- **Echo** — each value sent comes back unchanged. Trimming, case, `10.5` as
  `"10.50"` and a date returned as midnight of that date are accepted; a date
  shifted by a time zone is not. A difference is a **state_bug**.
- **Derived fields** — a string the API worked out from the input, such as a
  name `"2030-20"` from `startYear: 2030, endYear: 2020`, is read the way people
  write ranges. One that runs backwards, or names a number nothing sent, is a
  low-confidence **state_bug**.
- **Response schema** — the body has the documented shape (types, required
  properties, enums, items). A departure is a **spec_gap**.

Every finding has a category (`crash`, `validation_gap`, `state_bug`,
`spec_gap`), a confidence (`low`/`medium`/`high`), a
one-line `probable_cause`, and `reproduction`: the fewest requests that show the
problem, in order, setup requests included. It also carries its evidence and
links to the full `ResponseRecord`.

A case with no documented code passes on anything not ruled out above: asserting
a code the document never promised would manufacture failures.

### Test data that does not collide

The document's `example` values are the ones most likely to exist already in the
target, so a create sent with them fails for reasons that have nothing to do with
the request. Fields that look unique — names, codes, titles, years, emails,
usernames — get fresh values instead: `"MT"` becomes `"MT-3f9a1c"`, a year range
`2025–2026` becomes, say, `2061–2062` (the range keeps its width, and stays
inside `minimum`/`maximum`). A field named like an ID (`unitId`) is never
changed: it points at another record. Values are derived from the run and the
case name, so re-generating a run reproduces its requests exactly, while two runs
never share values.

If a happy path still collides — a 409, or a 4xx whose message says it already
exists — it is sent again with every free-text field freshened, up to three more
times, and judged on the last attempt. A semantic case that collides is rebuilt
from a retry seed, so the relation it tests (an end before its start) is applied
to values the target has not seen. A collision is never reported as "expected
201"; if the API's 409 is not
in the document, a single low **spec_gap** note per operation says so. A happy
path that answers 5xx but succeeds with fresh values is reported as a crash *on
existing values* (an unhandled unique constraint), not as "500 on happy path".

### Sequences

The happy path of a `GET`, `DELETE`, `PUT` or `PATCH` on `{collection}/{id}`
never uses the generated sample ID, which may name nothing — or somebody's
data. When the document has a `POST {collection}`, the case creates a resource
first (tracked for cleanup like any other), reads its ID from the response
(`id`, `_id`, `uuid`, or the path parameter's own name), and sends the request
to that. A `DELETE` that succeeds is then followed by a `GET` of the same ID:
still readable is a **state_bug**. A create that fails, or answers without an
ID, leaves the case `error`: an ID is never guessed, and an unexecuted check is
never a pass. The same goes for every other case of a `PUT`, `PATCH` or
`DELETE` on the item: it is sent to a record created for it, with its invalid
body or parameter intact, or `skipped` when no disposable record can be made.
A `GET` without a `POST` sends the sample value, and a 404 is `error`
(inconclusive) rather than a finding.

Cleanup deletes only what the run can show it created: a `POST` that answered
201. A `POST` documented as a create that answers 200 is listed as left behind
with the reason, because an upsert or find-or-create may have handed back a
record that existed; it is never deleted.

For every create — a POST documenting 201 — two more cases send several
requests:

- **`duplicate_create`** sends the same valid body twice. A 409/400 or a second
  201 passes; a 5xx is a **crash** titled "duplicate create returns 500
  (unhandled unique constraint)", reproduced by both requests.
- **`create_read_back`** creates a resource, then reads it with
  `GET {path}/{id}` (or finds it in `GET {path}`), and checks it holds the values
  sent. Missing or different is a **state_bug**; in a list that looks paginated
  the confidence is low.

### Semantic cases

A schema checks fields one at a time, so `{"startYear": 2030, "endYear": 2020}`
passes it. These cases send schema-valid values that a sensible API should still
question. Accepting the ones marked *flagged* is a "suspicious acceptance":

| Case | Values | If accepted |
| --- | --- | --- |
| `cross_field:a/b:end_before_start` | start/end, from/to, since/until, min/max pairs: end below start | flagged, medium, high confidence |
| `cross_field:a/b:end_equals_start` | end equal to start (not for min/max) | flagged, low |
| `cross_field:a/b:end_far_after_start` | end at its maximum, or far beyond start | flagged, low |
| `suspicious_value:x:negative` / `zero` / `huge` | prices, amounts, quantities — only where the schema allows the value | negative medium; zero and huge low |
| `suspicious_value:x:far_past` / `far_future` | dates in 1900 / 2999 | flagged, low |
| `suspicious_value:x:feb_29_non_leap` | `2025-02-29` | flagged, medium |
| `suspicious_value:x:empty` / `whitespace` | required names, titles, codes | flagged, medium |
| `suspicious_value:x:very_long` / `unicode` | 5,000 characters (when no `maxLength`) / accented, CJK and symbols | not flagged; a 5xx is a crash and a mangled echo a state_bug |

A semantic case answered with a 4xx passes: the API checks the rule.

### Cleaning up

Every resource a run creates — any 201, or a 2xx to a POST that returned an
ID — is recorded with how to delete it (`DELETE {path}/{id}`, when the document
has one). Create a run with `"cleanup": true` (the dashboard's *Clean up
afterwards*, on by default) and it deletes them when it finishes; or call
`POST /test-runs/<id>/cleanup/` later. `GET /test-runs/<id>/resources/` lists
them, a run reports `resources: {deleted, failed, left}`, and the PDF has a
*Data left behind* section naming what remains and why: no ID in the response,
no DELETE in the document, or a refused DELETE.

### Run lifecycle

`execute/` checks the run is `pending`, has a usable `base_url` and has test
cases, then moves it to `running` and queues it. The run ends `completed` once
every case has been sent, or `failed` with a `status_detail` saying why. The
run's `results` count cases per status while it executes, so polling it shows
progress. A run executes once; to test again, create a new run.

- A timeout or refused connection marks that case `error` and the run carries on.
- If the first three requests all fail to connect, the target is taken to be
  unreachable: the run stops, the rest are `skipped`, and the run is `failed`.
- Redirects are recorded, not followed. Response bodies are read up to
  `EXECUTOR_MAX_RESPONSE_BYTES`.

### Settings

| Variable | Default | |
| --- | --- | --- |
| `EXECUTOR_TIMEOUT_SECONDS` | `10` | Per request |
| `EXECUTOR_MAX_RESPONSE_BYTES` | `1048576` | Larger bodies are truncated |
| `EXECUTOR_VERIFY_TLS` | `True` | `False` only for self-signed certificates |
| `EXECUTOR_ALLOWED_HOSTS` | empty (any) | Comma-separated; `.example.com` allows subdomains |

**`EXECUTOR_ALLOWED_HOSTS` matters.** The AegisTest API has no authentication
yet, so anyone who can reach it can make the server send requests to any host
*it* can reach — internal services, cloud metadata endpoints. Leave it empty
only on your own machine.

### Execution limitations

- Request bodies are sent as JSON, whatever media type the document declares.
- Response bodies are checked against the schema of the lowest documented 2xx
  only, and the checker understands types, required properties, enums, items and
  compositions — not formats, patterns or `additionalProperties`.
- Semantic cases are heuristics on field names: a range spelled `opensAt` and
  `closesAt`, or a uniqueness rule on a field that does not look unique, is not
  recognised (the retry still catches the latter on a happy path).
- Every case of a `PUT`, `PATCH` or `DELETE` on `{collection}/{id}`, and the
  happy path of a `GET`, first creates a disposable record through
  `POST {collection}` and addresses that; the case keeps whatever it
  deliberately breaks. A mutating request is never sent to the sample path
  value: when the collection has no `POST`, the create answers something
  other than 201 (an upsert may return an existing record), or the case breaks
  the ID itself with a number, the case is `skipped` with the reason. A `GET`'s
  other cases are read-only and still use the sample value. An API that creates
  under a different path is not recognised.
- A run stuck in `running` because its worker died stays there; there is no
  cancel or retry yet.
- The same credentials are used for every case: there are no authentication or
  authorisation cases yet.

## Business rules

Generated tests catch what a schema forbids. They cannot catch a response that is
`200`, well-formed, and holds the wrong number. A business-rules file describes
what the numbers should be: scenarios of requests, and rules checked after them.

```yaml
version: 1
timezone: Asia/Kolkata            # what "today" means

setup:                            # supplied at run time, never stored
  employee_id: {}                 # read from AEGIS_EMPLOYEE_ID
  username: {}
  password: {}                    # secret: the name contains "password"

variables:
  leave_start: ${today - 4d}

timelines:                        # a quantity that changes over time
  accrual:
    - {from: today - 14d, to: today - 11d, rate: 2}
    - {from: today - 10d, rate: 4}

auth:                             # the token is sent on every later request
  method: POST
  path: /auth/login
  body: {username: "${username}", password: "${password}"}
  token: $.access_token

scenarios:
  - name: leave_balance
    steps:
      - name: apply
        method: POST
        path: /employees/{employee_id}/leaves
        body: {start: "${leave_start}", end: "${today}"}
        expect: 201
        save: {leave_id: $.id}           # usable by later steps
      - name: approve
        method: POST
        path: /leaves/{leave_id}/approve
        expect: 200
      - name: raise_rate
        method: PUT
        path: /employees/{employee_id}/accrual
        body: {rate: 5}
        expect: 200
        timeline:
          accrual: {from: today, rate: 5}  # closes the open period at today - 1d
    rules:
      - name: balance_is_accrual_minus_taken
        severity: high
        why: The balance must equal the leave accrued so far minus the leave taken.
        after: [approve, raise_rate]       # omit to check after every step
        fetch: {method: GET, path: "/employees/{employee_id}/balance"}
        actual: $.balance                  # a JSON path, or an expression
        expected: sum_over_periods(accrual) - days(leave_start, today)
    cleanup:                               # always runs
      - name: delete_leave
        method: DELETE
        path: /leaves/{leave_id}
```

When the API gets it wrong, the finding says what, why, and how the expected
value was reached:

```
BROKEN [high] balance_is_accrual_minus_taken after approve
  expected 47, got 55
  expected worked out as: 4 days × 2 + 11 days × 4 = 52; leave_start = 2026-09-23;
                          sum_over_periods(accrual) - days(leave_start, today) = 47
```

### Running one

```bash
python manage.py run_rules --specification 1 --rules rules.yaml \
    --base-url http://localhost:3000 --env-file .env.rules [--today 2026-09-27]
```

Setup values come from the environment (`AEGIS_<NAME>`, or the `env:` the file
names); `--env-file` adds a dotenv file that wins over the environment. The
command prints every rule check and exits `1` when a rule is broken or a
scenario stopped, so it can gate CI.

Over the API, the values go in the request and are never stored:

```bash
curl -X POST http://localhost:8000/api/v1/rules-runs/ -H 'Content-Type: application/json' \
  -d '{"specification": 1, "base_url": "http://host.docker.internal:3000",
       "rules": "<the YAML, as a string>",
       "setup": {"employee_id": "42", "username": "asha", "password": "..."}}'
```

They reach the worker as task arguments through Redis, and exist only in
memory while the run executes.

### Expressions

Expressions are evaluated by [simpleeval](https://pypi.org/project/simpleeval/),
never `eval()`: arithmetic, comparisons, indexing, and only these names —

| | |
| --- | --- |
| `today`, `today - 4d`, `today + 10d` | The run's date, in the file's time zone; day arithmetic |
| `days(from, to)` | Days between two dates, both ends included |
| `date("2026-01-31")` | A date from an ISO string |
| `sum_over_periods(timeline)` | Σ days(from, to) × rate; an open period runs to today |
| `response` | The last response body (or the rule's `fetch`) |
| `abs min max round sum len int float str` | As in Python |

In bodies, headers and queries, `${expr}` is replaced by its value; a string
that is exactly one `${...}` keeps the value's type. Paths use `{name}`.

Rules compare with `equals` (default, optionally with `tolerance`),
`not_equals`, `less_than`, `less_or_equal`, `greater_than` or
`greater_or_equal`.

### What is reported

| Outcome | Finding |
| --- | --- |
| A rule does not hold | **business_rule_violation**, at the rule's severity |
| A step returns a status its `expect` does not allow | **inconsistency** (medium), or **bug** (high) for a 5xx; the scenario stops |
| A rule cannot be checked (field missing, `fetch` failed) | none; the check is `unchecked` on the scenario |
| The target cannot be reached | none; the scenario records why it stopped |

Every finding carries the full request/response chain that led to it; the
chain's entries point at stored exchanges (`/rules-runs/<id>/exchanges/`).

### Credentials

- Setup values are never stored as a field of a run.
- Secret setup values, and the token the login returned, are scrubbed from every
  stored request and response, including URL-encoded forms. A value is secret
  when it says `secret: true`, or when its name contains `password`, `token`,
  `secret` or `key` and it does not say `secret: false`.
- Non-secret values, such as IDs, stay visible where the requests used them.
- `Authorization` and other credential headers are redacted as for test runs.

### `--today`

The same file and the same `today` send the same requests. `--today` (or
`today` in the API request) sets the date used for `today` in what AegisTest
sends. **It does not move the server's clock**: anything the API computes from
its own date still uses its real today, so a rule that depends on the server's
idea of today can only be checked on that day.

### Before a run starts

The whole file is checked first, and every problem is reported at once with
where it is: unknown keys, undefined variables (a step can only use values saved
by earlier steps), bad JSON paths, expression syntax, unknown step names in
`after`. A step whose request is not an operation of the specification is a
warning, not an error.

## LLM analysis

The deterministic checks above decide what is a finding. They cannot tell a
real defect from an artefact of the test — a happy path that got `404` because
the example ID does not exist in your data, a "below minimum" value the schema
actually allows, a business rule whose expected value was worked out wrongly.
An analysis sends a finished run's findings, with their evidence, to Claude and
records a second opinion on each one.

**Advisory only.** An analysis writes its own rows. It never changes a finding's
severity, category or triage status; a person still triages every finding, with
the assessment next to it.

```bash
# In .env: LLM_ANALYSIS_ENABLED=True and ANTHROPIC_API_KEY=...
curl -X POST http://localhost:8000/api/v1/test-runs/1/analyse/   # 202, runs on the worker
curl http://localhost:8000/api/v1/analyses/1/                    # poll: status, summary, groups
curl http://localhost:8000/api/v1/analyses/1/assessments/
```

Without a worker, run it in the foreground:

```bash
python manage.py analyse_run --test-run 1
python manage.py analyse_run --rules-run 1
# Analysis 1 completed: 15 of 15 findings assessed in 1 requests — 11 likely real,
# 3 likely false positives, 1 uncertain.
```

### What comes back

Each **assessment** (one per finding) has:

| Field | |
| --- | --- |
| `verdict` | `likely_real`, `likely_false_positive` or `uncertain` |
| `confidence` | `low`, `medium` or `high` |
| `suggested_severity` | What the model would rate it if real; compare with `finding_severity` |
| `reasoning` | Why, citing the evidence |
| `likely_cause` | Best hypothesis for the cause in the API or its document |
| `recommendation` | The fix, or what to check to confirm |

The **analysis** has a `summary` for whoever triages the run, and `groups`:
findings that look like one underlying problem (say, every validation gap on
one endpoint), each with its `finding_ids`, an explanation and one
recommendation. `results` counts assessments by verdict; `usage` sums the
requests and tokens spent; `warnings` lists anything the model got wrong that
was discarded.

### How it works

- Findings are sent most severe first, `LLM_FINDINGS_PER_REQUEST` at a time. A
  run that fits in one request is assessed and summarised in that request;
  a larger one gets one more request for the summary.
- The reply is constrained to a JSON schema, then checked: an assessment of a
  finding that was not sent, a second assessment of the same finding, or a
  group citing an unassessed finding is dropped and reported in `warnings`.
- Each batch commits on its own. If a request fails part-way, the analysis is
  `failed` with the reason in `status_detail`, and the batches already
  assessed are kept.
- A run can be analysed again (another model, another effort level); each
  analysis is kept. Only one analysis of a run may be in progress at a time.

### What is sent to Anthropic

For each finding: its title, description, severity, category and evidence —
the request that was sent and an excerpt of the response body — plus the
endpoint's path, parameters and request schema, or the scenario name and the
rules file. Credentials are redacted from evidence before it is ever stored,
and a run's `headers` and a rules run's setup values are never sent. Response
bodies from the API under test *are* sent: if those can hold personal data,
decide whether that is acceptable before turning analysis on.

The evidence is untrusted text from the API under test, and could try to
instruct the model. The prompt tells the model to ignore any such text, and
because an analysis can only write advisory rows, the worst a successful
injection can do is write a misleading assessment.

### Settings

| Variable | Default | |
| --- | --- | --- |
| `LLM_ANALYSIS_ENABLED` | `False` | Nothing is sent until this is `True` |
| `ANTHROPIC_API_KEY` | — | Read by the Anthropic SDK, not by Django settings |
| `LLM_MODEL` | `claude-opus-5` | |
| `LLM_EFFORT` | `high` | `low` … `max`; lower is cheaper and faster |
| `LLM_REFUSAL_FALLBACK` | `True` | Re-run a request the model's safety classifiers decline on Anthropic's recommended fallback model; `False` for models that do not support it |
| `LLM_FINDINGS_PER_REQUEST` | `20` | Lower it if replies are cut off |
| `LLM_MAX_FINDINGS` | `200` | Per analysis; the rest are reported in `warnings` |
| `LLM_TIMEOUT_SECONDS` | `600` | Per request |
| `LLM_MAX_RETRIES` | `3` | For rate limits, overload and 5xx |

### Analysis limitations

- The AegisTest API has no authentication yet, so anyone who can reach it can
  start an analysis and spend your Anthropic credit. Keep it private.
- An analysis stuck in `running` because its worker died blocks new analyses of
  that run; there is no cancel yet.
- Assessments are not yet shown next to findings in `/findings/`; read them
  from the analysis.

