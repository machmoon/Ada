"""Model access, behind a seam.

Every agent in this package talks to a :class:`Model`, not to a vendor SDK.
That is deliberate: it lets the whole pipeline be exercised offline against
recorded fixtures, which is what makes the agents testable at all. The engine
proper stays model-free; this package is the only place a network call lives.
"""

from __future__ import annotations

import json
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
]

#: Gemini's agentic-workflow model. Chosen for native PDF vision: a datasheet's
#: pinout table and package drawing are *pictures*, and text extraction throws
#: away exactly the information this pipeline needs.
DEFAULT_MODEL = "gemini-3.7-flash"

#: Cheaper tier for high-volume mechanical passes.
CHEAP_MODEL = "gemini-3.5-flash-lite"


class ModelError(RuntimeError):
    """The model call failed or returned something unusable."""


@dataclass(frozen=True)
class Document:
    """A PDF to put in front of the model.

    Either a ``url`` the provider fetches itself, or raw ``data``. Gemini reads
    up to 1000 pages / 50MB per document at roughly 258 tokens a page.
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
    ):
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - import guard
            raise ModelError(
                "google-genai is not installed. pip install google-genai"
            ) from exc

        key = api_key or os.getenv("GOOGLE_API_KEY")
        if not key:
            raise ModelError(
                "GOOGLE_API_KEY is not set. Copy .env.example to .env and fill it in."
            )
        self._genai = genai
        self._client = genai.Client(api_key=key)
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
