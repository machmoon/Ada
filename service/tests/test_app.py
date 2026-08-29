"""The Cloud Run surface, driven over a real socket with a scripted model."""

import http.client
import json
import threading
import urllib.error
import urllib.request

import pytest
from silkscreen.agents.model import ScriptedModel

from service.app import Handler, make_server
from service.cache import MemoryFactStore

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
        "citation",
        "suggested_fix",
    }
    assert first["parts"] == ["U1", "C2"]
    assert first["citation"] == "AMS1117-3.3 datasheet p.9"
    assert first["suggested_fix"].startswith("Add a 22uF")


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
