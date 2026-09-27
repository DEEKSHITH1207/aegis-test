"""Sending a test case's request to the API under test.

This is the only module in AegisTest that talks to the network. It is kept
free of the database in the same way ``openapi.py`` and ``generator.py`` are:
it turns a case's inputs into a request, sends it, and returns what came back
as plain dataclasses. Deciding what the response *means* is ``evaluation.py``'s
job; recording it is the service's.

Three choices here are about honest evidence rather than convenience:

* Redirects are not followed. A 302 in answer to an invalid body is itself the
  observation; following it would record the status of some other page.
* A response body is read only up to a fixed size, so a target that streams
  gigabytes cannot exhaust the worker's memory.
* Credentials are redacted before anything is stored. A run's Authorization
  header must not end up readable in every request record it produced.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

REDACTED = "[REDACTED]"

# Header names that carry credentials. Matched as substrings of the lowercased
# name, so X-Api-Key, X-Auth-Token and Proxy-Authorization are all covered.
_SENSITIVE_HEADER_MARKERS = ("auth", "cookie", "token", "secret", "api-key", "apikey", "password")

_PATH_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")

# How much of a non-JSON body is kept. Enough for an HTML error page's
# message, not so much that one response dominates the database.
_TEXT_BODY_MAX_CHARS = 10_000


@dataclass(frozen=True)
class PreparedRequest:
    """Everything needed to send one request, and to record what was sent."""

    method: str
    url: str
    headers: dict[str, str]
    query_params: dict[str, str | list[str]]
    path_params: dict[str, Any]
    # None means no body is sent at all.
    body: Any = None


@dataclass(frozen=True)
class ExecutionResult:
    """What came back. ``status_code`` is None when nothing did."""

    status_code: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    response_time_ms: int = 0
    error: str = ""
    # True when the target could not be reached at all, as opposed to a
    # request that timed out or was malformed. Used to stop a run early when
    # the base URL is simply wrong.
    connection_failed: bool = False


# --- Target validation --------------------------------------------------------


def base_url_problem(base_url: str, allowed_hosts: list[str]) -> str:
    """Why a base URL cannot be used, or "" when it can.

    ``allowed_hosts`` restricts which hosts a run may call. Empty means any
    host: AegisTest exists to call APIs its users name. A deployment reachable
    by people who should not be able to make it issue requests into its own
    network sets the list.
    """
    if not base_url:
        return "A base URL is required to execute a run."

    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"}:
        return "The base URL must start with http:// or https://."
    if not parts.hostname:
        return "The base URL must include a host."
    if parts.username or parts.password:
        return "Put credentials in the run's headers, not in the base URL."
    if parts.query or parts.fragment:
        return "The base URL must not contain a query string or fragment."
    if allowed_hosts and not _host_allowed(parts.hostname, allowed_hosts):
        return f"The host '{parts.hostname}' is not in EXECUTOR_ALLOWED_HOSTS."
    return ""


def _host_allowed(host: str, allowed_hosts: list[str]) -> bool:
    """Exact match, or a subdomain of an entry written with a leading dot."""
    host = host.lower().rstrip(".")
    for entry in allowed_hosts:
        entry = entry.lower().strip()
        if entry.startswith("."):
            if host == entry[1:] or host.endswith(entry):
                return True
        elif host == entry:
            return True
    return False


# --- Building a request -------------------------------------------------------


def prepare(
    *,
    method: str,
    path: str,
    input_data: dict[str, Any],
    base_url: str,
    run_headers: dict[str, str] | None = None,
) -> PreparedRequest:
    """Turn a test case's ``input_data`` into a concrete request.

    The case's own headers win over the run's: a case that deliberately sends
    a different Authorization header must actually send it.
    """
    path_params = _mapping(input_data.get("path"))
    query = _mapping(input_data.get("query"))
    case_headers = _mapping(input_data.get("headers"))
    body = input_data.get("body")

    headers = {"Accept": "application/json"}
    headers.update({str(name): _as_text(value) for name, value in (run_headers or {}).items()})
    headers.update({str(name): _as_text(value) for name, value in case_headers.items()})
    if body is not None and not _has_header(headers, "content-type"):
        headers["Content-Type"] = "application/json"

    return PreparedRequest(
        method=method.upper(),
        url=base_url.rstrip("/") + _fill_path(path, path_params),
        headers=headers,
        query_params={str(name): _query_value(value) for name, value in query.items()},
        path_params=path_params,
        body=body,
    )


def _fill_path(path: str, values: dict[str, Any]) -> str:
    """Substitute path parameters, percent-encoding each value whole.

    A value is encoded with no safe characters, so a slash inside it cannot
    turn /users/{id} into a different route. A placeholder with no value is
    left as written: the request then shows plainly what was missing.
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            return match.group(0)
        return quote(_as_text(values[name]), safe="")

    return _PATH_PLACEHOLDER.sub(replace, path)


