"""Tool-disabled OpenCode CLI adapter for optional placement proposals."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .agent import PlacementPolicyError, PlacementPolicyTimeout

__all__ = ["OpenCodePlacementModel"]


class OpenCodePlacementModel:
    """Run one text-only OpenCode turn with every agent tool denied."""

    proposer_name = "opencode"

    def __init__(
        self,
        *,
        model: str,
        binary: str = "opencode",
        timeout_s: float = 30.0,
        runner: Callable[..., Any] | None = None,
    ) -> None:
        if "/" not in model:
            raise ValueError("OpenCode model must use provider/model format")
        if timeout_s <= 0:
            raise ValueError("OpenCode timeout must be positive")
        self.model = model
        self.binary = binary
        self.timeout_s = timeout_s
        self._runner = runner
        self.last_usage: dict[str, int | float] = {}
        self._process: subprocess.Popen | None = None
        self._process_lock = threading.Lock()

    def cancel(self) -> None:
        """Terminate the isolated CLI process when its lane loses or expires."""
        with self._process_lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()

    def _run(self, command: list[str], **kwargs: Any) -> Any:
        if self._runner is not None:
            return self._runner(command, **kwargs)
        process = subprocess.Popen(
            command,
            cwd=kwargs["cwd"],
            env=kwargs["env"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        with self._process_lock:
            self._process = process
        try:
            stdout, stderr = process.communicate(timeout=kwargs["timeout"])
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        finally:
            with self._process_lock:
                self._process = None
        return subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout,
            stderr,
        )

    def _config(self, temperature: float) -> dict[str, Any]:
        return {
            "$schema": "https://opencode.ai/config.json",
            "agent": {
                "placement": {
                    "description": "Return bounded PCB placement actions only",
                    "mode": "primary",
                    "model": self.model,
                    "temperature": max(0.0, min(float(temperature), 1.0)),
                    "steps": 1,
                    "prompt": (
                        "You are a PCB placement proposal policy. Never use tools. "
                        "Return only PLACE or MOVE lines copied from the prompt."
                    ),
                    "tools": {"*": False},
                    "permission": {"*": "deny"},
                }
            },
        }

    def _parse(self, output: str) -> str:
        text_parts: list[str] = []
        input_tokens = output_tokens = 0
        cost_usd = 0.0
        for raw in output.splitlines():
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            part = event.get("part") or {}
            if event.get("type") == "text" and isinstance(part.get("text"), str):
                text_parts.append(part["text"])
            if event.get("type") == "step_finish":
                tokens = part.get("tokens") or {}
                input_tokens += int(tokens.get("input") or 0)
                output_tokens += int(tokens.get("output") or 0)
                cost_usd += float(part.get("cost") or 0.0)
        self.last_usage = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
        }
        response = "\n".join(text_parts).strip()
        if not response:
            raise PlacementPolicyError("OpenCode returned no text event")
        return response

    def generate(
        self,
        prompt: str,
        *,
        documents=None,
        system: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 8192,
    ) -> str:
        del documents, max_output_tokens
        message = f"{system}\n\n{prompt}" if system else prompt
        environment = os.environ.copy()
        environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(
            self._config(temperature), separators=(",", ":")
        )
        environment["NO_COLOR"] = "1"
        try:
            with tempfile.TemporaryDirectory(prefix="silkscreen-opencode-") as work:
                completed = self._run(
                    [
                        self.binary,
                        "run",
                        "--pure",
                        "--format",
                        "json",
                        "--agent",
                        "placement",
                        "--model",
                        self.model,
                        "--dir",
                        str(Path(work)),
                        message,
                    ],
                    cwd=work,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_s,
                    check=False,
                )
        except subprocess.TimeoutExpired as exc:
            raise PlacementPolicyTimeout(
                "OpenCode placement request timed out"
            ) from exc
        except (OSError, ValueError) as exc:
            raise PlacementPolicyError("OpenCode placement request failed") from exc
        if completed.returncode != 0:
            raise PlacementPolicyError("OpenCode placement command failed")
        return self._parse(completed.stdout)
