"""The Cloud Run surface, driven over a real socket with a scripted model."""

import http.client
import json
import os
import re
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from silkscreen.agents.model import ScriptedModel
from silkscreen.kicad import footprint_ref, load_board
from silkscreen.units import to_mm

from service.app import Handler, make_server
from service.cache import MemoryFactStore

try:
    import google.adk  # noqa: F401

    _HAS_ADK = True
except ImportError:  # a base install has no google.adk; the ADK test skips
    _HAS_ADK = False

REPO_ROOT = Path(__file__).resolve().parents[2]

CIRCUIT = {
    "devices": {"U1": {"pins": {"1": "GND", "2": "VOUT", "3": "VIN"}}},
    "passives": {
        "C1": {"type": "capacitor", "value": "10uF"},
        "C2": {"type": "capacitor", "value": "100nF"},
    },
    "nets": {
        "VIN": ["U1.3", "C1.1"],
        "GND": ["U1.1", "C1.2", "C2.2"],
        "VOUT": ["U1.2", "C2.1"],
    },
}
#: One finding of each severity. Every ref named here exists in CIRCUIT --
#: review_circuit drops part references the spec does not contain, so a made-up
#: ref would arrive as a silently empty parts list.
REVIEW = {
    "findings": [
        {
            "severity": "blocker",
            "title": "VOUT has no bulk capacitor",
            "detail": "The regulator needs bulk capacitance on its output to stay "
            "stable. Without it the loop oscillates.",
            "parts": ["U1", "C2"],
            "citation": "AMS1117-3.3 datasheet p.9",
            "suggested_fix": "Add a 22uF tantalum from VOUT to GND.",
        },
        {
            "severity": "marginal",
            "title": "Input capacitor is smaller than recommended",
            "detail": "10uF works, but the datasheet recommends more where the "
            "supply leads are long.",
            "parts": ["C1"],
            "citation": "",
            "suggested_fix": "Raise C1 to 22uF.",
        },
        {
            "severity": "note",
            "title": "No thermal relief on the tab",
            "detail": "The package dissipates through its tab; copper area sets "
            "the usable current.",
            "parts": ["U1"],
            "citation": "",
            "suggested_fix": "",
        },
    ]
}

#: What the model returns when it is asked to read a datasheet. Without this
#: the scripted model has no answer for a read, so any test that actually
#: triggers one fails as an upstream error rather than exercising the path.
DATASHEET = {
    "part_number": "AMS1117-3.3",
    "package": "SOT-223-3",
    "pin_count": 3,
    "pins": [
        {"number": "1", "name": "GND", "kind": "ground", "page": 1},
        {"number": "2", "name": "VOUT", "kind": "output", "page": 1},
        {"number": "3", "name": "VIN", "kind": "input", "page": 1},
    ],
    "requirements": [{"requirement": "22uF tantalum on VOUT", "page": 9}],
    "auxiliaries": [],
    "notes": "",
}


def scripted():
    return ScriptedModel(
        by_marker={
            "designing a printed circuit board": json.dumps(CIRCUIT),
            "reviewing a circuit someone else designed": json.dumps(REVIEW),
            "reading an electronic component datasheet": json.dumps(DATASHEET),
        }
    )


@pytest.fixture
def server():
    store = MemoryFactStore()
    Handler.model_factory = staticmethod(scripted)
    Handler.store = store
    srv = make_server(port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    srv.store = store
    yield srv
    srv.shutdown()
    srv.server_close()
    Handler.store = None


@pytest.fixture
def web_dist(tmp_path):
    """A minimal built bundle, installed on the handler for one test.

    The two sentinels outside the bundle are what makes the traversal cases
    mean anything: with nothing above the root to reach, every one of them
    would 404 on a missing file and pass whether the resolver defends the
    root or not.
    """
    (tmp_path / "OUTSIDE.txt").write_bytes(b"LEAKED")
    (tmp_path.parent / "OUTSIDE2.txt").write_bytes(b"LEAKED")
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><title>silkscreen</title>", encoding="utf-8"
    )
    (dist / "assets" / "app-abc123.js").write_text(
        "export const ok = true;\n", encoding="utf-8"
    )
    (dist / "assets" / "app-abc123.css").write_text(
        ":root{--paper:#F5F4EF}\n", encoding="utf-8"
    )
    # A build output whose name collides with the health probe.
    (dist / "healthz").write_text("not the health check", encoding="utf-8")
    # Copied verbatim from frontend/public: served from the bundle root, and its
    # name carries no content hash.
    (dist / "favicon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8"
    )
    previous = Handler.web_root
    Handler.web_root = dist
    yield dist
    Handler.web_root = previous


def url(srv, path):
    return f"http://127.0.0.1:{srv.server_port}{path}"


