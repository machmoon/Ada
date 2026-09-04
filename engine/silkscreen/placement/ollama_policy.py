"""Experimental Ollama placement policy for a private local GPU fast path."""

from __future__ import annotations

import json
import threading
import urllib.request
from collections.abc import Callable
from typing import Any

from .agent import PlacementPolicyError, PlacementPolicyTimeout

__all__ = ["OllamaPlacementModel"]


class OllamaPlacementModel:
    proposer_name = "gemma-local"

    def __init__(
        self,
        *,
        base_url: str,
        model: str = "gemma3:4b",
        timeout_s: float = 30.0,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("Ollama base_url must be HTTP or HTTPS")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self._opener = opener
        self._response = None
        self._response_lock = threading.Lock()
        self.last_usage: dict[str, int] = {}

    def cancel(self) -> None:
        with self._response_lock:
            response = self._response
        if response is not None:
            response.close()

    def generate(
        self,
        prompt: str,
        *,
        documents=None,
        system: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 8192,
    ) -> str:
        del documents
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": "15m",
            "options": {
                "temperature": max(temperature, 0.0),
                "num_predict": max_output_tokens,
                "seed": 0,
            },
        }
        request = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with self._opener(request, timeout=self.timeout_s) as response:
                with self._response_lock:
                    self._response = response
                result = json.loads(response.read())
        except TimeoutError as exc:
            raise PlacementPolicyTimeout("Ollama placement request timed out") from exc
        except Exception as exc:
            raise PlacementPolicyError("Ollama placement request failed") from exc
        finally:
            with self._response_lock:
                self._response = None
        content = result.get("message", {}).get("content")
        if not isinstance(content, str):
            raise PlacementPolicyError(
                "Ollama response did not contain message.content"
            )
        self.last_usage = {
            "input_tokens": int(result.get("prompt_eval_count") or 0),
            "output_tokens": int(result.get("eval_count") or 0),
        }
        return content.strip()
