"""Tests for the datasheet-constraints extractor, gate and checker.

The property under test end to end: nothing comes out more trusted than the
pipeline established. Quotes verify only against page text that actually
contains them, the gate only ever adds review reasons, and board findings are
``PROVEN`` only for human-confirmed, quote-verified constraints.
"""

from __future__ import annotations

import json

import pytest
from silkscreen.agents.model import ScriptedModel
from silkscreen.audit.findings import Origin, Severity
from silkscreen.audit.geometry import AuditBoard, AuditPad, AuditPart, Rect
from silkscreen.constraints import (
    SCHEMA_VERSION,
    ConstraintSet,
    Decoupling,
    DocumentInfo,
    Limit,
    PowerSequencing,
    Provenance,
    Rating,
    RatingKind,
    StrapPin,
    check_board,
    extract_constraints,
    gate,
    parse_farads,
    quote_on_page,
    verify_provenance,
)
from silkscreen.constraints.extract import (
    extract_design_requirements,
    extract_ratings,
)
from silkscreen.units import mm

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _prov(page=3, quote="VDD max 3.6 V", verified=False):
    return Provenance(page=page, section="Table 1", quote=quote,
                      verified=verified)


def _trusted(constraint):
    """Mark a constraint the way the pipeline marks a fully-trusted one."""
    from dataclasses import replace

    return replace(
        constraint,
        provenance=Provenance(
            constraint.provenance.page,
            constraint.provenance.section,
            constraint.provenance.quote,
            True,
        ),
        confidence=0.95,
        needs_review=False,
        review_reason="",
    )


def _confirmed(constraint):
    from dataclasses import replace

    return replace(_trusted(constraint), confirmed=True)


def _pad(ref, number, net, x_mm, y_mm, half=0.5):
    return AuditPad(
        ref=ref, number=number, net=net,
        rect=Rect(mm(x_mm - half), mm(y_mm - half),
                  mm(x_mm + half), mm(y_mm + half)),
        layers=("F.Cu",),
    )


def _part(ref, value, pads, x_mm=0.0, y_mm=0.0):
    xs = [p.rect.x0 for p in pads] + [p.rect.x1 for p in pads]
    ys = [p.rect.y0 for p in pads] + [p.rect.y1 for p in pads]
    courtyard = Rect(min(xs), min(ys), max(xs), max(ys))
    return AuditPart(
        ref=ref, value=value, lib_id=f"test:{ref}", x_nm=mm(x_mm),
        y_nm=mm(y_mm), angle=0.0, side="F", pads=tuple(pads),
        courtyard=courtyard,
    )


def _board(parts):
    nets = sorted({p.net for part in parts for p in part.pads if p.net})
    return AuditBoard(parts=list(parts), net_names=nets)


def _mcu_board(*, cap_at=(6.0, 0.0), cap_value="100nF", extra_parts=(),
               boot_net="BOOT", boot_peers=True):
    """U1 with VDD/GND/BOOT pads, one decoupling cap, optional extras."""
    u1 = _part("U1", "MCU", [
        _pad("U1", "1", "VDD", 0.0, 0.0),
        _pad("U1", "2", "GND", 0.0, 2.0),
        _pad("U1", "3", boot_net, 0.0, 4.0),
    ])
    c1 = _part("C1", cap_value, [
        _pad("C1", "1", "VDD", cap_at[0], cap_at[1]),
        _pad("C1", "2", "GND", cap_at[0], cap_at[1] + 1.5),
    ])
    parts = [u1, c1, *extra_parts]
    if boot_peers:
        parts.append(_part("J1", "hdr", [_pad("J1", "1", boot_net, 10.0, 4.0)]))
    return _board(parts)


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------


def _full_set():
    return ConstraintSet(
        part_number="STM32F030F4",
        manufacturer="ST",
        document=DocumentInfo(title="DS", revision="4", url="http://x",
                              sha256="ab" * 32, page_count=90),
        ratings=[
            Rating(id="abs-max.vdd", kind=RatingKind.ABSOLUTE_MAXIMUM,
                   parameter="Supply voltage", symbol="VDD",
                   limit=Limit(unit="V", min=-0.3, max=4.0,
                               conditions="TA = 25 °C"),
                   pins=("VDD",), provenance=_prov(), confidence=0.9),
        ],
        decoupling=[
            Decoupling(id="decouple.vdd", rail="VDD", value=100.0, unit="nF",
                       per_pin=True, placement="as close as possible",
                       provenance=_prov(page=5), confidence=0.9),
        ],
        power_sequencing=[
            PowerSequencing(id="power-seq", rails=("VDD", "VDDA"),
                            requirement="VDDA after VDD",
                            provenance=_prov(page=6)),
        ],
        strap_pins=[
            StrapPin(id="strap.boot0", pin="BOOT0", required_state="low",
                     condition="to boot from main flash",
                     provenance=_prov(page=7)),
        ],
        extracted_at="2026-08-31",
        extractor="test",
    )


