"""JSON in, testbench out.

The Python API is the real one, but an agent reaching this package through a
tool call has JSON and nothing else. This module is the bridge: plain dicts to
:class:`~silkscreen.spice.deck.Testbench` and
:class:`~silkscreen.spice.assertions.Assertion`, with the same
collect-every-error convention :func:`~silkscreen.netlist.parse_circuit_spec`
uses, so a malformed request comes back as one list of problems a model can
repair in a single pass rather than one problem per round trip.

Nothing here guesses. A missing ``stop`` on a transient is an error, not a
default, because a default would silently answer a different question than the
one asked.
"""

from __future__ import annotations

from typing import Any

from .assertions import Assertion
from .deck import (
    ACSweep,
    Analysis,
    DCSweep,
    OperatingPoint,
    PrimitiveModel,
    Source,
    SubcircuitModel,
    Testbench,
    Transient,
)
from .errors import DeckError
from .measure import MEASUREMENT_KINDS, Measurement

__all__ = [
    "analysis_from_dict",
    "source_from_dict",
    "testbench_from_dict",
    "measurement_from_dict",
    "assertion_from_dict",
    "assertions_from_dict",
    "REQUEST_SCHEMA",
]


def _number(data: dict, key: str, errors: list[str], *, where: str) -> float | None:
    if key not in data:
        errors.append(f"{where}: missing required field {key!r}")
        return None
    try:
        return float(data[key])
    except (TypeError, ValueError):
        errors.append(f"{where}: {key!r} must be a number, got {data[key]!r}")
        return None


def analysis_from_dict(data: dict[str, Any], errors: list[str]) -> Analysis | None:
    """``{"kind": "tran", "step": ..., "stop": ...}`` to an :class:`Analysis`."""
    kind = str(data.get("kind", "")).lower()
    where = f"analysis {kind!r}" if kind else "analysis"

    if kind == "op":
        return OperatingPoint()

    if kind == "tran":
        step = _number(data, "step", errors, where=where)
        stop = _number(data, "stop", errors, where=where)
        if step is None or stop is None:
            return None
        max_step = data.get("max_step")
        return Transient(
            step=step,
            stop=stop,
            start=float(data.get("start", 0.0)),
            max_step=float(max_step) if max_step is not None else None,
        )

    if kind == "ac":
        f_start = _number(data, "f_start", errors, where=where)
        f_stop = _number(data, "f_stop", errors, where=where)
        if f_start is None or f_stop is None:
            return None
        return ACSweep(
            f_start=f_start,
            f_stop=f_stop,
            points=int(data.get("points", 20)),
            sweep=str(data.get("sweep", "dec")),
        )

    if kind == "dc":
        source = data.get("source")
        start = _number(data, "start", errors, where=where)
        stop = _number(data, "stop", errors, where=where)
        step = _number(data, "step", errors, where=where)
        if not source:
            errors.append(f"{where}: missing required field 'source'")
        if source is None or start is None or stop is None or step is None:
            return None
        return DCSweep(source=str(source), start=start, stop=stop, step=step)

    errors.append(
        f"unknown analysis kind {data.get('kind')!r}; "
        f"known: 'op', 'tran', 'ac', 'dc'"
    )
    return None


def source_from_dict(data: dict[str, Any], errors: list[str]) -> Source | None:
    """One source. ``kind`` selects the stimulus shape; omit it for DC/AC."""
    name = str(data.get("name", ""))
    positive = str(data.get("positive", ""))
    negative = str(data.get("negative", ""))
    where = f"source {name!r}" if name else "source"

    for field_name, value in (
        ("name", name),
        ("positive", positive),
        ("negative", negative),
    ):
        if not value:
            errors.append(f"{where}: missing required field {field_name!r}")
    if not (name and positive and negative):
        return None

    kind = str(data.get("kind", "")).lower()

    if kind == "pulse":
        needed = ("initial", "pulsed", "width", "period")
        values = {k: _number(data, k, errors, where=where) for k in needed}
        if any(v is None for v in values.values()):
            return None
        return Source.pulse(
            name,
            positive,
            negative,
            initial=values["initial"],
            pulsed=values["pulsed"],
            width=values["width"],
            period=values["period"],
            delay=float(data.get("delay", 0.0)),
            rise=float(data.get("rise", 1e-9)),
            fall=float(data.get("fall", 1e-9)),
        )

    if kind == "sine":
        needed = ("offset", "amplitude", "frequency")
        values = {k: _number(data, k, errors, where=where) for k in needed}
        if any(v is None for v in values.values()):
            return None
        return Source.sine(
            name,
            positive,
            negative,
            offset=values["offset"],
            amplitude=values["amplitude"],
            frequency=values["frequency"],
            delay=float(data.get("delay", 0.0)),
        )

    if kind and kind not in ("dc", "ac", ""):
        errors.append(
            f"{where}: unknown source kind {kind!r}; "
            f"known: 'dc', 'ac', 'pulse', 'sine'"
        )
        return None

    dc = data.get("dc")
    ac = data.get("ac_magnitude", data.get("ac"))
    if dc is None and ac is None and not data.get("transient"):
        errors.append(
            f"{where}: needs at least one of 'dc', 'ac_magnitude' or a "
            f"'kind' of 'pulse'/'sine'"
        )
        return None
    return Source(
        name=name,
        positive=positive,
        negative=negative,
        dc=float(dc) if dc is not None else None,
        ac_magnitude=float(ac) if ac is not None else None,
        transient=str(data["transient"]) if data.get("transient") else None,
    )


