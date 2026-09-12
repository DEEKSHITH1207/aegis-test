from django.apps import AppConfig


class CoreConfig(AppConfig):
    """Cross-cutting infrastructure endpoints (health checks and similar)."""

    name = "apps.core"
    label = "core"
