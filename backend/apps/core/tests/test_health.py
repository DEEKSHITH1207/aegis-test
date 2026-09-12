from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient


def test_health_is_served_at_the_documented_path() -> None:
    assert reverse("health") == "/api/health/"


def test_health_returns_ok() -> None:
    response = APIClient().get("/api/health/")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "ok"}