def get(srv, path):
    try:
        with urllib.request.urlopen(url(srv, path)) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def raw_get(srv, target):
    """GET with the request-target sent exactly as written.

    urllib collapses ".." in the client, so a traversal test written with it
    never reaches the server and would pass vacuously.
    """
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_port)
    try:
        conn.request("GET", target)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def post(srv, payload, path="/generate"):
    req = urllib.request.Request(
        url(srv, path),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_health_check(server):
    with urllib.request.urlopen(url(server, "/healthz")) as resp:
        assert resp.status == 200
        assert json.loads(resp.read())["ok"] is True


def test_unknown_route_is_404(server):
    try:
        urllib.request.urlopen(url(server, "/nope"))
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
    else:
        pytest.fail("expected 404")


def test_generate_returns_a_board(server):
    status, body = post(server, {"intent": "a 3.3V regulator", "time_limit_s": 5})
    assert status == 200
    assert body["kicad_pcb"].startswith("(kicad_pcb")
    assert [p["ref"] for p in body["parts"]] == ["U1", "C1", "C2"]
    assert body["board_mm"][0] > 0


@pytest.mark.skipif(not _HAS_ADK, reason="the 'adk' extra is not installed")
def test_the_adk_engine_answers_with_the_same_contract(monkeypatch, server):
    """Which driver ran is an implementation detail; the response is not.

    ``SILKSCREEN_ENGINE`` is read inside ``generate_pcb``, and the handler runs
    in a thread of this process, so setting it here picks the driver for the
    next request. Both halves are set, not just the second: an exported
    ``SILKSCREEN_ENGINE`` would otherwise decide the baseline, and a developer
    running the suite with the workflow already selected would be comparing
    ADK against ADK and asserting nothing.

    The counter is what proves the workflow ran at all. Every assertion below
    holds trivially if both requests took the same driver, so the run is
    observed directly rather than inferred from a response that is designed
    not to distinguish them. The key set is compared against the SDK body
    rather than against a literal list: the response is additive-only, and a
    hand-written list would go stale the next time a field is added to it.
    """
    import silkscreen.agents.adk.runner as adk_runner

    ran = []
    workflow_driver = adk_runner.generate_pcb_adk

    def counting(*args, **kwargs):
        ran.append(1)
        return workflow_driver(*args, **kwargs)

    # ``generate_pcb`` does ``from .adk.runner import generate_pcb_adk`` inside
    # the call, so the name is looked up on this module every time and patching
    # the attribute here intercepts the dispatcher.
    monkeypatch.setattr(adk_runner, "generate_pcb_adk", counting)

    payload = {"intent": "a 3.3V regulator", "time_limit_s": 5}
    monkeypatch.setenv("SILKSCREEN_ENGINE", "sdk")
    sdk_status, sdk_body = post(server, payload)
    assert ran == [], "the baseline request went through the ADK workflow"

    monkeypatch.setenv("SILKSCREEN_ENGINE", "adk")
    status, body = post(server, payload)
    assert ran == [1], "the second request never reached the ADK workflow"

    assert sdk_status == 200 and status == 200
    assert set(body) == set(sdk_body)
    assert body["kicad_pcb"].startswith("(kicad_pcb")
    assert [p["ref"] for p in body["parts"]] == [p["ref"] for p in sdk_body["parts"]]
    assert body["board_mm"] == sdk_body["board_mm"]
    assert body["findings"] == sdk_body["findings"]
    assert body["blockers"] == sdk_body["blockers"]


def test_intent_is_required(server):
    status, body = post(server, {})
    assert status == 400 and "intent" in body["error"]


def test_datasheets_must_be_an_object(server):
    status, body = post(server, {"intent": "x", "datasheets": ["a"]})
    assert status == 400 and "datasheets" in body["error"]


def test_malformed_json_is_400(server):
    req = urllib.request.Request(
        url(server, "/generate"), data=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req)
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
    else:
        pytest.fail("expected 400")


def test_non_object_body_is_400(server):
    status, _ = post(server, [1, 2, 3])
    assert status == 400


def _cached_facts(part="AMS1117-3.3"):
    """A cache entry with real content, as the service now writes them."""
    return {
        "part_number": part,
        "package": "SOT-223-3",
        "pin_count": 3,
        "pins": [
            {"number": "1", "name": "GND", "kind": "ground", "page": 1},
            {"number": "2", "name": "VOUT", "kind": "output", "page": 1},
            {"number": "3", "name": "VIN", "kind": "input", "page": 1},
        ],
        "requirements": [{"requirement": "22uF tantalum on VOUT", "page": 9}],
        "auxiliaries": [],
        "notes": "",
        "source_url": "https://x/a.pdf",
    }


def test_a_cached_part_is_not_read_again(server):
    server.store.put("AMS1117-3.3", _cached_facts())
    status, body = post(
        server,
        {
            "intent": "a regulator",
            "datasheets": {"AMS1117-3.3": "https://x/a.pdf"},
            "time_limit_s": 5,
        },
    )
    assert status == 200
    assert body["cache"]["hit"] == ["AMS1117-3.3"]
    assert body["cache"]["read"] == [], "a cache hit must skip the datasheet read"


def test_a_cache_hit_still_supplies_the_facts(monkeypatch, server):
    """Skipping the read must not mean designing without the facts.

    The first version of this cache stored only a part number and passed
    nothing to the pipeline, so a hit silently produced a board designed as
    though the part were undocumented -- strictly worse than not caching.
    """
    seen = {}

    import service.app as app

    real = app.generate_pcb

    def spy(model, intent, **kw):
        seen.update(kw)
        return real(model, intent, **kw)

    monkeypatch.setattr(app, "generate_pcb", spy)

    server.store.put("AMS1117-3.3", _cached_facts())
    status, _ = post(
        server,
        {
            "intent": "a regulator",
            "datasheets": {"AMS1117-3.3": "https://x/a.pdf"},
            "time_limit_s": 5,
        },
    )
    assert status == 200

    preloaded = seen.get("preloaded_facts") or []
    assert [f.part_number for f in preloaded] == ["AMS1117-3.3"]
    assert preloaded[0].pin_map() == {"GND": "1", "VOUT": "2", "VIN": "3"}, (
        "the cached pins must reach the pipeline, not just the part number"
    )


def test_an_unreadable_cache_entry_falls_back_to_reading(server):
    """A legacy or corrupt entry is a miss, not a failed request."""
    server.store.put("AMS1117-3.3", {"pins": "not a list"})
    status, body = post(
        server,
        {
            "intent": "a regulator",
            "datasheets": {"AMS1117-3.3": "https://x/a.pdf"},
            "time_limit_s": 5,
        },
    )
    assert status == 200
    assert body["cache"]["unusable"] == ["AMS1117-3.3"]
    assert body["cache"]["read"] == ["AMS1117-3.3"]
    assert body["cache"]["hit"] == []


def test_upstream_failure_is_502_not_500(server):
    class Dead:
        def generate(self, *a, **kw):
            from silkscreen.agents.model import ModelError

            raise ModelError("gemini 503")

    Handler.model_factory = staticmethod(Dead)
    try:
        status, body = post(server, {"intent": "x", "time_limit_s": 5})
        assert status == 502, "an upstream outage is not the caller's fault"
        assert "503" in body["error"]
    finally:
        Handler.model_factory = staticmethod(scripted)


def test_findings_are_returned_in_full(server):
    status, body = post(server, {"intent": "a 3.3V regulator", "time_limit_s": 5})
    assert status == 200
    assert [f["severity"] for f in body["findings"]] == [
        "blocker",
        "marginal",
        "note",
    ], "severities serialize as the enum value, not its repr"
    first = body["findings"][0]
    assert set(first) == {
        "severity",
        "title",
        "detail",
        "parts",
        "refs",
        "citation",
        "suggested_fix",
    }
    assert first["parts"] == ["U1", "C2"]
    assert first["citation"] == "AMS1117-3.3 datasheet p.9"
    assert first["suggested_fix"].startswith("Add a 22uF")


#: A circuit named the way PROPOSE_PROMPT actually asks for one: devices keyed
#: by part number, passives by a descriptive id. CIRCUIT above names its parts
#: "U1"/"C1"/"C2", which are also the designators build_board hands out -- so a
#: client keying off finding.parts appears to work there and matches nothing on
#: any real board. This fixture is what keeps that from passing again.
NAMED_CIRCUIT = {
    "devices": {"AMS1117-3.3": {"pins": {"1": "GND", "2": "VOUT", "3": "VIN"}}},
    "passives": {
        "c_bulk_vin": {"type": "capacitor", "value": "10uF"},
        "c_dec_vout": {"type": "capacitor", "value": "100nF"},
    },
    "nets": {
        "VIN": ["AMS1117-3.3.3", "c_bulk_vin.1"],
        "GND": ["AMS1117-3.3.1", "c_bulk_vin.2", "c_dec_vout.2"],
        "VOUT": ["AMS1117-3.3.2", "c_dec_vout.1"],
    },
}
NAMED_REVIEW = {
    "findings": [
        {
            "severity": "blocker",
            "title": "VOUT has no bulk capacitor",
            "detail": "The loop oscillates without output bulk capacitance.",
            "parts": ["AMS1117-3.3", "c_dec_vout"],
            "citation": "",
            "suggested_fix": "Add a 22uF tantalum from VOUT to GND.",
        }
    ]
}


def scripted_named():
    return ScriptedModel(
        by_marker={
            "designing a printed circuit board": json.dumps(NAMED_CIRCUIT),
            "reviewing a circuit someone else designed": json.dumps(NAMED_REVIEW),
        }
    )


def test_findings_carry_board_refs_not_only_spec_names(server):
    """A finding has to be pointable at the board it is about.

    The reviewer speaks the spec's vocabulary and the board speaks in
    designators; the response has to carry both, or highlighting a finding on
    the board is a lookup that can only fail.
    """
    Handler.model_factory = staticmethod(scripted_named)
    try:
        status, body = post(server, {"intent": "a 3.3V regulator", "time_limit_s": 5})
    finally:
        Handler.model_factory = staticmethod(scripted)
    assert status == 200

    finding = body["findings"][0]
    assert finding["parts"] == ["AMS1117-3.3", "c_dec_vout"]
    assert finding["refs"] == ["U1", "C2"]

    on_board = {p["ref"] for p in body["placements"]["parts"]}
    assert set(finding["refs"]) <= on_board, "a ref must name a part on the board"
    assert not set(finding["parts"]) & on_board, (
        "the fixture is pointless unless the spec names really do differ"
    )


def test_schematic_carries_validated_parts_pins_and_nets(server):
    """The browser gets topology, not a CircuitSpec serialization accident."""
    Handler.model_factory = staticmethod(scripted_named)
    try:
        status, body = post(server, {"intent": "a 3.3V regulator", "time_limit_s": 5})
    finally:
        Handler.model_factory = staticmethod(scripted)
    assert status == 200

    schematic = body["schematic"]
    assert set(schematic) == {"version", "parts", "nets"}
    assert schematic["version"] == 1
    assert [part["id"] for part in schematic["parts"]] == [
        "AMS1117-3.3",
        "c_bulk_vin",
        "c_dec_vout",
    ]
    assert [part["ref"] for part in schematic["parts"]] == ["U1", "C1", "C2"]

    by_id = {part["id"]: part for part in schematic["parts"]}
    assert set(by_id["AMS1117-3.3"]) == {
        "id",
        "ref",
        "kind",
        "value",
        "symbol",
        "pins",
    }
    assert by_id["AMS1117-3.3"]["kind"] == "device"
    assert by_id["AMS1117-3.3"]["pins"] == [
        {"name": "1", "number": "GND"},
        {"name": "2", "number": "VOUT"},
        {"name": "3", "number": "VIN"},
    ]
    assert by_id["c_bulk_vin"]["kind"] == "capacitor"
    assert by_id["c_bulk_vin"]["value"] == "10uF"
    assert by_id["c_bulk_vin"]["pins"] == [
        {"name": "1", "number": "1"},
        {"name": "2", "number": "2"},
    ]

    declared = {net["name"] for net in schematic["nets"]}
    assert declared == set(NAMED_CIRCUIT["nets"])
    for net in schematic["nets"]:
        assert set(net) == {"name", "endpoints"}
        assert len(net["endpoints"]) >= 2
        for endpoint in net["endpoints"]:
            assert set(endpoint) == {"part_id", "ref", "pin", "number"}
            part = by_id[endpoint["part_id"]]
            assert endpoint["ref"] == part["ref"]
            assert {"name": endpoint["pin"], "number": endpoint["number"]} in part[
                "pins"
            ]


def test_blockers_stay_flattened_strings(server):
    """The old field keeps its old shape: it has readers."""
    status, body = post(server, {"intent": "a 3.3V regulator", "time_limit_s": 5})
    assert status == 200
    assert all(isinstance(b, str) for b in body["blockers"])
    assert len(body["blockers"]) == sum(
        1 for f in body["findings"] if f["severity"] == "blocker"
    )


def test_duration_is_reported(server):
    status, body = post(server, {"intent": "a 3.3V regulator", "time_limit_s": 5})
    assert status == 200
    assert isinstance(body["duration_s"], (int, float))
    assert body["duration_s"] >= 0


def test_warnings_and_nets_are_reported(server):
    status, body = post(server, {"intent": "a 3.3V regulator", "time_limit_s": 5})
    assert status == 200
    assert isinstance(body["warnings"], list)
    assert set(body["nets"]) == {"VIN", "GND", "VOUT"}


def test_datasheets_read_are_reported(server):
    status, body = post(
        server,
        {
            "intent": "a regulator",
            "datasheets": {"AMS1117-3.3": "https://x/a.pdf"},
            "time_limit_s": 5,
        },
    )
    assert status == 200
    assert body["datasheets"] == [
        {
            "part": "AMS1117-3.3",
            "package": "SOT-223-3",
            "pins": 3,
            "requirements": 1,
            "url": "https://x/a.pdf",
        }
    ]


def test_datasheets_are_reported_on_a_cache_hit(server):
    """A hit skips the read; the facts still have to be reported."""
    server.store.put("AMS1117-3.3", _cached_facts())
    status, body = post(
        server,
        {
            "intent": "a regulator",
            "datasheets": {"AMS1117-3.3": "https://x/a.pdf"},
            "time_limit_s": 5,
        },
    )
    assert status == 200
    assert body["cache"]["hit"] == ["AMS1117-3.3"]
    assert [d["part"] for d in body["datasheets"]] == ["AMS1117-3.3"]
    assert body["datasheets"][0]["pins"] == 3


def test_review_can_be_skipped(server):
    status, body = post(
        server, {"intent": "a regulator", "time_limit_s": 5, "review": False}
    )
    assert status == 200
    assert body["findings"] == []
    assert body["blockers"] == []


def test_index_is_served_at_the_root(server, web_dist):
    status, headers, body = get(server, "/")
    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert b"silkscreen" in body


def test_assets_get_real_content_types(server, web_dist):
    """Windows mimetypes maps .js to text/plain; browsers then refuse it."""
    status, headers, _ = get(server, "/assets/app-abc123.js")
    assert status == 200
    assert headers["Content-Type"] == "text/javascript; charset=utf-8"

    status, headers, _ = get(server, "/assets/app-abc123.css")
    assert status == 200
    assert headers["Content-Type"] == "text/css; charset=utf-8"


def test_healthz_beats_a_bundle_file_of_the_same_name(server, web_dist):
    status, headers, body = get(server, "/healthz")
    assert status == 200
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body)["ok"] is True


