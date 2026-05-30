"""Stubbed vendor SDK shape (a common chat-completions surface).

Drop-in replacement for a typical vendor SDK with
``client.chat.completions.create(...)`` — the shape many inference APIs
share. Reproduces a documented capability-mismatch failure mode: certain
models return a 400 with the message::

    BadRequestError: Grammar must have a 'properties' field

when the caller passes ``response_format={"type": "json_schema", ...}`` even
though the docs imply json_schema is broadly supported. The wording mirrors
the class of real-world inference-API errors this stub stands in for.

The stub is deliberately minimal: just enough to make the demo script read
like real customer code following a vendor-published Skill. No network, no
auth, no streaming.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class BadRequestError(Exception):
    """Mirrors the typical vendor ``BadRequestError`` — 400 from the API."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# A small allow-list of models that we'll pretend support json_schema. The
# real vendor's capability matrix is larger and shifts over time; for the
# demo we only need to distinguish "supports it" from "small model doesn't."
_JSON_SCHEMA_SUPPORTED_MODELS = frozenset(
    {
        "vendor/llm-8b-instruct",
        "vendor/llm-70b-instruct",
    }
)


@dataclass
class _Message:
    role: str
    content: str


@dataclass
class _Choice:
    index: int
    message: _Message
    finish_reason: str = "stop"


@dataclass
class _Usage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass
class ChatCompletionResponse:
    """Synthetic vendor chat completion response (OpenAI-compatible shape)."""

    id: str
    model: str
    choices: list[_Choice] = field(default_factory=list)
    usage: _Usage | None = None

    def model_dump(self) -> dict[str, Any]:
        """Match pydantic v2 ``.model_dump()`` so caller code reads cleanly."""
        return {
            "id": self.id,
            "model": self.model,
            "choices": [
                {
                    "index": c.index,
                    "message": {"role": c.message.role, "content": c.message.content},
                    "finish_reason": c.finish_reason,
                }
                for c in self.choices
            ],
            "usage": (
                {
                    "prompt_tokens": self.usage.prompt_tokens,
                    "completion_tokens": self.usage.completion_tokens,
                    "total_tokens": self.usage.total_tokens,
                }
                if self.usage
                else None
            ),
        }


class _Completions:
    def create(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_format: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> ChatCompletionResponse:
        # Reproduce the documented failure: json_schema on a model that
        # doesn't support it returns 400 with the cryptic grammar error.
        if response_format is not None and response_format.get("type") == "json_schema":
            if model not in _JSON_SCHEMA_SUPPORTED_MODELS:
                raise BadRequestError(
                    "Grammar must have a 'properties' field",
                    status_code=400,
                )

        # Otherwise — return a plausible response. Echo back something
        # vaguely related to the last user message so the demo output is readable.
        last_user_content = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        synthetic_reply = (
            "Based on your message: '" + last_user_content[:60] + "…' "
            "— here's a synthetic reply from the demo stub."
        )
        return ChatCompletionResponse(
            id="chatcmpl-demo-stub-0001",
            model=model,
            choices=[
                _Choice(index=0, message=_Message(role="assistant", content=synthetic_reply))
            ],
            usage=_Usage(prompt_tokens=42, completion_tokens=24, total_tokens=66),
        )


class _Chat:
    def __init__(self) -> None:
        self.completions = _Completions()


class VendorClient:
    """Demo stub of a typical vendor SDK client. Real customers would
    ``from <vendor> import <Client>`` instead — this file is the only swap.
    """

    def __init__(self, api_key: str | None = None) -> None:
        # Real clients typically read ``<VENDOR>_API_KEY`` from env when api_key=None.
        # Stub ignores it; demo doesn't need auth.
        self.api_key = api_key
        self.chat = _Chat()
