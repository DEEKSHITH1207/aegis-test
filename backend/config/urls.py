"""Root URL configuration.

/api/health/  - unversioned infrastructure endpoint (liveness probe).
/api/v1/      - the versioned product API; feature apps mount there.
"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("apps.core.urls")),
    path("api/v1/", include(("config.urls_v1", "v1"), namespace="v1")),
]
