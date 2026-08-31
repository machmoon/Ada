"""Tests for the pre-flight gate.

The gate's whole value is that a pass means something, so most of these tests
attack it rather than exercise it: corrupt one file in the package, break one
cross-reference, remove one check's dependency, and insist the gate notices.

The single most important test in the file is
``test_a_check_that_cannot_run_blocks_the_gate``. A missing check that reports
"pass" is worse than no gate at all, because it is a green light nobody looks
behind.
"""

from __future__ import annotations

import json

import pytest
from silkscreen.board import build_board, route_board
from silkscreen.fab import FabLayer, fab_files
from silkscreen.gate import REQUIRED_FILES, CheckStatus, GateCheck, GateReport, run_gate
from silkscreen.netlist import (
    CircuitSpec,
    Connection,
    Passive,
    PassiveType,
    parse_circuit_spec,
)
from silkscreen.order import OrderOptions

CIRCUIT = {
    "devices": {"U1": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}}},
    "passives": {
        "C1": {"type": "capacitor", "value": "22uF"},
        "C2": {"type": "capacitor", "value": "22uF"},
    },
    "nets": {
        "VIN": ["U1.VIN", "C1.1"],
        "+3V3": ["U1.VOUT", "C2.1"],
        "GND": ["U1.GND", "C1.2", "C2.2"],
    },
}


@pytest.fixture(scope="module")
def spec():
    return parse_circuit_spec(json.dumps(CIRCUIT))


@pytest.fixture(scope="module")
def placed(spec):
    """Placed and deliberately not routed: the state the gate must refuse."""
    return build_board(spec, time_limit_s=4.0)


@pytest.fixture(scope="module")
def routed(spec):
    board = build_board(spec, time_limit_s=4.0)
    route_board(board)
    return board


@pytest.fixture(scope="module")
def clean(routed, spec):
    return run_gate(routed, spec=spec)


def _check(report: GateReport, check_id: str) -> GateCheck:
    for check in report.checks:
        if check.id == check_id:
            return check
    raise AssertionError(f"no check {check_id!r} in {[c.id for c in report.checks]}")


def _swap(files, filename: str, content: str) -> list[FabLayer]:
    return [
        FabLayer(f.filename, content if f.filename == filename else f.content)
        for f in files
    ]


# ------------------------------------------------------------------ the shape


def test_the_gate_runs_every_check_and_names_them_uniquely(clean):
    ids = [c.id for c in clean.checks]
    assert len(ids) == len(set(ids))
    assert len(ids) == 12
    assert clean.counts()["pass"] + clean.counts()["warn"] == len(ids)


def test_every_check_carries_evidence_even_when_it_passes(clean):
    """A gate that says "passed" and shows nothing is asking to be trusted."""
    for check in clean.checks:
        assert check.evidence, f"{check.id} reports a verdict with no measurements"
        assert check.summary
        assert check.source


def test_the_checks_come_from_more_than_one_subsystem(clean):
    """A check written in terms of the code it checks shares its blind spot."""
    sources = {c.source for c in clean.checks}
    assert {"silkscreen.netlist", "silkscreen.audit", "silkscreen.fabhouse"} <= sources
    assert len(sources) >= 5


def test_a_clean_routed_board_is_a_go(clean):
    assert clean.go
    assert clean.blocking == ()
    assert clean.headline().startswith("GO")


def test_the_verdict_is_derived_from_the_checks_not_stored(routed):
    """A stored verdict can disagree with the checks under it."""
    failing = GateCheck(
        id="x", title="x", status=CheckStatus.FAIL, summary="", evidence=("e",)
    )
    assert not GateReport(checks=(failing,)).go
    warned = GateCheck(
        id="x", title="x", status=CheckStatus.WARN, summary="", evidence=("e",)
    )
    assert GateReport(checks=(warned,)).go


def test_the_report_is_json_safe(clean):
    payload = json.dumps(clean.as_dict())
    assert '"go": true' in payload


# --------------------------------------------------- a skip is not a pass


def test_a_check_that_cannot_run_blocks_the_gate(routed):
    """The failure mode this exists to prevent is the quiet one.

    Without a spec there is nothing to check the board's intent against. That
    is not a pass with a caveat, it is an absence of evidence, and the gate has
    to treat it as one.
    """
    report = run_gate(routed, spec=None)
    check = _check(report, "spec-validates")
    assert check.status is CheckStatus.SKIPPED
    assert check.blocking
    assert not report.go
    assert report.counts()["skipped"] == 1


