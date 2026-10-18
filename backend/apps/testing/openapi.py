"""Reading OpenAPI 3.x and Swagger 2.0 documents.

This module is pure: it takes a already-decoded JSON document and returns plain
dataclasses. It touches no database and, deliberately, no network — a `$ref`
pointing at an external URL is left unresolved rather than fetched, because
importing a specification must never make the server issue outbound requests on
a caller's behalf.

Parsing is lenient about the parts of a document that do not describe an
operation and strict about the parts that do. A single unusable operation is
skipped and reported; only a document that cannot describe an API at all is
rejected outright.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from apps.testing.models import Endpoint, HTTPMethod, SpecificationSourceType

# Keys a path item may hold that describe the path rather than an operation.
_PATH_ITEM_METADATA_KEYS = frozenset({"summary", "description", "servers", "parameters", "$ref"})

# Methods AegisTest can execute, keyed by their lowercase spelling in a document.
_SUPPORTED_METHODS = {method.value.lower(): method.value for method in HTTPMethod}

# TRACE is a valid OpenAPI operation but AegisTest has no way to execute it, so
# it is reported as skipped rather than silently dropped.
_UNSUPPORTED_METHODS = {"trace": "TRACE"}

# Guards a pathological or deeply self-referential document from exhausting the
# stack during $ref resolution.
_MAX_REF_DEPTH = 100

_PATH_MAX_LENGTH = Endpoint._meta.get_field("path").max_length
_OPERATION_ID_MAX_LENGTH = Endpoint._meta.get_field("operation_id").max_length
_SUMMARY_MAX_LENGTH = Endpoint._meta.get_field("summary").max_length


class SpecificationParseError(Exception):
    """The document cannot be read as an API description at all.

    Carries every problem found rather than only the first, so a caller fixing
    a specification sees the full list in one pass.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass(frozen=True)
class ParsedOperation:
    """One executable operation, in the shape the Endpoint model stores."""

    path: str
    method: str
    operation_id: str = ""
    summary: str = ""
    description: str = ""
    parameters: list[dict[str, Any]] = field(default_factory=list)
    request_schema: dict[str, Any] | None = None
    response_schema: dict[str, Any] | None = None
    # The sections the document files the operation under, in its order.
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SkippedOperation:
    """Something that looked like an operation but cannot be imported."""

    path: str
    method: str
    reason: str


