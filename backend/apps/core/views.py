from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView


class HealthView(APIView):
    """Liveness probe: confirms the Django process can accept and serve requests.

    Deliberately does not touch PostgreSQL or Redis. A liveness probe that fails
    when a dependency is down causes orchestrators to restart a perfectly
    healthy process. Dependency readiness gets its own endpoint when we need it.
    """

    def get(self, request: Request) -> Response:
        return Response({"status": "ok"})
