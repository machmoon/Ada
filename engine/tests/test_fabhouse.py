"""Tests for the fab-house layer: capabilities, prices, and the hard stop.

Two properties matter more than the rest here.

The first is that an unpriced quote carries no numbers. A zero in a money field
reads as "free" at a glance, and the whole purpose of the object is that a human
looks at it before spending anything.

The second is that :func:`submit_order` cannot be made to succeed. That one is
tested the way a safety interlock is tested -- by trying to defeat it.
"""

from __future__ import annotations

import json

import pytest
from silkscreen.board import build_board, route_board
from silkscreen.fab import SILK_WIDTH_NM
from silkscreen.fabhouse import (
    DEFAULT_SERVICE_ID,
    SERVICES,
    PriceBasis,
    SubmissionRefused,
    check_capabilities,
    quote,
    service_by_id,
    submit_order,
)
from silkscreen.netlist import parse_circuit_spec
from silkscreen.order import OrderIssueSeverity, OrderOptions
from silkscreen.routing import Track, Via
from silkscreen.units import mm

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
def routed():
    board = build_board(parse_circuit_spec(json.dumps(CIRCUIT)), time_limit_s=4.0)
    route_board(board)
    return board


def _codes(issues) -> set[str]:
    return {issue.code for issue in issues}


# ------------------------------------------------------------- the registry


def test_every_service_carries_the_source_for_its_own_numbers():
    """A capability without a citation is a number nobody can re-check.

    Fabs revise their limits, so these go stale by nature; the URL travelling
    with the number is what makes that recoverable rather than silent.
    """
    for service in SERVICES:
        assert service.source_url.startswith("https://"), service.id
        assert service.quote_url.startswith("https://"), service.id
        assert service.min_track_nm > 0
        assert service.min_side_nm < service.max_width_nm


def test_service_ids_are_unique_and_the_default_resolves():
    ids = [s.id for s in SERVICES]
    assert len(set(ids)) == len(ids)
    assert service_by_id(DEFAULT_SERVICE_ID).cents_per_sq_in is not None


def test_an_unknown_service_id_lists_the_known_ones():
    with pytest.raises(ValueError, match="unknown fab service"):
        service_by_id("not-a-fab")


def test_a_house_with_no_published_price_rule_says_why():
    """Silence would read as free; a stated reason reads as a missing step."""
    for service in SERVICES:
        if service.cents_per_sq_in is None:
            assert service.price_unavailable_reason
            assert "credential" in service.price_unavailable_reason.lower()


# ------------------------------------------------------------- capabilities


def test_the_generated_silkscreen_clears_every_house_minimum(routed):
    """The legend pen was 0.12 mm, under every house's minimum legend width."""
    for service in SERVICES:
        assert service.min_silk_width_nm <= SILK_WIDTH_NM, service.id
        assert "silkscreen-below-fab-minimum" not in _codes(
            check_capabilities(routed, service)
        )


def test_a_board_larger_than_the_house_panel_is_blocked(routed):
    service = service_by_id("pcbway-2layer")
    routed.width_nm, original = mm(900), routed.width_nm
    try:
        issues = check_capabilities(routed, service)
    finally:
        routed.width_nm = original
    assert "board-too-large" in _codes(issues)
    assert all(
        i.severity is OrderIssueSeverity.BLOCKER
        for i in issues
        if i.code == "board-too-large"
    )


def test_a_board_smaller_than_the_house_minimum_is_blocked(routed):
    service = service_by_id(DEFAULT_SERVICE_ID)
    original = routed.height_nm
    routed.height_nm = mm(2)
    try:
        assert "board-too-small" in _codes(check_capabilities(routed, service))
    finally:
        routed.height_nm = original


def test_a_track_under_the_house_minimum_is_blocked(routed):
    """Under the etch minimum a track is not thin, it may not be there at all."""
    service = service_by_id(DEFAULT_SERVICE_ID)
    original = list(routed.tracks)
    routed.tracks = [*original, Track(0, 0, mm(1), 0, original[0].layer, "N", mm(0.05))]
    try:
        assert "track-below-fab-minimum" in _codes(
            check_capabilities(routed, service)
        )
    finally:
        routed.tracks = original


def test_a_thin_annular_ring_is_blocked_at_the_house_that_forbids_it(routed):
    """The same via clears OSH Park and fails JLCPCB. Both answers are right."""
    assert routed.vias, "this fixture is meant to have vias"
    ring_nm = (routed.vias[0].diameter_nm - routed.vias[0].drill_nm) // 2

    oshpark = service_by_id("oshpark-2layer")
    jlcpcb = service_by_id("jlcpcb-2layer")
    assert oshpark.min_annular_ring_nm <= ring_nm < jlcpcb.min_annular_ring_nm

    assert "annular-ring-below-fab-minimum" not in _codes(
        check_capabilities(routed, oshpark)
    )
    assert "annular-ring-below-fab-minimum" in _codes(
        check_capabilities(routed, jlcpcb)
    )


def test_a_drill_under_the_house_minimum_is_blocked(routed):
    service = service_by_id(DEFAULT_SERVICE_ID)
    original = list(routed.vias)
    routed.vias = [*original, Via(0, 0, "N", mm(0.5), mm(0.05))]
    try:
        assert "drill-below-fab-minimum" in _codes(
            check_capabilities(routed, service)
        )
    finally:
        routed.vias = original


