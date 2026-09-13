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
evidence and findings — plus `openapi.py`, which reads specification documents. It is a single app rather than one app per model because
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
configure the queue. There are still no tasks and no worker service — those
arrive with the test executor, when there is real work to run. Phase 2 added the
data model those tasks will write to, not the tasks themselves.

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
