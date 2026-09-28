"""Django settings for AegisTest.

Every environment-specific value is read from an environment variable, so the
same code and the same container image run locally, in CI and in production.
Defaults are development-friendly; production must supply DJANGO_SECRET_KEY.
"""

from __future__ import annotations

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

# backend/config/settings.py -> backend/
BASE_DIR = Path(__file__).resolve().parent.parent

# Load the repository .env for local development. Under Docker and in CI the
# variables are already in the environment and this call changes nothing.
load_dotenv(BASE_DIR.parent / ".env")


def env(name: str, default: str | None = None) -> str:
    """Return an environment variable, failing fast when it is required."""
    value = os.getenv(name, default)
    if value is None:
        raise ImproperlyConfigured(f"Missing required environment variable: {name}")
    return value


def env_bool(name: str, *, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


# --- Core ---------------------------------------------------------------------

DEBUG = env_bool("DJANGO_DEBUG", default=False)

# A real secret key is mandatory outside DEBUG: a missing one should break the
# deploy loudly rather than silently fall back to something guessable.
SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-insecure-key" if DEBUG else None)

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "apps.core",
    "apps.testing",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# --- Database -----------------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", "aegis"),
        "USER": env("POSTGRES_USER", "aegis"),
        "PASSWORD": env("POSTGRES_PASSWORD", "aegis"),
        "HOST": env("POSTGRES_HOST", "localhost"),
        "PORT": env("POSTGRES_PORT", "5432"),
    }
}

# --- Redis / Celery -----------------------------------------------------------

REDIS_URL = env("REDIS_URL", "redis://localhost:6379/0")

CELERY_BROKER_URL = env("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", REDIS_URL)
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"

# In development without a worker, set this to run tasks inside the request
# that queues them. A whole test run then executes before the response returns.
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", default=False)

# --- Test execution -----------------------------------------------------------

# Seconds to wait for the target to connect and answer each request.
EXECUTOR_TIMEOUT_SECONDS = float(env("EXECUTOR_TIMEOUT_SECONDS", "10"))
# Response bodies are read up to this size; the rest is discarded.
EXECUTOR_MAX_RESPONSE_BYTES = int(env("EXECUTOR_MAX_RESPONSE_BYTES", "1048576"))
# Set to false only to test an API served with a self-signed certificate.
EXECUTOR_VERIFY_TLS = env_bool("EXECUTOR_VERIFY_TLS", default=True)
# Hosts a run may send requests to. Empty allows any host. Anyone who can reach
# this API can make the server issue requests to any host it can reach, so set
# this on any deployment that is not a single user's machine. A leading dot
# allows subdomains: ".example.com".
EXECUTOR_ALLOWED_HOSTS = env_list("EXECUTOR_ALLOWED_HOSTS", "")

# --- LLM analysis -------------------------------------------------------------

# Off by default. An analysis sends a run's findings and evidence — request
# and response bodies from the API under test — to Anthropic, and every one
# costs money, so a deployment opts in. Credentials are not settings: the
# Anthropic SDK reads ANTHROPIC_API_KEY from the environment.
LLM_ANALYSIS_ENABLED = env_bool("LLM_ANALYSIS_ENABLED", default=False)
LLM_MODEL = env("LLM_MODEL", "claude-opus-5")
# low | medium | high | xhigh | max. Empty leaves the model's default.
LLM_EFFORT = env("LLM_EFFORT", "high")
# Re-run a request the model's safety classifiers decline on Anthropic's
# recommended fallback model. Turn off for a model that does not support it.
LLM_REFUSAL_FALLBACK = env_bool("LLM_REFUSAL_FALLBACK", default=True)
# Findings sent per request, and at most per analysis (most severe first).
LLM_FINDINGS_PER_REQUEST = int(env("LLM_FINDINGS_PER_REQUEST", "20"))
LLM_MAX_FINDINGS = int(env("LLM_MAX_FINDINGS", "200"))
LLM_TIMEOUT_SECONDS = float(env("LLM_TIMEOUT_SECONDS", "600"))
LLM_MAX_RETRIES = int(env("LLM_MAX_RETRIES", "3"))

# --- REST framework -----------------------------------------------------------

REST_FRAMEWORK = {
    # No authentication layer yet; this is explicit so it is a conscious
    # decision to revisit rather than an accidental default.
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    # List endpoints are paginated by default: a specification can describe
    # hundreds of endpoints and a run can produce thousands of cases, and an
    # unbounded list response is a denial-of-service waiting to happen.
    # Service-layer validation errors become 400s instead of 500s.
    "EXCEPTION_HANDLER": "apps.core.exceptions.api_exception_handler",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": int(env("API_PAGE_SIZE", "50")),
}

# --- Internationalisation / static --------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("DJANGO_TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

CELERY_TIMEZONE = TIME_ZONE

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- Logging ------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "%(levelname)s %(name)s %(message)s"}},
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "root": {"handlers": ["console"], "level": env("DJANGO_LOG_LEVEL", "INFO")},
    # httpx logs every request at INFO; a test run sends hundreds.
    "loggers": {"httpx": {"level": "WARNING"}, "httpx2": {"level": "WARNING"}},
}
