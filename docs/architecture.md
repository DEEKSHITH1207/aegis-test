# Architecture

## Components

| Component      | Responsibility                                                     |
| -------------- | ------------------------------------------------------------------ |
| Django + DRF   | HTTP API, validation, persistence, orchestration of test runs      |
| PostgreSQL     | System of record: specs, test cases, requests, responses, findings |
| Redis          | Celery broker and result backend                                   |
| Celery worker  | Long-running work: executing API tests, LLM analysis (later)       |
| React frontend | Dashboard for test runs and findings (later)                       |

## Code layout

`backend/config/` is the Django *project*: it contains configuration only —
settings, the root URLconf, the Celery app, and the WSGI/ASGI entrypoints. It
holds no business logic.

`backend/apps/` holds Django *applications*. `apps.core` owns cross-cutting
infrastructure: the health endpoint and the DRF exception handler that turns a
service-layer validation error into a 400 rather than a 500.

`apps.testing` owns the product domain — specifications, endpoints, runs, cases,
evidence and findings — plus two pure modules: `openapi.py`, which reads
specification documents, and `generator.py`, which decides what to test. It is a single app rather than one app per model because
these seven models form one tightly connected graph with one lifecycle: they are
always queried together and a specification's deletion removes all of them.
Splitting them would create circular dependencies between apps and buy nothing.

Business logic belongs in a `services.py` module inside the owning app. Views
stay thin: parse input, call a service, serialize the result. This keeps the
logic callable from both DRF views and Celery tasks without duplication.

## Request flow

A request to `GET /api/health/`:

1. gunicorn (or `runserver` in development) accepts the connection and calls the
   WSGI application built by `config/wsgi.py`.
2. `config/settings.py` has already been loaded: environment variables were read
   at import time, so misconfiguration fails at startup rather than per request.
3. Django's middleware chain runs (security, session, common, CSRF, auth,
   messages, clickjacking).
4. `config/urls.py` matches the `api/` prefix and delegates to
   `apps/core/urls.py`, which matches `health/` and resolves to `HealthView`.
5. DRF wraps the request, applies the default permission class (`AllowAny`), and
   dispatches to `HealthView.get`.
6. The view returns `Response({"status": "ok"})`; DRF's `JSONRenderer` serialises
   it and the middleware chain unwinds on the way out.

The health check touches neither PostgreSQL nor Redis. A liveness probe that
fails when a dependency is down makes orchestrators restart a healthy process;
dependency checks belong in a separate readiness endpoint.

## Versioning

`/api/health/` is unversioned infrastructure. Product endpoints live under
`/api/v1/`, mounted from `config/urls_v1.py`, so a future `v2` can be introduced
alongside `v1` rather than breaking existing clients.

## Asynchronous work

`config/celery.py` defines the Celery app and reads its configuration from
Django settings via the `CELERY_` namespace, so there is a single place to
configure the queue. The one task, `apps/testing/tasks.py`, executes a test
run; the `worker` service in `docker-compose.yml` consumes it. The task is a
thin wrapper around `services.execute_test_run`, which the
`execute_test_run` management command also calls directly.

## Domain model

```
Specification ──< Endpoint
      │               │
      └──< TestRun ──< TestCase ──< RequestRecord ──1:1── ResponseRecord
                          │                                     │
                          └──────────< Finding >────────────────┘
```

`Specification` is the root of every tree. Deleting one cascades to its
endpoints, its runs, and everything those runs produced, so there is exactly one
place to remove an API and everything ever recorded about it.

`RequestRecord` and `ResponseRecord` are immutable evidence: written once when a
request is executed, never edited, so they carry `created_at` but no
`updated_at`. A request has at most one response (`OneToOneField`), while a test
case may have several requests — a retry or a setup call.

`Finding.response_record` is nullable and uses `SET_NULL`. A finding such as
"the endpoint never responded" has no response to cite, and pruning old evidence
must not erase the conclusion drawn from it.

One invariant cannot be expressed as a database constraint: a `TestCase`'s
endpoint must belong to its run's specification. Enforcing it in SQL would mean
denormalising the specification onto the row, so it lives in `TestCase.clean()`
and in the service that creates cases.

## Where logic lives

`apps/testing/services.py` holds the business operations. Views resolve inputs,
call a service and serialise the result; they hold no rules. Serializers check
the shape of a payload, not what the system should do about it. The split is not
ceremony — the Celery tasks added in a later phase need to run the same
operations with no view layer in front of them, and services are the only form
both callers can share.

## Reading specifications

`apps/testing/openapi.py` turns a decoded OpenAPI 3.x or Swagger 2.0 document
into plain dataclasses. It is deliberately pure: no database, and no network.

The no-network rule is a security property, not an optimisation. A `$ref` may
name an external URL, and resolving one would let anyone who can register a
specification make the server issue an outbound request of their choosing.
External references are therefore left unresolved and reported as a warning.

Local references *are* inlined, so a stored schema is self-contained and a later
phase can generate test data from it without carrying the whole document around.
Resolution is depth-limited and tracks the references already being followed, so
a self-referential schema (`User.manager` → `User`) terminates by leaving the
inner `$ref` in place rather than expanding forever.

Resolution happens once, at the outermost object. The helpers that then read a
schema out of a `content` map do not resolve again: each extra pass would walk
one further step into a recursive schema, so re-resolving would quietly undo the
cycle guard.

Parsing is lenient where a document is merely untidy and strict where it claims
to describe an operation. An unusable operation — a `trace`, a path that does not
start with `/`, a path longer than the column that stores it — is skipped and
reported with a reason. Only a document that cannot describe an API at all (no
version key, no `paths`, not an object) is rejected, and then every problem is
reported at once rather than only the first.