def test_schema_round_trip():
    cset = _full_set()
    rebuilt = ConstraintSet.from_dict(json.loads(json.dumps(cset.to_dict())))
    assert rebuilt == cset
    assert rebuilt.ratings[0].kind is RatingKind.ABSOLUTE_MAXIMUM
    assert rebuilt.ratings[0].pins == ("VDD",)
    assert rebuilt.power_sequencing[0].rails == ("VDD", "VDDA")


def test_schema_drops_unknown_keys():
    data = _full_set().to_dict()
    data["a_future_field"] = 1
    data["ratings"][0]["another"] = 2
    data["ratings"][0]["provenance"]["yet_another"] = 3
    rebuilt = ConstraintSet.from_dict(data)
    assert rebuilt.ratings[0].parameter == "Supply voltage"


def test_schema_refuses_other_major_version():
    data = _full_set().to_dict()
    data["schema_version"] = "2.0"
    with pytest.raises(ValueError, match="major version"):
        ConstraintSet.from_dict(data)
    # Same major, newer minor loads.
    data["schema_version"] = "1.9"
    assert ConstraintSet.from_dict(data).schema_version == "1.9"


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("part_number"),
    lambda d: d.__setitem__("ratings", "nope"),
    lambda d: d["ratings"][0].pop("id"),
    lambda d: d["ratings"][0].__setitem__("kind", "made_up_kind"),
    lambda d: d["decoupling"].__setitem__(0, "not an object"),
])
def test_schema_malformed_raises_value_error(mutate):
    data = _full_set().to_dict()
    mutate(data)
    with pytest.raises(ValueError):
        ConstraintSet.from_dict(data)


def test_needs_review_defaults_on():
    """A constraint born outside the gate is untrusted by construction."""
    c = Decoupling(id="d", rail="VDD", provenance=_prov())
    assert c.needs_review
    cset = ConstraintSet(part_number="X", decoupling=[c])
    assert cset.trusted() == []


# --------------------------------------------------------------------------
# extraction parsing
# --------------------------------------------------------------------------

_RATINGS_JSON = json.dumps({
    "ratings": [
        {"kind": "absolute_maximum", "parameter": "Supply voltage",
         "symbol": "VDD", "min": -0.3, "max": 4.0, "unit": "V",
         "conditions": "", "pins": ["VDD"], "page": 3,
         "section": "Table 1", "quote": "VDD -0.3 4.0 V",
         "confidence": 0.95},
        {"kind": "thermal", "parameter": "Junction temperature",
         "symbol": "TJ", "max": 125, "unit": "°C", "page": 60,
         "section": "Thermal", "quote": "TJ max 125", "confidence": 0.9},
        {"kind": "not_a_kind", "parameter": "dropme", "unit": "V", "page": 1,
         "quote": "x", "confidence": 1.0},
        {"kind": "thermal", "unit": "V"},  # no parameter -> dropped
        {"kind": "absolute_maximum", "parameter": "Supply voltage",
         "symbol": "VDD", "max": 4.0, "unit": "V", "page": 3,
         "quote": "dup", "confidence": 2.5},  # bad confidence -> 0.0
    ]
})


def test_extract_ratings_parses_and_filters():
    model = ScriptedModel(responses=[_RATINGS_JSON])
    out = extract_ratings(model, _doc(), "STM32F030F4")
    assert [r.id for r in out] == ["abs-max.vdd", "thermal.tj", "abs-max.vdd-2"]
    first = out[0]
    assert first.kind is RatingKind.ABSOLUTE_MAXIMUM
    assert first.limit == Limit(unit="V", min=-0.3, max=4.0, conditions="")
    assert first.provenance == Provenance(3, "Table 1", "VDD -0.3 4.0 V")
    assert first.confidence == 0.95
    assert first.needs_review  # ungated: still untrusted
    assert out[2].confidence == 0.0  # out-of-range confidence discarded