def test_generate_is_unaffected_by_the_bundle(server, web_dist):
    status, body = post(server, {"intent": "a 3.3V regulator", "time_limit_s": 5})
    assert status == 200
    assert body["kicad_pcb"].startswith("(kicad_pcb")


def test_traversal_targets_are_reachable_on_disk(web_dist):
    """The positive control for the traversal cases below. If this fails they
    are testing a missing file, not a defended root."""
    assert (web_dist / ".." / "OUTSIDE.txt").resolve().read_bytes() == b"LEAKED"
    assert (web_dist / ".." / ".." / "OUTSIDE2.txt").resolve().read_bytes() == b"LEAKED"


@pytest.mark.parametrize(
    "target",
    [
        "/../OUTSIDE.txt",
        "/..%2fOUTSIDE.txt",
        "/%2e%2e/%2e%2e/OUTSIDE2.txt",
        "/..%5cOUTSIDE.txt",
        "/assets/../../OUTSIDE.txt",
    ],
)
def test_path_traversal_is_refused(server, web_dist, target):
    status, body = raw_get(server, target)
    assert status == 404
    assert b"LEAKED" not in body


def test_unknown_route_is_404_with_a_bundle(server, web_dist):
    status, _, body = get(server, "/nope")
    assert status == 404
    assert json.loads(body)["error"]


def test_json_root_survives_when_there_is_no_bundle(server):
    previous = Handler.web_root
    Handler.web_root = None
    try:
        status, headers, body = get(server, "/")
        assert status == 200
        assert headers["Content-Type"] == "application/json"
        assert json.loads(body)["service"] == "silkscreen"
    finally:
        Handler.web_root = previous


def test_cache_headers(server, web_dist):
    _, headers, _ = get(server, "/")
    assert headers["Cache-Control"] == "no-cache", "index names hashed assets"

    _, headers, _ = get(server, "/assets/app-abc123.js")
    assert headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_a_non_asset_file_is_not_cached_for_a_year(server, web_dist):
    """Only assets/ carries a content hash, so only assets/ may be immutable.

    favicon.svg is copied from frontend/public under a fixed name. Pinned for a
    year it cannot be replaced at all -- the URL never changes, so there is no
    bust path short of waiting the year out.
    """
    _, headers, body = get(server, "/favicon.svg")
    assert headers["Cache-Control"] == "no-cache"
    assert headers["Content-Type"] == "image/svg+xml"
    assert b"<svg" in body


# ------------------------------------------------------- request-level errors


def test_a_non_numeric_time_limit_is_400_naming_the_field(server):
    """A time limit that is not a number is the caller's error, not a crash.

    ``generate`` coerces the field itself and raises ValueError naming
    ``time_limit_s``, which is the one thing a caller needs in order to fix
    the request.
    """
    status, body = post(server, {"intent": "a regulator", "time_limit_s": "abc"})
    assert status == 400
    assert "time_limit_s" in body["error"]