def _query_value(value: Any) -> str | list[str]:
    # An array parameter is sent as a repeated key (?tag=a&tag=b), the
    # OpenAPI default ("form" style, exploded).
    if isinstance(value, list):
        return [_as_text(item) for item in value]
    return _as_text(value)


def _as_text(value: Any) -> str:
    """Render a JSON value the way it would appear in a URL or header.

    Booleans are written JSON-style — Python's "True" is not what any API
    expects — and null becomes an empty value, the closest a query string or a
    header can come to one.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), sort_keys=True)
    return str(value)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _has_header(headers: dict[str, str], name: str) -> bool:
    return any(key.lower() == name for key in headers)


# --- Sending ------------------------------------------------------------------


def make_client(*, timeout: float, verify_tls: bool = True) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=False, verify=verify_tls)


def send(
    request: PreparedRequest, *, client: httpx.Client, max_response_bytes: int
) -> ExecutionResult:
    """Send a prepared request and capture the response, or why there was none.

    Never raises for anything the target does: a timeout, a refused connection
    or a header the HTTP library will not encode all come back as an
    ExecutionResult with ``error`` set, so one bad case cannot end a run.
    """
    content = None
    if request.body is not None:
        content = json.dumps(request.body).encode("utf-8")

    started = time.perf_counter()
    try:
        with client.stream(
            request.method,
            request.url,
            params=request.query_params,
            headers=request.headers,
            content=content,
        ) as response:
            raw, truncated = _read_bounded(response, max_response_bytes)
            elapsed = _elapsed_ms(started)
            return ExecutionResult(
                status_code=response.status_code,
                headers=dict(response.headers),
                body=_decode_body(raw, truncated=truncated, encoding=response.encoding),
                response_time_ms=elapsed,
            )
    except httpx.TimeoutException as exc:
        return ExecutionResult(
            response_time_ms=_elapsed_ms(started),
            error=f"No response within the timeout ({type(exc).__name__}).",
        )
    except httpx.ConnectError as exc:
        return ExecutionResult(
            response_time_ms=_elapsed_ms(started),
            error=f"Could not connect to the target: {exc}",
            connection_failed=True,
        )
    except httpx.HTTPError as exc:
        return ExecutionResult(
            response_time_ms=_elapsed_ms(started),
            error=f"The request failed: {type(exc).__name__}: {exc}",
        )
    except (UnicodeEncodeError, ValueError, TypeError) as exc:
        # httpx refuses, for example, a header value it cannot encode. The
        # request was never sent, so this is not an observation about the API.
        return ExecutionResult(error=f"The request could not be built: {exc}")


def _read_bounded(response: httpx.Response, limit: int) -> tuple[bytes, bool]:
    """Read at most ``limit`` bytes of the body. Returns (bytes, truncated)."""
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            return b"".join(chunks)[:limit], True
    return b"".join(chunks), False


def _decode_body(raw: bytes, *, truncated: bool, encoding: str | None) -> Any:
    if not raw:
        return None
    text = raw.decode(encoding or "utf-8", errors="replace")
    if not truncated:
        try:
            return json.loads(text)
        except ValueError:
            pass
    if len(text) > _TEXT_BODY_MAX_CHARS:
        text = text[:_TEXT_BODY_MAX_CHARS]
        truncated = True
    return text + " [truncated]" if truncated else text


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


# --- Redaction ----------------------------------------------------------------


def redact_headers(headers: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``headers`` with credential values replaced by a placeholder."""
    return {
        name: REDACTED if _is_sensitive(name) else value for name, value in headers.items()
    }


def _is_sensitive(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in _SENSITIVE_HEADER_MARKERS)