_DESIGN_JSON = json.dumps({
    "decoupling": [
        {"rail": "VDD", "value": 100, "unit": "nF", "per_pin": True,
         "placement": "as close as possible to the pin",
         "page": 55, "section": "Power supply scheme",
         "quote": "each VDD pin ... 100 nF ceramic", "confidence": 0.9},
    ],
    "power_sequencing": [
        {"rails": [], "requirement": "No sequencing required between "
         "VDD and VDDA", "page": 55, "section": "Power",
         "quote": "no constraint on the power-up sequence",
         "confidence": 0.85},
    ],
    "strap_pins": [
        {"pin": "BOOT0", "required_state": "low",
         "resistor_value": 10, "resistor_unit": "kOhm",
         "condition": "to boot from main flash", "page": 12,
         "section": "Boot configuration",
         "quote": "BOOT0 must be tied low", "confidence": 0.9},
        {"required_state": "low"},  # no pin -> dropped
    ],
})


def _doc():
    from silkscreen.agents.model import Document

    return Document(data=b"%PDF-fake")


def test_extract_design_requirements_parses_all_three():
    model = ScriptedModel(responses=[_DESIGN_JSON])
    dec, seq, straps = extract_design_requirements(model, _doc(), "STM32F030F4")
    assert [d.id for d in dec] == ["decouple.vdd"]
    assert dec[0].per_pin and dec[0].value == 100.0 and dec[0].unit == "nF"
    # "no sequencing required" with empty rails is kept: a fact worth having.
    assert len(seq) == 1 and seq[0].rails == ()
    assert [s.id for s in straps] == ["strap.boot0"]
    assert straps[0].resistor_value == 10.0


# --------------------------------------------------------------------------
# provenance verification and the gate
# --------------------------------------------------------------------------

_PAGE = (
    "Table 1. Absolute maximum ratings\n"
    "Symbol Parameter Min Max Unit\n"
    "VDD Supply voltage -0.3 4.0 V\n"
)


def test_quote_on_page_tolerates_column_soup():
    assert quote_on_page("VDD -0.3 4.0 V Supply voltage", _PAGE)


def test_quote_on_page_rejects_invented_rows():
    assert not quote_on_page("VDDQ 1.2 1.8 2.5 V DDR interface", _PAGE)
    assert not quote_on_page("", _PAGE)


def test_verify_provenance_only_on_claimed_page():
    c = Rating(id="r", parameter="VDD", limit=Limit(unit="V"),
               provenance=Provenance(page=2, quote="VDD Supply voltage 4.0"))
    verified = verify_provenance(c, ["other text", _PAGE])
    assert verified.provenance.verified
    wrong_page = verify_provenance(c, [_PAGE, "other text"])
    assert not wrong_page.provenance.verified
    out_of_range = verify_provenance(c, ["only one page"])
    assert not out_of_range.provenance.verified


def test_gate_passes_only_verified_and_confident():
    c = Rating(id="r", parameter="VDD", limit=Limit(unit="V"),
               confidence=0.9,
               provenance=Provenance(page=1, quote="VDD 4.0", verified=True))
    assert not gate(c).needs_review

    for bad, expect in [
        (Rating(id="r", parameter="p", limit=Limit(unit="V"), confidence=0.9,
                provenance=Provenance(page=1, quote="", verified=False)),
         "no verbatim quote"),
        (Rating(id="r", parameter="p", limit=Limit(unit="V"), confidence=0.9,
                provenance=Provenance(page=0, quote="x", verified=False)),
         "no source page"),
        (Rating(id="r", parameter="p", limit=Limit(unit="V"), confidence=0.9,
                provenance=Provenance(page=1, quote="x", verified=False)),
         "not found on page 1"),
        (Rating(id="r", parameter="p", limit=Limit(unit="V"), confidence=0.5,
                provenance=Provenance(page=1, quote="x", verified=True)),
         "confidence 0.50"),
    ]:
        gated = gate(bad)
        assert gated.needs_review and expect in gated.review_reason


def test_gate_reports_where_a_misplaced_quote_was_found():
    c = Rating(id="r", parameter="VDD", limit=Limit(unit="V"), confidence=0.9,
               provenance=Provenance(page=1, quote="VDD Supply voltage 4.0"))
    gated = gate(verify_provenance(c, ["nothing here", _PAGE]),
                 ["nothing here", _PAGE])
    assert gated.needs_review
    assert "not found on page 1 (found on page 2)" in gated.review_reason


