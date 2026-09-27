"""HTTP entry points for the testing domain.

Every view does three things and nothing else: resolve its inputs, call a
service, hand the result to a serializer. There is no business logic here, so
the same operations remain callable from a Celery task.
"""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.testing import services
from apps.testing.models import RulesRun, Specification, TestRun
from apps.testing.serializers import (
    EndpointSerializer,
    FindingSerializer,
    GenerationResultSerializer,
    ImportResultSerializer,
    RuleExchangeSerializer,
    RulesRunCreateSerializer,
    RulesRunSerializer,
    ScenarioResultSerializer,
    SpecificationSerializer,
    TestCaseSerializer,
    TestRunSerializer,
)


class SpecificationListCreateView(generics.ListCreateAPIView):
    """GET a page of specifications, or POST a new one."""

    serializer_class = SpecificationSerializer

    def get_queryset(self):
        return services.list_specifications()

    def perform_create(self, serializer: SpecificationSerializer) -> None:
        serializer.instance = services.create_specification(**serializer.validated_data)


class SpecificationDetailView(generics.RetrieveAPIView):
    serializer_class = SpecificationSerializer

    def get_queryset(self):
        return services.list_specifications()


class SpecificationEndpointListView(generics.ListAPIView):
    """Endpoints belonging to one specification."""

    serializer_class = EndpointSerializer

    def get_queryset(self):
        specification = get_object_or_404(Specification, pk=self.kwargs["specification_id"])
        return services.list_endpoints(specification=specification)


class SpecificationImportView(APIView):
    """Parse a specification's stored document into endpoints.

    Returns 200 rather than 201: the call is idempotent and may legitimately
    create nothing, so it reports what an import did rather than claiming a new
    resource every time.
    """

    def post(self, request: Request, specification_id: int) -> Response:
        specification = get_object_or_404(Specification, pk=specification_id)
        result = services.import_endpoints(specification=specification)
        return Response(ImportResultSerializer(result).data, status=status.HTTP_200_OK)


class TestRunListCreateView(generics.ListCreateAPIView):
    """GET a page of runs, optionally filtered by ?specification=<id>, or POST one."""

    serializer_class = TestRunSerializer

    def get_queryset(self):
        specification = None
        specification_id = self.request.query_params.get("specification")
        if specification_id:
            specification = get_object_or_404(Specification, pk=specification_id)
        return services.list_test_runs(specification=specification)

    def perform_create(self, serializer: TestRunSerializer) -> None:
        serializer.instance = services.create_test_run(**serializer.validated_data)


class TestRunDetailView(generics.RetrieveAPIView):
    serializer_class = TestRunSerializer

    def get_queryset(self):
        return services.list_test_runs()


class TestRunGenerateTestsView(APIView):
    """Generate the test cases for a run from its specification's endpoints.

    Returns 200, not 201: the call is idempotent and a repeat run of it
    legitimately creates nothing.
    """

    def post(self, request: Request, test_run_id: int) -> Response:
        test_run = get_object_or_404(TestRun, pk=test_run_id)
        result = services.generate_test_cases_for_run(test_run=test_run)
        return Response(GenerationResultSerializer(result).data, status=status.HTTP_200_OK)


class TestRunExecuteView(APIView):
    """Start sending a run's requests to its base URL.

    Returns 202: the run is handed to a worker and executes in the background.
    Poll the run to follow its progress; its ``results`` fill in as cases finish.
    """

    def post(self, request: Request, test_run_id: int) -> Response:
        test_run = get_object_or_404(TestRun, pk=test_run_id)
        test_run = services.start_execution(test_run=test_run)
        test_run = services.list_test_runs().get(pk=test_run.pk)
        return Response(TestRunSerializer(test_run).data, status=status.HTTP_202_ACCEPTED)


class TestRunTestCaseListView(generics.ListAPIView):
    """Test cases belonging to one run, optionally filtered by ?status=failed."""

    serializer_class = TestCaseSerializer

    def get_queryset(self):
        test_run = get_object_or_404(TestRun, pk=self.kwargs["test_run_id"])
        return services.list_test_cases(
            test_run=test_run, status=self.request.query_params.get("status")
        )


class TestRunFindingListView(generics.ListAPIView):
    """Findings produced by one run, across all of its test cases."""

    serializer_class = FindingSerializer

    def get_queryset(self):
        test_run = get_object_or_404(TestRun, pk=self.kwargs["test_run_id"])
        return services.list_findings(test_run=test_run)


class RulesRunListCreateView(generics.ListAPIView):
    """GET a page of rules runs (?specification=<id> filters), or POST one.

    POST returns 202: the run is checked, started and handed to a worker. The
    ``setup`` values in the request reach the worker and are never stored.
    """

    serializer_class = RulesRunSerializer

    def get_queryset(self):
        specification = None
        specification_id = self.request.query_params.get("specification")
        if specification_id:
            specification = get_object_or_404(Specification, pk=specification_id)
        return services.list_rules_runs(specification=specification)

    def post(self, request: Request) -> Response:
        payload = RulesRunCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        rules_run = services.submit_rules_run(
            specification=data["specification"],
            rules_text=data["rules"],
            base_url=data["base_url"],
            setup_values=data["setup"],
            name=data["name"],
            today=data["today"],
        )
        rules_run = services.list_rules_runs().get(pk=rules_run.pk)
        return Response(RulesRunSerializer(rules_run).data, status=status.HTTP_202_ACCEPTED)


class RulesRunDetailView(generics.RetrieveAPIView):
    serializer_class = RulesRunSerializer

    def get_queryset(self):
        return services.list_rules_runs()


class _RulesRunChildListView(generics.ListAPIView):
    def rules_run(self) -> RulesRun:
        return get_object_or_404(RulesRun, pk=self.kwargs["rules_run_id"])


class RulesRunScenarioListView(_RulesRunChildListView):
    """Each scenario's outcome, with every rule check: expected, working, actual."""

    serializer_class = ScenarioResultSerializer

    def get_queryset(self):
        return services.list_scenario_results(rules_run=self.rules_run())


class RulesRunExchangeListView(_RulesRunChildListView):
    """Every request the run made and what came back, in order, credentials removed."""

    serializer_class = RuleExchangeSerializer

    def get_queryset(self):
        return services.list_rule_exchanges(rules_run=self.rules_run())


class RulesRunFindingListView(_RulesRunChildListView):
    """The run's findings, most severe first."""

    serializer_class = FindingSerializer

    def get_queryset(self):
        return services.list_rules_findings(rules_run=self.rules_run())
