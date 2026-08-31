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
import math
import os
import sys
import time
import traceback
import urllib.parse
import uuid
from collections.abc import Callable
from dataclasses import fields, replace
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
from silkscreen.constraints import (  # noqa: E402
    parse_constraint_manifest,
    verify_constraint_manifest,
)
from silkscreen.fab import fab_files  # noqa: E402
from silkscreen.order import (  # noqa: E402
    OrderOptions,
    SolderMaskColour,
    SurfaceFinish,
    order_manifest,
    preflight,
)
from silkscreen.placement.agent import PlacementPolicyError  # noqa: E402
from silkscreen.placement.api import repair_request, run_to_dict  # noqa: E402
from silkscreen.placement.traces import (  # noqa: E402
    FactFailureTraceStore,
    FailureTraceStore,
    JsonlFailureTraceStore,
    build_failure_traces,
)
from silkscreen.units import to_mm  # noqa: E402

from .cache import FactStore, MemoryFactStore  # noqa: E402
from .configuration import configuration_status  # noqa: E402
from .models import (  # noqa: E402
    model_catalog,
    select_model,
    select_quota_rpm,
    select_thinking_level,
)
from .quota import GEMINI_REQUEST_PACER, RequestPacer  # noqa: E402

__all__ = [
    "Handler",
    "build_embedder",
    "build_model",
    "build_ollama_model",
    "build_pages_store",
    "build_failure_trace_store",
    "build_store",
    "build_tinker_model",
    "placement_policy_status",
    "resolve_placement_policy",
    "caused_by_model_failure",
    "generate",
    "make_server",
    "page_cache_key",
]

MAX_BODY_BYTES = 1 << 20
DEFAULT_TIME_LIMIT = 20.0
PAGES_COLLECTION = "datasheet_pages"
MAX_GROUND_PARTS = 25
MAX_ENCLOSURE_STYLE_CHARS = 500


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


class _ResponseSerializationError(RuntimeError):
    """A response or stream frame could not be represented as strict JSON."""


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite number {value} is not valid JSON")


def _parse_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        _reject_nonfinite_json(value)
    return number


def _json_line(payload: dict[str, Any]) -> bytes:
    """Encode one NDJSON frame without emitting JavaScript-only NaN values."""
    try:
        return (json.dumps(payload, allow_nan=False) + "\n").encode()
    except (TypeError, ValueError) as exc:
        raise _ResponseSerializationError(
            "stream frame is not JSON serializable"
        ) from exc


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


_ORDER_ENUMS = {
    "surface_finish": SurfaceFinish,
    "mask_colour": SolderMaskColour,
}


def order_options(raw: Any) -> OrderOptions:
    """Build :class:`OrderOptions` from a request body, or refuse it.

    Unknown keys are rejected rather than ignored: a client that misspells
    ``quantity`` is asking for a different order than the one it would get,
    and silently shipping five boards instead of fifty is the expensive
    failure mode here.
    """
    if not isinstance(raw, dict):
        raise ValueError("'order' must be an object of order options")
    known = {f.name for f in fields(OrderOptions)}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(
            f"unknown order option(s): {', '.join(unknown)}; "
            f"supported: {', '.join(sorted(known))}"
        )
    kwargs = dict(raw)
    for field_name, enum in _ORDER_ENUMS.items():
        if field_name in kwargs:
            try:
                kwargs[field_name] = enum(kwargs[field_name])
            except ValueError:
                allowed = ", ".join(m.value for m in enum)
                raise ValueError(
                    f"{field_name} must be one of {allowed}, "
                    f"got {kwargs[field_name]!r}"
                ) from None
    return OrderOptions(**kwargs)


