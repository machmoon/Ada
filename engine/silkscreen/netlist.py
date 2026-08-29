"""Validated intermediate representation for a circuit.

The original pipeline passed raw ``json.loads`` output from an LLM directly into
SKiDL part construction. Every downstream bug traced back to that: unvalidated
pin numbers, component types outside the supported set, references to nets that
were never created, and silently dropped connections.

This module is the contract. An LLM proposes a :class:`CircuitSpec`; nothing
touches KiCad until it validates. Failures name the offending field so they can
be fed back to the model for repair rather than crashing a worker thread.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "PassiveType",
    "Passive",
    "Device",
    "Connection",
    "CircuitSpec",
    "ValidationError",
    "parse_circuit_spec",
]

#: Reference designator prefixes KiCad expects for each passive type.
_REF_PREFIX = {
    "resistor": "R",
    "capacitor": "C",
    "inductor": "L",
    "diode": "D",
    "crystal": "Y",
}

_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_./+-]*$")


class ValidationError(ValueError):
    """Raised when a proposed circuit is not internally consistent.

    ``errors`` holds one human-readable message per problem so the whole batch
    can be returned to a model in a single repair prompt.
    """

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(
            f"{len(errors)} problem(s) in circuit spec:\n  - "
            + "\n  - ".join(errors)
        )


class PassiveType(StrEnum):
    RESISTOR = "resistor"
    CAPACITOR = "capacitor"
    INDUCTOR = "inductor"
    DIODE = "diode"
    CRYSTAL = "crystal"


@dataclass(frozen=True)
class Passive:
    """A two-terminal auxiliary part. Pins are always 1 and 2."""

    name: str
    type: PassiveType
    value: str

    @property
    def ref_prefix(self) -> str:
        return _REF_PREFIX[self.type.value]


@dataclass(frozen=True)
class Device:
    """A main IC.

    ``pins`` maps a datasheet pin *name* (e.g. ``"AVDD"``) to its physical pin
    number. The original code let the model invent both sides of this mapping
    and never checked the numbers against the symbol it actually instantiated.
    """

    name: str
    pins: dict[str, str]
    #: Resolved KiCad symbol, e.g. ``"MCU_ST_STM32F0:STM32F030C8Tx"``. Left
    #: unset until symbol resolution runs, so this module stays KiCad-free.
    symbol: str | None = None

    def pin_names(self) -> set[str]:
        return set(self.pins)


@dataclass(frozen=True)
class Connection:
    """One electrical net: a named node joined to a set of endpoints.

    An endpoint is either ``"<device>.<pin_name>"`` or ``"<passive>.1"`` /
    ``"<passive>.2"``. Requiring an explicit terminal is the fix for the
    original's inability to express "cap leg 1 to AVDD, leg 2 to AVSS" -- it
    only ever connected whole parts to nets, never specific pins.
    """

    net: str
    endpoints: tuple[str, ...]


@dataclass
class CircuitSpec:
    devices: list[Device] = field(default_factory=list)
    passives: list[Passive] = field(default_factory=list)
    connections: list[Connection] = field(default_factory=list)

    def validate(self) -> None:
        """Check internal consistency. Raises :class:`ValidationError`."""
        errors: list[str] = []

        device_by_name = {d.name: d for d in self.devices}
        passive_by_name = {p.name: p for p in self.passives}

        for name in list(device_by_name) + list(passive_by_name):
            if not _IDENT_RE.match(name):
                errors.append(f"part name {name!r} is not a valid identifier")

        dupes = set(device_by_name) & set(passive_by_name)
        for name in sorted(dupes):
            errors.append(f"{name!r} is declared as both a device and a passive")

        if len(device_by_name) != len(self.devices):
            errors.append("duplicate device names")
        if len(passive_by_name) != len(self.passives):
            errors.append("duplicate passive names")

        seen_nets: set[str] = set()
        for conn in self.connections:
            if conn.net in seen_nets:
                errors.append(f"net {conn.net!r} is declared more than once")
            seen_nets.add(conn.net)

            if len(conn.endpoints) < 2:
                errors.append(
                    f"net {conn.net!r} has {len(conn.endpoints)} endpoint(s); "
                    f"a net joining fewer than 2 pins is not a connection"
                )

            for ep in conn.endpoints:
                if "." not in ep:
                    errors.append(
                        f"net {conn.net!r}: endpoint {ep!r} must be "
                        f"'<part>.<pin>', not a bare part name"
                    )
                    continue
                part, _, pin = ep.partition(".")
                if part in device_by_name:
                    dev = device_by_name[part]
                    if pin not in dev.pins:
                        errors.append(
                            f"net {conn.net!r}: {part!r} has no pin named "
                            f"{pin!r} (known: {sorted(dev.pins)[:8]}...)"
                        )
                elif part in passive_by_name:
                    if pin not in ("1", "2"):
                        errors.append(
                            f"net {conn.net!r}: passive {part!r} has only "
                            f"pins 1 and 2, got {pin!r}"
                        )
                else:
                    errors.append(
                        f"net {conn.net!r}: endpoint {ep!r} refers to unknown "
                        f"part {part!r}"
                    )

        # A passive wired on only one leg is almost always a model error, and
        # the original silently emitted these as floating parts.
        for passive in self.passives:
            legs = {
                ep.partition(".")[2]
                for conn in self.connections
                for ep in conn.endpoints
                if ep.partition(".")[0] == passive.name
            }
            missing = {"1", "2"} - legs
            if missing:
                errors.append(
                    f"passive {passive.name!r} has no connection on pin(s) "
                    f"{sorted(missing)}; it would be left floating"
                )

        if errors:
            raise ValidationError(errors)

    def part_count(self) -> int:
        return len(self.devices) + len(self.passives)

    def net_count(self) -> int:
        return len(self.connections)


def _strip_code_fence(text: str) -> str:
    """Remove a ``` fence if the model wrapped its JSON in one.

    The original prompt's own example output was fenced, so fenced responses
    were both likely and fatal -- ``json.loads`` raised inside a worker thread
    with no handler.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_circuit_spec(raw: str | dict) -> CircuitSpec:
    """Parse and validate model output into a :class:`CircuitSpec`.

    Accepts either a already-decoded dict or raw model text, tolerating a
    Markdown code fence. Raises :class:`ValidationError` with every problem
    collected, so a repair prompt can address them all at once.
    """
    if isinstance(raw, str):
        try:
            data = json.loads(_strip_code_fence(raw))
        except json.JSONDecodeError as exc:
            raise ValidationError([f"response is not valid JSON: {exc}"]) from exc
    else:
        data = raw

    if not isinstance(data, dict):
        raise ValidationError([f"expected a JSON object, got {type(data).__name__}"])

    errors: list[str] = []

    devices: list[Device] = []
    for name, spec in (data.get("devices") or {}).items():
        if not isinstance(spec, dict) or not isinstance(spec.get("pins"), dict):
            errors.append(f"device {name!r} must have a 'pins' object")
            continue
        devices.append(
            Device(
                name=name,
                pins={str(k): str(v) for k, v in spec["pins"].items()},
                symbol=spec.get("symbol"),
            )
        )

    passives: list[Passive] = []
    for name, spec in (data.get("passives") or {}).items():
        if not isinstance(spec, dict):
            errors.append(f"passive {name!r} must be an object")
            continue
        try:
            ptype = PassiveType(str(spec.get("type", "")).lower())
        except ValueError:
            errors.append(
                f"passive {name!r} has unsupported type {spec.get('type')!r}; "
                f"allowed: {[t.value for t in PassiveType]}"
            )
            continue
        passives.append(
            Passive(name=name, type=ptype, value=str(spec.get("value", "")))
        )

    connections: list[Connection] = []
    for net, endpoints in (data.get("nets") or {}).items():
        if not isinstance(endpoints, list):
            errors.append(f"net {net!r} must map to a list of endpoints")
            continue
        connections.append(
            Connection(net=str(net), endpoints=tuple(str(e) for e in endpoints))
        )

    if errors:
        raise ValidationError(errors)

    spec = CircuitSpec(devices=devices, passives=passives, connections=connections)
    spec.validate()
    return spec