def test_the_design_rule_check_skips_rather_than_passes_when_it_cannot_run(
    routed, spec, monkeypatch
):
    """An unimportable review checker must not read as a clean board."""
    import builtins

    real_import = builtins.__import__

    def refuse(name, globals=None, locals=None, fromlist=(), level=0):
        if name.endswith("audit") or ".audit" in name:
            raise ImportError("pretend silkscreen.audit is not installed")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", refuse)
    report = run_gate(routed, spec=spec)
    check = _check(report, "design-rules")
    assert check.status is CheckStatus.SKIPPED
    assert not report.go


# ------------------------------------------------------------ real failures


def test_an_unrouted_board_fails_the_gate(placed, spec):
    """The check the whole gate exists for."""
    report = run_gate(placed, spec=spec)
    assert not report.go
    routing = _check(report, "routing-complete")
    assert routing.status is CheckStatus.FAIL
    assert any(i.code == "unrouted-nets" for i in routing.issues)
    assert any("0%" in e or "left open" in e for e in routing.evidence)


def test_an_invalid_spec_fails_and_lists_the_errors(routed):
    """A passive wired on one leg only: valid JSON, not a valid circuit."""
    broken = CircuitSpec(
        passives=[Passive(name="C9", type=PassiveType.CAPACITOR, value="1uF")],
        connections=[Connection(net="N", endpoints=("C9.1", "C9.1"))],
    )
    check = _check(run_gate(routed, spec=broken), "spec-validates")
    assert check.status is CheckStatus.FAIL
    assert check.evidence


def test_assembly_without_part_numbers_fails_but_bare_boards_only_warn(
    routed, spec
):
    """A value and a package say what shape the part is, not which part it is."""
    assembled = _check(
        run_gate(
            routed,
            spec=spec,
            options=OrderOptions(assembly=True),
            service="jlcpcb-2layer",
        ),
        "bom-valid",
    )
    assert assembled.status is CheckStatus.FAIL

    bare = _check(run_gate(routed, spec=spec), "bom-valid")
    assert bare.status is CheckStatus.WARN
    assert "not enough for assembly" in bare.summary


def test_the_same_board_can_be_buildable_at_one_house_and_not_another(routed, spec):
    """The router's vias clear OSH Park's annular ring and fail JLCPCB's."""
    oshpark = _check(run_gate(routed, spec=spec, service="oshpark-2layer"),
                     "fab-capabilities")
    jlcpcb = _check(run_gate(routed, spec=spec, service="jlcpcb-2layer"),
                    "fab-capabilities")
    assert oshpark.status is CheckStatus.PASS
    assert jlcpcb.status is CheckStatus.FAIL
    assert any("annular" in i.code for i in jlcpcb.issues)


# --------------------------------------------------- attacks on the package


def test_a_missing_file_fails_completeness(routed, spec):
    files = [f for f in fab_files(routed) if f.filename != "silkscreen-F_Mask.GTS"]
    check = _check(run_gate(routed, spec=spec, files=files), "package-complete")
    assert check.status is CheckStatus.FAIL
    assert "silkscreen-F_Mask.GTS" in check.summary


def test_the_required_file_list_is_not_taken_from_the_generator(routed):
    """A completeness check that asks the generator what it generated is circular."""
    assert set(REQUIRED_FILES) == {f.filename for f in fab_files(routed)}


def test_a_gerber_that_selects_an_undefined_aperture_fails(routed, spec):
    """Some readers substitute a default and plot the layer at the wrong width."""
    files = fab_files(routed)
    original = next(f.content for f in files if f.filename == "silkscreen-F_Cu.GTL")
    corrupted = original.replace("D10*", "D99*", 1)
    assert corrupted != original
    check = _check(
        run_gate(
            routed, spec=spec, files=_swap(files, "silkscreen-F_Cu.GTL", corrupted)
        ),
        "gerbers-wellformed",
    )
    assert check.status is CheckStatus.FAIL
    assert "undefined aperture" in check.summary


