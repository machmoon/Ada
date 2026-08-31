import pytest
from silkscreen.constraints import ConstraintManifest, parse_constraint_manifest


def _manifest(**updates):
    raw = {
        "version": 1,
        "approved": True,
        "board_layers": 2,
        "net_classes": [
            {
                "name": "I2C bus",
                "kind": "i2c",
                "nets": ["SDA", "SCL"],
                "allowed_layers": ["F.Cu", "B.Cu"],
                "max_layer_transitions": 2,
                "max_vias_per_net": 2,
                "pullups_required": True,
                "pullup_voltage_v": 3.3,
                "controlled_impedance": False,
                "impedance_ohms": None,
                "concerns": ["Pull-ups", "rise time"],
            }
        ],
    }
    raw.update(updates)
    return raw


def test_optional_constraint_manifest_preserves_old_requests():
    assert parse_constraint_manifest(None) is None


def test_manifest_round_trips_and_enters_the_agent_prompt():
    manifest = ConstraintManifest.from_dict(_manifest())

    assert manifest.to_dict()["net_classes"][0]["nets"] == ["SDA", "SCL"]
    assert "APPROVED NET AND ROUTING CONSTRAINT MANIFEST" in manifest.prompt_block()
    assert '"max_layer_transitions":2' in manifest.prompt_block()


def test_unapproved_manifest_is_rejected_before_build():
    with pytest.raises(ValueError, match="approved"):
        ConstraintManifest.from_dict(_manifest(approved=False))


def test_controlled_impedance_may_not_be_left_unresolved():
    raw = _manifest()
    raw["net_classes"][0].update(
        controlled_impedance=True,
        impedance_ohms=None,
        pullups_required=False,
        pullup_voltage_v=None,
    )

    with pytest.raises(ValueError, match="impedance_ohms"):
        ConstraintManifest.from_dict(raw)


def test_receipt_separates_connectivity_from_unchecked_routing():
    manifest = ConstraintManifest.from_dict(_manifest())

    accepted = manifest.receipt(["GND", "SDA", "SCL"])
    missing = manifest.receipt(["GND", "SDA"])

    assert accepted["overall"] == "verified"
    assert accepted["checks"][0]["net_presence"] == "verified"
    assert accepted["checks"][0]["routing"] == "not_checked"
    assert accepted["checks"][0]["pullups"] == "not_checked"
    assert missing["overall"] == "violated"
    assert missing["checks"][0]["missing_nets"] == ["SCL"]
