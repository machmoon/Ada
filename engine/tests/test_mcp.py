"""MCP: the protocol itself, not just the tools behind it."""

import io
import json

import pytest
from silkscreen.mcp.server import (
    METHOD_NOT_FOUND,
    PROTOCOL_VERSION,
    TOOLS,
    Server,
    handle,
)

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


def rpc(method, params=None, req_id=1):
    return handle(
        {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
    )


def call(name, arguments=None):
    return rpc("tools/call", {"name": name, "arguments": arguments or {}})


def payload(response):
    """The JSON a successful tool call carried back."""
    content = response["result"]["content"][0]["text"]
    return json.loads(content)


def test_initialize_reports_protocol_and_server():
    result = rpc("initialize")["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "silkscreen"
    assert "tools" in result["capabilities"]


def test_initialized_notification_gets_no_reply():
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_ping():
    assert rpc("ping")["result"] == {}


def test_tools_list_is_well_formed():
    tools = rpc("tools/list")["result"]["tools"]
    assert len(tools) == len(TOOLS)
    for tool in tools:
        assert tool["name"] and tool["description"]
        assert tool["inputSchema"]["type"] == "object"


def test_unknown_method_is_a_jsonrpc_error():
    err = rpc("does/not/exist")["error"]
    assert err["code"] == METHOD_NOT_FOUND


def test_wrong_jsonrpc_version_is_rejected():
    assert "error" in handle({"jsonrpc": "1.0", "id": 1, "method": "ping"})


def test_unknown_tool_is_a_jsonrpc_error():
    assert "error" in call("no_such_tool")


def test_response_id_matches_the_request():
    assert rpc("ping", req_id="abc-123")["id"] == "abc-123"


def test_validate_circuit_accepts_a_good_circuit():
    body = payload(call("validate_circuit", CIRCUIT))
    assert body == {"valid": True, "devices": 1, "passives": 2, "nets": 3}


def test_validate_circuit_reports_every_error_at_once():
    bad = {
        "devices": {"U1": {"pins": {"1": "GND"}}},
        "nets": {"GND": ["U1.1", "U1.99"], "FLOAT": ["U1.1"]},
    }
    body = payload(call("validate_circuit", bad))
    assert body["valid"] is False
    assert len(body["errors"]) >= 2, "all failures, not just the first"


def test_generate_footprint_returns_two_pads_for_a_chip_passive():
    body = payload(call("generate_footprint", {"package": "0805"}))
    assert body["name"] == "C_0805"
    assert len(body["pads"]) == 2
    assert body["pads"][0]["x_mm"] == -body["pads"][1]["x_mm"], "symmetric"
    assert body["courtyard_mm"][0] > 0


def test_generate_footprint_refuses_an_unknown_package():
    response = call("generate_footprint", {"package": "0201"})
    assert response["result"]["isError"] is True


def test_build_board_places_every_part():
    body = payload(call("build_board", {"circuit": CIRCUIT, "time_limit_s": 5}))
    assert [p["ref"] for p in body["parts"]] == ["U1", "C1", "C2"]
    assert body["board_mm"][0] > 0 and body["board_mm"][1] > 0


def test_emit_kicad_pcb_returns_a_parseable_board():
    body = payload(call("emit_kicad_pcb", {"circuit": CIRCUIT, "time_limit_s": 5}))
    text = body["kicad_pcb"]
    assert text.startswith("(kicad_pcb")
    assert text.count("(footprint") == 3
    assert body["bytes"] == len(text.encode())


def test_place_parts_returns_a_placement_per_part():
    body = payload(
        call(
            "place_parts",
            {
                "parts": [
                    {"ref": "U1", "width_mm": 10, "height_mm": 10},
                    {"ref": "C1", "width_mm": 2, "height_mm": 1.2},
                ],
                "time_limit_s": 3,
            },
        )
    )
    assert {p["ref"] for p in body["placements"]} == {"U1", "C1"}


def test_place_parts_rejects_an_empty_list():
    assert call("place_parts", {"parts": []})["result"]["isError"] is True


def test_a_bad_circuit_is_an_error_result_not_a_crash():
    response = call("build_board", {"circuit": {"nets": {"N": ["U9.1"]}}})
    assert response["result"]["isError"] is True


@pytest.mark.parametrize("name", [t["name"] for t in TOOLS])
def test_every_advertised_tool_is_dispatchable(name):
    assert "error" not in call(name, {}) or "unknown tool" not in str(call(name, {}))


def test_stdio_round_trip():
    """The transport, end to end, over real streams."""
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        "",
        "not json at all",
    ]
    out = io.StringIO()
    Server(stdin=io.StringIO("\n".join(lines)), stdout=out).serve_forever()
    replies = [json.loads(line) for line in out.getvalue().splitlines()]

    assert [r.get("id") for r in replies] == [1, 2, None]
    assert replies[1]["result"]["tools"]
    assert replies[2]["error"]["message"].startswith("invalid JSON")
