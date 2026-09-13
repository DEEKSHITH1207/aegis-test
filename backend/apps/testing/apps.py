from django.apps import AppConfig


class TestingConfig(AppConfig):
    """The domain of the product: specifications, test runs and their results."""

    name = "apps.testing"
    label = "testing"
    verbose_name = "API testing"