def test_gate_never_unsets_confirmed():
    c = _confirmed(Decoupling(id="d", rail="VDD", provenance=_prov()))
    assert gate(c, []).confirmed


# --------------------------------------------------------------------------
# the full pipeline, offline
# --------------------------------------------------------------------------


def test_extract_constraints_end_to_end(monkeypatch):
    model = ScriptedModel(by_marker={
        "ratings and characteristics": _RATINGS_JSON,
        "design requirements": _DESIGN_JSON,
    })
    pages = ["", "", _PAGE]  # page 3 carries the VDD row; nothing else matches
    monkeypatch.setattr(
        "silkscreen.constraints.pipeline.extract_pages", lambda data: pages
    )
    events = []
    cset = extract_constraints(
        model, "STM32F030F4", pdf_bytes=b"%PDF-fake",
        manufacturer="ST", on_event=events.append,
    )
    assert cset.schema_version == SCHEMA_VERSION
    assert cset.document.page_count == 3
    assert len(cset.document.sha256) == 64
    # Only the constraint whose quote really sits on its claimed page passes.
    trusted = {c.id for c in cset.trusted()}
    assert trusted == {"abs-max.vdd"}
    reasons = {c.id: c.review_reason for c in cset.all_constraints()
               if c.needs_review}
    assert "not found on page 55" in reasons["decouple.vdd"]
    assert {e["event"] for e in events} >= {"constraints.stage",
                                            "constraints.done"}
    done = next(e for e in events if e["event"] == "constraints.done")
    assert done["trusted"] == 1


def test_extract_constraints_degrades_when_pages_fail(monkeypatch):
    from silkscreen.agents.grounding import GroundingError

    def boom(data):
        raise GroundingError("scanned PDF, no text")

    monkeypatch.setattr("silkscreen.constraints.pipeline.extract_pages", boom)
    model = ScriptedModel(by_marker={
        "ratings and characteristics": _RATINGS_JSON,
        "design requirements": _DESIGN_JSON,
    })
    events = []
    cset = extract_constraints(model, "X", pdf_bytes=b"%PDF-fake",
                               on_event=events.append)
    # Extraction still ran, but nothing can verify, so nothing is trusted.
    assert cset.all_constraints() and cset.trusted() == []
    assert any(e["event"] == "constraints.pages_failed" for e in events)


# --------------------------------------------------------------------------
# value parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text,farads", [
    ("100nF", 100e-9),
    ("0.1uF", 100e-9),
    ("0,1uF", 100e-9),
    ("4u7", 4.7e-6),
    ("100n", 100e-9),
    ("10 pF", 10e-12),
    ("1µF", 1e-6),
])
def test_parse_farads(text, farads):
    assert parse_farads(text) == pytest.approx(farads)


@pytest.mark.parametrize("text", ["", "DNP", "10k", "abc", "100 xF"])
def test_parse_farads_rejects(text):
    assert parse_farads(text) is None


# --------------------------------------------------------------------------
# checking a board
# --------------------------------------------------------------------------


def test_check_skips_unreviewed_constraints_by_default():
    c = Decoupling(id="decouple.vdd", rail="VDD", provenance=_prov())
    result = check_board(_mcu_board(), ConstraintSet(part_number="X",
                                                     decoupling=[c]),
                         ref="U1")
    assert result.findings == []
    assert result.unchecked == [("decouple.vdd", "needs human review: ")]


def test_check_decoupling_satisfied_is_quiet():
    c = _trusted(Decoupling(id="d", rail="VDD", value=100.0, unit="nF",
                            provenance=_prov()))
    result = check_board(_mcu_board(), ConstraintSet(part_number="X",
                                                     decoupling=[c]),
                         ref="U1")
    assert result.findings == [] and result.unchecked == []


def test_check_decoupling_missing_cap():
    c = _trusted(Decoupling(id="d", rail="VDDA", provenance=_prov()))
    board = _mcu_board(extra_parts=[
        _part("J2", "hdr", [_pad("J2", "1", "VDDA", 12.0, 0.0),
                            _pad("J2", "2", "VDDA", 12.0, 2.0)]),
    ])
    result = check_board(board, ConstraintSet(part_number="X",
                                              decoupling=[c]), ref="U1")
    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.severity is Severity.MARGINAL
    assert f.origin is Origin.SUGGESTED  # trusted but not human-confirmed
    assert "0 found, 1 required" in f.evidence
    assert 'p.3' in f.detail and "VDD max 3.6 V" in f.detail  # provenance shown


