"""Cross-cutting error handling for the API.

Services validate their own inputs, because they are also called from places
that have no serializer in front of them — a Celery task, a management command.
That means a service can raise Django's ValidationError, which DRF does not
recognise and would report as a 500. Translating it here keeps the services
free of any knowledge of HTTP while still producing an honest status code.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import exceptions as drf_exceptions
from rest_framework.response import Response
from rest_framework.serializers import as_serializer_error
from rest_framework.views import exception_handler as drf_exception_handler


class DomainError(Exception):
    """A business rule was violated. Reported to the client as HTTP 400."""


def api_exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    if isinstance(exc, DjangoValidationError):
        exc = drf_exceptions.ValidationError(detail=as_serializer_error(exc))
    elif isinstance(exc, DomainError):
        exc = drf_exceptions.ValidationError(detail={"detail": str(exc)})
    return drf_exception_handler(exc, context)