def _model_from_dict(
    part: str, data: dict[str, Any], errors: list[str]
) -> PrimitiveModel | SubcircuitModel | None:
    kind = str(data.get("kind", "subckt")).lower()
    name = str(data.get("name", ""))
    text = str(data.get("text", ""))
    if not name or not text:
        errors.append(f"model for {part!r}: needs both 'name' and 'text'")
        return None
    if kind in ("subckt", "subcircuit"):
        pins = data.get("pins")
        if not isinstance(pins, list) or not pins:
            errors.append(
                f"model for {part!r}: a subcircuit needs 'pins', the device pin "
                f"names in the subcircuit's terminal order"
            )
            return None
        return SubcircuitModel(name=name, pins=tuple(str(p) for p in pins), text=text)
    if kind in ("model", "primitive"):
        return PrimitiveModel(name=name, text=text)
    errors.append(
        f"model for {part!r}: unknown kind {kind!r}; known: 'subckt', 'model'"
    )
    return None


def testbench_from_dict(data: dict[str, Any]) -> Testbench:
    """Build a :class:`Testbench`. Raises :class:`DeckError` listing every problem."""
    if not isinstance(data, dict):
        raise DeckError([f"testbench must be an object, got {type(data).__name__}"])

    errors: list[str] = []
    analysis_data = data.get("analysis")
    if not isinstance(analysis_data, dict):
        raise DeckError(
            ["testbench needs an 'analysis' object, e.g. {'kind': 'op'}"]
        )
    analysis = analysis_from_dict(analysis_data, errors)

    sources: list[Source] = []
    raw_sources = data.get("sources") or []
    if not isinstance(raw_sources, list):
        errors.append("'sources' must be a list")
        raw_sources = []
    for entry in raw_sources:
        if not isinstance(entry, dict):
            errors.append(f"each source must be an object, got {entry!r}")
            continue
        source = source_from_dict(entry, errors)
        if source is not None:
            sources.append(source)

    models: dict[str, PrimitiveModel | SubcircuitModel] = {}
    raw_models = data.get("models") or {}
    if not isinstance(raw_models, dict):
        errors.append("'models' must be an object keyed by part name")
        raw_models = {}
    for part, entry in raw_models.items():
        if not isinstance(entry, dict):
            errors.append(f"model for {part!r} must be an object")
            continue
        model = _model_from_dict(str(part), entry, errors)
        if model is not None:
            models[str(part)] = model

    if errors or analysis is None:
        raise DeckError(errors or ["could not build the analysis"])

    temperature = data.get("temperature_c")
    return Testbench(
        analysis=analysis,
        sources=sources,
        models=models,
        ground=str(data["ground"]) if data.get("ground") else None,
        probes=tuple(str(p) for p in (data.get("probes") or ())),
        temperature_c=float(temperature) if temperature is not None else None,
        options=tuple(str(o) for o in (data.get("options") or ())),
        strict=bool(data.get("strict", False)),
        title=str(data.get("title", "silkscreen simulation")),
    )


