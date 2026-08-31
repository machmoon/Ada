"""Pure-stdlib PCB placement geometry and deterministic repair.

Coordinates and dimensions are millimetres. Components use their lower-left
corner, which keeps geometry explicit and makes the JSON surface easy to audit.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

__all__ = [
    "Board",
    "CompanyProfile",
    "Component",
    "Keepout",
    "PlacementAction",
    "Violation",
    "apply_actions",
    "demo_board",
    "evaluate",
    "get_profile",
    "parse_actions",
    "repair",
]


def _require_finite(owner: str, **values: float) -> None:
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{owner} {name} must be a finite number")
        if not math.isfinite(float(value)):
            raise ValueError(f"{owner} {name} must be a finite number")


@dataclass(frozen=True)
class Component:
    ref: str
    x: float
    y: float
    width: float
    height: float
    angle: int = 0
    fixed: bool = False
    kind: str = "component"

    def __post_init__(self) -> None:
        if not self.ref.strip():
            raise ValueError("component ref cannot be empty")
        _require_finite(
            f"component {self.ref!r}",
            x=self.x,
            y=self.y,
            width=self.width,
            height=self.height,
        )
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"component {self.ref!r} has a non-positive size")
        if self.angle not in (0, 90, 180, 270):
            raise ValueError("component angle must be 0, 90, 180, or 270")

    @property
    def size(self) -> tuple[float, float]:
        if self.angle in (90, 270):
            return self.height, self.width
        return self.width, self.height

    @property
    def centre(self) -> tuple[float, float]:
        width, height = self.size
        return self.x + width / 2, self.y + height / 2

    def rect(self, pad: float = 0.0) -> tuple[float, float, float, float]:
        width, height = self.size
        return (
            self.x - pad,
            self.y - pad,
            self.x + width + pad,
            self.y + height + pad,
        )


@dataclass(frozen=True)
class Keepout:
    name: str
    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        _require_finite(
            f"keepout {self.name!r}",
            x=self.x,
            y=self.y,
            width=self.width,
            height=self.height,
        )
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"keepout {self.name!r} has a non-positive size")

    @property
    def rect(self) -> tuple[float, float, float, float]:
        return self.x, self.y, self.x + self.width, self.y + self.height


@dataclass(frozen=True)
class CompanyProfile:
    name: str
    clearance: float = 0.5
    edge_margin: float = 0.5
    fixed_refs: tuple[str, ...] = ()
    edge_refs: tuple[str, ...] = ()
    groups: tuple[tuple[str, ...], ...] = ()
    thermal_pairs: tuple[tuple[str, str, float], ...] = ()
    compactness_weight: float = 1.0
    grouping_weight: float = 1.0
    connector_edge_weight: float = 1.0
    thermal_weight: float = 1.0

    def __post_init__(self) -> None:
        _require_finite(
            "profile",
            clearance=self.clearance,
            edge_margin=self.edge_margin,
            compactness_weight=self.compactness_weight,
            grouping_weight=self.grouping_weight,
            connector_edge_weight=self.connector_edge_weight,
            thermal_weight=self.thermal_weight,
        )
        if self.clearance < 0 or self.edge_margin < 0:
            raise ValueError("profile clearance and edge margin cannot be negative")
        for weight in (
            self.compactness_weight,
            self.grouping_weight,
            self.connector_edge_weight,
            self.thermal_weight,
        ):
            if weight < 0:
                raise ValueError("profile weights cannot be negative")
        for left, right, minimum in self.thermal_pairs:
            _require_finite(
                f"thermal pair {left!r}/{right!r}", minimum=minimum
            )
            if minimum <= 0:
                raise ValueError("thermal pair distance must be positive")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CompanyProfile:
        thermal = tuple(
            (str(item[0]), str(item[1]), float(item[2]))
            for item in value.get("thermal_pairs", [])
            if isinstance(item, (list, tuple)) and len(item) == 3
        )
        return cls(
            name=str(value.get("name") or "custom"),
            clearance=float(value.get("clearance", 0.5)),
            edge_margin=float(value.get("edge_margin", 0.5)),
            fixed_refs=tuple(map(str, value.get("fixed_refs", []))),
            edge_refs=tuple(map(str, value.get("edge_refs", []))),
            groups=tuple(tuple(map(str, group)) for group in value.get("groups", [])),
            thermal_pairs=thermal,
            compactness_weight=float(value.get("compactness_weight", 1.0)),
            grouping_weight=float(value.get("grouping_weight", 1.0)),
            connector_edge_weight=float(value.get("connector_edge_weight", 1.0)),
            thermal_weight=float(value.get("thermal_weight", 1.0)),
        )


@dataclass(frozen=True)
class Board:
    width: float
    height: float
    components: tuple[Component, ...]
    keepouts: tuple[Keepout, ...] = ()

    def __post_init__(self) -> None:
        _require_finite("board", width=self.width, height=self.height)
        if self.width <= 0 or self.height <= 0:
            raise ValueError("board dimensions must be positive")
        refs = [component.ref for component in self.components]
        if len(refs) != len(set(refs)):
            raise ValueError("component refs must be unique")

    def component(self, ref: str) -> Component | None:
        return next(
            (component for component in self.components if component.ref == ref),
            None,
        )

    def replace_component(self, updated: Component) -> Board:
        return replace(
            self,
            components=tuple(
                updated if component.ref == updated.ref else component
                for component in self.components
            ),
        )


@dataclass(frozen=True)
class Violation:
    kind: str
    refs: tuple[str, ...]
    depth: float
    message: str


@dataclass(frozen=True)
class PlacementAction:
    kind: str
    ref: str
    x: float
    y: float
    angle: int | None = None

    def __post_init__(self) -> None:
        _require_finite("placement action", x=self.x, y=self.y)
        if self.angle is not None and self.angle not in (0, 90, 180, 270):
            raise ValueError("placement action angle must be 0, 90, 180, or 270")

    def as_text(self) -> str:
        suffix = f" {self.angle}" if self.angle is not None else ""
        return f"{self.kind} {self.ref} {self.x:.3f} {self.y:.3f}{suffix}"


@dataclass(frozen=True)
class Evaluation:
    hard: float
    soft: float
    total: float
    violations: tuple[Violation, ...] = field(default_factory=tuple)
    terms: dict[str, float] = field(default_factory=dict)


def _intersection_depth(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    overlap_x = min(first[2], second[2]) - max(first[0], second[0])
    overlap_y = min(first[3], second[3]) - max(first[1], second[1])
    if overlap_x <= 0 or overlap_y <= 0:
        return 0.0
    return min(overlap_x, overlap_y)


def _hard_violations(board: Board, profile: CompanyProfile) -> list[Violation]:
    violations: list[Violation] = []
    margin = profile.edge_margin
    clearance_pad = profile.clearance / 2

    for component in board.components:
        x1, y1, x2, y2 = component.rect()
        depth = sum(
            (
                max(0.0, margin - x1),
                max(0.0, margin - y1),
                max(0.0, x2 - (board.width - margin)),
                max(0.0, y2 - (board.height - margin)),
            )
        )
        if depth > 0:
            violations.append(
                Violation(
                    "boundary",
                    (component.ref,),
                    depth,
                    f"{component.ref} crosses the board margin by {depth:.3f} mm",
                )
            )

    for index, first in enumerate(board.components):
        for second in board.components[index + 1 :]:
            depth = _intersection_depth(
                first.rect(clearance_pad), second.rect(clearance_pad)
            )
            if depth > 0:
                violations.append(
                    Violation(
                        "clearance",
                        tuple(sorted((first.ref, second.ref))),
                        depth,
                        (
                            f"{first.ref} and {second.ref} violate clearance "
                            f"by {depth:.3f} mm"
                        ),
                    )
                )

    for component in board.components:
        for keepout in board.keepouts:
            depth = _intersection_depth(component.rect(clearance_pad), keepout.rect)
            if depth > 0:
                violations.append(
                    Violation(
                        "keepout",
                        (component.ref,),
                        depth,
                        (
                            f"{component.ref} enters keepout {keepout.name} "
                            f"by {depth:.3f} mm"
                        ),
                    )
                )
    return violations


def _grouping_cost(
    by_ref: dict[str, Component], groups: tuple[tuple[str, ...], ...], diagonal: float
) -> float:
    distances: list[float] = []
    for group in groups:
        present = [by_ref[ref] for ref in group if ref in by_ref]
        for index, first in enumerate(present):
            for second in present[index + 1 :]:
                distances.append(math.dist(first.centre, second.centre) / diagonal)
    return sum(distances) / len(distances) if distances else 0.0


def _connector_edge_cost(
    board: Board,
    by_ref: dict[str, Component],
    edge_refs: tuple[str, ...],
    diagonal: float,
) -> float:
    distances: list[float] = []
    for ref in edge_refs:
        component = by_ref.get(ref)
        if component is None:
            continue
        x, y = component.centre
        distances.append(min(x, y, board.width - x, board.height - y) / diagonal)
    return sum(distances) / len(distances) if distances else 0.0


def _compactness_cost(board: Board) -> float:
    if not board.components:
        return 0.0
    rects = [component.rect() for component in board.components]
    width = max(rect[2] for rect in rects) - min(rect[0] for rect in rects)
    height = max(rect[3] for rect in rects) - min(rect[1] for rect in rects)
    return (width * height) / (board.width * board.height)


def _thermal_cost(
    by_ref: dict[str, Component],
    pairs: tuple[tuple[str, str, float], ...],
) -> float:
    penalties: list[float] = []
    for left, right, minimum in pairs:
        first, second = by_ref.get(left), by_ref.get(right)
        if first is None or second is None or minimum <= 0:
            continue
        distance = math.dist(first.centre, second.centre)
        penalties.append(max(0.0, minimum - distance) / minimum)
    return sum(penalties) / len(penalties) if penalties else 0.0


def _soft_terms(board: Board, profile: CompanyProfile) -> dict[str, float]:
    by_ref = {component.ref: component for component in board.components}
    diagonal = math.hypot(board.width, board.height) or 1.0

    return {
        "compactness": _compactness_cost(board) * profile.compactness_weight,
        "grouping": _grouping_cost(by_ref, profile.groups, diagonal)
        * profile.grouping_weight,
        "connector_edge": _connector_edge_cost(
            board, by_ref, profile.edge_refs, diagonal
        )
        * profile.connector_edge_weight,
        "thermal_separation": _thermal_cost(by_ref, profile.thermal_pairs)
        * profile.thermal_weight,
    }


def evaluate(board: Board, profile: CompanyProfile) -> Evaluation:
    violations = tuple(_hard_violations(board, profile))
    hard = sum(violation.depth for violation in violations)
    terms = _soft_terms(board, profile)
    soft = sum(terms.values())
    return Evaluation(
        hard=round(hard, 6),
        soft=round(soft, 6),
        total=round(hard * 1000 + soft, 6),
        violations=violations,
        terms={name: round(value, 6) for name, value in terms.items()},
    )


_ACTION_RE = re.compile(
    r"^\s*(PLACE|MOVE)\s+([A-Za-z][A-Za-z0-9_.-]*)\s+"
    r"(-?(?:\d+(?:\.\d*)?|\.\d+))\s+"
    r"(-?(?:\d+(?:\.\d*)?|\.\d+))"
    r"(?:\s+(0|90|180|270))?\s*$",
    re.IGNORECASE,
)


def parse_actions(text: str) -> list[PlacementAction]:
    actions: list[PlacementAction] = []
    for line in str(text).splitlines():
        match = _ACTION_RE.match(line)
        if not match:
            continue
        kind, ref, x, y, angle = match.groups()
        actions.append(
            PlacementAction(
                kind=kind.upper(),
                ref=ref,
                x=float(x),
                y=float(y),
                angle=int(angle) if angle is not None else None,
            )
        )
    return actions


def apply_actions(
    board: Board,
    actions: Iterable[PlacementAction],
    profile: CompanyProfile,
) -> Board:
    current = board
    fixed = set(profile.fixed_refs)
    for action in actions:
        component = current.component(action.ref)
        if component is None or component.fixed or component.ref in fixed:
            continue
        if action.kind == "MOVE":
            x, y = component.x + action.x, component.y + action.y
        else:
            x, y = action.x, action.y
        angle = component.angle if action.angle is None else action.angle
        current = current.replace_component(replace(component, x=x, y=y, angle=angle))
    return current


def _candidate_positions(
    board: Board, component: Component, profile: CompanyProfile, grid: float
) -> Iterable[tuple[float, float]]:
    width, height = component.size
    max_x = board.width - profile.edge_margin - width
    max_y = board.height - profile.edge_margin - height
    x = profile.edge_margin
    while x <= max_x + 1e-9:
        y = profile.edge_margin
        while y <= max_y + 1e-9:
            yield round(x, 6), round(y, 6)
            y += grid
        x += grid


def _best_move(
    board: Board,
    profile: CompanyProfile,
    refs: Iterable[str],
    *,
    grid: float,
) -> tuple[Board, PlacementAction] | None:
    baseline = evaluate(board, profile)
    best_key = (baseline.hard, baseline.soft)
    best: tuple[Board, PlacementAction] | None = None
    fixed = set(profile.fixed_refs)
    for ref in sorted(set(refs)):
        component = board.component(ref)
        if component is None or component.fixed or ref in fixed:
            continue
        for x, y in _candidate_positions(board, component, profile, grid):
            if math.isclose(x, component.x) and math.isclose(y, component.y):
                continue
            candidate = board.replace_component(replace(component, x=x, y=y))
            result = evaluate(candidate, profile)
            key = (result.hard, result.soft)
            if key < best_key:
                best_key = key
                best = candidate, PlacementAction("PLACE", ref, x, y, component.angle)
    return best


def _repair_violations(
    board: Board,
    profile: CompanyProfile,
    actions: list[PlacementAction],
    *,
    max_steps: int,
    grid: float,
) -> Board:
    current = board
    for _ in range(max_steps):
        result = evaluate(current, profile)
        if result.hard == 0:
            return current
        refs = [ref for violation in result.violations for ref in violation.refs]
        move = _best_move(current, profile, refs, grid=grid)
        if move is None:
            return current
        current, action = move
        actions.append(action)
    return current


def _optimize_preferences(
    board: Board,
    profile: CompanyProfile,
    actions: list[PlacementAction],
    *,
    preference_steps: int,
    grid: float,
) -> Board:
    current = board
    for _ in range(preference_steps):
        if evaluate(current, profile).hard != 0:
            return current
        refs = (component.ref for component in current.components)
        move = _best_move(current, profile, refs, grid=grid)
        if move is None or evaluate(move[0], profile).hard != 0:
            return current
        current, action = move
        actions.append(action)
    return current


def repair(
    board: Board,
    profile: CompanyProfile,
    *,
    max_steps: int = 20,
    preference_steps: int = 6,
    grid: float = 1.0,
) -> tuple[Board, list[PlacementAction]]:
    """Repair hard violations, then coordinate-descent on profile preferences."""
    _require_finite("repair", grid=grid)
    if max_steps < 0 or preference_steps < 0 or grid <= 0:
        raise ValueError(
            "repair budgets must be non-negative and grid must be positive"
        )
    actions: list[PlacementAction] = []
    current = _repair_violations(
        board, profile, actions, max_steps=max_steps, grid=grid
    )
    current = _optimize_preferences(
        current,
        profile,
        actions,
        preference_steps=preference_steps,
        grid=grid,
    )
    return current, actions


_PROFILES = {
    "compact-control": CompanyProfile(
        name="Compact Control",
        clearance=0.6,
        edge_margin=0.8,
        fixed_refs=("J1",),
        edge_refs=("J1",),
        groups=(("U1", "C1", "C2"), ("U2", "Q1")),
        compactness_weight=2.2,
        grouping_weight=2.5,
        connector_edge_weight=1.5,
        thermal_weight=0.4,
    ),
    "thermal-first": CompanyProfile(
        name="Thermal First",
        clearance=0.8,
        edge_margin=1.0,
        fixed_refs=("J1",),
        edge_refs=("J1",),
        groups=(("U1", "C1", "C2"),),
        thermal_pairs=(("U1", "U2", 22.0), ("U2", "Q1", 16.0)),
        compactness_weight=0.3,
        grouping_weight=1.5,
        connector_edge_weight=1.5,
        thermal_weight=4.0,
    ),
}


def get_profile(name: str) -> CompanyProfile:
    key = str(name).strip().lower()
    if key not in _PROFILES:
        raise ValueError(f"unknown placement profile {name!r}")
    return _PROFILES[key]


def demo_board() -> Board:
    """A deliberately damaged motor-controller placement for the UI demo."""
    return Board(
        width=58.0,
        height=38.0,
        components=(
            Component("J1", 1.0, 14.0, 8.0, 10.0, fixed=True, kind="connector"),
            Component("U1", 18.0, 13.0, 10.0, 10.0, kind="mcu"),
            Component("C1", 22.0, 16.0, 4.0, 3.0, kind="decoupling"),
            Component("C2", 27.0, 17.0, 4.0, 3.0, kind="decoupling"),
            Component("U2", 31.0, 10.0, 13.0, 10.0, kind="driver"),
            Component("Q1", 39.0, 14.0, 8.0, 8.0, kind="power"),
        ),
        keepouts=(Keepout("mounting-hole", 48.0, 2.0, 7.0, 7.0),),
    )
