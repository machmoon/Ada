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

import json
import os
import sys
import time
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from silkscreen.agents import ModelError, generate_pcb  # noqa: E402
from silkscreen.agents.datasheet import PartFacts  # noqa: E402
from silkscreen.agents.model import GeminiModel  # noqa: E402
from silkscreen.agents.resilience import (  # noqa: E402
    AllProvidersFailed,
    FallbackModel,
    Provider,
)
from silkscreen.board import emit_kicad_pcb  # noqa: E402
from silkscreen.units import to_mm  # noqa: E402

from .cache import FactStore, MemoryFactStore  # noqa: E402

__all__ = [
    "Handler",
    "build_model",
    "build_store",
    "caused_by_model_failure",
    "generate",
    "make_server",
]

MAX_BODY_BYTES = 1 << 20
DEFAULT_TIME_LIMIT = 20.0

#: Where the built web bundle lives. The override exists because the container
#: copies the bundle to a path the repo layout does not imply.
WEB_DIST = Path(
    os.getenv("SILKSCREEN_WEB_DIST")
    or Path(__file__).resolve().parent.parent / "web" / "dist"
)

# Spelled out rather than taken from mimetypes: on Windows mimetypes reads the
# registry, which routinely maps .js to text/plain, and a browser hard-refuses
# a module script served with the wrong type. That failure would appear on a
# developer's machine and disappear in the container.
_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".ico": "image/x-icon",
    ".jpg": "image/jpeg",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}
_DEFAULT_CONTENT_TYPE = "application/octet-stream"


def _finding_dict(finding) -> dict[str, Any]:
    """One review finding, whole.

    ``blockers`` flattens a finding to a single string, dropping the severity,
    the detail, the citation and the suggested fix -- everything a reader needs
    in order to act on it.
    """
    return {
        "severity": finding.severity.value,
        "title": finding.title,
        "detail": finding.detail,
        "parts": list(finding.parts),
        "citation": finding.citation,
        "suggested_fix": finding.suggested_fix,
    }


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
) -> dict[str, Any]:
    """Run the pipeline for one request body."""
    started = time.monotonic()

    intent = str(payload.get("intent") or "").strip()
    if not intent:
        raise ValueError("'intent' is required")

    datasheets = payload.get("datasheets") or {}
    if not isinstance(datasheets, dict):
        raise ValueError("'datasheets' must be an object of {part: url}")

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
    return {
        "intent": intent,
        "board_mm": [round(to_mm(board.width_nm), 3), round(to_mm(board.height_nm), 3)],
        "status": str(board.solver_status),
        "parts": [{"ref": p.ref, "footprint": p.footprint.name} for p in board.parts],
        "kicad_pcb": emit_kicad_pcb(board),
        "repair_rounds": result.repair_rounds,
        "blockers": [str(b) for b in result.blockers],
        "findings": [_finding_dict(f) for f in result.findings],
        "duration_s": round(time.monotonic() - started, 3),
        "warnings": list(board.warnings),
        "nets": list(board.nets),
        # Both freshly read and cache-supplied facts land in result.facts, so
        # this reports what the design was actually informed by.
        "datasheets": [
            {
                "part": f.part_number,
                "package": f.package,
                "pins": len(f.pins),
                "requirements": len(f.requirements),
                "url": f.source_url,
            }
            for f in result.facts
        ],
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


class Handler(BaseHTTPRequestHandler):
    """Three routes: a health check Cloud Run can probe, the generator, and
    the built web bundle."""

    model_factory = staticmethod(build_model)
    store: FactStore | None = None
    #: Root of the built bundle; None serves no static files at all.
    web_root: Path | None = WEB_DIST

    # There are deliberately no CORS headers and no do_OPTIONS: the bundle is
    # served from this same origin, so nothing the UI sends is cross-origin.
    # Adding them defensively would only widen who may call /generate.

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _resolve_static(self, route: str) -> Path | None:
        """The bundle file ``route`` names, or None if it names none.

        Two independent defences, because each covers what the other misses.
        The segment whitelist runs on the *decoded* path, so ``%2e%2e%2f`` and
        the Windows ``..%5c`` are refused before any filesystem call; the
        resolve/relative_to containment catches a symlink pointing out of the
        bundle, which no string check can see.
        """
        root = self.web_root
        if root is None:
            return None

        segments = urllib.parse.unquote(route).lstrip("/").split("/")
        if segments == [""]:
            segments = ["index.html"]
        for segment in segments:
            if segment in ("", ".", "..") or "\\" in segment or ":" in segment:
                return None

        try:
            resolved = root.joinpath(*segments).resolve()
            resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            return None
        return resolved if resolved.is_file() else None

    def _send_file(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send(404, {"error": f"no route {self.path}"})
            return
        self.send_response(200)
        self.send_header(
            "Content-Type",
            _CONTENT_TYPES.get(path.suffix.lower(), _DEFAULT_CONTENT_TYPE),
        )
        self.send_header("Content-Length", str(len(body)))
        # index.html names the fingerprinted assets, so caching it would pin a
        # client to the previous deploy. Everything else carries a content hash
        # in its filename and can never go stale under it.
        if path.name == "index.html":
            self.send_header("Cache-Control", "no-cache")
        else:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        route = urllib.parse.urlsplit(self.path).path

        # The probe is answered before the bundle is consulted, so a build
        # output file named "healthz" can never shadow the check Cloud Run
        # uses to decide whether this revision is alive.
        if route == "/healthz":
            self._send(200, {"ok": True, "service": "silkscreen"})
            return

        static = self._resolve_static(route)
        if static is not None:
            self._send_file(static)
            return

        # No blanket SPA fallback: the UI's tabs are hash fragments that never
        # reach the server, so a miss here is a genuinely missing file, and
        # answering it with index.html turns that into a blank page.
        root = self.web_root
        if route == "/" and (root is None or not (root / "index.html").is_file()):
            self._send(200, {"ok": True, "service": "silkscreen"})
            return

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
            result = generate(payload, model=self.model_factory(), store=store)
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
        except (AllProvidersFailed, ModelError) as exc:
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