def test_a_null_time_limit_is_400(server):
    """A JSON null time limit is the caller's error, like every other bad value.

    ``payload.get("time_limit_s", DEFAULT_TIME_LIMIT)`` returns the default
    only when the key is *absent*; an explicit null returns None, and
    ``float(None)`` raises TypeError rather than ValueError. This used to fall
    past the 400 handler into the generic 500 -- a client error answered with
    a server error, and an error_id in the log for a request nobody needs to
    investigate.
    """
    status, body = post(server, {"intent": "a regulator", "time_limit_s": None})
    assert status == 400
    assert "time_limit_s" in body["error"]
    assert "error_id" not in body, "a caller's bad value is not an incident"


def test_an_internal_typeerror_is_still_a_500(monkeypatch, server):
    """The 400 net stays narrow: only field-level failures are the caller's.

    A TypeError from inside the pipeline (model output putting an object
    where a number belongs, say) is our bug, and must keep its error_id and
    logged traceback instead of leaking a raw internal message as a 400.
    """
    import service.app as app

    def boom(*a, **kw):
        raise TypeError("unsupported operand hidden deep in the pipeline")

    monkeypatch.setattr(app, "generate_pcb", boom)
    status, body = post(server, {"intent": "a regulator", "time_limit_s": 5})
    assert status == 500
    assert body["error"] == "internal error"
    assert "error_id" in body
    assert "unsupported operand" not in json.dumps(body)


def post_without_content_length(srv, path="/generate"):
    """POST with no Content-Length header at all.

    Written on a raw socket on purpose: http.client inserts
    ``Content-Length: 0`` for a bodyless POST, so the same test written
    through urllib or http.client would pass without ever exercising the
    missing header.
    """
    request = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{srv.server_port}\r\n"
        "Content-Type: application/json\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode()
    with socket.create_connection(("127.0.0.1", srv.server_port), timeout=10) as sock:
        sock.sendall(request)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].decode().split()[1])
    return status, json.loads(body)


def test_a_post_without_content_length_reads_as_an_empty_body(server):
    """No Content-Length means no body, which means the intent-required 400.

    The handler defaults a missing header to zero and then substitutes ``{}``
    for the empty read, so the request reaches the same validation a ``{}``
    body does instead of hanging on a read or dropping the connection.
    """
    status, body = post_without_content_length(server)
    assert status == 400
    assert "intent" in body["error"]


# ----------------------------------------------------------------- served_by


def test_served_by_is_present_and_null_without_a_provider(server):
    """The field is always there; ScriptedModel just has nothing to report.

    A client reading ``served_by`` should not have to distinguish "key
    missing" from "no provider recorded", so the getattr default has to reach
    the response body rather than drop the key.
    """
    status, body = post(server, {"intent": "a 3.3V regulator", "time_limit_s": 5})
    assert status == 200
    assert "served_by" in body
    assert body["served_by"] is None


class TaggedModel:
    """A model that names the provider that answered, as FallbackModel does.

    ScriptedModel carries no ``last_provider``, so without this the echo half
    of ``getattr(model, "last_provider", None)`` is never exercised.
    """

    last_provider = "gemini-cheap"

    def __init__(self):
        self._inner = scripted()

    def generate(self, *args, **kwargs):
        return self._inner.generate(*args, **kwargs)


def test_served_by_echoes_the_provider_that_answered(server):
    Handler.model_factory = staticmethod(TaggedModel)
    try:
        status, body = post(server, {"intent": "a 3.3V regulator", "time_limit_s": 5})
        assert status == 200
        assert body["served_by"] == "gemini-cheap"
    finally:
        Handler.model_factory = staticmethod(scripted)


# ----------------------------------------------------------- board round-trip


def test_the_returned_board_reparses_as_a_real_kicad_file(server, tmp_path):
    """The string in the response has to survive being opened as a board.

    Every other assertion about ``kicad_pcb`` checks a prefix, which a
    truncated or malformed file would also satisfy. Parsing it back and
    recovering the same reference designators the response advertises is what
    proves the caller was handed something KiCad can actually open.
    """
    status, body = post(server, {"intent": "a 3.3V regulator", "time_limit_s": 5})
    assert status == 200

    path = tmp_path / "from_response.kicad_pcb"
    path.write_text(body["kicad_pcb"], encoding="utf-8")
    reloaded = load_board(path)

    advertised = {p["ref"] for p in body["parts"]}
    assert advertised, "the response must name at least one part"
    assert {footprint_ref(f) for f in reloaded.footprints} == advertised
    assert len(reloaded.footprints) == len(body["parts"])


# ------------------------------------------------------------------ placements


#: Every key a placements part must carry, exactly. Spelled out rather than
#: checked with ``in``: a renderer that reads a key the server stopped sending
#: draws nothing, and a key the server started sending silently goes unused.
PART_KEYS = {
    "ref",
    "footprint",
    "value",
    "layer",
    "rotated",
    "x_mm",
    "y_mm",
    "courtyard_mm",
    "pads",
}


def placements_of(srv):
    status, body = post(srv, {"intent": "a 3.3V regulator", "time_limit_s": 5})
    assert status == 200
    return body


def test_placements_carry_the_contract(server):
    body = placements_of(server)
    placements = body["placements"]
    assert set(placements) == {"board_mm", "frame", "parts"}
    assert placements["frame"] == "solver-y-up"
    assert placements["board_mm"] == body["board_mm"], (
        "one board size, reported once -- two copies that can disagree is a bug"
    )
    assert [p["ref"] for p in placements["parts"]] == [
        p["ref"] for p in body["parts"]
    ]

    for part in placements["parts"]:
        assert set(part) == PART_KEYS
        assert isinstance(part["rotated"], bool)
        assert part["layer"] in ("top", "bottom")
        assert part["value"] is None or isinstance(part["value"], str)
        assert len(part["courtyard_mm"]) == 4
        assert part["pads"], f"{part['ref']} has no pads"
        for pad in part["pads"]:
            assert set(pad) == {"number", "net", "rect_mm"}
            assert isinstance(pad["number"], str)
            assert len(pad["rect_mm"]) == 4


def test_every_rect_is_numeric_and_inside_the_board(server):
    """A rect outside the board is a broken transform, not a loose bound.

    The solver constrains every part to the board it sized, so nothing it
    placed can legitimately hang over an edge. Anything that does got there in
    this serializer's own arithmetic.
    """
    placements = placements_of(server)["placements"]
    width, height = placements["board_mm"]
    assert width > 0 and height > 0

    def check(rect, what):
        x, y, w, h = rect
        for value in rect:
            assert isinstance(value, float), f"{what}: {value!r} is not a number"
            assert round(value, 3) == value, f"{what}: {value} is not 3dp"
        assert w > 0 and h > 0, f"{what} has no area"
        assert x >= 0 and y >= 0, f"{what} starts outside the board"
        assert x + w <= width, f"{what} runs past the right edge"
        assert y + h <= height, f"{what} runs past the top edge"

    for part in placements["parts"]:
        check(part["courtyard_mm"], f"{part['ref']} courtyard")
        for pad in part["pads"]:
            check(pad["rect_mm"], f"{part['ref']}.{pad['number']}")


def test_pads_stay_inside_their_own_courtyard(server):
    """The courtyard is what the placer keeps apart, so a pad outside it is a
    clearance guarantee the board does not actually have."""
    for part in placements_of(server)["placements"]["parts"]:
        cx, cy, cw, ch = part["courtyard_mm"]
        for pad in part["pads"]:
            x, y, w, h = pad["rect_mm"]
            assert cx <= x and x + w <= cx + cw, f"{part['ref']}.{pad['number']}"
            assert cy <= y and y + h <= cy + ch, f"{part['ref']}.{pad['number']}"