def test_a_truncated_gerber_fails(routed, spec):
    files = fab_files(routed)
    original = next(f.content for f in files if f.filename == "silkscreen-B_Cu.GBL")
    check = _check(
        run_gate(
            routed,
            spec=spec,
            files=_swap(files, "silkscreen-B_Cu.GBL", original.replace("M02*\n", "")),
        ),
        "gerbers-wellformed",
    )
    assert check.status is CheckStatus.FAIL


def test_a_via_in_the_non_plated_program_fails(routed, spec):
    """An unplated via is an open circuit that passes visual inspection."""
    files = fab_files(routed)
    plated = next(f.content for f in files if f.filename == "silkscreen-PTH.DRL")
    check = _check(
        run_gate(routed, spec=spec, files=_swap(files, "silkscreen-NPTH.DRL", plated)),
        "drill-consistent",
    )
    assert check.status is CheckStatus.FAIL
    assert "unconnected" in check.summary or "non-plated" in check.summary


def test_a_drill_hit_with_no_tool_defined_fails(routed, spec):
    files = fab_files(routed)
    plated = next(f.content for f in files if f.filename == "silkscreen-PTH.DRL")
    stripped = "\n".join(
        line for line in plated.splitlines() if not line.startswith("T1C")
    ) + "\n"
    check = _check(
        run_gate(
            routed, spec=spec, files=_swap(files, "silkscreen-PTH.DRL", stripped)
        ),
        "drill-consistent",
    )
    assert check.status is CheckStatus.FAIL


def test_a_bom_that_omits_a_part_fails_consistency(routed, spec):
    files = fab_files(routed)
    bom = next(f.content for f in files if f.filename == "silkscreen-BOM.csv")
    lines = bom.splitlines()
    trimmed = "\n".join(lines[:-1]) + "\n"
    report = run_gate(
        routed, spec=spec, files=_swap(files, "silkscreen-BOM.csv", trimmed)
    )
    assert _check(report, "package-consistent").status is CheckStatus.FAIL
    assert _check(report, "bom-valid").status is CheckStatus.FAIL
    assert not report.go


def test_a_part_placed_outside_the_profile_fails_consistency(routed, spec):
    """Silent in every single-file check, obvious in a cross-file one."""
    files = fab_files(routed)
    cpl = next(f.content for f in files if f.filename == "silkscreen-CPL.csv")
    lines = cpl.splitlines()
    ref = lines[1].split(",")[0]
    lines[1] = f"{ref},999.0000,999.0000,Top,0"
    check = _check(
        run_gate(
            routed,
            spec=spec,
            files=_swap(files, "silkscreen-CPL.csv", "\n".join(lines) + "\n"),
        ),
        "package-consistent",
    )
    assert check.status is CheckStatus.FAIL
    assert "outside the" in check.summary


def test_a_pick_and_place_side_that_contradicts_the_board_fails(routed, spec):
    files = fab_files(routed)
    cpl = next(f.content for f in files if f.filename == "silkscreen-CPL.csv")
    flipped = cpl.replace(",Top,", ",Bottom,", 1)
    check = _check(
        run_gate(routed, spec=spec, files=_swap(files, "silkscreen-CPL.csv", flipped)),
        "package-consistent",
    )
    assert check.status is CheckStatus.FAIL


def test_notes_that_disagree_with_the_boards_routing_state_fail(routed, spec):
    """Placed and routed boards ship an identical file list."""
    files = fab_files(routed)
    notes = next(f.content for f in files if f.filename == "README-fab.txt")
    check = _check(
        run_gate(
            routed,
            spec=spec,
            files=_swap(files, "README-fab.txt", notes + "\nDo not run it.\n"),
        ),
        "package-consistent",
    )
    assert check.status is CheckStatus.FAIL


def test_an_unparseable_board_file_skips_rather_than_passes(routed, spec):
    check = _check(
        run_gate(routed, spec=spec, board_text="(this is not a board file"),
        "board-file-reparses",
    )
    assert check.status in (CheckStatus.SKIPPED, CheckStatus.FAIL)
    assert check.blocking


def test_nothing_short_circuits_when_a_check_fails(placed, spec):
    """Three problems in one round trip beats three round trips."""
    report = run_gate(
        placed, spec=None, options=OrderOptions(assembly=True), service="jlcpcb-2layer"
    )
    assert len(report.checks) == 12
    assert len(report.blocking) >= 3
