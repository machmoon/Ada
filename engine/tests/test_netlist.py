"""Tests for the validated circuit IR.

Most of these pin down failures the original pipeline could not detect: it fed
raw model JSON straight into SKiDL, so a bad pin name or a half-connected
capacitor became a corrupt netlist rather than an error.
"""

from __future__ import annotations

import json

import pytest
from silkscreen import (
    CircuitSpec,
    Connection,
    Device,
    Passive,
    PassiveType,
    ValidationError,
    parse_circuit_spec,
)


def _good_spec_dict():
    return {
        "devices": {
            "U1": {
                "symbol": "MCU_ST_STM32F0:STM32F030C8Tx",
                "pins": {"VDD": "1", "VSS": "8", "NRST": "7"},
            }
        },
        "passives": {
            "C1": {"type": "capacitor", "value": "100nF"},
            "R1": {"type": "resistor", "value": "10k"},
        },
        "nets": {
            "VDD": ["U1.VDD", "C1.1", "R1.1"],
            "GND": ["U1.VSS", "C1.2"],
            "NRST": ["U1.NRST", "R1.2"],
        },
    }


# ---------------------------------------------------------------- happy path


def test_parses_a_valid_spec():
    spec = parse_circuit_spec(_good_spec_dict())
    assert spec.part_count() == 3
    assert spec.net_count() == 3
    assert spec.devices[0].symbol == "MCU_ST_STM32F0:STM32F030C8Tx"
    assert spec.passives[0].type is PassiveType.CAPACITOR


def test_accepts_raw_json_text():
    spec = parse_circuit_spec(json.dumps(_good_spec_dict()))
    assert spec.part_count() == 3


def test_tolerates_a_markdown_code_fence():
    """The original prompt's own example was fenced, and json.loads died on it."""
    fenced = "```json\n" + json.dumps(_good_spec_dict()) + "\n```"
    spec = parse_circuit_spec(fenced)
    assert spec.part_count() == 3


def test_passive_ref_prefixes_match_kicad_convention():
    spec = parse_circuit_spec(_good_spec_dict())
    prefixes = {p.name: p.ref_prefix for p in spec.passives}
    assert prefixes == {"C1": "C", "R1": "R"}


# ---------------------------------------------------------------- rejections


def test_rejects_non_json():
    with pytest.raises(ValidationError, match="not valid JSON"):
        parse_circuit_spec("I'm sorry, I can't help with that.")


def test_rejects_unknown_passive_type():
    data = _good_spec_dict()
    data["passives"]["FB1"] = {"type": "ferrite_bead", "value": "600R"}
    with pytest.raises(ValidationError, match="unsupported type"):
        parse_circuit_spec(data)


def test_rejects_endpoint_referring_to_a_nonexistent_pin():
    data = _good_spec_dict()
    data["nets"]["VDD"] = ["U1.AVDD", "C1.1"]  # U1 has no AVDD
    with pytest.raises(ValidationError, match="has no pin named 'AVDD'"):
        parse_circuit_spec(data)


def test_rejects_endpoint_referring_to_an_unknown_part():
    data = _good_spec_dict()
    data["nets"]["VDD"].append("U99.VDD")
    with pytest.raises(ValidationError, match="unknown part 'U99'"):
        parse_circuit_spec(data)


def test_rejects_bare_part_name_as_endpoint():
    """Regression: the original could only connect whole parts to nets.

    That is exactly why it could not express 'cap leg 1 to VDD, leg 2 to GND'.
    Requiring an explicit terminal makes the failure loud instead of silent.
    """
    data = _good_spec_dict()
    data["nets"]["VDD"] = ["U1.VDD", "C1"]
    with pytest.raises(ValidationError, match="must be '<part>.<pin>'"):
        parse_circuit_spec(data)


def test_rejects_passive_pin_outside_1_and_2():
    data = _good_spec_dict()
    data["nets"]["VDD"] = ["U1.VDD", "C1.3"]
    with pytest.raises(ValidationError, match="only .*pins 1 and 2"):
        parse_circuit_spec(data)


def test_rejects_floating_passive():
    """A capacitor wired on one leg is a model error, not a valid circuit."""
    data = _good_spec_dict()
    data["nets"]["GND"] = ["U1.VSS", "R1.2"]  # C1.2 now unconnected
    with pytest.raises(ValidationError, match=r"C1.*no connection on pin\(s\) \['2'\]"):
        parse_circuit_spec(data)


def test_rejects_single_endpoint_net():
    data = _good_spec_dict()
    data["nets"]["DANGLING"] = ["U1.NRST"]
    with pytest.raises(ValidationError, match="fewer than 2 pins"):
        parse_circuit_spec(data)


def test_rejects_name_used_as_both_device_and_passive():
    data = _good_spec_dict()
    data["passives"]["U1"] = {"type": "resistor", "value": "1k"}
    with pytest.raises(ValidationError, match="both a device and a passive"):
        parse_circuit_spec(data)


def test_collects_every_error_at_once():
    """A repair prompt should get all problems in one pass, not one per round."""
    data = _good_spec_dict()
    data["nets"]["VDD"] = ["U1.NOPE", "C1", "U99.X"]
    with pytest.raises(ValidationError) as exc:
        parse_circuit_spec(data)
    assert len(exc.value.errors) >= 3


def test_error_message_lists_every_problem():
    spec = CircuitSpec(
        devices=[Device(name="U1", pins={"VDD": "1"})],
        passives=[Passive(name="C1", type=PassiveType.CAPACITOR, value="1u")],
        connections=[Connection(net="N1", endpoints=("U1.VDD", "C1.1"))],
    )
    with pytest.raises(ValidationError) as exc:
        spec.validate()
    # C1.2 is floating.
    assert any("C1" in e for e in exc.value.errors)


def test_rejects_a_pin_that_joins_two_nets():
    """One pin, one net. Two nets sharing a pin are electrically one net.

    Nothing downstream raises on this: CircuitSpec.nets_of keeps the last net
    it sees, the schematic labels the pin one way, and the board's pad-to-net
    map can resolve it another. Two self-consistent files, different circuits.
    """
    data = _good_spec_dict()
    data["nets"]["VDD_ALT"] = ["U1.VDD", "R1.2"]  # U1.VDD is already on VDD
    with pytest.raises(ValidationError, match=r"pin 'U1\.VDD' is on 2 nets"):
        parse_circuit_spec(data)


def test_a_pin_repeated_inside_one_net_is_not_an_error():
    """Redundant, not ambiguous: there is still only one net on that pin."""
    data = _good_spec_dict()
    data["nets"]["VDD"] = ["U1.VDD", "U1.VDD", "C1.1", "R1.1"]
    assert parse_circuit_spec(data).net_count() == 3


def test_rejects_two_pin_names_on_one_pin_number():
    """The number is what reaches the footprint and the symbol.

    A second name on the same number silently overwrites the first, so one
    specified connection disappears from both emitted files without raising.
    """
    data = _good_spec_dict()
    data["devices"]["U1"]["pins"]["VDDA"] = "1"  # VDD is already pin 1
    with pytest.raises(ValidationError) as exc:
        parse_circuit_spec(data)
    # Exactly this rule, and only this rule: an unconnected device pin is not
    # itself an error, so nothing else here should fire.
    assert [e for e in exc.value.errors if "pin number '1'" in e]
    assert len(exc.value.errors) == 1