def _order_block(board, spec, options: OrderOptions) -> dict[str, Any]:
    """Preflight, manifest and fab files for one board.

    The files are small enough to inline -- a dozen text artefacts totalling a
    few kilobytes -- so the client gets everything it needs to show, zip or
    download an order in the same response that produced the board.
    """
    pre = preflight(board, spec=spec, options=options)
    return {
        "manifest": order_manifest(board, options, pre),
        "issues": [issue.as_dict() for issue in pre.issues],
        "orderable": pre.orderable,
        "files": [
            {"filename": layer.filename, "content": layer.content}
            for layer in fab_files(board)
        ],
    }


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


def _enclosure_dict(enclosure) -> dict[str, Any] | None:
    """The additive ``enclosure`` response block (docs/ai-cad-plan.md).

    ``None`` is the contract's honest degradation: the stage failed or was
    skipped, and the board is still the product. On success the ``.scad``
    rides the JSON exactly as ``kicad_pcb`` does, with the fit receipt's
    signed per-axis margins converted to mm here -- the one formatting
    crossing, mirroring ``FitReport.params_mm``. Nothing else is added:
    the one-shot response must never grow raw model output.
    """
    if enclosure is None:
        return None
    fit = enclosure.fit
    return {
        "scad": enclosure.scad,
        "params": dict(fit.params_mm),
        "fit": {
            "margins_mm": {
                axis: round(to_mm(value), 3)
                for axis, value in fit.margins_nm.items()
            }
        },
        "warnings": list(fit.warnings),
        "repair_rounds": enclosure.repair_rounds,
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
    if isinstance(
        exc,
        (AllProvidersFailed, GroundingError, ModelError, PlacementPolicyError),
    ):
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


def build_failure_trace_store() -> FailureTraceStore:
    """Firestore in Cloud Run, append-only JSONL for local post-training."""
    if os.getenv("GOOGLE_CLOUD_PROJECT") and os.getenv("USE_FIRESTORE", "1") != "0":
        from .cache import FirestoreFactStore

        return FactFailureTraceStore(
            FirestoreFactStore("placement_failure_traces")
        )
    configured = os.getenv("PLACEMENT_FAILURE_TRACE_PATH", "").strip()
    path = (
        Path(configured)
        if configured
        else Path(__file__).resolve().parent.parent
        / "artifacts"
        / "placement-failure-traces.jsonl"
    )
    return JsonlFailureTraceStore(path)


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


def build_tinker_model():
    """Load the promoted small-policy checkpoint, never an untrained base model."""
    checkpoint = os.getenv("TINKER_PLACEMENT_MODEL", "").strip()
    if not checkpoint:
        raise ValueError(
            "tinker policy is not configured; set TINKER_PLACEMENT_MODEL to "
            "the promoted tinker:// sampler checkpoint"
        )
    if not checkpoint.startswith("tinker://"):
        raise ValueError("TINKER_PLACEMENT_MODEL must be a tinker:// checkpoint")
    from silkscreen.placement.tinker_policy import TinkerPlacementModel

    return TinkerPlacementModel(model_path=checkpoint)


def build_ollama_model():
    base_url = os.getenv("OLLAMA_PLACEMENT_URL", "").strip()
    if not base_url:
        raise ValueError(
            "local policy is not configured; set OLLAMA_PLACEMENT_URL to the "
            "private Ollama endpoint"
        )
    from silkscreen.placement.ollama_policy import OllamaPlacementModel

    return OllamaPlacementModel(
        base_url=base_url,
        model=os.getenv("OLLAMA_PLACEMENT_MODEL", "gemma3:4b"),
    )


def build_fast_placement_model():
    if _tinker_configured():
        return build_tinker_model()
    return build_ollama_model()


def _tinker_configured() -> bool:
    checkpoint = os.getenv("TINKER_PLACEMENT_MODEL", "").strip()
    return bool(os.getenv("TINKER_API_KEY") and checkpoint.startswith("tinker://"))


def _experimental_requested(payload: dict[str, Any]) -> bool:
    value = payload.get("experimental_placement", False)
    if not isinstance(value, bool):
        raise ValueError("experimental_placement must be a boolean")
    return value


def placement_policy_status(*, experimental: bool = False) -> dict[str, bool]:
    gemini = bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))
    tinker = experimental and _tinker_configured()
    ollama = experimental and bool(os.getenv("OLLAMA_PLACEMENT_URL"))
    return {
        "deterministic": True,
        "gemini": gemini,
        "tinker": tinker,
        "ollama": ollama,
        "hybrid": gemini and (tinker or ollama),
    }


