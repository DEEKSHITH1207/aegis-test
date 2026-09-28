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
        "specifications/upload/",
        views.SpecificationUploadView.as_view(),
        name="specification-upload",
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
    path(
        "specifications/<int:specification_id>/import/",
        views.SpecificationImportView.as_view(),
        name="specification-import",
    ),
    path("test-runs/", views.TestRunListCreateView.as_view(), name="test-run-list"),
    path("test-runs/<int:pk>/", views.TestRunDetailView.as_view(), name="test-run-detail"),
    path(
        "test-runs/<int:test_run_id>/generate-tests/",
        views.TestRunGenerateTestsView.as_view(),
        name="test-run-generate-tests",
    ),
    path(
        "test-runs/<int:test_run_id>/execute/",
        views.TestRunExecuteView.as_view(),
        name="test-run-execute",
    ),
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
    path("rules-runs/", views.RulesRunListCreateView.as_view(), name="rules-run-list"),
    path("rules-runs/<int:pk>/", views.RulesRunDetailView.as_view(), name="rules-run-detail"),
    path(
        "rules-runs/<int:rules_run_id>/scenarios/",
        views.RulesRunScenarioListView.as_view(),
        name="rules-run-scenario-list",
    ),
    path(
        "rules-runs/<int:rules_run_id>/exchanges/",
        views.RulesRunExchangeListView.as_view(),
        name="rules-run-exchange-list",
    ),
    path(
        "rules-runs/<int:rules_run_id>/findings/",
        views.RulesRunFindingListView.as_view(),
        name="rules-run-finding-list",
    ),
    path(
        "test-runs/<int:test_run_id>/analyse/",
        views.TestRunAnalyseView.as_view(),
        name="test-run-analyse",
    ),
    path(
        "rules-runs/<int:rules_run_id>/analyse/",
        views.RulesRunAnalyseView.as_view(),
        name="rules-run-analyse",
    ),
    path("analyses/", views.AnalysisListView.as_view(), name="analysis-list"),
    path("analyses/<int:pk>/", views.AnalysisDetailView.as_view(), name="analysis-detail"),
    path(
        "analyses/<int:analysis_id>/assessments/",
        views.AnalysisAssessmentListView.as_view(),
        name="analysis-assessment-list",
    ),
    path(
        "test-runs/<int:test_run_id>/report.pdf",
        views.TestRunReportView.as_view(),
        name="test-run-report",
    ),
    path("features/", views.FeaturesView.as_view(), name="features"),
]
