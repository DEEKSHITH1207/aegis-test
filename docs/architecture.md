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
infrastructure endpoints. Phase 2 adds feature apps (specifications, test runs,
findings) as siblings, each with its own models, serializers, services and URLs.

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
configure the queue. There are no tasks and no worker service yet — those arrive
with the test executor in Phase 2, when there is real work to run.
