"""The live Gemini path, exercised for real behind an API-key gate.

Every other test in this suite runs offline against :class:`ScriptedModel`, and
CLAUDE.md promises the full suite needs no keys. This module keeps that promise:
the tests that actually call Gemini are marked ``requires_api_key`` and skip
unless ``GOOGLE_API_KEY`` is set, so a default ``pytest`` run stays offline and
free. The mark is applied per test rather than as a module-level ``pytestmark``
because the no-key construction test below must run in exactly the environment
that has no key.

When the gate is open the live surface is deliberately tiny: one call to the
cheap model asking for one small JSON object. That is enough to catch the
failures a scripted model cannot -- a renamed model id, a changed config key, an
SDK response object that no longer carries ``.text``.
"""

from __future__ import annotations

import os
import sys
from types import ModuleType, SimpleNamespace

import pytest
from silkscreen.agents.model import (
    CHEAP_MODEL,
    Document,
    GeminiModel,
    ModelError,
    strip_code_fence,
)

requires_api_key = pytest.mark.skipif(
    not os.getenv("GOOGLE_API_KEY"),
    reason="live Gemini test: set GOOGLE_API_KEY to run",
)


# ------------------------------------------------------ no key, no network


def test_gemini_model_without_api_key_raises_model_error(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    with pytest.raises(ModelError) as excinfo:
        GeminiModel()

    assert "GOOGLE_API_KEY" in str(excinfo.value)


def test_document_request_uses_valid_high_media_resolution(monkeypatch):
    """Catch request-config errors without making a live Gemini call."""
    captured = {}

    class FakeModels:
        def generate_content(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(text="ok")

    class FakeClient:
        def __init__(self, **_kwargs):
            self.models = FakeModels()

    fake_google = ModuleType("google")
    fake_genai = ModuleType("google.genai")
    fake_genai.Client = FakeClient
    fake_google.genai = fake_genai
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)

    model = GeminiModel(api_key="test-key")
    response = model.generate(
        "read this datasheet",
        documents=[Document(url="https://example.com/part.pdf")],
    )

    assert response == "ok"
    assert captured["config"]["media_resolution"] == "MEDIA_RESOLUTION_HIGH"


# ------------------------------------------------------------- live Gemini

#: A token the model has to echo back. Nonsense on purpose: it cannot be
#: produced by a cached, refused, or truncated answer that happens to look fine.
MARKER = "silkscreen-live-ok"

PROMPT = (
    "Reply with exactly this JSON object and nothing else: "
    f'{{"marker": "{MARKER}"}}'
)


@pytest.fixture(scope="module")
def live_response() -> str:
    """One real call to the cheap model, shared by every live test here.

    Module-scoped so the whole gated suite costs a single request. Only
    ``@requires_api_key`` tests may request it -- an ungated consumer would
    spend a real request on a machine that never asked for one.
    """
    model = GeminiModel(CHEAP_MODEL)
    return model.generate(PROMPT, temperature=0.0, max_output_tokens=512)


@requires_api_key
def test_live_generate_returns_non_empty_text(live_response):
    assert isinstance(live_response, str)
    assert live_response.strip()


@requires_api_key
def test_live_generate_returns_the_requested_marker(live_response):
    # The model may wrap JSON in a Markdown fence; parse_circuit_spec tolerates
    # that in production, so this test does too rather than failing on style.
    assert MARKER in strip_code_fence(live_response)
