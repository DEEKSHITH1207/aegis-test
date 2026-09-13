"""Routes for the testing domain, mounted under /api/v1/.

No app_name is declared: these are the product's primary endpoints, so their
names live directly in the "v1" namespace (reverse("v1:specification-list")).
"""

from django.urls import path

from apps.testing import views

urlpatterns = [
    path(
        "specifications/",
        views.SpecificationListCreateView.as_view(),
        name="specification-list",
    ),
    path(
        "specifications/<int:pk>/",
        views.SpecificationDetailView.as_view(),
        name="specification-detail",
    ),
    path(
        "specifications/<int:specification_id>/endpoints/",
        views.SpecificationEndpointListView.as_view(),
        name="specification-endpoint-list",
    ),
    path("test-runs/", views.TestRunListCreateView.as_view(), name="test-run-list"),
    path("test-runs/<int:pk>/", views.TestRunDetailView.as_view(), name="test-run-detail"),
    path(
        "test-runs/<int:test_run_id>/test-cases/",
        views.TestRunTestCaseListView.as_view(),
        name="test-run-test-case-list",
    ),
    path(
        "test-runs/<int:test_run_id>/findings/",
        views.TestRunFindingListView.as_view(),
        name="test-run-finding-list",
    ),
]