def test_assembly_at_a_bare_board_house_is_blocked(routed):
    """OSH Park sells bare boards; asking it to populate one is not an option."""
    issues = check_capabilities(
        routed, service_by_id("oshpark-2layer"), options=OrderOptions(assembly=True)
    )
    assert "assembly-not-offered" in _codes(issues)
    assert "assembly-not-offered" not in _codes(
        check_capabilities(
            routed,
            service_by_id("jlcpcb-2layer"),
            options=OrderOptions(assembly=True),
        )
    )


def test_a_thickness_the_service_does_not_build_warns_rather_than_blocks(routed):
    """The house substitutes silently, so the buyer has to hear it here."""
    issues = check_capabilities(
        routed,
        service_by_id("oshpark-2layer"),
        options=OrderOptions(thickness_mm=0.8),
    )
    substituted = [i for i in issues if i.code == "thickness-substituted"]
    assert substituted and substituted[0].severity is OrderIssueSeverity.WARNING


def test_a_layer_count_mismatch_is_blocked(routed):
    issues = check_capabilities(
        routed, service_by_id("oshpark-2layer"), options=OrderOptions(layers=4)
    )
    assert "layer-count-mismatch" in _codes(issues)


def test_a_clean_board_at_its_default_house_raises_nothing(routed):
    assert check_capabilities(routed, service_by_id(DEFAULT_SERVICE_ID)) == ()


# ------------------------------------------------------------------ quotes


def test_the_published_rule_price_is_area_times_rate_times_units(routed):
    """OSH Park's price is arithmetic, so the test does the arithmetic."""
    options = OrderOptions(quantity=5)
    result = quote(routed, options, service="oshpark-2layer")

    assert result.basis is PriceBasis.PUBLISHED_RULE
    # 5 boards at 3 per unit is 2 units, so 6 boards arrive.
    assert result.boards_ordered == 6
    assert result.subtotal_cents == round(500 * result.area_sq_in * 2)
    assert result.total_cents == result.subtotal_cents + result.shipping_cents


def test_the_billed_area_is_the_routed_outline_not_the_placement(routed):
    """The fab cuts the profile in the Gerbers, margin included, and bills that."""
    margin_nm = mm(2.0)
    width = routed.width_nm + 2 * margin_nm
    height = routed.height_nm + 2 * margin_nm
    expected = (width * height) / (25_400_000 * 25_400_000)
    assert quote(routed, service="oshpark-2layer").area_sq_in == pytest.approx(
        expected
    )
    assert expected > (routed.width_nm * routed.height_nm) / (
        25_400_000 * 25_400_000
    )


def test_quantity_rounds_up_to_the_purchasable_unit_and_says_so(routed):
    result = quote(routed, OrderOptions(quantity=4), service="oshpark-2layer")
    assert result.quantity == 4
    assert result.boards_ordered == 6
    assert any("6 will arrive" in note for note in result.notes)


def test_the_swift_service_costs_twice_the_prototype_service(routed):
    options = OrderOptions(quantity=3)
    standard = quote(routed, options, service="oshpark-2layer")
    swift = quote(routed, options, service="oshpark-2layer-swift")
    assert swift.total_cents == 2 * standard.total_cents
    assert swift.lead_time_days[1] < standard.lead_time_days[0]


def test_an_unpriced_quote_carries_no_numbers_at_all(routed):
    """A zero in a money field is indistinguishable from "free" at a glance."""
    for service_id in ("jlcpcb-2layer", "pcbway-2layer"):
        result = quote(routed, service=service_id)
        assert result.basis is PriceBasis.UNAVAILABLE
        assert not result.priced
        assert result.subtotal_cents is None
        assert result.shipping_cents is None
        assert result.total_cents is None
        assert result.total_text() == "no price"
        assert result.unavailable_reason
        assert result.as_dict()["quote_url"].startswith("https://")


def test_quote_dicts_are_json_safe(routed):
    for service in SERVICES:
        json.dumps(quote(routed, service=service).as_dict())


# ------------------------------------------------------- the hard boundary


def test_submit_order_refuses_however_it_is_called():
    """Tested the way an interlock is tested: by trying to defeat it."""
    for args, kwargs in (
        ((), {}),
        (("oshpark-2layer",), {}),
        ((), {"confirmed": True, "force": True, "i_understand": True}),
        ((object(),), {"approved_by": "a human, honestly"}),
    ):
        with pytest.raises(SubmissionRefused):
            submit_order(*args, **kwargs)


def test_the_refusal_explains_itself_rather_than_just_failing():
    with pytest.raises(SubmissionRefused) as caught:
        submit_order()
    message = str(caught.value)
    assert "does not place them" in message
    assert "the last step is a person" in message.lower()


def test_nothing_in_the_package_calls_a_fab_over_the_network():
    """The quote path is arithmetic. If that changes, this test should fail.

    A network call in this module would mean the engine can reach a fab, which
    is one short step from the engine being able to buy from one.
    """
    import silkscreen.fabhouse as module

    source = module.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    for forbidden in ("requests", "urllib.request", "httpx", "urlopen", "socket"):
        assert forbidden not in text, f"{forbidden} appears in fabhouse.py"