def test_served_pads_match_the_board_file_in_the_same_response(server, tmp_path):
    """The pads drawn and the pads fabricated are the same pads.

    One placement leaves in two serialisations, and a footprint's own pad
    coordinates are KiCad's -- Y counts *down* from the anchor. Miss that flip
    and every multi-row package comes out mirrored inside its own courtyard:
    still on the board, still inside the courtyard, still numerically sane, so
    the board file in the same response is the only thing that can catch it.
    """
    body = placements_of(server)
    height_mm = body["placements"]["board_mm"][1]

    path = tmp_path / "served.kicad_pcb"
    path.write_text(body["kicad_pcb"], encoding="utf-8")
    reloaded = load_board(path)

    on_file: dict[tuple[str, str], tuple[float, float]] = {}
    stacked = False
    for fp in reloaded.footprints:
        assert not (fp.position.angle or 0), (
            "written for the unrotated case, which is the only one the "
            "pipeline produces; a rotated part needs the rotation applied on "
            "both sides before a comparison means anything"
        )
        local_ys = {pad.position.Y for pad in fp.pads}
        stacked = stacked or len(local_ys) > 1
        for pad in fp.pads:
            on_file[(footprint_ref(fp), str(pad.number))] = (
                round(fp.position.X + pad.position.X, 3),
                round(height_mm - (fp.position.Y + pad.position.Y), 3),
            )
    assert stacked, (
        "every footprint here has its pads in one row, which is exactly the "
        "case a Y flip is invisible in -- this fixture proves nothing"
    )

    served = {
        (part["ref"], pad["number"]): (
            round(pad["rect_mm"][0] + pad["rect_mm"][2] / 2, 3),
            round(pad["rect_mm"][1] + pad["rect_mm"][3] / 2, 3),
        )
        for part in body["placements"]["parts"]
        for pad in part["pads"]
    }

    assert set(served) == set(on_file), "the two serialisations list the same pads"
    for key, centre in sorted(served.items()):
        assert centre == pytest.approx(on_file[key], abs=0.002), key


def test_pad_nets_come_from_the_spec(server):
    """Named pads carry a net the circuit actually declares.

    An unconnected pad is null rather than an empty string: null is a value a
    renderer can test, "" is one it has to remember to test for.
    """
    placements = placements_of(server)["placements"]
    declared = set(CIRCUIT["nets"])
    seen = set()
    for part in placements["parts"]:
        for pad in part["pads"]:
            assert pad["net"] is None or pad["net"] in declared, pad["net"]
            if pad["net"]:
                seen.add(pad["net"])
    assert seen == declared, "every net in the spec should reach at least one pad"


def test_wirelength_is_reported_in_mm(server):
    body = placements_of(server)
    assert "wirelength_mm" in body
    assert body["wirelength_mm"] is None or body["wirelength_mm"] > 0
    assert round(body["wirelength_mm"], 3) == body["wirelength_mm"]


def test_placements_are_additive(server):
    """Nothing that was in the response before is gone or reshaped."""
    body = placements_of(server)
    for key in (
        "intent",
        "board_mm",
        "status",
        "parts",
        "kicad_pcb",
        "repair_rounds",
        "blockers",
        "findings",
        "duration_s",
        "warnings",
        "nets",
        "datasheets",
        "cache",
        "served_by",
        "schematic",
    ):
        assert key in body, f"{key} disappeared from the response"
    assert all(set(p) == {"ref", "footprint"} for p in body["parts"]), (
        "the flat parts list is a separate contract; geometry belongs in "
        "placements"
    )
    assert "grounding" not in body, "grounding stays opt-in"


def test_a_rotated_part_arrives_already_transformed():
    """Rotation is the server's arithmetic, never the client's.

    Driven through the serializer directly because the pipeline never sets
    ``allow_rotation``, so no request can produce a rotated part today -- and
    the moment one does, a client that had quietly been ignoring the flag
    would draw every pad in the wrong place.
    """
    from silkscreen.board import PlacedPart
    from silkscreen.footprints import chip_passive
    from silkscreen.packing import Layer

    from service.app import _placements_dict

    fp = chip_passive("1206", net1="VIN", net2="GND")
    cw, ch = fp.courtyard_w_nm, fp.courtyard_h_nm
    assert cw != ch, "a square courtyard would make the swap invisible"

    class Board:
        width_nm = 4 * cw
        height_nm = 4 * ch
        parts = [
            PlacedPart(ref="R1", footprint=fp, value="10k", x_nm=0, y_nm=0),
            PlacedPart(
                ref="R2",
                footprint=fp,
                value="10k",
                x_nm=0,
                y_nm=2 * ch,
                rotated=True,
                layer=Layer.BOTTOM,
            ),
        ]

    parts = {p["ref"]: p for p in _placements_dict(Board())["parts"]}
    flat, turned = parts["R1"], parts["R2"]

    assert flat["courtyard_mm"][2:] == [
        round(to_mm(2 * cw), 3),
        round(to_mm(2 * ch), 3),
    ]
    assert turned["courtyard_mm"][2:] == flat["courtyard_mm"][2:][::-1], (
        "a rotated courtyard's extents swap"
    )
    assert turned["layer"] == "bottom" and turned["rotated"] is True

    # The two pads sit side by side along X unrotated and stack along Y
    # rotated, and each pad's own rect turns with the part.
    flat_pads = {p["number"]: p["rect_mm"] for p in flat["pads"]}
    turned_pads = {p["number"]: p["rect_mm"] for p in turned["pads"]}
    assert flat_pads["1"][1] == flat_pads["2"][1], "unrotated pads share a row"
    assert turned_pads["1"][0] == turned_pads["2"][0], "rotated pads share a column"
    for number in ("1", "2"):
        assert turned_pads[number][2:] == flat_pads[number][2:][::-1]

    cx, cy, cw_mm, ch_mm = turned["courtyard_mm"]
    for number, rect in turned_pads.items():
        x, y, w, h = rect
        assert cx <= x and x + w <= cx + cw_mm, number
        assert cy <= y and y + h <= cy + ch_mm, number


# ------------------------------------------------------------- cache outcomes


def _datasheet_for(part):
    """The scripted datasheet answer, relabelled for one part number."""
    return json.dumps({**DATASHEET, "part_number": part})


def scripted_reading(*parts):
    """A scripted model that answers each part's datasheet read distinctly.

    The per-part markers go in first: ScriptedModel returns the first marker
    found in the prompt, and the datasheet prompt ends with
    "The part is: <part>", so an exact part marker wins over the generic
    datasheet marker. Without this every read comes back labelled AMS1117-3.3
    and the reported datasheets collapse into indistinguishable duplicates.
    """
    by_marker = {f"The part is: {p}": _datasheet_for(p) for p in parts}
    by_marker.update(scripted().by_marker)
    return ScriptedModel(by_marker=by_marker)