def measurement_from_dict(
    data: dict[str, Any], errors: list[str]
) -> Measurement | None:
    if not isinstance(data, dict):
        errors.append(f"measurement must be an object, got {data!r}")
        return None
    kind = str(data.get("kind", ""))
    signal = str(data.get("signal", ""))
    if kind not in MEASUREMENT_KINDS:
        errors.append(
            f"unknown measurement kind {kind!r}; known: {sorted(MEASUREMENT_KINDS)}"
        )
        return None
    if not signal:
        errors.append(f"measurement {kind!r}: missing 'signal'")
        return None

    window = data.get("window")
    if window is not None:
        if not isinstance(window, (list, tuple)) or len(window) != 2:
            errors.append(
                f"measurement {kind!r}: 'window' must be [start, end]"
            )
            return None
        window = (float(window[0]), float(window[1]))

    at = data.get("at")
    return Measurement(
        kind=kind,
        signal=signal,
        reference=str(data["reference"]) if data.get("reference") else None,
        window=window,
        at=float(at) if at is not None else None,
        low_pct=float(data.get("low_pct", 0.1)),
        high_pct=float(data.get("high_pct", 0.9)),
        tolerance=float(data.get("settle_tolerance", 0.02)),
    )


def assertion_from_dict(data: dict[str, Any], errors: list[str]) -> Assertion | None:
    if not isinstance(data, dict):
        errors.append(f"assertion must be an object, got {data!r}")
        return None
    name = str(data.get("name", "")) or "unnamed assertion"
    measurement = measurement_from_dict(data.get("measurement") or {}, errors)
    op = str(data.get("op", ""))
    if not op:
        errors.append(f"assertion {name!r}: missing 'op'")
    if "value" not in data:
        errors.append(f"assertion {name!r}: missing 'value'")
        return None
    try:
        value = float(data["value"])
    except (TypeError, ValueError):
        errors.append(f"assertion {name!r}: 'value' must be a number")
        return None
    if measurement is None or not op:
        return None
    return Assertion(
        name=name,
        measurement=measurement,
        op=op,
        value=value,
        tolerance=float(data.get("tolerance", 0.0)),
        absolute_tolerance=bool(data.get("absolute_tolerance", False)),
        unit=str(data.get("unit", "")),
    )


def assertions_from_dict(items: Any) -> list[Assertion]:
    """Build a whole specification. Raises :class:`DeckError` with every problem."""
    if items is None:
        return []
    if not isinstance(items, list):
        raise DeckError(["'assertions' must be a list"])
    errors: list[str] = []
    built: list[Assertion] = []
    for entry in items:
        assertion = assertion_from_dict(entry, errors)
        if assertion is not None:
            built.append(assertion)
    if errors:
        raise DeckError(errors)
    return built


#: JSON Schema for a simulation request, shared by the MCP tool and anything
#: else that wants to describe this interface to a model.
REQUEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "circuit": {
            "type": "object",
            "description": "A Silkscreen circuit: devices, passives, nets.",
        },
        "testbench": {
            "type": "object",
            "description": (
                "What to drive the circuit with and what to analyse. "
                "The circuit alone does not say."
            ),
            "properties": {
                "analysis": {
                    "type": "object",
                    "description": (
                        "{'kind':'op'} | {'kind':'tran','step':s,'stop':s} | "
                        "{'kind':'ac','f_start':Hz,'f_stop':Hz,'points':n} | "
                        "{'kind':'dc','source':'V1','start':v,'stop':v,'step':v}"
                    ),
                },
                "sources": {
                    "type": "array",
                    "description": (
                        "Each: name (must start with V or I), positive, "
                        "negative, and either dc / ac_magnitude or "
                        "kind='pulse' (initial, pulsed, width, period) or "
                        "kind='sine' (offset, amplitude, frequency)."
                    ),
                    "items": {"type": "object"},
                },
                "models": {
                    "type": "object",
                    "description": (
                        "Keyed by part name. A device (IC) has no behaviour in "
                        "the IR and cannot be simulated without one: "
                        "{'kind':'subckt','name':...,'pins':[...],'text':...}"
                    ),
                },
                "ground": {"type": "string"},
                "strict": {
                    "type": "boolean",
                    "description": "Treat warnings (e.g. a substituted generic "
                    "diode model) as errors.",
                },
            },
            "required": ["analysis"],
        },
        "assertions": {
            "type": "array",
            "description": (
                "Specification clauses. Each: name, measurement "
                "{kind, signal, window?, at?}, op (<, <=, >, >=, ==, within), "
                "value, tolerance?, unit?."
            ),
            "items": {"type": "object"},
        },
        "simulator": {
            "type": "string",
            "description": "'ngspice' or 'ltspice'; omit to auto-select.",
        },
        "timeout_s": {"type": "number"},
        "max_points": {
            "type": "integer",
            "description": "Downsampled waveform points to return per signal; "
            "0 (the default) returns summary statistics only.",
        },
    },
    "required": ["circuit", "testbench"],
}
