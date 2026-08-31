"""Gemini model discovery for the same-origin web client.

The Gemini API is the authority on what the current key may call.  A small
fallback catalog keeps the UI useful before a key is configured and during a
transient discovery failure; it is a fallback, not a claim that those models
are currently reachable.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from silkscreen.agents.model import CHEAP_MODEL, DEFAULT_MODEL

__all__ = ["model_catalog", "select_model"]

_CACHE_TTL_S = 15 * 60
_cache_lock = threading.Lock()
_cache_at = 0.0
_cache: dict[str, Any] | None = None


def _fallback(reason: str = "") -> dict[str, Any]:
    models = []
    for model_id, label in (
        (DEFAULT_MODEL, "Default Gemini model"),
        (CHEAP_MODEL, "Economy Gemini model"),
    ):
        if any(item["id"] == model_id for item in models):
            continue
        models.append(
            {
                "id": model_id,
                "name": label,
                "description": "Configured by the Silkscreen service.",
                "input_token_limit": None,
                "output_token_limit": None,
                "thinking": None,
            }
        )
    return {
        "default": "auto",
        "auto_model": os.getenv("SILKSCREEN_ORCHESTRATOR_MODEL") or DEFAULT_MODEL,
        "source": "fallback",
        "models": models,
        **({"warning": reason} if reason else {}),
    }


def _clean_name(value: object) -> str:
    name = str(value or "")
    return name.removeprefix("models/")


def _live_catalog() -> dict[str, Any]:
    from google import genai

    key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not key:
        return _fallback("Gemini model discovery needs GOOGLE_API_KEY.")

    client = genai.Client(api_key=key)
    try:
        models = []
        for raw in client.models.list():
            actions = list(getattr(raw, "supported_actions", None) or [])
            if "generateContent" not in actions:
                continue
            model_id = _clean_name(getattr(raw, "name", ""))
            if not model_id.startswith("gemini-"):
                continue
            models.append(
                {
                    "id": model_id,
                    "name": str(getattr(raw, "display_name", None) or model_id),
                    "description": str(getattr(raw, "description", None) or ""),
                    "input_token_limit": getattr(raw, "input_token_limit", None),
                    "output_token_limit": getattr(raw, "output_token_limit", None),
                    "thinking": getattr(raw, "thinking", None),
                }
            )
    finally:
        client.close()

    models.sort(key=lambda item: item["id"])
    if not models:
        return _fallback("Gemini returned no generateContent models.")
    auto_model = os.getenv("SILKSCREEN_ORCHESTRATOR_MODEL") or DEFAULT_MODEL
    return {
        "default": "auto",
        "auto_model": auto_model,
        "source": "gemini",
        "models": models,
    }


def model_catalog(*, refresh: bool = False) -> dict[str, Any]:
    """Available text-generation models, cached briefly per service process."""
    global _cache, _cache_at

    now = time.monotonic()
    with _cache_lock:
        if not refresh and _cache is not None and now - _cache_at < _CACHE_TTL_S:
            return _cache
        try:
            catalog = _live_catalog()
        except Exception as exc:  # discovery must never take the UI down
            catalog = _fallback(f"Model discovery failed: {type(exc).__name__}: {exc}")
        _cache = catalog
        _cache_at = now
        return catalog


def select_model(requested: object, catalog: dict[str, Any]) -> str:
    """Resolve ``auto`` or require a model advertised by this server."""
    choice = str(requested or "auto").strip()
    if not choice or choice == "auto":
        return str(catalog.get("auto_model") or DEFAULT_MODEL)
    allowed = {str(item.get("id") or "") for item in catalog.get("models", [])}
    if choice not in allowed:
        raise ValueError("'model' must be 'auto' or an available Gemini model")
    return choice