def test_hit_read_and_unusable_are_all_correct_in_one_response(server):
    """One request, all three cache outcomes at once.

    The existing cache tests each drive a single outcome, so nothing catches a
    classification that is right in isolation and wrong when the three sets
    have to be partitioned out of the same dict -- notably that an unusable
    entry has to be subtracted from ``hit`` and added to ``read``.
    """
    server.store.put("AMS1117-3.3", _cached_facts())
    server.store.put("LM317", {"pins": "not a list"})

    Handler.model_factory = staticmethod(lambda: scripted_reading("LM317", "NCP1117"))
    try:
        status, body = post(
            server,
            {
                "intent": "a regulator",
                "datasheets": {
                    "AMS1117-3.3": "https://x/a.pdf",
                    "LM317": "https://x/b.pdf",
                    "NCP1117": "https://x/c.pdf",
                },
                "time_limit_s": 5,
            },
        )
    finally:
        Handler.model_factory = staticmethod(scripted)

    assert status == 200
    assert body["cache"] == {
        "hit": ["AMS1117-3.3"],
        "read": ["LM317", "NCP1117"],
        "unusable": ["LM317"],
    }
    assert sorted(d["part"] for d in body["datasheets"]) == [
        "AMS1117-3.3",
        "LM317",
        "NCP1117",
    ], "the cached part and both read parts all inform the design"


def test_datasheets_are_reported_when_review_is_skipped(server):
    """Turning off the review must not turn off the datasheet reads.

    The reads happen before the proposal and the review runs after the board
    is placed, so ``review: false`` should cost the findings and nothing else.
    A future short-circuit that skipped the reads along with the review would
    silently design the board undocumented, and only this test would notice.
    """
    status, body = post(
        server,
        {
            "intent": "a regulator",
            "datasheets": {"AMS1117-3.3": "https://x/a.pdf"},
            "time_limit_s": 5,
            "review": False,
        },
    )
    assert status == 200
    assert body["findings"] == []
    assert body["blockers"] == []
    assert body["cache"]["read"] == ["AMS1117-3.3"]
    assert body["datasheets"] == [
        {
            "part": "AMS1117-3.3",
            "package": "SOT-223-3",
            "pins": 3,
            "requirements": 1,
            "url": "https://x/a.pdf",
        }
    ]


# -------------------------------------------------------- SILKSCREEN_WEB_DIST


def test_the_web_dist_env_var_is_read_only_at_import(monkeypatch, tmp_path):
    """Setting the variable inside a running process changes nothing.

    ``WEB_DIST`` is a module-level ``Path(os.getenv(...))`` evaluated once on
    import, and ``Handler.web_root`` defaults to that same object. Recording
    the fact here is what keeps the companion test below honest: an
    in-process monkeypatch is not a weak test of the override, it is a test
    of nothing.
    """
    import service.app as app

    at_import = app.WEB_DIST
    monkeypatch.setenv("SILKSCREEN_WEB_DIST", str(tmp_path / "elsewhere"))
    assert at_import == app.WEB_DIST
    assert at_import == Handler.web_root


