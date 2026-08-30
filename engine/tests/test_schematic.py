"""Schematic emission tests.

The claim under test is not "a ``.kicad_sch`` was written" -- it is that KiCad
can open the file and that the circuit drawn on it is the circuit the board was
built from. So the checks here re-read the emitted file with ``kiutils`` rather
than inspecting :class:`~silkscreen.schematic.SchematicResult`, for the same
reason ``test_routing.py`` and ``test_kicad.py`` do: a check written in terms of
the emitter shares the emitter's blind spots.

The bug class this guards is specific and quiet. The schematic and the board
are written by two different emitters from one spec. If they number parts
independently, both files are internally consistent, both open fine, and ``C1``
on the drawing is a different capacitor from ``C1`` on the board. Nothing
raises. A human reviews the schematic, approves it, and the fab builds the
other circuit.
"""

from __future__ import annotations

import json

import pytest
from kiutils.schematic import Schematic
from silkscreen.board import build_board
from silkscreen.netlist import PassiveType, parse_circuit_spec
from silkscreen.schematic import (
    build_schematic,
    emit_kicad_pro,
    emit_kicad_sch,
    write_project,
    write_schematic,
)

REGULATOR = {
    "devices": {"AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}}},
    "passives": {
        "Cin": {"type": "capacitor", "value": "10uF"},
        "Cout": {"type": "capacitor", "value": "22uF"},
        "Rled": {"type": "resistor", "value": "1k"},
        "D1": {"type": "diode", "value": "LED"},
    },
    "nets": {
        "VIN": ["AMS1117-3.3.VIN", "Cin.1"],
        "GND": ["AMS1117-3.3.GND", "Cin.2", "Cout.2", "D1.2"],
        "VOUT": ["AMS1117-3.3.VOUT", "Cout.1", "Rled.1"],
        "LED_A": ["Rled.2", "D1.1"],
    },
}


@pytest.fixture(scope="module")
def spec():
    return parse_circuit_spec(REGULATOR)


@pytest.fixture(scope="module")
def sheet(spec):
    return build_schematic(spec)


@pytest.fixture(scope="module")
def reparsed(sheet, tmp_path_factory):
    """The emitted schematic as KiCad's own parser sees it."""
    path = tmp_path_factory.mktemp("sch") / "reg.kicad_sch"
    write_schematic(sheet, path, project_name="reg")
    return Schematic().from_file(str(path))


def _ref_of(symbol) -> str:
    for prop in symbol.properties:
        if prop.key == "Reference":
            return prop.value
    raise AssertionError("symbol has no Reference property")


def _value_of(symbol) -> str:
    for prop in symbol.properties:
        if prop.key == "Value":
            return prop.value
    raise AssertionError("symbol has no Value property")


# ------------------------------------------------------------------ the file


def test_the_emitted_schematic_reparses(reparsed):
    """KiCad's parser is the only opinion that counts about the syntax."""
    assert reparsed.schematicSymbols, "no symbols survived the round trip"


def test_every_part_in_the_spec_becomes_exactly_one_symbol(spec, reparsed):
    assert len(reparsed.schematicSymbols) == spec.part_count()


def test_each_symbol_carries_its_own_library_definition(reparsed):
    """The file must open where no KiCad symbol library is installed.

    A ``lib_id`` pointing at a library the reader does not have gives a broken
    symbol, or worse, a *different* part with the same name.
    """
    defined = {sym.libId for sym in reparsed.libSymbols}
    used = {sym.libId for sym in reparsed.schematicSymbols}
    assert used <= defined


def test_the_project_file_is_valid_json_naming_the_schematic():
    parsed = json.loads(emit_kicad_pro("reg"))
    assert parsed["meta"]["filename"] == "reg.kicad_pro"
    assert parsed["sheets"][0][1] == "Root"


def test_the_project_file_points_at_the_schematics_own_sheet(tmp_path):
    """The pair only opens as one project if the sheet UUIDs agree."""
    sheet_uuid = json.loads(emit_kicad_pro("reg"))["sheets"][0][0]
    path = write_project(tmp_path / "reg.kicad_pro", project_name="reg")
    assert sheet_uuid in path.read_text()

    spec = parse_circuit_spec(REGULATOR)
    assert f'(uuid "{sheet_uuid}")' in emit_kicad_sch(
        build_schematic(spec), project_name="reg"
    )


# ----------------------------------------------------- schematic vs the board


def test_the_schematic_and_the_board_agree_on_every_reference(spec, reparsed):
    """The invariant ``CircuitSpec.assign_refs`` exists to hold.

    Numbered separately these two files would each be self-consistent and
    describe different circuits.
    """
    board = build_board(spec, time_limit_s=5.0)
    assert sorted(_ref_of(s) for s in reparsed.schematicSymbols) == sorted(
        p.ref for p in board.parts
    )


def test_a_reference_names_the_same_part_in_both_files(spec, reparsed):
    """Matching *sets* of refs is not enough -- R1 must be the same resistor."""
    board = build_board(spec, time_limit_s=5.0)
    board_value = {p.ref: p.value for p in board.parts}
    for symbol in reparsed.schematicSymbols:
        ref = _ref_of(symbol)
        assert _value_of(symbol) == board_value[ref], (
            f"{ref} is {_value_of(symbol)!r} on the schematic and "
            f"{board_value[ref]!r} on the board"
        )


def test_the_footprint_field_names_the_land_pattern_the_board_placed(spec):
    board = build_board(spec, time_limit_s=5.0)
    footprints = {p.ref: f"silkscreen:{p.footprint.name}" for p in board.parts}
    text = emit_kicad_sch(build_schematic(spec, footprints=footprints))
    for ref, name in footprints.items():
        assert name in text, f"{ref}'s footprint {name} is missing from the sheet"


def test_an_unknown_footprint_field_is_left_empty_rather_than_guessed(spec):
    """A schematic naming a land pattern the board did not use is a trap."""
    text = emit_kicad_sch(build_schematic(spec, footprints=None))
    assert '(property "Footprint" ""' in text


# ----------------------------------------------------------- the connectivity


def test_every_net_in_the_spec_is_labelled_on_the_sheet(spec, reparsed):
    """A net drawn nowhere is a connection silently dropped from the drawing."""
    labelled = {label.text for label in reparsed.labels}
    assert {c.net for c in spec.connections} <= labelled


def test_every_connected_pin_gets_a_wire_stub_and_a_label(spec, sheet, reparsed):
    """One label per connected pin, not one per net.

    Pin-level connections are the whole reason ``netlist.py`` requires ``C1.1``
    rather than ``C1``; collapsing them here would throw that away.
    """
    expected = sum(
        1
        for sym in sheet.symbols
        for pin in sym.shape.pins
        if sym.pin_nets.get(pin.number)
    )
    assert expected == sum(len(c.endpoints) for c in spec.connections)
    assert len(reparsed.labels) == expected


def test_each_label_sits_on_the_end_of_its_own_pins_stub(sheet, reparsed):
    """A label a hair off the wire end connects nothing.

    This is the schematic's version of a track that stops short of its pad: the
    drawing looks right and the netlist KiCad extracts is missing a connection.
    """
    wire_ends = {
        (round(pt.X, 4), round(pt.Y, 4))
        for item in reparsed.graphicalItems
        if getattr(item, "points", None)
        for pt in item.points
    }
    for label in reparsed.labels:
        at = (round(label.position.X, 4), round(label.position.Y, 4))
        assert at in wire_ends, f"label {label.text} at {at} is on no wire end"


def test_an_unconnected_pin_gets_no_label(spec):
    """An unconnected pin and a pin on a net named '' are different circuits."""
    lonely = dict(REGULATOR)
    lonely["devices"] = {
        "AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3", "NC": "4"}}
    }
    sheet = build_schematic(parse_circuit_spec(lonely))
    ic = next(s for s in sheet.symbols if s.ref.startswith("U"))
    assert "4" not in ic.pin_nets