def test_check_decoupling_per_pin_counts_supply_pads():
    c = _trusted(Decoupling(id="d", rail="VDD", per_pin=True,
                            provenance=_prov()))
    board = _board([
        _part("U1", "MCU", [
            _pad("U1", "1", "VDD", 0.0, 0.0),
            _pad("U1", "2", "VDD", 0.0, 2.0),
            _pad("U1", "3", "GND", 0.0, 4.0),
        ]),
        _part("C1", "100nF", [_pad("C1", "1", "VDD", 5.0, 0.0),
                              _pad("C1", "2", "GND", 5.0, 1.5)]),
    ])
    result = check_board(board, ConstraintSet(part_number="X",
                                              decoupling=[c]), ref="U1")
    assert len(result.findings) == 1
    assert "1 found, 2 required" in result.findings[0].evidence


def test_check_decoupling_wrong_value_is_a_note():
    c = _trusted(Decoupling(id="d", rail="VDD", value=100.0, unit="nF",
                            provenance=_prov()))
    result = check_board(_mcu_board(cap_value="10uF"),
                         ConstraintSet(part_number="X", decoupling=[c]),
                         ref="U1")
    titles = [f.title for f in result.findings]
    assert any("C1 is 10uF" in t for t in titles)
    assert all(f.severity is Severity.NOTE for f in result.findings)


def test_check_decoupling_unparseable_value_goes_to_unchecked():
    c = _trusted(Decoupling(id="d", rail="VDD", value=100.0, unit="nF",
                            provenance=_prov()))
    result = check_board(_mcu_board(cap_value="DNP"),
                         ConstraintSet(part_number="X", decoupling=[c]),
                         ref="U1")
    assert result.findings == []
    assert any("unparseable" in reason for _, reason in result.unchecked)


def test_check_decoupling_distance_measured_against_limit():
    c = _trusted(Decoupling(id="d", rail="VDD", max_distance_mm=3.0,
                            provenance=_prov()))
    near = check_board(_mcu_board(cap_at=(2.0, 0.0)),
                       ConstraintSet(part_number="X", decoupling=[c]),
                       ref="U1")
    assert near.findings == []
    far = check_board(_mcu_board(cap_at=(20.0, 0.0)),
                      ConstraintSet(part_number="X", decoupling=[c]),
                      ref="U1")
    assert len(far.findings) == 1
    assert "20.000 mm measured, limit 3.000 mm" in far.findings[0].evidence


def test_check_decoupling_unknown_rail_is_unchecked_not_silent():
    c = _trusted(Decoupling(id="d", rail="VDDIO2", provenance=_prov()))
    result = check_board(_mcu_board(),
                         ConstraintSet(part_number="X", decoupling=[c]),
                         ref="U1")
    assert result.findings == []
    assert result.unchecked == [("d", "no board net matches rail 'VDDIO2'")]


def test_check_strap_needs_a_pin_map():
    c = _trusted(StrapPin(id="s", pin="BOOT0", required_state="low",
                          provenance=_prov()))
    result = check_board(_mcu_board(),
                         ConstraintSet(part_number="X", strap_pins=[c]),
                         ref="U1")
    assert result.unchecked == [
        ("s", "pin 'BOOT0' is not in the supplied pin map")
    ]


def test_check_strap_floating_pin_is_a_blocker():
    c = _trusted(StrapPin(id="s", pin="BOOT0", required_state="low",
                          provenance=_prov()))
    board = _mcu_board(boot_peers=False)  # BOOT net has one pad: floating
    result = check_board(board, ConstraintSet(part_number="X",
                                              strap_pins=[c]),
                         ref="U1", pin_map={"BOOT0": "3"})
    assert len(result.findings) == 1
    assert result.findings[0].severity is Severity.BLOCKER


def test_check_strap_undefined_level_is_a_blocker():
    c = _trusted(StrapPin(id="s", pin="BOOT0", required_state="low",
                          provenance=_prov()))
    result = check_board(_mcu_board(),  # BOOT goes to a header, no level
                         ConstraintSet(part_number="X", strap_pins=[c]),
                         ref="U1", pin_map={"BOOT0": "3"})
    assert len(result.findings) == 1
    assert "defined level" in result.findings[0].title


