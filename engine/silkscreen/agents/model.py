"""Model access, behind a seam.

Every agent in this package talks to a :class:`Model`, not to a vendor SDK.
That is deliberate: it lets the whole pipeline be exercised offline against
recorded fixtures, which is what makes the agents testable at all. The engine
proper stays model-free; this package is the only place a network call lives.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "Model",
    "GeminiModel",
    "ScriptedModel",
    "ModelError",
    "Document",
    "strip_code_fence",
    "default_model",
    "DEFAULT_TIMEOUT_S",
    "TIMEOUT_ENV_VAR",
    "request_timeout_ms",
]

#: Gemini's agentic-workflow model. Chosen for native PDF vision: a datasheet's
#: pinout table and package drawing are *pictures*, and text extraction throws
#: away exactly the information this pipeline needs.
DEFAULT_MODEL = "gemini-3.7-flash"

#: Cheaper tier for high-volume mechanical passes.
CHEAP_MODEL = "gemini-3.5-flash-lite"


#: Per-request deadline, in seconds. The SDK sets no timeout of its own, so
#: without this a degraded model that accepts the connection and never answers
#: hangs the caller indefinitely (observed 2026-08-30: a bare-model CLI run sat
#: 686 s with no output while the primary was 504-degraded). 60 s is generous
#: for a datasheet-reading call with document parts, yet small enough that two
#: failed primary attempts plus failover complete in about a minute.
DEFAULT_TIMEOUT_S = 60.0

#: Environment override for the deadline, in seconds.
TIMEOUT_ENV_VAR = "SILKSCREEN_MODEL_TIMEOUT_S"


class ModelError(RuntimeError):
    """The model call failed or returned something unusable."""


def request_timeout_ms(timeout_s: float | None = None) -> int:
    """Resolve the per-request deadline into the SDK's unit, milliseconds.

    An explicit ``timeout_s`` wins, then :data:`TIMEOUT_ENV_VAR`, then
    :data:`DEFAULT_TIMEOUT_S`. The result feeds ``HttpOptions.timeout``, which
    google-genai defines in *milliseconds* (it converts to seconds itself
    before handing httpx the request) -- the seconds-to-ms conversion lives
    here and nowhere else, the same single-owner rule the engine applies to
    coordinate flips.

    A malformed or non-positive value raises :class:`ModelError` instead of
    silently falling back: the failure this deadline guards against is a
    silent hang, so a config typo that silently removed the deadline would
    recreate the bug while looking fixed.
    """
    if timeout_s is None:
        raw = os.getenv(TIMEOUT_ENV_VAR)
        if raw is None or not raw.strip():
            timeout_s = DEFAULT_TIMEOUT_S
        else:
            try:
                timeout_s = float(raw)
            except ValueError as exc:
                raise ModelError(
                    f"{TIMEOUT_ENV_VAR}={raw!r} is not a number of seconds"
                ) from exc
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ModelError(
            f"model request timeout must be a positive number of seconds, "
            f"got {timeout_s!r}"
        )
    return max(1, round(timeout_s * 1000))


@dataclass(frozen=True)
class Document:
    """A PDF to put in front of the model.

    Prefer ``data``. Gemini reads up to 1000 pages / 50MB per document at
    roughly 258 tokens a page, and inline bytes are the transport every caller
    in this repo uses -- :func:`agents.datasheet.read_datasheet` downloads a
    URL and passes what it got.

    ``url`` is **not** "a link the provider fetches for you", which is what this
    docstring used to claim and what cost a day of debugging. Gemini's
    ``file_uri`` accepts a Files API URI
    (``generativelanguage.googleapis.com/v1beta/files/...``) or a YouTube link,
    and nothing else. Handed a public datasheet URL it answers
    ``429 RESOURCE_EXHAUSTED`` -- carrying no quota metric and no retry delay,
    so it reads as an exhausted key and defeats every failover attempt.
    Measured against one 2.3MB PDF: the same file costs 48,485 prompt tokens
    inline and 48,483 through a Files API URI, while its own https URL is a 429.
    """

    url: str | None = None
    data: bytes | None = None
    mime_type: str = "application/pdf"

    def __post_init__(self) -> None:
        if not self.url and not self.data:
            raise ValueError("Document needs either a url or data")


class Model(Protocol):
    """Anything that can answer a prompt, optionally about a document."""

    def generate(
        self,
        prompt: str,
        *,
        documents: list[Document] | None = None,
        system: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 8192,
    ) -> str: ...


def strip_code_fence(text: str) -> str:
    """Remove a Markdown fence if the model wrapped its JSON in one."""
    s = text.strip()
    if not s.startswith("```"):
        return s
    lines = s.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_json(text: str) -> Any:
    """Parse model output as JSON, tolerating a fence and surrounding prose."""
    cleaned = strip_code_fence(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost {...} or [...] in the response.
    match = re.search(r"[\{\[].*[\}\]]", cleaned, re.DOTALL)
    if not match:
        raise ModelError(
            f"Response contained no JSON. First 200 chars: {cleaned[:200]!r}"
        )
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ModelError(f"Response was not valid JSON: {exc}") from exc


class GeminiModel:
    """The live path. Requires ``GOOGLE_API_KEY``."""

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        *,
        api_key: str | None = None,
        media_resolution: str = "MEDIA_RESOLUTION_HIGH",
        timeout_s: float | None = None,
    ):
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - import guard
            raise ModelError(
                "google-genai is not installed. pip install google-genai"
            ) from exc

        key = (
            api_key
            or os.getenv("GOOGLE_API_KEY")
            or os.getenv("GEMINI_API_KEY")
        )
        if not key:
            raise ModelError(
                "GOOGLE_API_KEY is not set. Copy .env.example to .env and fill it in."
            )
        # Every request gets a deadline at client construction, so every call
        # path through this client is covered. Without one, a hung upstream
        # never raises, so FallbackModel never gets its chance to fail over.
        timeout_ms = request_timeout_ms(timeout_s)
        self.timeout_s = timeout_ms / 1000.0
        self._genai = genai
        self._client = genai.Client(
            api_key=key, http_options={"timeout": timeout_ms}
        )
        self.model = model
        # Datasheet pin tables are small type; high resolution is the lever
        # that makes them legible, at the cost of more image tokens per page.
        # Use the API enum value, not the display label "high": the latter is
        # serialized verbatim by google-genai and rejected by the API.
        self.media_resolution = media_resolution

    def generate(
        self,
        prompt: str,
        *,
        documents: list[Document] | None = None,
        system: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 8192,
    ) -> str:
        parts: list[Any] = []
        for doc in documents or []:
            if doc.url:
                parts.append(
                    {"file_data": {"file_uri": doc.url, "mime_type": doc.mime_type}}
                )
            else:
                parts.append(
                    {"inline_data": {"data": doc.data, "mime_type": doc.mime_type}}
                )
        parts.append(prompt)

        config: dict[str, Any] = {
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
        }
        if system:
            config["system_instruction"] = system
        if documents:
            config["media_resolution"] = self.media_resolution

        try:
            resp = self._client.models.generate_content(
                model=self.model, contents=parts, config=config
            )
        except Exception as exc:
            raise ModelError(f"{self.model} call failed: {exc}") from exc

        text = getattr(resp, "text", None)
        if not text:
            raise ModelError(f"{self.model} returned an empty response")
        return text


@dataclass
class ScriptedModel:
    """A deterministic stand-in for tests.

    ``responses`` are returned in order; ``by_marker`` matches a substring of the
    prompt, which is how a test drives several different agents through one
    model object. Records every call so a test can assert on what was asked.
    """

    responses: list[str] = field(default_factory=list)
    by_marker: dict[str, str] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def generate(
        self,
        prompt: str,
        *,
        documents: list[Document] | None = None,
        system: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 8192,
    ) -> str:
        self.calls.append(
            {
                "prompt": prompt,
                "documents": list(documents or []),
                "system": system,
            }
        )
        for marker, response in self.by_marker.items():
            if marker in prompt:
                return response
        if self.responses:
            return self.responses.pop(0)
        raise ModelError("ScriptedModel ran out of responses")


def default_model(**kwargs: Any) -> Model:
    """A live Gemini model. Raises :class:`ModelError` without a key."""
    return GeminiModel(**kwargs)
