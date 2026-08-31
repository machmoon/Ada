"""Tests for the prepared order and the command line around it.

The behavioural tests here matter less than the structural ones. What this file
is really pinning down is that there is no way, from any object this module
produces, to place an order -- and that no future edit can add one without a
test going red.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from silkscreen.approval import BOARD_FILENAME, GATE_FILENAME, prepare_order
from silkscreen.board import build_board, route_board
from silkscreen.fabhouse import PriceBasis, SubmissionRefused
from silkscreen.netlist import parse_circuit_spec
from silkscreen.order import MANIFEST_FILENAME, OrderOptions
from silkscreen.ordercli import NO_GO, main

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
def routed(spec):
    board = build_board(spec, time_limit_s=4.0)
    route_board(board)
    return board


@pytest.fixture(scope="module")
def placed(spec):
    return build_board(spec, time_limit_s=4.0)


@pytest.fixture(scope="module")
def order(routed, spec):
    return prepare_order(routed, spec=spec, options=OrderOptions(quantity=5))


@pytest.fixture
def circuit_file(tmp_path):
    path = tmp_path / "circuit.json"
    path.write_text(json.dumps(CIRCUIT), encoding="utf-8")
    return path


# ------------------------------------------------------- the boundary itself


def test_a_prepared_order_exposes_no_way_to_place_one(order):
    """The absence is the feature, so it is asserted rather than assumed."""
    forbidden = {
        "submit",
        "place",
        "confirm",
        "buy",
        "purchase",
        "checkout",
        "pay",
        "approve",
    }
    exposed = {name for name in dir(order) if not name.startswith("_")}
    assert not (exposed & forbidden), f"a purchase path appeared: {exposed & forbidden}"


def test_human_approval_is_required_no_matter_how_clean_the_board_is(order):
    assert order.gate.go
    assert order.ready_for_human_review
    assert order.requires_human_approval
    assert order.manifest()["requires_human_approval"] is True


def test_the_only_submit_function_in_the_package_refuses():
    from silkscreen import fabhouse

    with pytest.raises(SubmissionRefused):
        fabhouse.submit_order(order=None, confirmed=True)


def test_the_summary_ends_by_handing_the_decision_to_a_person(order):
    text = order.render()
    assert "prepared, not placed" in text
    assert "NEXT STEP -- a person, not this program" in text
    assert "Silkscreen prepares orders. It does not place them." in text


def test_no_payment_field_appears_anywhere_in_the_manifest(order):
    payload = json.dumps(order.manifest()).lower()
    for word in ("card", "cvv", "payment", "credential", "api_key", "token"):
        # "credential" is allowed only inside a reason explaining that we have
        # none, which is the opposite of storing one.
        if word == "credential":
            continue
        assert word not in payload, f"{word!r} appears in the order manifest"


# ------------------------------------------------------------- what it holds


def test_the_gate_runs_over_the_files_that_ship(order):
    """A check that passes on one rendering and ships another verifies nothing."""
    completeness = next(
        c for c in order.gate.checks if c.id == "package-complete"
    )
    listed = completeness.evidence[1].removeprefix("present: ").split(", ")
    assert sorted(f.filename for f in order.files) == listed


def test_the_manifest_cannot_claim_orderable_over_a_failing_gate(placed, spec):
    manifest = prepare_order(placed, spec=spec).manifest()
    assert manifest["gate"]["go"] is False
    assert manifest["orderable"] is False


def test_a_failing_gate_still_produces_the_whole_order(placed, spec):
    """A verdict with no evidence behind it leaves the reader nowhere to go."""
    order = prepare_order(placed, spec=spec)
    assert not order.gate.go
    assert order.files
    assert order.render()
    assert "NOT ready" in order.render()
    assert order.package()


def test_the_quote_travels_with_the_order(order):
    assert order.quote.basis is PriceBasis.PUBLISHED_RULE
    assert order.quote.total_cents == order.manifest()["quote"]["total_cents"]
    assert f"${order.quote.total_cents / 100:,.2f}" in order.render()


def test_an_unpriced_house_says_so_in_the_summary(routed, spec):
    order = prepare_order(routed, spec=spec, service="pcbway-2layer")
    text = order.render()
    assert "No price." in text
    assert "Quote it yourself at: https://www.pcbway.com" in text


# ---------------------------------------------------------------- packaging


def test_the_zip_carries_the_package_the_board_and_the_gate(order):
    with zipfile.ZipFile(io.BytesIO(order.package())) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read(MANIFEST_FILENAME))
    assert {f.filename for f in order.files} <= names
    assert BOARD_FILENAME in names
    assert GATE_FILENAME in names
    assert manifest["gate"]["go"] is True


def test_the_zip_is_byte_identical_across_calls(order):
    """Two runs over the same inputs must hash the same or nothing can be diffed."""
    assert order.package() == order.package()


def test_write_lays_the_whole_order_out_on_disk(order, tmp_path):
    written = order.write(tmp_path / "out")
    names = {path.name for path in written}
    assert {f.filename for f in order.files} <= names
    assert {
        BOARD_FILENAME,
        GATE_FILENAME,
        MANIFEST_FILENAME,
        "order-summary.txt",
        "order-package.zip",
    } <= names
    assert all(path.exists() for path in written)


# ---------------------------------------------------------------------- cli


def test_the_cli_exits_zero_on_a_go(circuit_file, tmp_path, capsys):
    code = main([str(circuit_file), "--quantity", "5", "--out", str(tmp_path / "o")])
    assert code == 0
    assert "VERDICT: GO" in capsys.readouterr().out


def test_the_cli_exits_two_on_a_no_go(circuit_file, capsys):
    """A distinct code, because a refused board is a successful run."""
    code = main([str(circuit_file), "--no-route"])
    assert code == NO_GO
    assert "VERDICT: NO-GO" in capsys.readouterr().out


def test_the_cli_reports_bad_options_without_solving_anything(circuit_file, capsys):
    assert main([str(circuit_file), "--quantity", "0"]) == 1
    assert "bad order options" in capsys.readouterr().err


def test_the_cli_lists_services_with_their_sources(capsys):
    assert main(["--list-services"]) == 0
    out = capsys.readouterr().out
    assert "oshpark-2layer" in out
    assert "no published price rule" in out
    assert "https://" in out


def test_the_cli_json_output_is_the_manifest(circuit_file, capsys):
    main([str(circuit_file), "--json"])
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["requires_human_approval"] is True
    assert manifest["gate"]["go"] is True


def test_the_cli_has_no_flag_that_places_an_order():
    from silkscreen.ordercli import build_parser

    flags = {
        option
        for action in build_parser()._actions
        for option in action.option_strings
    }
    for forbidden in ("--submit", "--place", "--buy", "--confirm", "--yes"):
        assert forbidden not in flags