## Importing

`services.import_endpoints` applies a parsed document to the database in one
transaction, so a specification never ends up describing a mixture of two
versions of an API.

Endpoints are matched on `(path, method)` — the pair the database already treats
as an endpoint's identity — which makes re-import idempotent and lets an edited
operation be updated in place, keeping the test cases and findings attached to
it. An import only ever writes the fields the parser produces, so data added by
any other means is left alone.

Endpoints that disappear from a document are reported as `stale` rather than
deleted. Deleting one would cascade to its test cases and every request,
response and finding recorded against them: an edit to a document would silently
destroy test history. Deciding what to do about a stale endpoint is a product
decision, and it needs an explicit action rather than a side effect of import.

Only `source_type` is copied back onto the specification from the document. Name
and version are not: they identify the specification and are covered by a unique
constraint, so overwriting them from a file could collide with another row or
rename something a user is already working with.

## Generating test cases

```
View  →  Service  →  Generator  →  TestCase rows
```

`apps/testing/generator.py` is pure in the same way `openapi.py` is: it reads an
Endpoint's fields and returns dataclasses. It opens no database connection and
imports nothing that can reach the network — a test asserts the latter against
the module's own import list, because the whole point of this phase is that it
describes requests without making any. Persistence stays in the service.

Two properties do the real work.

**Deterministic.** No randomness, no clock. The same schema always produces the
same cases with the same names and values, which is what makes re-generation
comparable and a failure reproducible. Property names are sorted before use, so
the output does not depend on the key order a JSON column happens to return.

**One violation per case.** An invalid case breaks exactly one rule and is
otherwise a valid request. A body missing a field *and* carrying a malformed
email cannot tell you which one the API rejected, and a case whose input is
secretly valid reports a bug that does not exist. That second risk is why
`pattern` is skipped with a warning rather than guessed at, why an enum gets no
separate type case, and why a nullable field gets no null case.

### Expected status codes

`parse` keeps only the success *schema*, because that is what an Endpoint
stores — the response codes themselves are not on the model. They are read back
out of the stored document by `openapi.documented_status_codes` at generation
time, which keeps the decision out of the import and off the schema.

The rule is never to invent one: the lowest documented 2xx for a happy path;
400, then 422, then the lowest other 4xx for a rejected request; and `null` when
the document promises neither. `TestCase.expected_status_code` is nullable for
exactly this reason. A null expectation is not a gap — a case with no documented
answer still records what the API did, and a 5xx is a finding whatever the
document says.

### Identity and re-generation

A generated case is identified by `(test_run, endpoint, name)`, and its name is
deterministic, so a second pass matches the rows it wrote the first time and
updates them in place. No database constraint enforces this: generation is the
only writer of these names, and a hand-authored case should not be forbidden
from reusing one. The service owns the rule, and a test asserts that generating
three times leaves the row count unchanged.

Only the fields generation produces are rewritten, so a value set by any other
means survives. The pass is one transaction: a run half-populated with test
cases would be executed as though it were complete, and would under-report.

Generation is refused unless the run is `pending`. Adding cases to a run that has
already executed would silently change what its results mean.

## Executing a run

```
View ─ start_execution ─(on commit)→ Celery task ─ execute_test_run
                                                     │ per case:
                                     executor.prepare → executor.send
                                                     → RequestRecord / ResponseRecord
                                     evaluation.evaluate → TestCase.status, Findings
```

Three modules, each with one job:

- `executor.py` is the **only** module that reaches the network. It builds a
  request from a case's `input_data`, sends it, and returns what came back.
  It never raises for anything the target does — a timeout or refused
  connection is a result, not an exception — so one bad case cannot end a run.
- `evaluation.py` is pure: status code and expectation in, verdict and
  finding drafts out. The rules live in one function and are tested as plain
  calls.
- `services.py` sequences them and owns persistence.

**Starting is separate from executing.** `start_execution` locks the run,
checks it is pending, has a usable base URL and has cases, and moves it to
`running` in one transaction; the task is queued `on_commit`, so a worker never
picks up a run that is still pending. If queueing itself fails — Redis is down
— the run is marked `failed` rather than left `running` with nothing working
on it.

**Each case commits on its own.** Progress is visible while a run executes,
and a crash loses only the case in flight. A run does not execute twice:
re-running would mix two sets of evidence under one set of cases.

**Evidence is recorded before it is judged,** and credentials are redacted
before they are recorded. A run's `headers` stay in the database so the worker
can send them, but never appear in a response, a `RequestRecord` or a finding.

**Unreachable targets stop early.** If the first three requests cannot
connect, every remaining case would wait out the same failure; the run stops
with the rest `skipped`. Once the target has answered, a later connection
failure is recorded against its case and the run continues — an API that falls
over mid-run is itself worth seeing.

### Why 401, 403 and 429 are not verdicts

A case exists to test one rule. A request refused for missing credentials, or
rate-limited, never reached the code that enforces that rule, so passing or
failing it would be claiming something the response does not show. Such cases
are `error`, with a detail pointing at the run's headers.

### Outbound requests

Executing a run makes the server send requests to a host its caller chose.
That is the product, but combined with an API that has no authentication it is
also a server-side request forgery surface. `EXECUTOR_ALLOWED_HOSTS` restricts
the hosts a run may target and is checked when the run is created and again
when it executes. Redirects are not followed, so an allowed host cannot bounce a
request to one that is not.
