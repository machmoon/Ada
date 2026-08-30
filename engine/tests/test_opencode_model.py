"""Offline contract tests for the OpenCode text fallback."""

from __future__ import annotations

import json
from subprocess import CompletedProcess

import pytest
from silkscreen.agents.model import Document, ModelError, OpenCodeModel


def text_event(value: str) -> str:
    return json.dumps({"type": "text", "part": {"text": value}})


def test_opencode_model_extracts_text_events(monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return CompletedProcess(command, 0, stdout=text_event("answer"), stderr="")

    monkeypatch.setattr("silkscreen.agents.model.subprocess.run", fake_run)

    result = OpenCodeModel("opencode-go/glm-5.3-flash").generate(
        "prompt", system="system"
    )

    assert result == "answer"
    assert seen["command"][:3] == ["opencode", "run", "--pure"]
    assert "opencode-go/glm-5.3-flash" in seen["command"]
    assert "system\n\nprompt" in seen["command"][-1]
    assert seen["kwargs"]["timeout"] == 120.0


def test_opencode_model_rejects_documents_before_calling_cli(monkeypatch):
    called = False

    def fake_run(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("silkscreen.agents.model.subprocess.run", fake_run)

    with pytest.raises(ModelError, match="text-only"):
        OpenCodeModel("opencode-go/glm-5.3-flash").generate(
            "read it", documents=[Document(url="https://example.com/a.pdf")]
        )

    assert called is False


def test_opencode_model_reports_cli_failure(monkeypatch):
    def fake_run(command, **kwargs):
        return CompletedProcess(command, 7, stdout="", stderr="provider failed")

    monkeypatch.setattr("silkscreen.agents.model.subprocess.run", fake_run)

    with pytest.raises(ModelError, match="provider failed"):
        OpenCodeModel("opencode-go/glm-5.3-flash").generate("prompt")


def test_opencode_model_requires_a_text_event(monkeypatch):
    def fake_run(command, **kwargs):
        return CompletedProcess(command, 0, stdout='{"type":"step_finish"}', stderr="")

    monkeypatch.setattr("silkscreen.agents.model.subprocess.run", fake_run)

    with pytest.raises(ModelError, match="no text event"):
        OpenCodeModel("opencode-go/glm-5.3-flash").generate("prompt")
