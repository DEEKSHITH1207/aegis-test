"""Version 1 of the AegisTest API.

Feature apps register their routes here so that every product endpoint is
reachable under /api/v1/ and a future v2 can be introduced without breaking
existing clients.
"""

from django.urls import include, path

urlpatterns = [
    path("", include("apps.testing.urls")),
]
