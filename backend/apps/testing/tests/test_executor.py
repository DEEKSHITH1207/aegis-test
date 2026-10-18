"""Building, sending and recording requests. No database, and no real network:
every request goes to an in-process httpx.MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest

from apps.testing import executor


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def _prepare(**overrides) -> executor.PreparedRequest:
    arguments = {
        "method": "get",
        "path": "/users",
        "input_data": {"path": {}, "query": {}, "headers": {}, "body": None},
        "base_url": "http://api.test",
    }
    arguments.update(overrides)
    return executor.prepare(**arguments)


# --- Base URL validation ------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["http://localhost:3000", "https://api.example.com/v1", "http://api:8080", "http://10.0.0.5"],
)
def test_usable_base_urls_have_no_problem(url: str) -> None:
    assert executor.base_url_problem(url, []) == ""


@pytest.mark.parametrize(
    ("url", "fragment"),
    [
        ("", "required"),
        ("ftp://api.example.com", "http"),
        ("api.example.com", "http"),
        ("http://", "host"),
        ("http://user:secret@api.example.com", "credentials"),
        ("http://api.example.com?x=1", "query"),
    ],
)
def test_unusable_base_urls_are_explained(url: str, fragment: str) -> None:
    assert fragment in executor.base_url_problem(url, [])


def test_the_allow_list_restricts_hosts() -> None:
    allowed = ["localhost", ".example.com"]

    assert executor.base_url_problem("http://localhost:3000", allowed) == ""
    assert executor.base_url_problem("https://api.example.com", allowed) == ""
    assert executor.base_url_problem("https://example.com", allowed) == ""
    assert "not in EXECUTOR_ALLOWED_HOSTS" in executor.base_url_problem(
        "http://169.254.169.254", allowed
    )
    # A suffix match must be on a label boundary.
    assert executor.base_url_problem("https://evilexample.com", allowed) != ""


# --- Preparing a request ------------------------------------------------------


def test_path_parameters_are_substituted_and_encoded_whole() -> None:
    prepared = _prepare(
        path="/users/{userId}/files/{name}",
        input_data={"path": {"userId": 7, "name": "a/b c"}},
    )

    assert prepared.url == "http://api.test/users/7/files/a%2Fb%20c"
    assert prepared.path_params == {"userId": 7, "name": "a/b c"}


def test_a_trailing_slash_on_the_base_url_is_not_doubled() -> None:
    assert _prepare(base_url="http://api.test/v1/").url == "http://api.test/v1/users"


def test_a_path_parameter_without_a_value_is_left_visible() -> None:
    assert _prepare(path="/users/{userId}").url == "http://api.test/users/{userId}"


def test_query_values_are_rendered_the_way_an_api_expects() -> None:
    prepared = _prepare(
        input_data={"query": {"active": True, "page": 2, "tag": ["a", "b"], "empty": None}}
    )

    assert prepared.query_params == {
        "active": "true",
        "page": "2",
        "tag": ["a", "b"],
        "empty": "",
    }


def test_case_headers_override_run_headers() -> None:
    prepared = _prepare(
        input_data={"headers": {"Authorization": "Bearer case"}},
        run_headers={"Authorization": "Bearer run", "X-Tenant": "acme"},
    )

    assert prepared.headers["Authorization"] == "Bearer case"
    assert prepared.headers["X-Tenant"] == "acme"
    assert prepared.headers["Accept"] == "application/json"


def test_a_body_is_sent_as_json() -> None:
    prepared = _prepare(method="post", input_data={"body": {"name": "x"}})

    assert prepared.headers["Content-Type"] == "application/json"
    assert prepared.body == {"name": "x"}


def test_no_body_means_no_content_type() -> None:
    assert "Content-Type" not in _prepare().headers


# --- Sending ------------------------------------------------------------------


def test_a_json_response_is_captured() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": 1}, headers={"X-Request-Id": "abc"})

    prepared = _prepare(
        method="post", input_data={"query": {"dry": False}, "body": {"name": "x"}}
    )
    result = executor.send(prepared, client=_client(handler), max_response_bytes=10_000)

    assert result.status_code == 201
    assert result.body == {"id": 1}
    assert result.headers["x-request-id"] == "abc"
    assert result.error == ""
    assert seen == {"url": "http://api.test/users?dry=false", "body": {"name": "x"}}


def test_a_non_json_response_is_kept_as_text() -> None:
    client = _client(lambda request: httpx.Response(500, text="<h1>Internal Error</h1>"))

    result = executor.send(_prepare(), client=client, max_response_bytes=10_000)

    assert result.status_code == 500
    assert result.body == "<h1>Internal Error</h1>"


def test_an_empty_response_has_no_body() -> None:
    client = _client(lambda request: httpx.Response(204))

    assert executor.send(_prepare(), client=client, max_response_bytes=10).body is None


def test_a_large_response_is_truncated() -> None:
    client = _client(lambda request: httpx.Response(200, content=b"x" * 5_000))

    result = executor.send(_prepare(), client=client, max_response_bytes=100)

    assert result.body == "x" * 100 + " [truncated]"


def test_redirects_are_recorded_not_followed() -> None:
    client = _client(
        lambda request: httpx.Response(302, headers={"Location": "http://elsewhere.test/"})
    )

    assert executor.send(_prepare(), client=client, max_response_bytes=10).status_code == 302


def test_a_timeout_is_an_error_not_an_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    result = executor.send(_prepare(), client=_client(handler), max_response_bytes=10)

    assert result.status_code is None
    assert "timeout" in result.error
    assert result.connection_failed is False


def test_a_refused_connection_is_flagged() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    result = executor.send(_prepare(), client=_client(handler), max_response_bytes=10)

    assert result.status_code is None
    assert result.connection_failed is True
    assert "Connection refused" in result.error


def test_a_header_that_cannot_be_encoded_is_an_error() -> None:
    client = _client(lambda request: httpx.Response(200))
    prepared = _prepare(input_data={"headers": {"X-Name": "café ☃"}})

    result = executor.send(prepared, client=client, max_response_bytes=10)

    assert result.status_code is None
    assert "could not be built" in result.error


# --- Redaction ----------------------------------------------------------------


def test_credentials_are_redacted() -> None:
    redacted = executor.redact_headers(
        {
            "Authorization": "Bearer abc",
            "X-API-Key": "k",
            "Cookie": "session=1",
            "X-Auth-Token": "t",
            "Accept": "application/json",
        }
    )

    assert redacted == {
        "Authorization": executor.REDACTED,
        "X-API-Key": executor.REDACTED,
        "Cookie": executor.REDACTED,
        "X-Auth-Token": executor.REDACTED,
        "Accept": "application/json",
    }
