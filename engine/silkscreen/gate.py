"""The pre-flight gate: what has to be true before a board may be ordered.

Ordering and verification are the same problem. An agent can be trusted to
spend money on a board only in proportion to what it can prove about that
board, so this module is the proof, and the order path in
:mod:`silkscreen.approval` refuses to assemble an order without it.

Three rules shape everything here.

**A check that did not run is not a check that passed.** :attr:`CheckStatus.
SKIPPED` blocks the gate exactly as :attr:`CheckStatus.FAIL` does. The failure
mode this exists to prevent is the quiet one: an import that is not there, a
file that could not be read, and a green report that means nothing. There is no
flag to downgrade a skip.

**Every check carries its evidence.** A check reports the measurements it made,
not merely its verdict, so a reader can disagree with it. A gate that says
"passed" and shows nothing is asking to be trusted, which is the opposite of
the point.

**The gate is not the last word.** Clearing it earns the right to *prepare* an
order and hand it to a person. It does not earn the right to place one -- see
:data:`silkscreen.fabhouse.SUBMISSION_BOUNDARY` for why that distinction is
permanent rather than a stage of implementation.

The checks are deliberately drawn from different subsystems that share no code:
the netlist validator, the placer's own verdict, the deterministic half of
:mod:`silkscreen.audit`, the fab-house capability tables, and a reader that
parses the emitted files back. A check written in terms of the code it is
checking shares that code's blind spot; this file's job is to keep them apart.
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .board import DEFAULT_BOARD_MARGIN_NM, BoardResult, emit_kicad_pcb
from .fab import FabLayer, fab_files
from .fabhouse import DEFAULT_SERVICE_ID, FabService, check_capabilities, service_by_id
from .netlist import CircuitSpec, ValidationError
from .order import OrderIssue, OrderIssueSeverity, OrderOptions, preflight
from .packing import Layer
from .units import NM_PER_MM, to_mm

__all__ = [
    "CheckStatus",
    "GateCheck",
    "GateReport",
    "run_gate",
]


class CheckStatus(StrEnum):
    """The four things a check can say. Only one of them is good news."""

    PASS = "pass"
    #: Something the check measured is wrong enough to stop the order.
    FAIL = "fail"
    #: Nothing blocking, but the buyer should read it before paying.
    WARN = "warn"
    #: The check could not run. Blocks, because an unrun check proves nothing
    #: and an agent must not spend money on the absence of evidence.
    SKIPPED = "skipped"


_BLOCKING = frozenset({CheckStatus.FAIL, CheckStatus.SKIPPED})


@dataclass(frozen=True)
class GateCheck:
    """One check, its verdict, and the measurements that produced it."""

    id: str
    title: str
    status: CheckStatus
    summary: str
    #: What was measured, in the check's own words. Present on a pass as well
    #: as a failure: "11 nets, all routed" is the evidence for the pass.
    evidence: tuple[str, ...] = field(default_factory=tuple)
    issues: tuple[OrderIssue, ...] = field(default_factory=tuple)
    #: Which subsystem answered. Named so a reader can see that the checks do
    #: not all come from one place.
    source: str = ""

    @property
    def blocking(self) -> bool:
        return self.status in _BLOCKING

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "status": str(self.status),
            "summary": self.summary,
            "evidence": list(self.evidence),
            "issues": [issue.as_dict() for issue in self.issues],
            "source": self.source,
            "blocking": self.blocking,
        }


@dataclass(frozen=True)
class GateReport:
    """Every check that ran, and the single verdict they add up to."""

    checks: tuple[GateCheck, ...]

    @property
    def go(self) -> bool:
        """True only when nothing failed and nothing was skipped.

        Derived rather than stored, for the same reason
        :attr:`silkscreen.order.OrderPreflight.orderable` is: a stored verdict
        can disagree with the checks under it, and this one decides whether
        money gets spent.
        """
        return not any(check.blocking for check in self.checks)

    @property
    def blocking(self) -> tuple[GateCheck, ...]:
        return tuple(c for c in self.checks if c.blocking)

    def counts(self) -> dict[str, int]:
        return {
            status.value: sum(1 for c in self.checks if c.status is status)
            for status in CheckStatus
        }

    def headline(self) -> str:
        counts = self.counts()
        verdict = "GO" if self.go else "NO-GO"
        return (
            f"{verdict} -- {counts['pass']} of {len(self.checks)} checks passed, "
            f"{counts['fail']} failed, {counts['warn']} warned, "
            f"{counts['skipped']} could not run"
        )

    def as_dict(self) -> dict:
        return {
            "go": self.go,
            "headline": self.headline(),
            "counts": self.counts(),
            "checks": [check.as_dict() for check in self.checks],
        }


# --------------------------------------------------------------- small tools


def _verdict(issues: Sequence[OrderIssue]) -> CheckStatus:
    """Blocker -> FAIL, anything else -> WARN, nothing -> PASS."""
    if any(i.severity == OrderIssueSeverity.BLOCKER for i in issues):
        return CheckStatus.FAIL
    return CheckStatus.WARN if issues else CheckStatus.PASS


def _issue_summary(issues: Sequence[OrderIssue], clean: str) -> str:
    if not issues:
        return clean
    return "; ".join(f"{i.severity}: {i.title}" for i in issues[:3]) + (
        f" (and {len(issues) - 3} more)" if len(issues) > 3 else ""
    )


def _mm3(value_nm: int) -> float:
    return round(to_mm(value_nm), 3)


#: Which preflight issue code belongs to which gate check. Unmapped codes fall
#: through to ``order-options`` rather than disappearing: a new check in
#: :func:`silkscreen.order.preflight` must never become invisible here just
#: because this table was not updated with it.
_PREFLIGHT_ROUTING = frozenset({"unrouted-nets"})
_PREFLIGHT_PLACEMENT = frozenset(
    {
        "no-parts",
        "degenerate-outline",
        "tiny-board",
        "fallback-placement",
        "solver-warnings",
    }
)


# -------------------------------------------------------------- the checks


def _check_spec(spec: CircuitSpec | None) -> GateCheck:
    """The circuit IR the board was built from still validates.

    Re-run rather than trusted. The spec passed validation once, on the way in
    from the model; anything that mutated it since -- a repair pass, a hand
    edit, a merge of two specs -- would not have been re-checked, and the board
    is downstream of whatever it says now.
    """
    if spec is None:
        return GateCheck(
            id="spec-validates",
            title="The circuit specification validates",
            status=CheckStatus.SKIPPED,
            summary=(
                "No circuit specification was supplied, so the board cannot be "
                "checked against the intent it came from."
            ),
            source="silkscreen.netlist",
        )
    try:
        spec.validate()
    except ValidationError as exc:
        return GateCheck(
            id="spec-validates",
            title="The circuit specification validates",
            status=CheckStatus.FAIL,
            summary=f"{len(exc.errors)} validation error(s) in the circuit spec.",
            evidence=tuple(exc.errors[:8]),
            source="silkscreen.netlist",
        )
    return GateCheck(
        id="spec-validates",
        title="The circuit specification validates",
        status=CheckStatus.PASS,
        summary="The circuit the board was built from is internally consistent.",
        evidence=(
            f"{len(spec.devices)} device(s), {len(spec.passives)} passive(s), "
            f"{len(spec.connections)} net(s)",
            "every endpoint names a pin that exists on the part it names",
            "no passive is left floating on either leg",
        ),
        source="silkscreen.netlist",
    )


def _check_routing(board: BoardResult, issues: Sequence[OrderIssue]) -> GateCheck:
    """Every net that needs copper has copper.

    This is the check the whole gate exists for. A placed board and a routed
    board are the same file list, the same outline and the same parts; the
    difference is whether the thing works at all.
    """
    # BoardResult.route_completion is 1.0 when both lists are empty, which is
    # the right answer for "there was nothing to route" and exactly the wrong
    # one for a board the router was never run on. Quoting it unconditionally
    # would print "route completion 100%" directly beneath a failure saying
    # three nets have no copper -- evidence that contradicts its own verdict is
    # worse than no evidence, because it teaches a reader to skim past it.
    never_ran = not board.tracks and not board.routed_nets and not board.unrouted_nets
    evidence = [
        f"{len(board.routed_nets)} net(s) fully routed, "
        f"{len(board.unrouted_nets)} left open",
        f"{len(board.tracks)} track segment(s), {len(board.vias)} via(s)",
        (
            "the router has not been run on this board, so there is no "
            "completion figure to quote"
            if never_ran
            else f"route completion {board.route_completion:.0%}"
        ),
    ]
    evidence += [
        f"open: {net} -- {why}" for net, why in sorted(board.unrouted_nets.items())
    ][:5]
    return GateCheck(
        id="routing-complete",
        title="Every net that needs copper has it",
        status=_verdict(issues),
        summary=_issue_summary(
            issues, f"All {len(board.routed_nets)} routable net(s) are connected."
        ),
        evidence=tuple(evidence),
        issues=tuple(issues),
        source="silkscreen.order.preflight",
    )


def _check_placement(board: BoardResult, issues: Sequence[OrderIssue]) -> GateCheck:
    return GateCheck(
        id="placement-sound",
        title="The placement is a real, solved layout",
        status=_verdict(issues),
        summary=_issue_summary(
            issues,
            f"{len(board.parts)} part(s) placed on a "
            f"{_mm3(board.width_nm)} x {_mm3(board.height_nm)} mm board.",
        ),
        evidence=(
            f"solver status {board.solver_status!r}",
            f"board {_mm3(board.width_nm)} x {_mm3(board.height_nm)} mm",
            f"{len(board.parts)} placed part(s)",
            f"placer warnings: {len(board.warnings)}",
        ),
        issues=tuple(issues),
        source="silkscreen.order.preflight",
    )


def _check_options(
    options: OrderOptions, issues: Sequence[OrderIssue]
) -> GateCheck:
    return GateCheck(
        id="order-options",
        title="The order options are coherent with the board",
        status=_verdict(issues),
        summary=_issue_summary(
            issues, "The requested options match what the board actually is."
        ),
        evidence=(
            f"{options.quantity} board(s), {options.layers} layer(s), "
            f"{options.thickness_mm} mm",
            f"assembly={options.assembly} ({options.assembly_side})",
            f"panel {options.panel_columns} x {options.panel_rows}",
        ),
        issues=tuple(issues),
        source="silkscreen.order.preflight",
    )


def _check_design_rules(board_path: Path) -> GateCheck:
    """The deterministic half of the review checker, over the written file.

    Run against the emitted ``.kicad_pcb`` rather than the in-memory board on
    purpose. The file is what the fab package was rendered from and what a
    human would open, so measuring the file measures the artefact rather than
    the intention behind it.

    The model half is deliberately not run. Its findings are suggestions and
    carry no measurement, and a suggestion is not something to hold an order
    open on -- or to let one through.
    """
    try:
        from .audit import Origin, Severity, review_board
    except ImportError as exc:  # pragma: no cover - audit ships in this package
        return GateCheck(
            id="design-rules",
            title="Deterministic design rule check",
            status=CheckStatus.SKIPPED,
            summary=f"silkscreen.audit could not be imported: {exc}",
            source="silkscreen.audit",
        )

    try:
        result = review_board(board_path, effort="deep", model=None)
    except Exception as exc:  # noqa: BLE001 - a crashed check is a skipped check
        return GateCheck(
            id="design-rules",
            title="Deterministic design rule check",
            status=CheckStatus.SKIPPED,
            summary=f"the review checker raised {type(exc).__name__}: {exc}",
            source="silkscreen.audit",
        )

    proven = [f for f in result.findings if f.origin is Origin.PROVEN]
    blockers = [f for f in proven if f.severity is Severity.BLOCKER]
    marginal = [f for f in proven if f.severity is Severity.MARGINAL]

    if blockers:
        status = CheckStatus.FAIL
    elif marginal:
        status = CheckStatus.WARN
    else:
        status = CheckStatus.PASS

    evidence = [
        f"{len(result.rules_run)} rule(s) ran: {', '.join(sorted(result.rules_run))}",
        f"{len(blockers)} blocker(s), {len(marginal)} marginal, "
        f"{len(proven) - len(blockers) - len(marginal)} note(s), all proven by "
        f"measurement",
        "the model half was not run: suggestions carry no measurement and are "
        "not grounds to hold or release an order",
    ]
    evidence += [f"{f.severity}: {f.title} -- {f.evidence}" for f in proven[:6]]

    return GateCheck(
        id="design-rules",
        title="Deterministic design rule check",
        status=status,
        summary=(
            f"{len(result.rules_run)} measured rule(s) ran; "
            f"{len(blockers)} blocker(s) found."
            if blockers
            else f"{len(result.rules_run)} measured rule(s) ran and found no blockers."
        ),
        evidence=tuple(evidence),
        source="silkscreen.audit",
    )


def _check_capabilities(
    board: BoardResult, service: FabService, options: OrderOptions
) -> GateCheck:
    issues = check_capabilities(board, service, options=options)
    tracks = [t.width_nm for t in board.tracks]
    rings = [(v.diameter_nm - v.drill_nm) // 2 for v in board.vias]
    evidence = [
        f"house {service.house}, service {service.service} ({service.source_url})",
        # The profile the fab cuts -- placement plus outline margin -- which is
        # the rectangle check_capabilities compares and quote() bills.
        f"board profile {_mm3(board.width_nm + 2 * DEFAULT_BOARD_MARGIN_NM)} x "
        f"{_mm3(board.height_nm + 2 * DEFAULT_BOARD_MARGIN_NM)} mm against a "
        f"{_mm3(service.max_width_nm)} x {_mm3(service.max_height_nm)} mm maximum",
    ]
    if tracks:
        evidence.append(
            f"narrowest track {_mm3(min(tracks))} mm against a "
            f"{_mm3(service.min_track_nm)} mm minimum"
        )
    if rings:
        evidence.append(
            f"thinnest annular ring {_mm3(min(rings))} mm against a "
            f"{_mm3(service.min_annular_ring_nm)} mm minimum"
        )
        evidence.append(
            f"smallest drill {_mm3(min(v.drill_nm for v in board.vias))} mm against "
            f"a {_mm3(service.min_drill_nm)} mm minimum"
        )
    return GateCheck(
        id="fab-capabilities",
        title=f"{service.house} can actually build this board",
        status=_verdict(issues),
        summary=_issue_summary(
            issues,
            f"Every measured feature is inside {service.house}'s published "
            f"limits for {service.service}.",
        ),
        evidence=tuple(evidence),
        issues=tuple(issues),
        source="silkscreen.fabhouse",
    )


# --------------------------------------------------- reading the package back

_APERTURE_DEF_RE = re.compile(r"^%ADD(\d+)[CR],", re.M)
_APERTURE_SEL_RE = re.compile(r"^D(\d+)\*$", re.M)
_COORD_RE = re.compile(r"^X(-?\d+)Y(-?\d+)D0[123]\*$", re.M)
_DRILL_TOOL_DEF_RE = re.compile(r"^T(\d+)C([\d.]+)$", re.M)
_DRILL_TOOL_SEL_RE = re.compile(r"^T([1-9]\d*)\s*$", re.M)
_DRILL_HIT_RE = re.compile(r"^X(-?[\d.]+)Y(-?[\d.]+)$", re.M)

#: Every file the package must contain. Named here rather than derived from
#: :func:`silkscreen.fab.fab_files`, because a completeness check that asks the
#: generator what it generated cannot catch the generator dropping a file.
REQUIRED_FILES: tuple[str, ...] = (
    "silkscreen-F_Cu.GTL",
    "silkscreen-B_Cu.GBL",
    "silkscreen-F_Mask.GTS",
    "silkscreen-B_Mask.GBS",
    "silkscreen-F_Paste.GTP",
    "silkscreen-B_Paste.GBP",
    "silkscreen-F_Silkscreen.GTO",
    "silkscreen-B_Silkscreen.GBO",
    "silkscreen-Edge_Cuts.GKO",
    "silkscreen-PTH.DRL",
    "silkscreen-NPTH.DRL",
    "silkscreen-drill-report.txt",
    "silkscreen-BOM.csv",
    "silkscreen-CPL.csv",
    "README-fab.txt",
)

_GERBERS = tuple(n for n in REQUIRED_FILES if n.startswith("silkscreen-") and
                 n.rsplit(".", 1)[1] in {"GTL", "GBL", "GTS", "GBS", "GTP",
                                         "GBP", "GTO", "GBO", "GKO"})


def _check_package_complete(files: Sequence[FabLayer]) -> GateCheck:
    """Every file a fab expects is present, and none is silently duplicated."""
    by_name = {f.filename: f.content for f in files}
    problems: list[str] = []

    missing = [name for name in REQUIRED_FILES if name not in by_name]
    if missing:
        problems.append(f"missing: {', '.join(missing)}")

    names = [f.filename for f in files]
    duplicated = sorted({n for n in names if names.count(n) > 1})
    if duplicated:
        problems.append(f"duplicated: {', '.join(duplicated)}")

    empty = sorted(n for n, text in by_name.items() if not text.strip())
    if empty:
        problems.append(f"zero-length: {', '.join(empty)}")

    # The profile is the one layer that can never be empty: without it the fab
    # does not know where to cut, and the outline is what every edge clearance
    # was measured against.
    outline = by_name.get("silkscreen-Edge_Cuts.GKO", "")
    if not _COORD_RE.search(outline):
        problems.append("the board profile contains no geometry")

    return GateCheck(
        id="package-complete",
        title="The fab package contains every file a fab expects",
        status=CheckStatus.FAIL if problems else CheckStatus.PASS,
        summary=(
            "; ".join(problems)
            if problems
            else f"All {len(REQUIRED_FILES)} required files are present and non-empty."
        ),
        evidence=(
            f"{len(files)} file(s) generated, {len(REQUIRED_FILES)} required",
            f"present: {', '.join(sorted(by_name))}",
            f"profile geometry: {len(_COORD_RE.findall(outline))} coordinate(s)",
        ),
        source="silkscreen.gate",
    )


def _check_gerbers_wellformed(files: Sequence[FabLayer]) -> GateCheck:
    """Each Gerber parses, and never selects an aperture it did not define.

    A Gerber that references an undefined aperture is not rejected by every
    reader -- some substitute a default and plot the layer anyway, at the wrong
    width. That is the failure this catches: a file that opens fine and is
    wrong.
    """
    by_name = {f.filename: f.content for f in files}
    problems: list[str] = []
    total_coords = 0

    for name in _GERBERS:
        text = by_name.get(name, "")
        lines = text.splitlines()
        if not lines or lines[0] != "%FSLAX46Y46*%":
            problems.append(f"{name}: does not open with the format spec")
            continue
        if lines[-1] != "M02*":
            problems.append(f"{name}: does not terminate with M02")
        if "%MOMM*%" not in text:
            problems.append(f"{name}: does not declare millimetres")

        defined = {int(code) for code in _APERTURE_DEF_RE.findall(text)}
        selected = {int(code) for code in _APERTURE_SEL_RE.findall(text)}
        undefined = sorted(selected - defined)
        if undefined:
            problems.append(f"{name}: selects undefined aperture(s) {undefined}")

        coords = _COORD_RE.findall(text)
        total_coords += len(coords)
        if coords and not selected:
            problems.append(f"{name}: draws before selecting any aperture")
        negative = [c for c in coords if int(c[0]) < 0 or int(c[1]) < 0]
        if negative:
            problems.append(f"{name}: {len(negative)} negative coordinate(s)")

    return GateCheck(
        id="gerbers-wellformed",
        title="Every Gerber parses and defines what it draws with",
        status=CheckStatus.FAIL if problems else CheckStatus.PASS,
        summary=(
            "; ".join(problems[:3])
            if problems
            else f"All {len(_GERBERS)} Gerbers parse, with no undefined apertures "
            f"and no negative coordinates."
        ),
        evidence=(
            f"{len(_GERBERS)} Gerber layer(s) read back and re-parsed",
            f"{total_coords} coordinate command(s) across all layers",
            "every selected aperture is defined earlier in its own file",
            "no coordinate in any layer is negative",
        ),
        source="silkscreen.gate",
    )


def _check_drill(board: BoardResult, files: Sequence[FabLayer]) -> GateCheck:
    """Every hole is drilled exactly once, in the file that matches its plating."""
    by_name = {f.filename: f.content for f in files}
    plated = by_name.get("silkscreen-PTH.DRL", "")
    unplated = by_name.get("silkscreen-NPTH.DRL", "")
    problems: list[str] = []

    hits = _DRILL_HIT_RE.findall(plated)
    if len(hits) != len(board.vias):
        problems.append(
            f"the plated program drills {len(hits)} hole(s) for "
            f"{len(board.vias)} via(s)"
        )

    defined = {int(t) for t, _ in _DRILL_TOOL_DEF_RE.findall(plated)}
    selected = {int(t) for t in _DRILL_TOOL_SEL_RE.findall(plated)}
    if selected - defined:
        problems.append(
            f"the plated program selects undefined tool(s) "
            f"{sorted(selected - defined)}"
        )
    if hits and not defined:
        problems.append("the plated program drills holes with no tool defined")

    if _DRILL_HIT_RE.search(unplated):
        problems.append(
            "the non-plated program contains holes; a via drilled unplated "
            "leaves the two copper layers unconnected"
        )
    if "TF.FileFunction,Plated" not in plated:
        problems.append("the plated program does not declare itself plated")
    if "TF.FileFunction,NonPlated" not in unplated:
        problems.append("the non-plated program does not declare itself non-plated")

    sizes = {float(size) for _, size in _DRILL_TOOL_DEF_RE.findall(plated)}
    expected_sizes = {round(v.drill_nm / NM_PER_MM, 6) for v in board.vias}
    if sizes != expected_sizes:
        problems.append(
            f"tool sizes {sorted(sizes)} do not match the board's drills "
            f"{sorted(expected_sizes)}"
        )

    report = by_name.get("silkscreen-drill-report.txt", "")
    if f"Total holes: {len(board.vias)}" not in report:
        problems.append("the drill report's hole count disagrees with the board")

    return GateCheck(
        id="drill-consistent",
        title="Holes are drilled once each, in the right plating class",
        status=CheckStatus.FAIL if problems else CheckStatus.PASS,
        summary=(
            "; ".join(problems[:3])
            if problems
            else f"{len(board.vias)} plated hole(s), 0 non-plated, tools and "
            f"report agree."
        ),
        evidence=(
            f"{len(board.vias)} via(s) on the board, {len(hits)} hit(s) in the "
            f"plated program",
            f"tool sizes in the file: {sorted(sizes)} mm",
            "the non-plated program is present and contains no hits",
            "the drill report's total matches the board",
        ),
        source="silkscreen.gate",
    )


def _parse_csv(text: str) -> tuple[list[str], list[list[str]]]:
    import csv
    import io

    rows = list(csv.reader(io.StringIO(text)))
    return (rows[0], rows[1:]) if rows else ([], [])


def _check_bom(
    board: BoardResult, files: Sequence[FabLayer], options: OrderOptions
) -> GateCheck:
    """The BOM describes exactly the parts on the board, and can be sourced.

    Two different questions, and the second is the one that gets skipped. A BOM
    can be perfectly consistent with the board and still be unbuyable: a line
    with a value and a footprint but no part number tells an assembler what
    shape the part is, not which part it is. That is fine for bare boards and
    fatal for assembly, so it blocks only when assembly was asked for.
    """
    by_name = {f.filename: f.content for f in files}
    header, rows = _parse_csv(by_name.get("silkscreen-BOM.csv", ""))
    problems: list[str] = []
    warnings: list[str] = []

    if header[:4] != ["Comment", "Designator", "Footprint", "Quantity"]:
        problems.append(f"unexpected BOM header {header!r}")

    listed: list[str] = []
    for row in rows:
        if len(row) < 4:
            problems.append(f"short BOM row {row!r}")
            continue
        comment, designators, footprint, quantity = row[0], row[1], row[2], row[3]
        refs = [d for d in designators.split(";") if d]
        listed += refs
        if not comment.strip():
            problems.append(f"BOM row for {designators} has no value")
        if not footprint.strip():
            problems.append(f"BOM row for {designators} names no footprint")
        if quantity != str(len(refs)):
            problems.append(
                f"BOM row for {designators} claims {quantity} but lists {len(refs)}"
            )

    board_refs = sorted(part.ref for part in board.parts)
    if sorted(listed) != board_refs:
        problems.append(
            f"the BOM lists {sorted(listed)}, the board carries {board_refs}"
        )
    if len(set(listed)) != len(listed):
        problems.append("a reference designator appears in two BOM rows")

    # No column here can carry a manufacturer or supplier part number, because
    # nothing upstream resolves one. Saying so is the point.
    part_number_columns = {
        "lcsc",
        "lcsc part #",
        "mpn",
        "manufacturer part number",
        "supplier part number",
    }
    has_part_numbers = any(
        column.strip().lower() in part_number_columns for column in header
    )
    if options.assembly and not has_part_numbers:
        problems.append(
            "assembly was requested, but no BOM line carries a manufacturer or "
            "supplier part number -- an assembler cannot buy a part from a "
            "value and a package alone"
        )
    elif not has_part_numbers:
        warnings.append(
            "no BOM line carries a manufacturer or supplier part number; fine "
            "for bare boards, not enough for assembly"
        )

    if problems:
        status = CheckStatus.FAIL
    elif warnings:
        status = CheckStatus.WARN
    else:
        status = CheckStatus.PASS

    return GateCheck(
        id="bom-valid",
        title="The bill of materials matches the board and can be sourced",
        status=status,
        summary=(
            "; ".join(problems[:3])
            if problems
            else (warnings[0] if warnings else
                  f"{len(rows)} BOM line(s) cover all {len(board_refs)} part(s).")
        ),
        evidence=(
            f"{len(rows)} BOM line(s) covering {len(listed)} designator(s)",
            f"board carries {len(board_refs)} part(s): {', '.join(board_refs)}",
            f"quantities sum to {len(listed)}",
            f"part numbers present: {has_part_numbers}",
            f"assembly requested: {options.assembly}",
        ),
        source="silkscreen.gate",
    )


def _check_package_consistent(
    board: BoardResult, files: Sequence[FabLayer]
) -> GateCheck:
    """The files agree with each other and with the board they came from.

    Each file is individually plausible; the danger is that they describe
    different boards. A pick-and-place row for a part that is not in the BOM,
    or a part placed outside the outline the fab will cut, are both silent in
    every single-file check and obvious in a cross-file one.
    """
    by_name = {f.filename: f.content for f in files}
    problems: list[str] = []

    bom_header, bom_rows = _parse_csv(by_name.get("silkscreen-BOM.csv", ""))
    cpl_header, cpl_rows = _parse_csv(by_name.get("silkscreen-CPL.csv", ""))

    bom_refs = {
        ref
        for row in bom_rows
        if len(row) > 1
        for ref in row[1].split(";")
        if ref
    }
    cpl_refs = {row[0] for row in cpl_rows if row}
    board_refs = {part.ref for part in board.parts}

    if bom_refs != board_refs:
        problems.append(
            f"BOM and board disagree: only in BOM {sorted(bom_refs - board_refs)}, "
            f"only on board {sorted(board_refs - bom_refs)}"
        )
    if cpl_refs != board_refs:
        problems.append(
            f"pick-and-place and board disagree: only in CPL "
            f"{sorted(cpl_refs - board_refs)}, only on board "
            f"{sorted(board_refs - cpl_refs)}"
        )

    # Every placement must land inside the profile the fab cuts. The profile is
    # the placement plus its margin on all four sides, shifted to the origin.
    outline = by_name.get("silkscreen-Edge_Cuts.GKO", "")
    corners = [(int(x), int(y)) for x, y, in
               ((m[0], m[1]) for m in _COORD_RE.findall(outline))]
    if corners:
        max_x = max(x for x, _ in corners)
        max_y = max(y for _, y in corners)
        side_by_ref = {
            part.ref: (
                "Bottom" if part.layer is Layer.BOTTOM else "Top"
            )
            for part in board.parts
        }
        for row in cpl_rows:
            if len(row) < 5:
                problems.append(f"short pick-and-place row {row!r}")
                continue
            ref, x_mm, y_mm, side = row[0], float(row[1]), float(row[2]), row[3]
            x_nm, y_nm = round(x_mm * NM_PER_MM), round(y_mm * NM_PER_MM)
            if not (0 <= x_nm <= max_x and 0 <= y_nm <= max_y):
                problems.append(
                    f"{ref} is placed at ({x_mm}, {y_mm}) mm, outside the "
                    f"{max_x / NM_PER_MM} x {max_y / NM_PER_MM} mm profile"
                )
            if side_by_ref.get(ref) != side:
                problems.append(
                    f"{ref} is on {side_by_ref.get(ref)} on the board and "
                    f"{side} in the pick-and-place file"
                )
    else:
        problems.append("the profile has no geometry to place parts inside")

    if cpl_header[:5] != ["Designator", "Mid X", "Mid Y", "Layer", "Rotation"]:
        problems.append(f"unexpected pick-and-place header {cpl_header!r}")

    # The notes must describe this board and not a previous one.
    notes = by_name.get("README-fab.txt", "")
    if board.unrouted_nets or not board.tracks:
        if "Do not run it." not in notes:
            problems.append(
                "the board is not fully routed and the fab notes do not say so"
            )
    elif "Do not run it." in notes:
        problems.append("the fab notes warn about routing on a fully routed board")

    return GateCheck(
        id="package-consistent",
        title="The files in the package describe the same board",
        status=CheckStatus.FAIL if problems else CheckStatus.PASS,
        summary=(
            "; ".join(problems[:3])
            if problems
            else f"BOM, pick-and-place, profile and notes agree on all "
            f"{len(board_refs)} part(s)."
        ),
        evidence=(
            f"{len(bom_refs)} designator(s) in the BOM, {len(cpl_refs)} in the "
            f"pick-and-place, {len(board_refs)} on the board",
            "every placement lies inside the profile the fab will cut",
            "every placement's side matches the side the board puts it on",
            "the notes' routing statement matches the board's routing state",
            f"BOM header {bom_header}",
        ),
        source="silkscreen.gate",
    )


def _check_board_file(board_path: Path, board: BoardResult) -> GateCheck:
    """The emitted ``.kicad_pcb`` reparses and still holds every part.

    Written last and checked here because it is the file a human opens to
    disagree with the machine. If KiCad's own parser cannot read it, every
    other check in this gate was performed on something nobody can inspect.
    """
    try:
        from kiutils.board import Board

        parsed = Board.from_file(str(board_path))
    except Exception as exc:  # noqa: BLE001 - a crashed check is a skipped check
        return GateCheck(
            id="board-file-reparses",
            title="The emitted board file reparses",
            status=CheckStatus.SKIPPED,
            summary=f"kiutils could not read the written board: {exc}",
            source="kiutils",
        )

    footprints = len(parsed.footprints or [])
    segments = len(getattr(parsed, "traceItems", []) or [])
    problems: list[str] = []
    if footprints != len(board.parts):
        problems.append(
            f"the file holds {footprints} footprint(s) for {len(board.parts)} "
            f"placed part(s)"
        )
    if not any(
        str(getattr(g, "layer", "")) == "Edge.Cuts" for g in parsed.graphicItems or []
    ):
        problems.append("the file has no Edge.Cuts outline")

    return GateCheck(
        id="board-file-reparses",
        title="The emitted board file reparses",
        status=CheckStatus.FAIL if problems else CheckStatus.PASS,
        summary=(
            "; ".join(problems)
            if problems
            else f"KiCad's own parser reads the file back with all "
            f"{footprints} footprint(s)."
        ),
        evidence=(
            f"{footprints} footprint(s) read back",
            f"{segments} copper item(s) read back",
            "an Edge.Cuts outline is present",
            f"file size {board_path.stat().st_size} bytes",
        ),
        source="kiutils",
    )


# ------------------------------------------------------------------ the gate


def run_gate(
    board: BoardResult,
    *,
    spec: CircuitSpec | None = None,
    options: OrderOptions | None = None,
    service: FabService | str = DEFAULT_SERVICE_ID,
    files: Sequence[FabLayer] | None = None,
    board_text: str | None = None,
) -> GateReport:
    """Run every check that can be run, and return all of them.

    Nothing short-circuits. A failing check does not stop the ones after it,
    because the value of the report is the whole picture -- being told the
    board is unrouted *and* that its BOM cannot be sourced is one round trip,
    while being told them one at a time is three.

    Args:
        board: The placed, and hopefully routed, board.
        spec: The circuit it was built from. Without it the intent check is
            skipped, which blocks -- a board with no stated intent cannot be
            checked against one.
        options: What is being asked of the fab.
        service: The house the board would be built at.
        files: A pre-rendered fab package, if one was already produced. Left
            unset it is rendered here, from this board.
        board_text: A pre-rendered ``.kicad_pcb``, same reasoning.
    """
    options = options or OrderOptions()
    if isinstance(service, str):
        service = service_by_id(service)
    files = list(files if files is not None else fab_files(board))
    board_text = board_text if board_text is not None else emit_kicad_pcb(board)

    issues = preflight(board, spec=spec, options=options).issues
    routing = [i for i in issues if i.code in _PREFLIGHT_ROUTING]
    placement = [i for i in issues if i.code in _PREFLIGHT_PLACEMENT]
    # Everything unmapped lands here rather than vanishing, so a check added to
    # preflight later cannot become invisible by not being listed above.
    other = [
        i
        for i in issues
        if i.code not in _PREFLIGHT_ROUTING and i.code not in _PREFLIGHT_PLACEMENT
    ]

    with tempfile.TemporaryDirectory(prefix="silkscreen-gate-") as tmp:
        board_path = Path(tmp) / "board.kicad_pcb"
        board_path.write_text(board_text, encoding="utf-8")
        checks = (
            _check_spec(spec),
            _check_routing(board, routing),
            _check_placement(board, placement),
            _check_options(options, other),
            _check_design_rules(board_path),
            _check_capabilities(board, service, options),
            _check_package_complete(files),
            _check_gerbers_wellformed(files),
            _check_drill(board, files),
            _check_bom(board, files, options),
            _check_package_consistent(board, files),
            _check_board_file(board_path, board),
        )
    return GateReport(checks=checks)
