"""Cloud Run service: prompt in, KiCad board out.

A single HTTP surface over :func:`silkscreen.agents.generate_pcb`, built on the
standard library so the container stays small and the dependency list stays
honest. Datasheet facts persist to Firestore, so the second request for a part
skips the most expensive stage in the pipeline.

Run locally::

    python -m service.app

Deploy::

    gcloud run deploy silkscreen --source . --region us-central1
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import traceback
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from silkscreen.agents import ModelError, generate_pcb  # noqa: E402
from silkscreen.agents.datasheet import PartFacts  # noqa: E402
from silkscreen.agents.grounding import (  # noqa: E402
    BatchingEmbedder,
    GroundingError,
    build_index,
    ground_findings,
    load_pages,
    pages_for_part,
    store_pages,
)
from silkscreen.agents.model import GeminiModel  # noqa: E402
from silkscreen.agents.resilience import (  # noqa: E402
    AllProvidersFailed,
    FallbackModel,
    Provider,
)
from silkscreen.agents.retrieval import GeminiEmbedder  # noqa: E402
from silkscreen.board import emit_kicad_pcb  # noqa: E402
from silkscreen.units import to_mm  # noqa: E402

from .cache import FactStore, MemoryFactStore  # noqa: E402

__all__ = [
    "Handler",
    "build_embedder",
    "build_model",
    "build_pages_store",
    "build_store",
    "caused_by_model_failure",
    "generate",
    "make_server",
    "page_cache_key",
]

MAX_BODY_BYTES = 1 << 20
DEFAULT_TIME_LIMIT = 20.0
PAGES_COLLECTION = "datasheet_pages"
MAX_GROUND_PARTS = 25


def page_cache_key(part: str, url: str) -> str:
    return f"{part}\x00{hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]}"


def caused_by_model_failure(exc: BaseException) -> bool:
    """True if a model outage is anywhere under this exception.

    The pipeline wraps a failed call in ProposalError, which is a RuntimeError
    and not a ModelError -- so a Gemini outage and a Gemini response we could
    not use arrive as the same type. They are different failures: one is ours
    to retry, the other is the caller's prompt to fix. Walking the cause chain
    is what keeps a 503 upstream from being reported as our 500.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (ModelError, AllProvidersFailed)):
            return True
        current = current.__cause__ or current.__context__
    return False


def build_store() -> FactStore:
    """Firestore when deployed, in-memory when not configured."""
    if os.getenv("GOOGLE_CLOUD_PROJECT") and os.getenv("USE_FIRESTORE", "1") != "0":
        from .cache import FirestoreFactStore

        return FirestoreFactStore()
    return MemoryFactStore()


def build_pages_store() -> FactStore:
    if os.getenv("GOOGLE_CLOUD_PROJECT") and os.getenv("USE_FIRESTORE", "1") != "0":
        from .cache import FirestoreFactStore

        return FirestoreFactStore(PAGES_COLLECTION)
    return MemoryFactStore()


def build_embedder() -> BatchingEmbedder:
    return BatchingEmbedder(GeminiEmbedder())


def build_model():
    """Primary Gemini model with a cheaper tier behind it.

    Two tiers, not one: a rate limit or a transient 5xx on the primary should
    degrade the answer, not lose the request.
    """
    from silkscreen.agents.model import CHEAP_MODEL, DEFAULT_MODEL

    return FallbackModel(
        providers=[
            Provider("gemini-primary", GeminiModel(DEFAULT_MODEL), attempts=2),
            Provider("gemini-cheap", GeminiModel(CHEAP_MODEL), attempts=2),
        ]
    )