def test_check_strap_pulled_through_resistor_is_quiet():
    c = _trusted(StrapPin(id="s", pin="BOOT0", required_state="low",
                          provenance=_prov()))
    board = _mcu_board(extra_parts=[
        _part("R1", "10k", [_pad("R1", "1", "BOOT", 8.0, 4.0),
                            _pad("R1", "2", "GND", 8.0, 5.5)]),
    ])
    result = check_board(board, ConstraintSet(part_number="X",
                                              strap_pins=[c]),
                         ref="U1", pin_map={"BOOT0": "3"})
    assert result.findings == []


def test_check_strap_tied_the_wrong_way_is_a_blocker():
    c = _trusted(StrapPin(id="s", pin="BOOT0", required_state="high",
                          provenance=_prov()))
    board = _mcu_board(boot_net="GND", boot_peers=False)
    result = check_board(board, ConstraintSet(part_number="X",
                                              strap_pins=[c]),
                         ref="U1", pin_map={"BOOT0": "3"})
    assert len(result.findings) == 1
    assert "tied low" in result.findings[0].title
    assert result.findings[0].severity is Severity.BLOCKER


def test_ratings_are_reported_unchecked_not_dropped():
    r = _trusted(Rating(id="abs-max.vdd", kind=RatingKind.ABSOLUTE_MAXIMUM,
                        parameter="Supply voltage",
                        limit=Limit(unit="V", max=4.0), provenance=_prov()))
    result = check_board(_mcu_board(),
                         ConstraintSet(part_number="X", ratings=[r]),
                         ref="U1")
    assert result.findings == []
    assert result.unchecked and "not board-checkable" in result.unchecked[0][1]


def test_finding_origin_tracks_confirmation():
    """SUGGESTED until a human confirms; PROVEN after."""
    base = Decoupling(id="d", rail="VDDA", provenance=_prov())
    board = _mcu_board(extra_parts=[
        _part("J2", "hdr", [_pad("J2", "1", "VDDA", 12.0, 0.0),
                            _pad("J2", "2", "VDDA", 12.0, 2.0)]),
    ])
    suggested = check_board(board, ConstraintSet(part_number="X",
                                                 decoupling=[_trusted(base)]),
                            ref="U1")
    assert suggested.findings[0].origin is Origin.SUGGESTED
    proven = check_board(board, ConstraintSet(part_number="X",
                                              decoupling=[_confirmed(base)]),
                         ref="U1")
    assert proven.findings[0].origin is Origin.PROVEN


def test_include_needs_review_checks_but_stays_suggested():
    c = Decoupling(id="d", rail="VDDA", confidence=0.2, provenance=_prov())
    board = _mcu_board(extra_parts=[
        _part("J2", "hdr", [_pad("J2", "1", "VDDA", 12.0, 0.0),
                            _pad("J2", "2", "VDDA", 12.0, 2.0)]),
    ])
    result = check_board(board,
                         ConstraintSet(part_number="X", decoupling=[c]),
                         ref="U1", include_needs_review=True)
    assert len(result.findings) == 1
    assert result.findings[0].origin is Origin.SUGGESTED


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_writes_json_and_tallies(tmp_path, monkeypatch, capsys):
    from silkscreen.constraints import cli as ccli

    pdf = tmp_path / "ds.pdf"
    pdf.write_bytes(b"%PDF-fake")
    out = tmp_path / "out.json"

    class FakeGemini:
        model = "scripted"

        def __init__(self, name):
            self._inner = ScriptedModel(by_marker={
                "ratings and characteristics": _RATINGS_JSON,
                "design requirements": _DESIGN_JSON,
            })

        def generate(self, *a, **k):
            return self._inner.generate(*a, **k)

    monkeypatch.setattr("silkscreen.agents.model.GeminiModel", FakeGemini)
    monkeypatch.setattr(
        "silkscreen.constraints.pipeline.extract_pages",
        lambda data: ["", "", _PAGE],
    )
    code = ccli.main([str(pdf), "--part", "STM32F030F4",
                      "-o", str(out), "--report"])
    assert code == 0
    rebuilt = ConstraintSet.from_dict(json.loads(out.read_text()))
    assert rebuilt.part_number == "STM32F030F4"
    err = capsys.readouterr()
    assert "1 passed the gate" in err.err
    assert "Review worksheet" in err.out