# ------------------------------------------------------------------ the glyphs


def test_a_capacitor_does_not_look_like_a_resistor(spec):
    """A schematic that reads as correct and is not is worse than none.

    Every passive type gets its own body, so the drawing cannot quietly show
    the wrong component.
    """
    bodies = {}
    for ptype in PassiveType:
        # Both legs need a net: the validator rejects a floating passive, and
        # rightly so.
        single = {
            "devices": {},
            "passives": {
                "X": {"type": ptype.value, "value": "1"},
                "Y": {"type": "resistor", "value": "1k"},
            },
            "nets": {"A": ["X.1", "Y.1"], "B": ["X.2", "Y.2"]},
        }
        sheet = build_schematic(parse_circuit_spec(single))
        under_test = next(s for s in sheet.symbols if s.value == "1")
        bodies[ptype] = tuple(under_test.shape.graphics)
    assert len(set(bodies.values())) == len(bodies), (
        "two passive types share a body outline"
    )


# ------------------------------------------------------------------ the sheet


def test_output_is_byte_identical_across_runs():
    """Randomised UUIDs would make ``git diff`` on a schematic useless."""
    first = emit_kicad_sch(build_schematic(parse_circuit_spec(REGULATOR)))
    second = emit_kicad_sch(build_schematic(parse_circuit_spec(REGULATOR)))
    assert first == second


def test_a_circuit_too_big_for_one_sheet_says_so(spec):
    """Silently drawing off the page opens to an apparently empty sheet."""
    big = {
        "devices": {},
        "passives": {
            f"R{i}": {"type": "resistor", "value": "1k"} for i in range(200)
        },
        # A long series chain, so every leg is connected and the spec validates.
        "nets": {
            "N0": ["R0.1"] + ["R199.2"],
            **{f"N{i + 1}": [f"R{i}.2", f"R{i + 1}.1"] for i in range(199)},
        },
    }
    sheet = build_schematic(parse_circuit_spec(big))
    assert sheet.warnings, "200 symbols fitted one A4 sheet without complaint"
    assert "A4" in sheet.warnings[0] or "paper size" in sheet.warnings[0]

    assert not build_schematic(spec).warnings