def generate(
    payload: dict[str, Any],
    *,
    model,
    store: FactStore,
    pages_store: FactStore | None = None,
    embedder_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Run the pipeline for one request body."""
    intent = str(payload.get("intent") or "").strip()
    if not intent:
        raise ValueError("'intent' is required")

    datasheets = payload.get("datasheets") or {}
    if not isinstance(datasheets, dict):
        raise ValueError("'datasheets' must be an object of {part: url}")
    if any(not isinstance(u, str) or not u for u in datasheets.values()):
        raise ValueError("each datasheet value must be a non-empty URL string")
    if payload.get("ground") is True:
        if not datasheets:
            raise ValueError("'ground' requires 'datasheets'")
        if len(datasheets) > MAX_GROUND_PARTS:
            raise ValueError(
                f"'ground' supports at most {MAX_GROUND_PARTS} datasheets per request"
            )
        for url in datasheets.values():
            shape = urlsplit(url)
            if shape.scheme.lower() not in ("http", "https") or not shape.hostname:
                raise ValueError("datasheet URL is not an http(s) URL")

    # Anything already in Firestore is not read again -- but the facts we
    # stored are handed to the pipeline in the read's place. Skipping the read
    # without supplying the facts would design the board blind, which is
    # strictly worse than not caching at all.
    cached = {p: store.get(p) for p in datasheets}
    to_read = {p: u for p, u in datasheets.items() if cached.get(p) is None}

    preloaded: list[PartFacts] = []
    unusable: list[str] = []
    for part, raw in cached.items():
        if raw is None:
            continue
        try:
            preloaded.append(PartFacts.from_dict(raw))
        except (TypeError, ValueError):
            # A malformed or legacy entry is a cache miss, not a failed
            # request: fall back to reading the datasheet again.
            unusable.append(part)
            to_read[part] = datasheets[part]

    result = generate_pcb(
        model,
        intent,
        datasheets=to_read,
        preloaded_facts=preloaded,
        time_limit_s=float(payload.get("time_limit_s", DEFAULT_TIME_LIMIT)),
        review=bool(payload.get("review", True)),
    )

    for fact in result.facts:
        part = getattr(fact, "part_number", None)
        if part:
            store.put(part, fact.to_dict())

    board = result.board
    response: dict[str, Any] = {
        "intent": intent,
        "board_mm": [round(to_mm(board.width_nm), 3), round(to_mm(board.height_nm), 3)],
        "status": str(board.solver_status),
        "parts": [{"ref": p.ref, "footprint": p.footprint.name} for p in board.parts],
        "kicad_pcb": emit_kicad_pcb(board),
        "repair_rounds": result.repair_rounds,
        "blockers": [str(b) for b in result.blockers],
        # A "hit" is an entry we could actually use. An entry that was present
        # but unreadable is reported as a miss and a re-read, because that is
        # what happened.
        "cache": {
            "hit": sorted(
                p for p, v in cached.items() if v is not None and p not in unusable
            ),
            "read": sorted(to_read),
            "unusable": sorted(unusable),
        },
        "served_by": getattr(model, "last_provider", None),
    }

    if payload.get("ground") is True:
        if not result.findings:
            response["grounding"] = {
                "findings": [],
                "pages": {"cached": [], "read": []},
            }
            return response

        pages_store = pages_store if pages_store is not None else build_pages_store()
        embedder = (embedder_factory or build_embedder)()

        indexes: dict[str, Any] = {}
        pages_cached: list[str] = []
        pages_read: list[str] = []
        for part, url in datasheets.items():
            pages = load_pages(pages_store, page_cache_key(part, url))
            if pages is None:
                try:
                    pages = pages_for_part(url=url)
                except ValueError as exc:
                    sys.stderr.write(f"grounding rejected datasheet url: {exc}\n")
                    raise ValueError("datasheet URL is not allowed") from exc
                store_pages(pages_store, page_cache_key(part, url), pages)
                pages_read.append(part)
            else:
                pages_cached.append(part)
            index = build_index(pages, embedder)
            if len(index):
                indexes[part] = index

        grounded = ground_findings(indexes, result.findings)
        response["grounding"] = {
            "findings": [
                {
                    "severity": g.finding.severity.value,
                    "title": g.finding.title,
                    "detail": g.finding.detail,
                    "parts": list(g.finding.parts),
                    "citation": g.finding.citation,
                    "suggested_fix": g.finding.suggested_fix,
                    "status": g.status.value,
                    "evidence": [
                        {
                            "part": e.part,
                            "page": e.page,
                            "score": round(e.score, 4),
                            "quote": e.quote,
                        }
                        for e in g.evidence
                    ],
                }
                for g in grounded
            ],
            "pages": {"cached": sorted(pages_cached), "read": sorted(pages_read)},
        }
    return response


class Handler(BaseHTTPRequestHandler):
    """Two routes: a health check Cloud Run can probe, and the generator."""

    model_factory = staticmethod(build_model)
    store: FactStore | None = None
    pages_store: FactStore | None = None
    embedder_factory = staticmethod(build_embedder)

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in ("/", "/healthz"):
            self._send(200, {"ok": True, "service": "silkscreen"})
        else:
            self._send(404, {"error": f"no route {self.path}"})

    def do_POST(self) -> None:
        if self.path not in ("/generate", "/"):
            self._send(404, {"error": f"no route {self.path}"})
            return

        # A malformed Content-Length is the client's error. Parsing it outside
        # a guard lets a header of "abc" raise before any response is sent, so
        # the caller sees a dropped connection instead of a 400.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, {"error": "invalid Content-Length"})
            return
        if length < 0:
            self._send(400, {"error": "invalid Content-Length"})
            return
        if length > MAX_BODY_BYTES:
            self._send(413, {"error": "request body too large"})
            return
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            self._send(400, {"error": f"invalid JSON: {exc}"})
            return
        if not isinstance(payload, dict):
            self._send(400, {"error": "body must be a JSON object"})
            return

        try:
            store = self.store if self.store is not None else build_store()
            result = generate(
                payload,
                model=self.model_factory(),
                store=store,
                pages_store=self.pages_store,
                embedder_factory=self.embedder_factory,
            )
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
        except (AllProvidersFailed, GroundingError, ModelError) as exc:
            # Upstream is down, not the caller's fault: 502, not 500.
            self._send(502, {"error": str(exc)})
        except Exception as exc:
            if caused_by_model_failure(exc):
                self._send(502, {"error": str(exc)})
            else:
                # The traceback goes to the log, not to the caller. This is a
                # public endpoint, and a stack trace hands an anonymous client
                # our file layout and internal call structure. The id is what
                # makes the two halves joinable when someone reports a failure.
                error_id = uuid.uuid4().hex[:12]
                sys.stderr.write(
                    f"error {error_id}: {type(exc).__name__}: {exc}\n"
                    f"{traceback.format_exc()}\n"
                )
                self._send(
                    500,
                    {
                        "error": "internal error",
                        "error_id": error_id,
                    },
                )
        else:
            self._send(200, result)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")


def make_server(port: int | None = None) -> ThreadingHTTPServer:
    port = port if port is not None else int(os.getenv("PORT", "8080"))
    return ThreadingHTTPServer(("0.0.0.0", port), Handler)


def main() -> int:  # pragma: no cover - process entry
    server = make_server()
    sys.stderr.write(f"listening on :{server.server_port}\n")
    server.serve_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
