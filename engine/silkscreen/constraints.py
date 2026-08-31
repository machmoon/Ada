"""Validated net and routing constraints shared by every board stage.

The manifest is deliberately small. It records what the engineer approved
before generation without pretending that the current placer can verify trace
geometry that only exists after routing.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

MAX_NET_CLASSES = 24
MAX_NETS_PER_CLASS = 64
MAX_LAYERS_PER_CLASS = 16
MAX_TEXT = 160
MAX_LAYER_TRANSITIONS = 32
MAX_VIAS_PER_NET = 64
MAX_DISTANCE_MM = 10_000.0
MAX_FREQUENCY_HZ = 100_000_000_000.0


def _text(value: Any, field: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"'{field}' must be a string")
    cleaned = value.strip()
    if required and not cleaned:
        raise ValueError(f"'{field}' cannot be empty")
    if len(cleaned) > MAX_TEXT:
        raise ValueError(f"'{field}' is too long")
    return cleaned


def _strings(value: Any, field: str, limit: int) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"'{field}' must be a non-empty array")
    if len(value) > limit:
        raise ValueError(f"'{field}' has too many entries")
    cleaned = tuple(_text(item, field) for item in value)
    if len(set(cleaned)) != len(cleaned):
        raise ValueError(f"'{field}' cannot contain duplicates")
    return cleaned


def _optional_number(
    value: Any,
    field: str,
    *,
    low: float,
    high: float,
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"'{field}' must be a number or null")
    number = float(value)
    if not math.isfinite(number) or number < low or number > high:
        raise ValueError(f"'{field}' is outside the supported range")
    return number


def _bounded_int(value: Any, field: str, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"'{field}' must be an integer")
    if value < 0 or value > high:
        raise ValueError(f"'{field}' is outside the supported range")
    return value


def _switch(raw: dict[str, Any], field: str) -> bool:
    value = raw.get(field, False)
    if not isinstance(value, bool):
        raise ValueError(f"'{field}' must be a boolean")
    return value


def _required_when(enabled: bool, value: float | None, field: str) -> float | None:
    if enabled and value is None:
        raise ValueError(f"enabled constraint requires an approved '{field}'")
    return value


def _concerns(raw: dict[str, Any]) -> tuple[str, ...]:
    values = raw.get("concerns", [])
    if not isinstance(values, list) or len(values) > 16:
        raise ValueError("'concerns' must be an array with at most 16 entries")
    return tuple(_text(item, "concerns") for item in values)


def _board_layers(raw: dict[str, Any]) -> int:
    value = raw.get("board_layers")
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 32:
        raise ValueError("'board_layers' must be an integer between 1 and 32")
    return value


def _net_classes(raw: dict[str, Any]) -> tuple[NetClassConstraint, ...]:
    values = raw.get("net_classes")
    if not isinstance(values, list) or not values:
        raise ValueError("'net_classes' must be a non-empty array")
    if len(values) > MAX_NET_CLASSES:
        raise ValueError(f"at most {MAX_NET_CLASSES} net classes are supported")
    parsed = tuple(NetClassConstraint.from_dict(item) for item in values)
    if len({item.name for item in parsed}) != len(parsed):
        raise ValueError("net-class names must be unique")
    return parsed


@dataclass(frozen=True)
class NetClassConstraint:
    name: str
    kind: str
    nets: tuple[str, ...]
    allowed_layers: tuple[str, ...]
    max_layer_transitions: int
    max_vias_per_net: int
    pullups_required: bool = False
    pullup_voltage_v: float | None = None
    controlled_impedance: bool = False
    impedance_ohms: float | None = None
    min_trace_width_mm: float | None = None
    max_length_mm: float | None = None
    max_skew_mm: float | None = None
    max_frequency_hz: float | None = None
    concerns: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: Any) -> NetClassConstraint:
        if not isinstance(raw, dict):
            raise ValueError("each net class must be an object")
        pullups = _switch(raw, "pullups_required")
        controlled = _switch(raw, "controlled_impedance")
        impedance = _required_when(
            controlled,
            _optional_number(
                raw.get("impedance_ohms"), "impedance_ohms", low=1.0, high=1_000.0
            ),
            "impedance_ohms",
        )
        pullup_voltage = _required_when(
            pullups,
            _optional_number(
                raw.get("pullup_voltage_v"),
                "pullup_voltage_v",
                low=0.1,
                high=1_000.0,
            ),
            "pullup_voltage_v",
        )
        return cls(
            name=_text(raw.get("name"), "name"),
            kind=_text(raw.get("kind", "signal"), "kind"),
            nets=_strings(raw.get("nets"), "nets", MAX_NETS_PER_CLASS),
            allowed_layers=_strings(
                raw.get("allowed_layers"), "allowed_layers", MAX_LAYERS_PER_CLASS
            ),
            max_layer_transitions=_bounded_int(
                raw.get("max_layer_transitions"),
                "max_layer_transitions",
                MAX_LAYER_TRANSITIONS,
            ),
            max_vias_per_net=_bounded_int(
                raw.get("max_vias_per_net"), "max_vias_per_net", MAX_VIAS_PER_NET
            ),
            pullups_required=pullups,
            pullup_voltage_v=pullup_voltage,
            controlled_impedance=controlled,
            impedance_ohms=impedance,
            min_trace_width_mm=_optional_number(
                raw.get("min_trace_width_mm"),
                "min_trace_width_mm",
                low=0.01,
                high=MAX_DISTANCE_MM,
            ),
            max_length_mm=_optional_number(
                raw.get("max_length_mm"),
                "max_length_mm",
                low=0.01,
                high=MAX_DISTANCE_MM,
            ),
            max_skew_mm=_optional_number(
                raw.get("max_skew_mm"),
                "max_skew_mm",
                low=0.0,
                high=MAX_DISTANCE_MM,
            ),
            max_frequency_hz=_optional_number(
                raw.get("max_frequency_hz"),
                "max_frequency_hz",
                low=1.0,
                high=MAX_FREQUENCY_HZ,
            ),
            concerns=_concerns(raw),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "nets": list(self.nets),
            "allowed_layers": list(self.allowed_layers),
            "max_layer_transitions": self.max_layer_transitions,
            "max_vias_per_net": self.max_vias_per_net,
            "pullups_required": self.pullups_required,
            "pullup_voltage_v": self.pullup_voltage_v,
            "controlled_impedance": self.controlled_impedance,
            "impedance_ohms": self.impedance_ohms,
            "min_trace_width_mm": self.min_trace_width_mm,
            "max_length_mm": self.max_length_mm,
            "max_skew_mm": self.max_skew_mm,
            "max_frequency_hz": self.max_frequency_hz,
            "concerns": list(self.concerns),
        }


@dataclass(frozen=True)
class ConstraintManifest:
    board_layers: int
    net_classes: tuple[NetClassConstraint, ...]
    version: int = 1
    approved: bool = True

    @classmethod
    def from_dict(cls, raw: Any) -> ConstraintManifest:
        if not isinstance(raw, dict):
            raise ValueError("'constraints' must be an object")
        if raw.get("version", 1) != 1:
            raise ValueError("unsupported constraint manifest version")
        if raw.get("approved") is not True:
            raise ValueError("constraints must be approved before build")
        return cls(board_layers=_board_layers(raw), net_classes=_net_classes(raw))

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "approved": self.approved,
            "board_layers": self.board_layers,
            "net_classes": [item.to_dict() for item in self.net_classes],
        }

    def prompt_block(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return (
            "\n\nAPPROVED NET AND ROUTING CONSTRAINT MANIFEST\n"
            "Treat connectivity and every declared limit as a hard requirement. "
            "Do not invent values for null fields. Preserve exact net names.\n"
            f"{payload}"
        )

    def receipt(self, generated_nets: list[str]) -> dict[str, Any]:
        available = set(generated_nets)
        checks = []
        overall = "verified"
        for item in self.net_classes:
            missing = [net for net in item.nets if net not in available]
            net_status = "violated" if missing else "verified"
            if missing:
                overall = "violated"
            checks.append(
                {
                    "net_class": item.name,
                    "net_presence": net_status,
                    "missing_nets": missing,
                    "pullups": "not_checked"
                    if item.pullups_required
                    else "not_required",
                    "routing": "not_checked",
                    "note": (
                        "Routing limits require routed copper and KiCad DRC."
                        if not missing
                        else "The generated circuit omitted approved net names."
                    ),
                }
            )
        return {"overall": overall, "checks": checks}


def parse_constraint_manifest(raw: Any) -> ConstraintManifest | None:
    """Parse an optional manifest while keeping old request bodies valid."""
    if raw is None:
        return None
    return ConstraintManifest.from_dict(raw)