@dataclass(frozen=True)
class ParsedDocument:
    source_type: str
    title: str = ""
    api_version: str = ""
    operations: list[ParsedOperation] = field(default_factory=list)
    skipped: list[SkippedOperation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse(document: Any) -> ParsedDocument:
    """Read a specification document, or raise SpecificationParseError."""
    source_type = _detect_source_type(document)
    paths = _validated_paths(document)

    info = document.get("info") if isinstance(document.get("info"), dict) else {}
    warnings: list[str] = []
    operations: list[ParsedOperation] = []
    skipped: list[SkippedOperation] = []

    for raw_path, path_item in paths.items():
        _parse_path_item(
            raw_path=raw_path,
            path_item=path_item,
            document=document,
            source_type=source_type,
            operations=operations,
            skipped=skipped,
            warnings=warnings,
        )

    return ParsedDocument(
        source_type=source_type,
        title=str(info.get("title") or ""),
        api_version=str(info.get("version") or ""),
        operations=operations,
        skipped=skipped,
        warnings=warnings,
    )


def suggested_base_url(document: Any) -> str:
    """Where the document says its API is served, or "" when it does not say.

    OpenAPI 3 lists ``servers``; Swagger 2 splits the URL into ``schemes``,
    ``host`` and ``basePath``. A relative server URL or one with template
    variables (``https://{region}.example.com``) cannot be used as it stands,
    so it is not suggested.
    """
    if not isinstance(document, dict):
        return ""
    servers = document.get("servers")
    if isinstance(servers, list) and servers:
        first = servers[0]
        url = first.get("url") if isinstance(first, dict) else None
        if isinstance(url, str) and url.startswith(("http://", "https://")) and "{" not in url:
            return url.rstrip("/")
        return ""
    host = document.get("host")
    if isinstance(host, str) and host and "{" not in host:
        schemes = document.get("schemes")
        scheme = "https"
        if isinstance(schemes, list) and schemes and "https" not in schemes:
            scheme = str(schemes[0])
        base_path = document.get("basePath")
        base_path = base_path if isinstance(base_path, str) else ""
        return f"{scheme}://{host}{base_path}".rstrip("/")
    return ""


def documented_status_codes(document: Any, path: str, method: str) -> list[str]:
    """The response codes a document declares for one operation.

    ``parse`` keeps only the success *schema*, because that is what an Endpoint
    stores; the codes themselves are needed later, when deciding what a test
    case should expect. Reading them back out of the stored document keeps that
    decision out of the import and off the model.

    Returns an empty list for anything it cannot find, including a document that
    would not parse: an absent expectation is the correct answer here, and an
    exception would turn "this API documents nothing" into a failure.
    """
    if not isinstance(document, dict):
        return []

    paths = document.get("paths")
    if not isinstance(paths, dict):
        return []

    path_item = paths.get(path)
    if not isinstance(path_item, dict):
        return []

    operation = path_item.get(method.lower())
    if not isinstance(operation, dict):
        operation = path_item.get(method.upper())
    if not isinstance(operation, dict):
        return []

    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return []

    return [code for code in responses if isinstance(code, str)]


# --- Document-level validation ------------------------------------------------


def _detect_source_type(document: Any) -> str:
    """Decide which dialect the document is written in."""
    if not isinstance(document, dict):
        raise SpecificationParseError(
            [f"Specification must be a JSON object, got {type(document).__name__}."]
        )
    if not document:
        raise SpecificationParseError(["Specification is empty."])

    openapi_version = document.get("openapi")
    swagger_version = document.get("swagger")

    if isinstance(openapi_version, str) and openapi_version.strip():
        major = openapi_version.split(".", 1)[0]
        if major != "3":
            raise SpecificationParseError(
                [f"Unsupported OpenAPI version '{openapi_version}'; expected 3.x."]
            )
        return SpecificationSourceType.OPENAPI

    if isinstance(swagger_version, str) and swagger_version.strip():
        if not swagger_version.startswith("2"):
            raise SpecificationParseError(
                [f"Unsupported Swagger version '{swagger_version}'; expected 2.0."]
            )
        return SpecificationSourceType.SWAGGER

    raise SpecificationParseError(
        ["Specification must declare a version: an 'openapi' (3.x) or 'swagger' (2.0) key."]
    )


def _validated_paths(document: dict[str, Any]) -> dict[str, Any]:
    if "paths" not in document:
        raise SpecificationParseError(["Specification has no 'paths' object."])

    paths = document["paths"]
    if not isinstance(paths, dict):
        raise SpecificationParseError(
            [f"'paths' must be a JSON object, got {type(paths).__name__}."]
        )
    return paths


# --- Path items and operations ------------------------------------------------


def _parse_path_item(
    *,
    raw_path: str,
    path_item: Any,
    document: dict[str, Any],
    source_type: str,
    operations: list[ParsedOperation],
    skipped: list[SkippedOperation],
    warnings: list[str],
) -> None:
    if not isinstance(path_item, dict):
        skipped.append(
            SkippedOperation(
                path=str(raw_path),
                method="",
                reason=f"Path item must be an object, got {type(path_item).__name__}.",
            )
        )
        return

    path_errors = _path_problems(raw_path)
    if path_errors:
        skipped.append(SkippedOperation(path=str(raw_path), method="", reason=path_errors))
        return

    # Parameters declared on the path apply to every operation under it.
    shared_parameters = _resolve(path_item.get("parameters", []), document, warnings)
    if not isinstance(shared_parameters, list):
        shared_parameters = []

    for key, operation in path_item.items():
        method_key = key.lower() if isinstance(key, str) else ""

        if method_key in _PATH_ITEM_METADATA_KEYS or method_key.startswith("x-"):
            continue

        if method_key in _UNSUPPORTED_METHODS:
            skipped.append(
                SkippedOperation(
                    path=raw_path,
                    method=_UNSUPPORTED_METHODS[method_key],
                    reason="AegisTest cannot execute this method.",
                )
            )
            continue

        if method_key not in _SUPPORTED_METHODS:
            continue  # Not an operation; vendor extension or unknown key.

        method = _SUPPORTED_METHODS[method_key]

        if not isinstance(operation, dict):
            skipped.append(
                SkippedOperation(
                    path=raw_path,
                    method=method,
                    reason=f"Operation must be an object, got {type(operation).__name__}.",
                )
            )
            continue

        operations.append(
            _parse_operation(
                path=raw_path,
                method=method,
                operation=operation,
                shared_parameters=shared_parameters,
                document=document,
                source_type=source_type,
                warnings=warnings,
            )
        )


def _path_problems(raw_path: Any) -> str:
    if not isinstance(raw_path, str) or not raw_path:
        return "Path must be a non-empty string."
    if not raw_path.startswith("/"):
        return "Path must start with '/'."
    if len(raw_path) > _PATH_MAX_LENGTH:
        # The path is the endpoint's identity, so it cannot be truncated.
        return f"Path exceeds {_PATH_MAX_LENGTH} characters."
    return ""


def _parse_operation(
    *,
    path: str,
    method: str,
    operation: dict[str, Any],
    shared_parameters: list[Any],
    document: dict[str, Any],
    source_type: str,
    warnings: list[str],
) -> ParsedOperation:
    parameters = _merge_parameters(
        shared_parameters, _resolve(operation.get("parameters", []), document, warnings)
    )

    return ParsedOperation(
        path=path,
        method=method,
        operation_id=_bounded(
            operation.get("operationId"),
            _OPERATION_ID_MAX_LENGTH,
            label="operationId",
            path=path,
            method=method,
            warnings=warnings,
        ),
        summary=_bounded(
            operation.get("summary"),
            _SUMMARY_MAX_LENGTH,
            label="summary",
            path=path,
            method=method,
            warnings=warnings,
        ),
        description=str(operation.get("description") or ""),
        parameters=parameters,
        request_schema=_request_schema(operation, parameters, document, source_type, warnings),
        response_schema=_response_schema(operation, document, source_type, warnings),
        tags=operation_tags(operation),
    )


# A tag is a section name; anything longer is not one worth showing whole.
_TAG_MAX_LENGTH = 100


def operation_tags(operation: Any) -> list[str]:
    """An operation's tags: non-empty strings, trimmed, each once, in order."""
    raw = operation.get("tags") if isinstance(operation, dict) else None
    if not isinstance(raw, list):
        return []
    tags: list[str] = []
    for tag in raw:
        if isinstance(tag, str) and tag.strip() and tag.strip()[:_TAG_MAX_LENGTH] not in tags:
            tags.append(tag.strip()[:_TAG_MAX_LENGTH])
    return tags


def _merge_parameters(shared: list[Any], own: Any) -> list[dict[str, Any]]:
    """Combine path-level and operation-level parameters.

    A parameter is identified by its name and location, and the operation's own
    declaration wins over the one inherited from the path.
    """
    if not isinstance(own, list):
        own = []

    merged: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []

    for parameter in [*shared, *own]:
        if not isinstance(parameter, dict):
            continue
        key = (str(parameter.get("name", "")), str(parameter.get("in", "")))
        if key not in merged:
            order.append(key)
        merged[key] = parameter

    return [merged[key] for key in order]


def _request_schema(
    operation: dict[str, Any],
    parameters: list[dict[str, Any]],
    document: dict[str, Any],
    source_type: str,
    warnings: list[str],
) -> dict[str, Any] | None:
    if source_type == SpecificationSourceType.SWAGGER:
        # Swagger 2.0 models the request body as a parameter with in: body.
        for parameter in parameters:
            if parameter.get("in") == "body":
                schema = parameter.get("schema")
                return schema if isinstance(schema, dict) else None
        return None

    request_body = _resolve(operation.get("requestBody"), document, warnings)
    if not isinstance(request_body, dict):
        return None
    return _json_schema_from_content(request_body.get("content"))


def _response_schema(
    operation: dict[str, Any],
    document: dict[str, Any],
    source_type: str,
    warnings: list[str],
) -> dict[str, Any] | None:
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return None

    response = _resolve(_select_success_response(responses), document, warnings)
    if not isinstance(response, dict):
        return None

    if source_type == SpecificationSourceType.SWAGGER:
        schema = response.get("schema")
        return schema if isinstance(schema, dict) else None

    return _json_schema_from_content(response.get("content"))


def _select_success_response(responses: dict[str, Any]) -> Any:
    """Pick the response that describes success: the lowest 2xx, else default."""
    codes = [
        key
        for key in responses
        if isinstance(key, str) and key.isdigit() and 200 <= int(key) < 300
    ]
    if codes:
        return responses[min(codes, key=int)]
    return responses.get("default")


def _json_schema_from_content(content: Any) -> dict[str, Any] | None:
    """Pull the JSON schema out of an already-resolved OpenAPI 3 content map.

    JSON is preferred because it is what AegisTest can generate and assert
    against; any other single media type is accepted as a fallback so that a
    schema is not lost entirely.

    This does not resolve references: its caller has already resolved the whole
    enclosing object. Resolving again would walk one further step into a
    self-referential schema on every pass.
    """
    if not isinstance(content, dict) or not content:
        return None

    media_type = _preferred_media_type(content)
    media_object = content.get(media_type)
    if not isinstance(media_object, dict):
        return None

    schema = media_object.get("schema")
    return schema if isinstance(schema, dict) else None


def _preferred_media_type(content: dict[str, Any]) -> str:
    keys = [key for key in content if isinstance(key, str)]
    if "application/json" in keys:
        return "application/json"
    # "application/json; charset=utf-8" and friends.
    for key in sorted(keys):
        if key.split(";", 1)[0].strip() == "application/json":
            return key
    for key in sorted(keys):
        if key.split(";", 1)[0].strip().endswith("+json"):
            return key
    return sorted(keys)[0] if keys else ""


def _bounded(
    value: Any, limit: int, *, label: str, path: str, method: str, warnings: list[str]
) -> str:
    text = str(value or "")
    if len(text) > limit:
        warnings.append(f"{method} {path}: {label} truncated to {limit} characters.")
        return text[:limit]
    return text


# --- $ref resolution ----------------------------------------------------------


def _resolve(
    node: Any,
    document: dict[str, Any],
    warnings: list[str],
    *,
    seen: frozenset[str] = frozenset(),
    depth: int = 0,
) -> Any:
    """Inline local `$ref` pointers so stored schemas are self-contained.

    A reference that is circular, external or dangling is left exactly as it is:
    the document still says something true, and a caller can see why it was not
    inlined. Resolving an external reference would mean fetching a URL, which
    this parser never does.
    """
    if depth > _MAX_REF_DEPTH:
        return node

    if isinstance(node, list):
        return [_resolve(item, document, warnings, seen=seen, depth=depth + 1) for item in node]

    if not isinstance(node, dict):
        return node

    reference = node.get("$ref")
    if not isinstance(reference, str):
        return {
            key: _resolve(value, document, warnings, seen=seen, depth=depth + 1)
            for key, value in node.items()
        }

    if not reference.startswith("#/"):
        warnings.append(f"External reference '{reference}' was not resolved.")
        return node

    if reference in seen:
        return node  # Circular: the reference itself is the honest answer.

    target = _lookup(document, reference)
    if target is None:
        warnings.append(f"Reference '{reference}' could not be resolved.")
        return node

    resolved = _resolve(
        target, document, warnings, seen=seen | {reference}, depth=depth + 1
    )

    # OpenAPI 3.1 allows keywords alongside $ref; they override the target.
    siblings = {key: value for key, value in node.items() if key != "$ref"}
    if siblings and isinstance(resolved, dict):
        return {
            **resolved,
            **_resolve(siblings, document, warnings, seen=seen, depth=depth + 1),
        }
    return resolved


def _lookup(document: dict[str, Any], reference: str) -> Any:
    """Follow a local JSON pointer such as '#/components/schemas/User'."""
    current: Any = document
    for token in reference[2:].split("/"):
        if not token:
            continue
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            if not token.isdigit() or int(token) >= len(current):
                return None
            current = current[int(token)]
        elif isinstance(current, dict):
            if token not in current:
                return None
            current = current[token]
        else:
            return None
    return current