def test_the_web_dist_env_var_is_honoured_by_a_fresh_import(tmp_path):
    """A process started with the variable set serves the bundle from there.

    Run as a subprocess rather than an ``importlib.reload``: reloading
    service.app rebinds Handler and make_server to brand-new class and
    function objects, while this module's top-level ``from service.app import
    Handler, make_server`` still names the originals -- so every later test in
    the file would be configuring one Handler and serving with another. A
    subprocess reads the import-time value with no effect on this interpreter
    at all.
    """
    dist = tmp_path / "container-bundle"
    dist.mkdir()
    env = {
        **os.environ,
        "SILKSCREEN_WEB_DIST": str(dist),
        "PYTHONPATH": str(REPO_ROOT),
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import service.app as app;"
            "print(app.WEB_DIST);"
            "print(app.Handler.web_root)",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=180,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    reported = [Path(line) for line in proc.stdout.split()]
    assert reported == [dist, dist], (
        "the override has to reach Handler.web_root, not just the constant"
    )
GROUND_REVIEW = {
    "findings": [
        {
            "severity": "marginal",
            "title": "VOUT capacitor may be undersized",
            "detail": (
                "The output requires a 22uF tantalum capacitor for stability"
            ),
            "parts": ["C2"],
            "citation": "p.3",
            "suggested_fix": "use 22uF tantalum",
        }
    ]
}

DATASHEET_PAGES = [
    "AMS1117-3.3 low dropout regulator. Absolute maximum ratings.",
    "Pin configuration: 1 GND, 2 VOUT, 3 VIN. SOT-223-3 package.",
    "Place a 22uF tantalum capacitor on VOUT for stability.",
]

GROUND_REQUEST = {
    "intent": "a 3.3V regulator",
    "datasheets": {"AMS1117-3.3": "https://x/a.pdf"},
    "ground": True,
    "time_limit_s": 5,
}


def ground_scripted():
    return ScriptedModel(
        by_marker={
            "designing a printed circuit board": json.dumps(CIRCUIT),
            "reviewing a circuit someone else designed": json.dumps(GROUND_REVIEW),
            "reading an electronic component datasheet": json.dumps(DATASHEET),
        }
    )


@pytest.fixture
def ground_server():
    from silkscreen.agents.retrieval import HashEmbedder

    from service.app import build_embedder

    store = MemoryFactStore()
    pages_store = MemoryFactStore()
    Handler.model_factory = staticmethod(ground_scripted)
    Handler.store = store
    Handler.pages_store = pages_store
    Handler.embedder_factory = staticmethod(HashEmbedder)
    srv = make_server(port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    srv.store = store
    srv.pages_store = pages_store
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        Handler.model_factory = staticmethod(scripted)
        Handler.store = None
        Handler.pages_store = None
        Handler.embedder_factory = staticmethod(build_embedder)


def seed_pages(srv, part="AMS1117-3.3", url="https://x/a.pdf"):
    from silkscreen.agents.grounding import store_pages

    from service.app import page_cache_key

    store_pages(srv.pages_store, page_cache_key(part, url), DATASHEET_PAGES)


def test_grounding_corroborates_a_cited_finding(ground_server):
    ground_server.store.put("AMS1117-3.3", _cached_facts())
    seed_pages(ground_server)

    status, body = post(ground_server, GROUND_REQUEST)
    assert status == 200

    finding = body["grounding"]["findings"][0]
    assert finding["status"] == "corroborated"
    assert finding["title"] == "VOUT capacitor may be undersized"
    assert finding["citation"] == "p.3"
    assert finding["parts"] == ["C2"]
    assert finding["evidence"][0]["page"] == 3
    assert "22uF tantalum" in finding["evidence"][0]["quote"]
    assert body["grounding"]["pages"]["cached"] == ["AMS1117-3.3"]
    assert body["grounding"]["pages"]["read"] == []

    assert body["kicad_pcb"].startswith("(kicad_pcb")
    assert [p["ref"] for p in body["parts"]] == ["U1", "C1", "C2"]
    assert body["cache"]["hit"] == ["AMS1117-3.3"]


def test_datasheet_pages_are_read_once_then_cached(monkeypatch, ground_server):
    import service.app as app

    calls = []

    def spy(*, url, **kw):
        calls.append(url)
        return list(DATASHEET_PAGES)

    monkeypatch.setattr(app, "pages_for_part", spy)
    ground_server.store.put("AMS1117-3.3", _cached_facts())

    status, body = post(ground_server, GROUND_REQUEST)
    assert status == 200
    assert calls == ["https://x/a.pdf"]
    assert body["grounding"]["pages"]["read"] == ["AMS1117-3.3"]
    assert body["grounding"]["pages"]["cached"] == []

    status, body = post(ground_server, GROUND_REQUEST)
    assert status == 200
    assert calls == ["https://x/a.pdf"], "cached pages must not be fetched again"
    assert body["grounding"]["pages"]["cached"] == ["AMS1117-3.3"]
    assert body["grounding"]["pages"]["read"] == []


def test_ground_without_datasheets_is_400(ground_server):
    status, body = post(
        ground_server,
        {"intent": "a 3.3V regulator", "ground": True, "time_limit_s": 5},
    )
    assert status == 400 and "ground" in body["error"]


def test_no_grounding_key_unless_it_was_asked_for(ground_server):
    status, body = post(
        ground_server, {"intent": "a 3.3V regulator", "time_limit_s": 5}
    )
    assert status == 200
    assert "grounding" not in body
    assert body["kicad_pcb"].startswith("(kicad_pcb")


def test_a_failed_datasheet_fetch_is_502(monkeypatch, ground_server):
    from silkscreen.agents.grounding import GroundingError

    import service.app as app

    def boom(**kw):
        raise GroundingError("fetching https://x/a.pdf failed: HTTP 503")

    monkeypatch.setattr(app, "pages_for_part", boom)
    ground_server.store.put("AMS1117-3.3", _cached_facts())

    status, body = post(ground_server, GROUND_REQUEST)
    assert status == 502, "a failed datasheet fetch is upstream, not the caller"
    assert "503" in body["error"]


def test_a_rejected_datasheet_url_is_400(ground_server):
    ground_server.store.put("AMS1117-3.3", _cached_facts())

    status, body = post(
        ground_server,
        {
            "intent": "a 3.3V regulator",
            "datasheets": {"AMS1117-3.3": "http://169.254.169.254/x.pdf"},
            "ground": True,
            "time_limit_s": 5,
        },
    )
    assert status == 400
    assert "169.254" not in body["error"]
    assert "non-global" not in body["error"]
    assert "not allowed" in body["error"] or "not an http(s)" in body["error"]


def test_different_url_for_same_part_is_a_cache_miss(monkeypatch, ground_server):
    import service.app as app

    seed_pages(ground_server, url="https://a.example/a.pdf")

    calls = []

    def spy(*, url, **kw):
        calls.append(url)
        return list(DATASHEET_PAGES)

    monkeypatch.setattr(app, "pages_for_part", spy)
    ground_server.store.put("AMS1117-3.3", _cached_facts())

    request = dict(GROUND_REQUEST)
    request["datasheets"] = {"AMS1117-3.3": "https://b.example/b.pdf"}
    status, body = post(ground_server, request)
    assert status == 200
    assert calls == ["https://b.example/b.pdf"]
    assert body["grounding"]["pages"]["read"] == ["AMS1117-3.3"]
    assert body["grounding"]["pages"]["cached"] == []


def test_too_many_ground_datasheets_is_400(ground_server):
    datasheets = {f"P{i}": "https://x/a.pdf" for i in range(26)}
    status, body = post(
        ground_server,
        {"intent": "x", "datasheets": datasheets, "ground": True, "time_limit_s": 5},
    )
    assert status == 400
    assert "at most" in body["error"]


def test_no_findings_skips_grounding_fetch(monkeypatch, ground_server):
    import service.app as app

    def clean_review():
        return ScriptedModel(
            by_marker={
                "designing a printed circuit board": json.dumps(CIRCUIT),
                "reviewing a circuit someone else designed": json.dumps(
                    {"findings": []}
                ),
                "reading an electronic component datasheet": json.dumps(DATASHEET),
            }
        )

    monkeypatch.setattr(Handler, "model_factory", staticmethod(clean_review))

    calls = []

    def spy(**kw):
        calls.append(kw)
        return list(DATASHEET_PAGES)

    monkeypatch.setattr(app, "pages_for_part", spy)
    ground_server.store.put("AMS1117-3.3", _cached_facts())

    status, body = post(ground_server, GROUND_REQUEST)
    assert status == 200
    assert body["grounding"]["findings"] == []
    assert body["grounding"]["pages"] == {"cached": [], "read": []}
    assert calls == []


def test_ground_flag_must_be_boolean_true(monkeypatch, ground_server):
    import service.app as app

    calls = []

    def spy(**kw):
        calls.append(kw)
        return list(DATASHEET_PAGES)

    monkeypatch.setattr(app, "pages_for_part", spy)
    ground_server.store.put("AMS1117-3.3", _cached_facts())

    status, body = post(
        ground_server,
        {
            "intent": "a 3.3V regulator",
            "datasheets": {"AMS1117-3.3": "https://x/a.pdf"},
            "ground": "false",
            "time_limit_s": 5,
        },
    )
    assert status == 200
    assert "grounding" not in body
    assert calls == []


def test_grounding_payload_is_json_safe(ground_server):
    ground_server.store.put("AMS1117-3.3", _cached_facts())
    seed_pages(ground_server)

    status, body = post(ground_server, GROUND_REQUEST)
    assert status == 200

    findings = body["grounding"]["findings"]
    assert findings
    for finding in findings:
        assert finding["severity"] in {"blocker", "marginal", "note"}
        assert finding["status"] in {"corroborated", "related", "unsupported"}
        assert isinstance(finding["parts"], list)
        assert finding["evidence"]
        for evidence in finding["evidence"]:
            assert isinstance(evidence["part"], str)
            assert isinstance(evidence["page"], int) and evidence["page"] >= 1
            assert isinstance(evidence["score"], float)
            assert isinstance(evidence["quote"], str)
            assert len(evidence["quote"]) <= 300


# ------------------------------------------------------------ /generate/stream


def _request_bytes(srv, path, body, content_length):
    """One raw POST, with the framing headers written by hand.

    Written on a socket for the same reason post_without_content_length is:
    the streaming response carries no Content-Length, so a client that waits
    for one -- urllib does -- would hang or buffer the whole run and prove
    nothing about when each frame arrived.
    """
    lines = [
        f"POST {path} HTTP/1.1",
        f"Host: 127.0.0.1:{srv.server_port}",
        "Content-Type: application/json",
        "Connection: close",
    ]
    if content_length is not None:
        lines.append(f"Content-Length: {content_length}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def _split_head(buffer):
    head, _, rest = buffer.partition(b"\r\n\r\n")
    first, _, header_text = head.partition(b"\r\n")
    status = int(first.decode().split()[1])
    headers = {}
    for line in header_text.split(b"\r\n"):
        if not line:
            continue
        name, _, value = line.decode().partition(":")
        headers[name.strip().lower()] = value.strip()
    return status, headers, rest


def post_raw(srv, path, body=b"", content_length=None):
    """POST exactly these bytes and read the whole response until close."""
    request = _request_bytes(srv, path, body, content_length)
    with socket.create_connection(("127.0.0.1", srv.server_port), timeout=30) as sock:
        sock.sendall(request)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return _split_head(b"".join(chunks))


def post_stream(srv, payload, path="/generate/stream", content_length="auto"):
    """POST and collect NDJSON frames until the server closes the connection.

    Frames are split as they arrive rather than after the read finishes, so a
    handler that forgot to flush would show up here as a stalled read instead
    of passing on the buffer the socket happened to deliver at the end.

    ``content_length=None`` omits the header, and then sends no body with it:
    bytes the handler never reads would still be sitting in the socket when it
    closes, and an unread request is what turns a clean close into a reset.
    """
    body = b"" if content_length is None else json.dumps(payload).encode()
    if content_length == "auto":
        content_length = len(body)
    request = _request_bytes(srv, path, body, content_length)
    frames = []
    with socket.create_connection(("127.0.0.1", srv.server_port), timeout=60) as sock:
        sock.sendall(request)
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = sock.recv(65536)
            if not chunk:
                pytest.fail("the server closed before sending any headers")
            buffer += chunk
        status, headers, rest = _split_head(buffer)
        while True:
            while b"\n" in rest:
                line, _, rest = rest.partition(b"\n")
                if line.strip():
                    frames.append(json.loads(line))
            chunk = sock.recv(65536)
            if not chunk:
                break
            rest += chunk
    assert not rest.strip(), "a frame arrived without its terminating newline"
    return status, headers, frames


def test_a_stream_reports_the_run_and_ends_with_the_one_shot_body(server):
    """The stream is the same answer, delivered as it is worked out.

    The last frame carries the one-shot response object unchanged -- no
    reshaping, no subset -- so a client that streams gets everything a client
    that waits gets, and the two code paths cannot drift apart silently.
    """
    request = {"intent": "a 3.3V regulator", "time_limit_s": 5}
    status, headers, frames = post_stream(server, request)

    assert status == 200
    assert headers["content-type"] == "application/x-ndjson"
    assert headers["cache-control"] == "no-cache"
    assert "content-length" not in headers, "the connection close delimits the body"

    assert len(frames) >= 3
    assert all(isinstance(frame, dict) for frame in frames)
    assert frames[0] == {"event": "run.accepted", "t_s": 0.0}
    assert frames[-1]["event"] == "run.done"

    events = [frame["event"] for frame in frames]
    assert "stage.start" in events, "the pipeline's stages must reach the wire"
    assert "model.call" in events, "so must its model round-trips"

    once_status, once_body = post(server, request)
    assert once_status == 200
    streamed = frames[-1]["result"]
    # Wall clock is the one field that cannot match: the two runs are two runs.
    del streamed["duration_s"]
    del once_body["duration_s"]
    assert streamed == once_body


#: Any valid request; these tests are about what comes back, not what goes in.
REQUEST = {"intent": "a regulator", "time_limit_s": 5}


def _pipeline_that_raises(exc):
    """A generate_pcb that reports two events, then fails.

    The events matter as much as the failure: a run that dies must still have
    told the client what it managed to do first.
    """

    def fake(model, intent, **kw):
        on_event = kw["on_event"]
        on_event({"event": "stage.start", "stage": "propose"})
        on_event({"event": "stage.done", "stage": "propose"})
        raise exc

    return fake


def _reported_events(frames):
    return [frame["event"] for frame in frames[1:-1]]


def test_a_streamed_value_error_is_a_400_run_error(monkeypatch, server):
    """The caller's error, with its message, exactly as the one-shot route sends it."""
    import service.app as app

    boom = _pipeline_that_raises(ValueError("'nets' must be an object"))
    monkeypatch.setattr(app, "generate_pcb", boom)
    status, _, frames = post_stream(server, REQUEST)

    assert status == 200, "the headers are already sent; failure lives in the frames"
    assert _reported_events(frames) == ["stage.start", "stage.done"]
    assert frames[-1] == {
        "event": "run.error",
        "status": 400,
        "error": "'nets' must be an object",
    }


def test_a_streamed_model_outage_is_a_502_run_error(monkeypatch, server):
    """An upstream outage is not the caller's fault here either."""
    from silkscreen.agents.model import ModelError

    import service.app as app

    monkeypatch.setattr(
        app, "generate_pcb", _pipeline_that_raises(ModelError("gemini 503"))
    )
    _, _, frames = post_stream(server, REQUEST)

    assert _reported_events(frames) == ["stage.start", "stage.done"]
    assert frames[-1]["event"] == "run.error"
    assert frames[-1]["status"] == 502
    assert "503" in frames[-1]["error"]


def test_a_streamed_internal_error_keeps_its_error_id_and_hides_the_detail(
    monkeypatch, server
):
    """Our bug, reported the way the one-shot route reports it.

    The id is the only thing joining this frame to the logged traceback, and
    the internal message must not travel with it.
    """
    import service.app as app

    monkeypatch.setattr(
        app,
        "generate_pcb",
        _pipeline_that_raises(RuntimeError("a detail from deep inside the pipeline")),
    )
    _, _, frames = post_stream(server, REQUEST)

    assert _reported_events(frames) == ["stage.start", "stage.done"], (
        "the events before the failure still belong to the client"
    )
    assert frames[-1]["event"] == "run.error"
    assert frames[-1]["status"] == 500
    assert frames[-1]["error"] == "internal error"
    assert re.fullmatch(r"[0-9a-f]{12}", frames[-1]["error_id"])
    assert "deep inside" not in json.dumps(frames)


def test_a_malformed_content_length_on_the_stream_route_is_a_plain_400(server):
    """A request rejected before the run starts is answered as an ordinary error.

    Nothing has been written at that point, so there is no stream to put the
    error in -- and a client that gets a stream back for a request that never
    ran would have to parse frames to learn its header was garbage.
    """
    status, headers, body = post_raw(
        server, "/generate/stream", b"", content_length="abc"
    )
    assert status == 400
    assert headers["content-type"] == "application/json"
    assert json.loads(body) == {"error": "invalid Content-Length"}


def test_malformed_json_on_the_stream_route_is_a_plain_400(server):
    status, headers, body = post_raw(
        server, "/generate/stream", b"{not json", content_length=9
    )
    assert status == 400
    assert headers["content-type"] == "application/json"
    assert "invalid JSON" in json.loads(body)["error"]


def test_a_non_object_body_on_the_stream_route_is_a_plain_400(server):
    body_bytes = b"[1, 2, 3]"
    status, _, body = post_raw(
        server, "/generate/stream", body_bytes, content_length=len(body_bytes)
    )
    assert status == 400
    assert json.loads(body) == {"error": "body must be a JSON object"}


def test_a_missing_content_length_reaches_the_stream_as_an_empty_body(server):
    """The boundary between the two error styles, stated as a test.

    A missing Content-Length is not a malformed one: the handler reads it as
    an empty body, which is a valid ``{}`` request that then fails validation
    inside the run. So this one is a stream carrying a 400, not a plain 400 --
    the same substitution the one-shot route makes, with the stream's framing.
    """
    status, headers, frames = post_stream(server, {}, content_length=None)
    assert status == 200
    assert headers["content-type"] == "application/x-ndjson"
    assert frames[0]["event"] == "run.accepted"
    assert frames[-1]["event"] == "run.error"
    assert frames[-1]["status"] == 400
    assert "intent" in frames[-1]["error"]


def test_a_debug_stream_carries_the_raw_model_responses(server):
    """The one request field that puts model output on the wire.

    Reading a bad board means reading what the model was told and what it
    said back; without this the stream reports only that a call happened.
    """
    status, _, frames = post_stream(server, {**REQUEST, "debug": True})

    assert status == 200
    responses = [frame for frame in frames if frame["event"] == "model.response"]
    assert responses, "a debug run must report what the model answered"
    for frame in responses:
        assert isinstance(frame["text"], str) and frame["text"]
    assert frames[-1]["event"] == "run.done"


def test_a_stream_without_the_debug_flag_carries_no_model_responses(server):
    """The default stream stays a progress signal, exactly as it was."""
    status, _, frames = post_stream(server, REQUEST)

    assert status == 200
    assert [frame for frame in frames if frame["event"] == "model.response"] == []
    assert "model.call" in [frame["event"] for frame in frames]


def test_a_streamed_grounded_run_reports_each_part(ground_server):
    """The events the service owns, for the stage the pipeline cannot see.

    Grounding runs after generate_pcb has returned, so no pipeline event can
    describe it. Without these frames the stream would fall silent through the
    slowest part of a grounded request, which is exactly when a client is
    still waiting to hear something.
    """
    ground_server.store.put("AMS1117-3.3", _cached_facts())
    seed_pages(ground_server)

    status, _, frames = post_stream(ground_server, GROUND_REQUEST)
    assert status == 200

    grounded = [frame for frame in frames if frame["event"] == "ground.part"]
    assert len(grounded) == 1
    assert grounded[0]["part"] == "AMS1117-3.3"
    assert grounded[0]["cached"] is True
    assert isinstance(grounded[0]["t_s"], float)

    assert frames[-1]["event"] == "run.done"
    pages = frames[-1]["result"]["grounding"]["pages"]
    assert pages == {"cached": ["AMS1117-3.3"], "read": []}