def resolve_placement_policy(
    requested: str, available: dict[str, bool] | None = None
) -> str:
    """Resolve the single fast product mode to the best configured backend."""
    status = available if available is not None else placement_policy_status()
    if requested != "fast":
        if requested not in status:
            raise ValueError(f"unknown placement policy {requested!r}")
        if not status[requested]:
            raise ValueError(f"placement policy {requested!r} is not available")
        return requested
    for candidate in ("hybrid", "tinker", "ollama"):
        if status.get(candidate):
            return candidate
    return "deterministic"


def _placement_model_id(policy: str) -> str:
    if policy == "ollama":
        return os.getenv("OLLAMA_PLACEMENT_MODEL", "gemma3:4b")
    if policy == "tinker":
        return os.getenv("TINKER_PLACEMENT_MODEL", "Qwen/Qwen3.5-4B")
    if policy == "hybrid":
        if os.getenv("TINKER_PLACEMENT_MODEL"):
            return os.getenv("TINKER_PLACEMENT_MODEL", "Qwen/Qwen3.5-4B")
        return os.getenv("OLLAMA_PLACEMENT_MODEL", "gemma3:4b")
    return policy


def _trace_consent(payload: dict[str, Any]) -> tuple[bool, str]:
    """Training traces are always an explicit, experimental opt-in."""
    supplied = payload.get("record_trace")
    if "record_trace" in payload and not isinstance(supplied, bool):
        raise ValueError("record_trace must be a boolean")
    if supplied and payload.get("experimental_placement") is not True:
        raise ValueError("record_trace requires experimental placement features")
    origin = "uploaded-board" if "board" in payload else "generated-board"
    return bool(supplied), origin


def _store_failure_traces(
    result: dict[str, Any],
    store: FailureTraceStore,
    *,
    input_origin: str,
) -> list[str]:
    traces = build_failure_traces(
        result,
        model_id=_placement_model_id(str(result.get("policy", ""))),
        input_origin=input_origin,
    )
    return [store.append(trace) for trace in traces]


def _placement_models(
    policy: str,
    gemini_factory: Callable[[], Any],
) -> tuple[Any | None, Any | None]:
    if policy == "gemini":
        return gemini_factory(), None
    if policy == "tinker":
        return build_tinker_model(), None
    if policy == "ollama":
        return build_ollama_model(), None
    if policy == "hybrid":
        return build_fast_placement_model(), gemini_factory()
    return None, None


def _run_placement_policy(
    payload: dict[str, Any],
    policy: str,
    gemini_factory: Callable[[], Any],
) -> dict[str, Any]:
    model, fallback_model = _placement_models(policy, gemini_factory)
    return repair_request(
        {**payload, "policy": policy},
        model=model,
        fallback_model=fallback_model,
    )


def _record_failure_trace_ids(
    payload: dict[str, Any],
    result: dict[str, Any],
    store: FailureTraceStore | None = None,
) -> list[str]:
    consent, input_origin = _trace_consent(payload)
    if not consent:
        return []
    attempted = result.get("policy_attempt")
    trace_result = attempted if isinstance(attempted, dict) else result
    selected_store = store or build_failure_trace_store()
    try:
        return _store_failure_traces(
            trace_result,
            selected_store,
            input_origin=input_origin,
        )
    except Exception as exc:
        # Training telemetry must never take down board repair.
        sys.stderr.write(
            "placement trace write failed: "
            f"{type(exc).__name__}: {exc}\n"
        )
        return []


