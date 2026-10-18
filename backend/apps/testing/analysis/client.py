"""The one module that talks to the model provider.

``Analyst.ask`` sends one prompt and returns the reply's text, which the API
has already constrained to the given JSON schema. It raises ``AnalystError``
for every way a request can fail to produce a usable reply — an API error, a
refusal, a reply cut off at the token limit — with a message fit to show on
the analysis, so the service handles one exception type.

The SDK retries rate limits, overload and 5xx responses itself
(``LLM_MAX_RETRIES``); an error that reaches this module has already been
retried.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import anthropic
from django.conf import settings

from apps.testing.analysis.prompts import SYSTEM_PROMPT

# Fallbacks re-run a request that a model's safety classifiers declined on the
# model Anthropic recommends for that kind of refusal, inside the same call.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Room for adaptive thinking plus a batch of assessments. Streaming is used,
# so a large ceiling does not risk an HTTP timeout.
_MAX_TOKENS = 32_000

_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


class AnalystError(Exception):
    """A request produced no usable reply. The message says why."""


@dataclass(frozen=True)
class Reply:
    text: str
    model: str
    request_id: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


class Analyst:
    def __init__(
        self,
        *,
        client: Any,
        model: str,
        effort: str = "",
        refusal_fallback: bool = True,
        max_tokens: int = _MAX_TOKENS,
    ) -> None:
        self.client = client
        self.model = model
        self.effort = effort
        self.refusal_fallback = refusal_fallback
        self.max_tokens = max_tokens

    @classmethod
    def from_settings(cls) -> Analyst:
        # Credentials come from the environment: ANTHROPIC_API_KEY, or any
        # other source the SDK resolves. None are held in settings.
        client = anthropic.Anthropic(
            timeout=settings.LLM_TIMEOUT_SECONDS, max_retries=settings.LLM_MAX_RETRIES
        )
        return cls(
            client=client,
            model=settings.LLM_MODEL,
            effort=settings.LLM_EFFORT,
            refusal_fallback=settings.LLM_REFUSAL_FALLBACK,
        )

    def ask(self, *, prompt: str, schema: dict[str, Any]) -> Reply:
        output_config: dict[str, Any] = {"format": {"type": "json_schema", "schema": schema}}
        if self.effort:
            output_config["effort"] = self.effort
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
            "thinking": {"type": "adaptive"},
            "output_config": output_config,
        }
        if self.refusal_fallback:
            request["betas"] = [_FALLBACK_BETA]
            request["fallbacks"] = "default"

        try:
            with self.client.beta.messages.stream(**request) as stream:
                message = stream.get_final_message()
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise AnalystError(
                f"The model provider rejected the credentials: {_message(exc)}. "
                f"Check ANTHROPIC_API_KEY."
            ) from exc
        except anthropic.RateLimitError as exc:
            raise AnalystError(f"Still rate limited after retrying: {_message(exc)}") from exc
        except anthropic.BadRequestError as exc:
            raise AnalystError(f"The model provider refused the request: {_message(exc)}") from exc
        except anthropic.APIStatusError as exc:
            raise AnalystError(
                f"The model provider answered {exc.status_code}: {_message(exc)}"
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise AnalystError(f"Could not reach the model provider: {exc}") from exc
        except anthropic.AnthropicError as exc:
            # Missing credentials and other client-side failures.
            raise AnalystError(f"The model could not be called: {exc}") from exc

        return _reply(message)


def _reply(message: Any) -> Reply:
    if message.stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None)
        raise AnalystError(
            "The model declined to analyse these findings"
            + (f" (category: {category})." if category else ".")
        )
    if message.stop_reason == "max_tokens":
        raise AnalystError(
            "The reply was cut off at the token limit. Lower LLM_FINDINGS_PER_REQUEST."
        )
    text = "".join(block.text for block in message.content if block.type == "text")
    if not text.strip():
        raise AnalystError(f"The reply held no text (stop reason: {message.stop_reason}).")

    usage = {
        name: getattr(message.usage, name, None) or 0 for name in _USAGE_FIELDS
    }
    return Reply(
        text=text,
        model=message.model,
        request_id=getattr(message, "_request_id", None),
        usage=usage,
    )


def _message(exc: anthropic.APIStatusError) -> str:
    return getattr(exc, "message", None) or str(exc)
