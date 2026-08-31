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

import contextlib
import hashlib
import json
import os
import sys
import time
import traceback
import urllib.parse
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


#: Where the built web bundle lives. The override exists because the container
#: copies the bundle to a path the repo layout does not imply.
WEB_DIST = Path(
    os.getenv("SILKSCREEN_WEB_DIST")
    or Path(__file__).resolve().parent.parent / "frontend" / "dist"
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


def _refs_by_spec_name(spec, board) -> dict[str, str]:
    """Spec part names to the reference designators the board gave them.

    A finding names parts the way the *spec* does -- ``AMS1117-3.3``,
    ``c_bulk_vin`` -- because review_circuit validates them against the spec.
    The board names the same parts ``U1`` and ``C1``. Without this map a client
    that highlights by ref matches nothing at all, which looks exactly like a
    finding about no part rather than a lookup failure.

    ``build_board`` assigns refs by walking devices then passives in spec
    order, so the two lists pair up positionally. The device half is checked
    against the value it carries; if anything about that pairing stops holding,
    an empty map is the honest answer, because a wrong ref highlights the
    wrong part.
    """
    names = [d.name for d in spec.devices] + [p.name for p in spec.passives]
    parts = list(board.parts)
    if len(names) != len(parts):
        return {}
    for device, part in zip(spec.devices, parts, strict=False):
        if part.value != device.name:
            return {}
    return {name: part.ref for name, part in zip(names, parts, strict=True)}


def _schematic_dict(spec, refs: dict[str, str]) -> dict[str, Any]:
    """The validated circuit topology in a renderer-sized wire format.

    ``CircuitSpec`` is deliberately an engine object, not an HTTP contract.
    Sending its dataclass fields directly would make the browser understand
    enums, tuples and the endpoint mini-language.  Resolve those here instead:
    every part has one stable spec id and its board reference, and every net
    endpoint names both the logical pin and its physical number.

    Geometry is absent on purpose.  The service owns electrical truth; a view
    owns layout.  Versioning the block lets a later renderer add richer symbol
    metadata without guessing which shape it received.
    """
    devices = {device.name: device for device in spec.devices}
    passives = {passive.name: passive for passive in spec.passives}

    parts: list[dict[str, Any]] = []
    for device in spec.devices:
        parts.append(
            {
                "id": device.name,
                "ref": refs.get(device.name),
                "kind": "device",
                "value": device.name,
                "symbol": device.symbol,
                "pins": [
                    {"name": name, "number": number}
                    for name, number in device.pins.items()
                ],
            }
        )
    for passive in spec.passives:
        parts.append(
            {
                "id": passive.name,
                "ref": refs.get(passive.name),
                "kind": passive.type.value,
                "value": passive.value,
                "symbol": None,
                "pins": [
                    {"name": "1", "number": "1"},
                    {"name": "2", "number": "2"},
                ],
            }
        )

    nets: list[dict[str, Any]] = []
    for connection in spec.connections:
        endpoints: list[dict[str, Any]] = []
        for raw in connection.endpoints:
            part_id, _, pin = raw.rpartition(".")
            device = devices.get(part_id)
            number = device.pins.get(pin) if device is not None else pin
            # The spec was validated before this point, so the only other
            # legitimate endpoint owner is a declared two-terminal passive.
            if device is None and part_id not in passives:
                continue
            endpoints.append(
                {
                    "part_id": part_id,
                    "ref": refs.get(part_id),
                    "pin": pin,
                    "number": number,
                }
            )
        nets.append({"name": connection.net, "endpoints": endpoints})

    return {"version": 1, "parts": parts, "nets": nets}


def _finding_dict(finding, refs: dict[str, str] | None = None) -> dict[str, Any]:
    """One review finding, whole.

    ``blockers`` flattens a finding to a single string, dropping the severity,
    the detail, the citation and the suggested fix -- everything a reader needs
    in order to act on it.

    ``parts`` keeps the spec's names, which is the vocabulary the title and
    detail are written in; ``refs`` carries the same parts as they are labelled
    on the board, so a reader can point at them without guessing.
    """
    refs = refs or {}
    return {
        "severity": finding.severity.value,
        "title": finding.title,
        "detail": finding.detail,
        "parts": list(finding.parts),
        "refs": [refs[p] for p in finding.parts if p in refs],
        "citation": finding.citation,
        "suggested_fix": finding.suggested_fix,
    }


def _rect_mm(x_nm: int, y_nm: int, w_nm: int, h_nm: int) -> list[float]:
    """A rectangle as ``[x, y, w, h]`` in millimetres, min-corner first."""
    return [
        round(to_mm(x_nm), 3),
        round(to_mm(y_nm), 3),
        round(to_mm(w_nm), 3),
        round(to_mm(h_nm), 3),
    ]


def _placements_dict(board) -> dict[str, Any]:
    """Every rectangle a renderer needs, with rotation already applied.

    ``parts`` in the response names refs and footprints only, which is enough
    to list a board and not enough to draw one. This carries the geometry: the
    courtyard each part occupies and every pad inside it, absolute, in the
    solver's Y-up frame.

    Rotation is resolved here rather than on the wire. A rotated part's box and
    pads arrive already transformed, so a client's only coordinate work is the
    single Y flip its own frame needs -- the repository's rule that exactly one
    place owns each frame change, applied across the HTTP boundary.
    """
    parts: list[dict[str, Any]] = []
    for part in board.parts:
        fp = part.footprint
        cw, ch = fp.courtyard_w_nm, fp.courtyard_h_nm
        # x_nm/y_nm are the courtyard's bottom-left corner, Y up; the
        # footprint's own pad coordinates are centred on its anchor. A
        # 90-degree rotation is about the box, so its extents swap.
        box_w, box_h = (2 * ch, 2 * cw) if part.rotated else (2 * cw, 2 * ch)

        pads: list[dict[str, Any]] = []
        for pad in fp.pads:
            # Offsets from that same bottom-left corner. A footprint's own pad
            # coordinates are KiCad's, so Y counts downward from the anchor --
            # the same flip kicad.py performs on the read side, and the one
            # emit_kicad_pcb relies on when it writes these pads out verbatim.
            # Skipping it mirrors every multi-row package inside its own
            # courtyard, silently disagreeing with the board file we serve
            # alongside. Rotation then maps (ox, oy) to (height - oy, ox),
            # exactly as the solver models it.
            ox, oy = pad.x_nm + cw, ch - pad.y_nm
            if part.rotated:
                ox, oy = 2 * ch - oy, ox
                pw, ph = pad.h_nm, pad.w_nm
            else:
                pw, ph = pad.w_nm, pad.h_nm
            pads.append(
                {
                    "number": pad.number,
                    "net": pad.net or None,
                    "rect_mm": _rect_mm(
                        part.x_nm + ox - pw // 2,
                        part.y_nm + oy - ph // 2,
                        pw,
                        ph,
                    ),
                }
            )

        parts.append(
            {
                "ref": part.ref,
                "footprint": fp.name,
                "value": part.value or None,
                "layer": str(part.layer),
                "rotated": bool(part.rotated),
                "x_mm": round(to_mm(part.x_nm), 3),
                "y_mm": round(to_mm(part.y_nm), 3),
                "courtyard_mm": _rect_mm(part.x_nm, part.y_nm, box_w, box_h),
                "pads": pads,
            }
        )

    return {
        "board_mm": [
            round(to_mm(board.width_nm), 3),
            round(to_mm(board.height_nm), 3),
        ],
        "frame": "solver-y-up",
        "parts": parts,
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


def _error_response(exc: BaseException) -> tuple[int, dict[str, Any]]:
    """One failed run, as the status and body a caller should be told about.

    Both POST routes answer failures the same way and differ only in framing:
    the one-shot route sends this as the whole response, and the streaming
    route wraps it in a final ``run.error`` frame. Holding the taxonomy in one
    place is also what makes it fixable in one place -- the bare ValueError
    branch below still returns an internal message verbatim, and this function
    is where a dedicated request-error type will replace it.
    """
    if isinstance(exc, ValueError):
        # Deliberately narrow: generate() converts field-level failures
        # (including float(None)'s TypeError) into ValueError, so a TypeError
        # arriving here is an internal bug and falls through to the 500 branch
        # with its error id and logged traceback.
        return 400, {"error": str(exc)}
    if isinstance(exc, (AllProvidersFailed, GroundingError, ModelError)):
        # Upstream is down, not the caller's fault: 502, not 500.
        return 502, {"error": str(exc)}
    if caused_by_model_failure(exc):
        return 502, {"error": str(exc)}
    # The traceback goes to the log, not to the caller. This is a public
    # endpoint, and a stack trace hands an anonymous client our file layout
    # and internal call structure. The id is what makes the two halves
    # joinable when someone reports a failure.
    error_id = uuid.uuid4().hex[:12]
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    sys.stderr.write(f"error {error_id}: {type(exc).__name__}: {exc}\n{trace}\n")
    return 500, {"error": "internal error", "error_id": error_id}


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
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run the pipeline for one request body.

    ``on_event`` is handed to the pipeline unchanged and additionally receives
    the grounding events this function owns, since grounding happens after the
    pipeline has returned and the pipeline therefore cannot report it. Its
    ``t_s`` counts from this call rather than from the pipeline's own start,
    so the two clocks differ by the cache reads done before the pipeline is
    entered; that gap is real and reporting one clock as the other would only
    hide it. Without ``on_event`` nothing is emitted and the response is
    exactly what it was.
    """
    started = time.monotonic()

    def emit(event: dict[str, Any]) -> None:
        if on_event is None:
            return
        event["t_s"] = round(time.monotonic() - started, 3)
        on_event(event)

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

    # Coerce here, not in the route handler: float() raises TypeError on a
    # JSON null and ValueError on a string, and both are this caller's error.
    # Naming the field keeps the route's except clause narrow, so a genuine
    # internal TypeError still surfaces as a 500 with an error id.
    try:
        time_limit_s = float(payload.get("time_limit_s", DEFAULT_TIME_LIMIT))
    except (TypeError, ValueError):
        raise ValueError("'time_limit_s' must be a number") from None

    result = generate_pcb(
        model,
        intent,
        datasheets=to_read,
        preloaded_facts=preloaded,
        time_limit_s=time_limit_s,
        review=bool(payload.get("review", True)),
        on_event=on_event,
        # Debugging a run means reading what the model actually said, so the
        # raw answers join the stream only when the caller asks for them.
        include_responses=bool(payload.get("debug", False)),
    )

    for fact in result.facts:
        part = getattr(fact, "part_number", None)
        if part:
            store.put(part, fact.to_dict())

    board = result.board
    refs = _refs_by_spec_name(result.spec, board)
    response: dict[str, Any] = {
        "intent": intent,
        "board_mm": [round(to_mm(board.width_nm), 3), round(to_mm(board.height_nm), 3)],
        "status": str(board.solver_status),
        "parts": [{"ref": p.ref, "footprint": p.footprint.name} for p in board.parts],
        "kicad_pcb": emit_kicad_pcb(board),
        "repair_rounds": result.repair_rounds,
        "blockers": [str(b) for b in result.blockers],
        "findings": [_finding_dict(f, refs) for f in result.findings],
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
        # Geometry, resolved server-side. Additive: nothing above changes
        # shape, so a client that only reads "parts" keeps working.
        "placements": _placements_dict(board),
        # Electrical topology, separate from physical placement.  The browser
        # lays this out as a schematic without having to parse CircuitSpec.
        "schematic": _schematic_dict(result.spec, refs),
        "wirelength_mm": (
            None
            if board.wirelength_nm is None
            else round(to_mm(board.wirelength_nm), 3)
        ),
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
            # Read before the fetch below reassigns it: afterwards every part
            # holds pages and the distinction that matters is gone.
            cached = pages is not None
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
            emit({"event": "ground.part", "part": part, "cached": cached})
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
    """Four routes: a health check Cloud Run can probe, the generator in its
    one-shot and streaming forms, and the built web bundle."""

    model_factory = staticmethod(build_model)
    store: FactStore | None = None
    pages_store: FactStore | None = None
    embedder_factory = staticmethod(build_embedder)

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

    def _is_fingerprinted(self, path: Path) -> bool:
        """True only for files the build named with a content hash.

        The bundler puts those in ``assets/``. Everything else -- index.html,
        and anything copied verbatim from ``frontend/public`` -- keeps a fixed name
        across deploys, so an immutable year on it is a cache entry with no
        way to be busted short of a new URL.
        """
        root = self.web_root
        if root is None:
            return False
        try:
            relative = path.resolve().relative_to(root.resolve())
        except (OSError, ValueError):
            return False
        return len(relative.parts) > 1 and relative.parts[0] == "assets"

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
        if self._is_fingerprinted(path):
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-cache")
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
        if self.path == "/generate/stream":
            self._generate_stream()
            return
        if self.path in ("/generate", "/"):
            self._generate_once()
            return
        self._send(404, {"error": f"no route {self.path}"})

    def _read_payload(self) -> dict[str, Any] | None:
        """The request body as a JSON object, or None once its error is sent.

        Shared by both POST routes. Everything checked here fails before a
        single byte of response has been written, which is what lets the
        streaming route answer a bad request with a plain JSON error rather
        than a stream whose only frame is an apology.
        """
        # A malformed Content-Length is the client's error. Parsing it outside
        # a guard lets a header of "abc" raise before any response is sent, so
        # the caller sees a dropped connection instead of a 400.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, {"error": "invalid Content-Length"})
            return None
        if length < 0:
            self._send(400, {"error": "invalid Content-Length"})
            return None
        if length > MAX_BODY_BYTES:
            self._send(413, {"error": "request body too large"})
            return None
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            self._send(400, {"error": f"invalid JSON: {exc}"})
            return None
        if not isinstance(payload, dict):
            self._send(400, {"error": "body must be a JSON object"})
            return None
        return payload

    def _run(
        self,
        payload: dict[str, Any],
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """One pipeline run, wired to whatever this handler was injected with."""
        store = self.store if self.store is not None else build_store()
        return generate(
            payload,
            model=self.model_factory(),
            store=store,
            pages_store=self.pages_store,
            embedder_factory=self.embedder_factory,
            on_event=on_event,
        )

    def _generate_once(self) -> None:
        """The whole run in one response, once it is finished."""
        payload = self._read_payload()
        if payload is None:
            return
        try:
            result = self._run(payload)
        except Exception as exc:
            status, body = _error_response(exc)
            self._send(status, body)
        else:
            self._send(200, result)

    def _generate_stream(self) -> None:
        """The same run, reported while it happens, as NDJSON.

        No Content-Length and no chunked encoding: this server speaks HTTP/1.0,
        where a body is delimited by the connection closing. That is also why
        every frame is flushed the moment it is written -- a buffer that
        delivered the frames at the end would turn progress into a transcript.
        """
        payload = self._read_payload()
        if payload is None:
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        gone = False

        def emit(event: dict[str, Any]) -> None:
            """One frame, on the wire immediately.

            A write to a client that has hung up re-raises on purpose: the
            pipeline lets a callback's exception abandon the run, which is how
            a reader's disconnect cancels the work being done for it.
            """
            nonlocal gone
            try:
                self.wfile.write((json.dumps(event) + "\n").encode())
                self.wfile.flush()
            except ConnectionError:
                # BrokenPipeError and ConnectionResetError are the two that
                # normally arrive; Windows reports the same disconnect as
                # ConnectionAbortedError, and all three are ConnectionError.
                gone = True
                raise

        try:
            # Before any work starts, so a client can tell an accepted request
            # from one still waiting for a connection.
            emit({"event": "run.accepted", "t_s": 0.0})
            result = self._run(payload, on_event=emit)
        except Exception as exc:
            if gone:
                # The exception is our own emit reporting the disconnect, on
                # its way out through the pipeline. There is nobody left to
                # tell, and writing again would only raise a second time.
                sys.stderr.write(f"stream client disconnected: {self.path}\n")
                return
            status, body = _error_response(exc)
            # Whether this last frame lands is the client's business now; if
            # it left between the failure and this write there is nothing
            # further to do about it.
            with contextlib.suppress(ConnectionError):
                emit({"event": "run.error", "status": status, **body})
            return

        with contextlib.suppress(ConnectionError):
            emit({"event": "run.done", "result": result})

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