def run_chat_orchestrator(**kwargs):
    """Load ADK only for the route that needs its LLM agent."""
    try:
        from silkscreen.agents.adk.orchestrator import run_orchestrator
    except ImportError as exc:
        raise RuntimeError(
            "the chat orchestrator needs the adk extra: pip install 'silkscreen[adk]'"
        ) from exc
    return run_orchestrator(**kwargs)


class _PacedModel:
    """Apply a pre-call hook to a non-fallback model injected into the service."""

    def __init__(self, model, before_attempt: Callable[[str], None]) -> None:
        self._model = model
        self._before_attempt = before_attempt

    @property
    def last_provider(self):
        return getattr(self._model, "last_provider", None)

    @property
    def last_model(self):
        return getattr(self._model, "last_model", None)

    def generate(self, prompt: str, **kwargs):
        self._before_attempt("worker")
        return self._model.generate(prompt, **kwargs)


def _with_request_pacing(model, before_attempt: Callable[[str], None]):
    """Pace every explicit fallback attempt, or one ordinary model call."""
    if isinstance(model, FallbackModel):
        return replace(model, before_attempt=before_attempt)
    return _PacedModel(model, before_attempt)


def generate(
    payload: dict[str, Any],
    *,
    model,
    store: FactStore,
    pages_store: FactStore | None = None,
    embedder_factory: Callable[[], Any] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    placement_policy: str = "deterministic",
    placement_model=None,
    placement_fallback_model=None,
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

    # An approved manifest is caller-owned input, so reject it before cache
    # reads, datasheet downloads, or a model call can spend time or quota.
    constraint_manifest = parse_constraint_manifest(payload.get("constraints"))
    model_intent = intent
    if constraint_manifest is not None:
        model_intent += constraint_manifest.prompt_block()

    # Enclosure opt-in, validated before any cache read or model call spends
    # time or quota -- field-level failures are the caller's to fix (the
    # known-issue-10 taxonomy: a plain 400, never a stream frame apology).
    enclosure_requested = payload.get("enclosure", False)
    if not isinstance(enclosure_requested, bool):
        raise ValueError("'enclosure' must be a boolean")
    enclosure_style = payload.get("enclosure_style", "")
    if not isinstance(enclosure_style, str):
        raise ValueError("'enclosure_style' must be a string")
    if len(enclosure_style) > MAX_ENCLOSURE_STYLE_CHARS:
        raise ValueError(
            "'enclosure_style' must be at most "
            f"{MAX_ENCLOSURE_STYLE_CHARS} characters"
        )
    enclosure_rigorous = payload.get("enclosure_rigorous", False)
    if not isinstance(enclosure_rigorous, bool):
        raise ValueError("'enclosure_rigorous' must be a boolean")

    datasheets = payload.get("datasheets") or {}
    if not isinstance(datasheets, dict):
        raise ValueError("'datasheets' must be an object of {part: url}")
    if any(not isinstance(u, str) or not u for u in datasheets.values()):
        raise ValueError("each datasheet value must be a non-empty URL string")
    order_opts = None
    if payload.get("order") is not None:
        # Validated here, before the pipeline spends a model call: a rejected
        # option is the caller's to fix and should cost them nothing.
        order_opts = order_options(payload["order"])

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

    no_solver_budget = payload.get("no_solver_budget", False)
    if not isinstance(no_solver_budget, bool):
        raise ValueError("'no_solver_budget' must be a boolean")
    if no_solver_budget:
        time_limit_s = None
    else:
        # Coerce here, not in the route handler: float() raises TypeError on a
        # JSON null and ValueError on a string, and both are this caller's error.
        # Naming the field keeps the route's except clause narrow, so a genuine
        # internal TypeError still surfaces as a 500 with an error id.
        try:
            time_limit_s = float(payload.get("time_limit_s", DEFAULT_TIME_LIMIT))
        except (TypeError, ValueError):
            raise ValueError("'time_limit_s' must be a number") from None

    placement_profile = payload.get("placement_profile")
    if placement_profile is not None and (
        not isinstance(placement_profile, str) or not placement_profile.strip()
    ):
        raise ValueError("'placement_profile' must be a non-empty profile name")
    placement_feedback = payload.get("placement_feedback")
    if placement_feedback is not None and not isinstance(placement_feedback, dict):
        raise ValueError("'placement_feedback' must be an object")
    placement_max_turns = payload.get("placement_max_turns", 8)
    if isinstance(placement_max_turns, bool) or not isinstance(
        placement_max_turns, int
    ):
        raise ValueError("'placement_max_turns' must be an integer")

    # Passed only when opted in, so a default request reaches generate_pcb
    # with the exact call it always made (and both drivers stay
    # event-identical by default, per the plan).
    enclosure_kwargs: dict[str, Any] = (
        {
            "enclosure": True,
            "enclosure_style": enclosure_style.strip(),
            # Fast by default; the strict repair loop is the caller's opt-in.
            "enclosure_rigorous": enclosure_rigorous,
        }
        if enclosure_requested
        else {}
    )

    result = generate_pcb(
        model,
        model_intent,
        datasheets=to_read,
        preloaded_facts=preloaded,
        time_limit_s=time_limit_s,
        review=bool(payload.get("review", True)),
        on_event=on_event,
        # Debugging a run means reading what the model actually said, so the
        # raw answers join the stream only when the caller asks for them.
        include_responses=bool(payload.get("debug", False)),
        placement_profile=placement_profile,
        placement_policy=placement_policy,
        placement_feedback=placement_feedback,
        placement_model=placement_model,
        placement_fallback_model=placement_fallback_model,
        placement_max_turns=placement_max_turns,
        **enclosure_kwargs,
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

    if constraint_manifest is not None:
        receipt = verify_constraint_manifest(
            constraint_manifest,
            result.spec,
            board,
            result.route,
        )
        checks = [
            check
            for group in receipt.get("net_classes", [])
            for check in group.get("checks", [])
        ]
        checks.extend(receipt.get("mechanical", []))
        counts = {
            status: sum(check.get("status") == status for check in checks)
            for status in ("verified", "violated", "unresolved")
        }

        response["constraint_manifest"] = constraint_manifest.to_dict()
        response["constraint_receipt"] = receipt
        # This is production-promotion eligibility metadata, not an artifact
        # gate. The generated KiCad board stays available in this response.
        response["promotion_status"] = (
            "constraint_passed" if receipt["promotable"] else "constraint_blocked"
        )
        response["blockers"].extend(
            f"constraint {item['scope']}/{item['name']}: {item['detail']}"
            for item in receipt["blockers"]
        )
        emit(
            {
                "event": "constraints.verify",
                "manifest_version": constraint_manifest.version,
                "hard_gate": receipt["hard_gate"],
                "promotable": receipt["promotable"],
                "blockers": len(receipt["blockers"]),
                "verified": counts["verified"],
                "violated": counts["violated"],
                "unresolved": counts["unresolved"],
                "artifact_available": True,
            }
        )

    if enclosure_requested:
        # Additive, and only when opted in. ``getattr`` rather than an
        # attribute read: enclosure failure inside the stage already means
        # ``None`` (board still delivered), and the visible warning keeps the
        # degradation honest in the one-shot response, where the
        # ``enclosure.failed`` stream event cannot be seen.
        response["enclosure"] = _enclosure_dict(getattr(result, "enclosure", None))
        if response["enclosure"] is None:
            response["warnings"].append(
                "enclosure generation failed; the board is delivered without a case"
            )

    if result.route is not None:
        response["routing"] = {
            "tracks": len(result.route.tracks),
            "vias": len(result.route.vias),
            "routed": list(result.route.routed),
            "unrouted": dict(result.route.unrouted),
            "warnings": list(result.route.warnings),
            "completion": round(result.route.completion, 4),
        }

    if result.placement is not None:
        placement = run_to_dict(result.placement.run, placement_feedback)
        placement["requested_policy"] = result.placement.requested_policy
        placement["applied"] = result.placement.applied
        placement["policy_fallback"] = result.placement.policy_fallback
        if result.placement.attempted_run is not None:
            placement["policy_attempt"] = run_to_dict(
                result.placement.attempted_run,
                placement_feedback,
            )
        response["placement_repair"] = placement

    if order_opts is not None:
        response["order"] = _order_block(board, result.spec, order_opts)

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
    """Same-origin chat, generation, placement repair, and built web UI."""

    model_factory = staticmethod(build_model)
    model_catalog_factory = staticmethod(model_catalog)
    configuration_status_factory = staticmethod(configuration_status)
    orchestrator_runner = staticmethod(run_chat_orchestrator)
    request_pacer: RequestPacer = GEMINI_REQUEST_PACER
    store: FactStore | None = None
    pages_store: FactStore | None = None
    failure_trace_store: FailureTraceStore | None = None
    embedder_factory = staticmethod(build_embedder)

    #: Root of the built bundle; None serves no static files at all.
    web_root: Path | None = WEB_DIST

    # There are deliberately no CORS headers and no do_OPTIONS: the bundle is
    # served from this same origin, so nothing the UI sends is cross-origin.
    # Adding them defensively would only widen who may call /generate.

    def _send(
        self,
        code: int,
        payload: dict[str, Any],
        *,
        cache_control: str | None = None,
    ) -> None:
        try:
            body = json.dumps(payload, allow_nan=False).encode()
        except (TypeError, ValueError) as exc:
            error_id = uuid.uuid4().hex[:12]
            sys.stderr.write(
                f"error {error_id}: response serialization failed: {exc}\n"
            )
            code = 500
            body = json.dumps(
                {"error": "internal error", "error_id": error_id}
            ).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if cache_control:
            self.send_header("Cache-Control", cache_control)
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

        if route == "/models":
            catalog = dict(self.model_catalog_factory())
            catalog["placement"] = {
                # The visible request toggle is the opt-in. Individual providers
                # remain unavailable until their server-side configuration exists.
                "experimental_enabled": True,
                "profiles": ["compact-control", "thermal-first"],
                "policies": placement_policy_status(experimental=True),
            }
            self._send(200, catalog)
            return

        if route == "/config/status":
            self._send(
                200,
                self.configuration_status_factory(),
                cache_control="no-store",
            )
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
        if self.path == "/placement/repair":
            self._placement_repair()
            return
        if self.path == "/chat/stream":
            self._chat_stream()
            return
        if self.path == "/generate/stream":
            self._generate_stream()
            return
        if self.path in ("/generate", "/"):
            self._generate_once()
            return
        self._send(404, {"error": f"no route {self.path}"})

    def _placement_repair(self) -> None:
        payload = self._read_payload()
        if payload is None:
            return
        try:
            experimental = _experimental_requested(payload)
            requested_policy = str(
                payload.get("policy", "deterministic")
            ).strip().lower()
            policy_status = placement_policy_status(experimental=experimental)
            policy = resolve_placement_policy(requested_policy, policy_status)
            quota_rpm = select_quota_rpm(payload.get("quota_rpm"))

            def gemini_factory():
                model = self.model_factory()
                if quota_rpm is not None:
                    model = _with_request_pacing(
                        model,
                        lambda _provider: self.request_pacer.wait(quota_rpm),
                    )
                return model

            try:
                result = _run_placement_policy(payload, policy, gemini_factory)
            except (OSError, PlacementPolicyError, TimeoutError):
                if requested_policy != "fast" or policy == "deterministic":
                    raise
                unavailable_policy = policy
                policy = "deterministic"
                result = _run_placement_policy(payload, policy, gemini_factory)
                result["policy_fallback"] = {
                    "from": unavailable_policy,
                    "to": policy,
                    "reason": "fast proposer unavailable",
                }
            if (
                requested_policy == "fast"
                and policy != "deterministic"
                and not result.get("completed")
            ):
                incomplete_policy = policy
                attempted = result
                policy = "deterministic"
                result = _run_placement_policy(payload, policy, gemini_factory)
                result["policy_attempt"] = attempted
                result["policy_fallback"] = {
                    "from": incomplete_policy,
                    "to": policy,
                    "reason": "fast proposer did not complete repair",
                }
            result["requested_policy"] = requested_policy
            result["available_policies"] = policy_status
            trace_ids = _record_failure_trace_ids(
                payload, result, self.failure_trace_store
            )
            result["failure_trace_ids"] = trace_ids
            result["failure_trace_count"] = len(trace_ids)
        except Exception as exc:
            status, body = _error_response(exc)
            self._send(status, body)
        else:
            self._send(200, result)

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
            payload = json.loads(
                self.rfile.read(length) or b"{}",
                parse_constant=_reject_nonfinite_json,
                parse_float=_parse_json_float,
            )
        except (json.JSONDecodeError, ValueError) as exc:
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
        quota_rpm: int | None = None,
    ) -> dict[str, Any]:
        """One pipeline run, wired to whatever this handler was injected with."""
        if quota_rpm is None:
            quota_rpm = select_quota_rpm(payload.get("quota_rpm"))
        store = self.store if self.store is not None else build_store()
        model = self.model_factory()
        if quota_rpm is not None:

            def before_attempt(provider: str) -> None:
                self.request_pacer.wait(
                    quota_rpm,
                    on_wait=lambda delay: on_event
                    and on_event(
                        {
                            "event": "quota.wait",
                            "layer": "worker",
                            "provider": provider,
                            "quota_rpm": quota_rpm,
                            "delay_s": round(delay, 3),
                        }
                    ),
                )

            model = _with_request_pacing(model, before_attempt)

        experimental = _experimental_requested(payload)
        trace_consent, _ = _trace_consent(payload)
        placement_profile = payload.get("placement_profile")
        placement_status = placement_policy_status(experimental=experimental)
        placement_policy = "deterministic"
        requested_placement_policy: str | None = None
        placement_model = None
        placement_fallback_model = None
        if placement_profile is not None:
            requested_placement_policy = str(
                payload.get("placement_policy", "deterministic")
            ).strip().lower()
            placement_policy = resolve_placement_policy(
                requested_placement_policy, placement_status
            )
            placement_model, placement_fallback_model = _placement_models(
                placement_policy, lambda: model
            )
        elif trace_consent:
            raise ValueError("record_trace requires placement_profile")

        result = generate(
            payload,
            model=model,
            store=store,
            pages_store=self.pages_store,
            embedder_factory=self.embedder_factory,
            on_event=on_event,
            placement_policy=placement_policy,
            placement_model=placement_model,
            placement_fallback_model=placement_fallback_model,
        )
        placement = result.get("placement_repair")
        if isinstance(placement, dict):
            if requested_placement_policy is not None:
                placement["requested_policy"] = requested_placement_policy
            placement["available_policies"] = placement_status
            placement["experimental_placement"] = experimental
            trace_ids = _record_failure_trace_ids(
                payload, placement, self.failure_trace_store
            )
            placement["failure_trace_ids"] = trace_ids
            placement["failure_trace_count"] = len(trace_ids)
        return result

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
                self.wfile.write(_json_line(event))
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

        try:
            emit({"event": "run.done", "result": result})
        except _ResponseSerializationError as exc:
            status, body = _error_response(exc)
            with contextlib.suppress(ConnectionError):
                emit({"event": "run.error", "status": status, **body})
        except ConnectionError:
            pass

    def _chat_stream(self) -> None:
        """One ADK orchestrator turn, with pipeline and model events inline."""
        payload = self._read_payload()
        if payload is None:
            return

        intent = payload.get("intent")
        clarification = payload.get("clarification", "")
        if not isinstance(intent, str) or not intent.strip():
            self._send(400, {"error": "'intent' is required"})
            return
        if not isinstance(clarification, str):
            self._send(400, {"error": "'clarification' must be a string"})
            return

        session_id = str(payload.get("session_id") or uuid.uuid4().hex)
        turn_id = str(payload.get("turn_id") or uuid.uuid4().hex[:12])
        if len(session_id) > 128 or len(turn_id) > 128:
            self._send(
                400,
                {"error": "session and turn ids must be at most 128 characters"},
            )
            return

        try:
            # Validate board constraints before even resolving the chat model.
            # generate() repeats this at the tool boundary so direct calls get
            # the same guarantee.
            parse_constraint_manifest(payload.get("constraints"))
            catalog = self.model_catalog_factory()
            orchestrator_model = select_model(payload.get("model"), catalog)
            thinking_level = select_thinking_level(payload.get("thinking_level"))
            quota_rpm = select_quota_rpm(payload.get("quota_rpm"))
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        started = time.monotonic()
        event_seq = 0
        gone = False

        def emit(event: dict[str, Any]) -> None:
            nonlocal event_seq, gone
            event_seq += 1
            frame = {
                "schema_version": 1,
                "session_id": session_id,
                "turn_id": turn_id,
                "event_id": f"e{event_seq}",
                **event,
            }
            frame.setdefault("t_s", round(time.monotonic() - started, 3))
            try:
                self.wfile.write(_json_line(frame))
                self.wfile.flush()
            except ConnectionError:
                gone = True
                raise

        def generate_board() -> dict[str, Any]:
            board_payload = {
                key: value
                for key, value in payload.items()
                if key
                not in {
                    "clarification",
                    "session_id",
                    "turn_id",
                    "model",
                    "thinking_level",
                    "quota_rpm",
                }
            }
            if clarification.strip():
                board_payload["intent"] = (
                    f"{intent.strip()}\n\nClarification: {clarification.strip()}"
                )
            return self._run(board_payload, on_event=emit, quota_rpm=quota_rpm)

        def pace_orchestrator() -> None:
            self.request_pacer.wait(
                quota_rpm,
                on_wait=lambda delay: emit(
                    {
                        "event": "quota.wait",
                        "layer": "orchestrator",
                        "model": orchestrator_model,
                        "quota_rpm": quota_rpm,
                        "delay_s": round(delay, 3),
                    }
                ),
            )

        try:
            emit(
                {
                    "event": "chat.accepted",
                    "layer": "orchestrator",
                    "model": orchestrator_model,
                    "thinking_level": thinking_level or "auto",
                    "quota_rpm": quota_rpm or "auto",
                }
            )
            outcome = self.orchestrator_runner(
                message=intent,
                clarification=clarification,
                model=orchestrator_model,
                thinking_level=thinking_level,
                session_id=session_id,
                generate=generate_board,
                emit=emit,
                debug=bool(payload.get("debug", False)),
                before_model_call=pace_orchestrator,
            )
        except Exception as exc:
            if gone:
                sys.stderr.write(f"stream client disconnected: {self.path}\n")
                return
            status, body = _error_response(exc)
            with contextlib.suppress(ConnectionError):
                emit({"event": "chat.error", "status": status, **body})
            return

        try:
            emit(
                {
                    "event": "chat.done",
                    "assistant": outcome.assistant,
                    "needs_clarification": outcome.needs_clarification,
                    "model": outcome.model,
                    "thinking_level": thinking_level or "auto",
                    "quota_rpm": quota_rpm or "auto",
                    "result": outcome.result,
                }
            )
        except _ResponseSerializationError as exc:
            status, body = _error_response(exc)
            with contextlib.suppress(ConnectionError):
                emit({"event": "chat.error", "status": status, **body})
        except ConnectionError:
            pass

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
