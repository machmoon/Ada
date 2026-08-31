"""Order demo: design a board, prove it, price it, and stop.

    python scripts/order_demo.py

Runs with no network, no API key and no KiCad install. The whole point is the
middle: twelve checks stand between a placement and an order, and an unrouted
board fails the first one that matters -- copper that does not exist cannot
carry a signal, and a board fabricated in that state arrives dead. Route it and
the same gate passes, at which point the demo produces a real price and hands
the decision to a person.

Nothing here contacts a fabricator or spends money, and there is no flag that
makes it. See silkscreen.fabhouse.SUBMISSION_BOUNDARY.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))

from silkscreen.approval import prepare_order  # noqa: E402
from silkscreen.board import build_board, route_board  # noqa: E402
from silkscreen.fabhouse import SERVICES, check_capabilities, quote  # noqa: E402
from silkscreen.netlist import parse_circuit_spec  # noqa: E402
from silkscreen.order import OrderOptions  # noqa: E402

#: A 3.3 V LDO with input and output bulk capacitors -- the same circuit the
#: live pipeline produces from a plain-language prompt. Note that Device.pins
#: maps a pin NAME to its NUMBER, not the other way round.
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


def rule(title: str) -> None:
    print(f"\n{title}\n" + "-" * 66)


def show_gate(report) -> None:
    for check in report.checks:
        print(f"  [{str(check.status).upper():<7}] {check.title}")
        if check.status.value in ("fail", "skipped", "warn"):
            print(f"            {check.summary}")


def main() -> int:
    spec = parse_circuit_spec(json.dumps(CIRCUIT))
    options = OrderOptions(quantity=10)

    rule("1. Place the board")
    board = build_board(spec, time_limit_s=8.0)
    print(f"  {len(board.parts)} parts, {len(board.nets)} nets")
    print(
        f"  {board.width_nm / 1e6:.2f} x {board.height_nm / 1e6:.2f} mm"
        f"  [{board.solver_status}]"
    )

    rule("2. Try to order it, unrouted -- the gate refuses")
    before = prepare_order(board, spec=spec, options=options)
    print(f"  {before.gate.headline()}")
    for check in before.gate.blocking:
        print(f"  BLOCKING: {check.title}")
        print(f"            {check.summary}")

    rule("3. Route it")
    route_board(board)
    print(f"  routed: {', '.join(board.routed_nets) or 'nothing'}")
    print(f"  {len(board.tracks)} tracks, {len(board.vias)} vias")
    for net, why in sorted(board.unrouted_nets.items()):
        print(f"  unrouted: {net} -- {why}")

    rule("4. The pre-flight gate, in full")
    order = prepare_order(board, spec=spec, options=options)
    show_gate(order.gate)
    print(f"\n  {order.gate.headline()}")

    rule("5. The same board, priced at every house we know")
    for service in SERVICES:
        priced = quote(board, options, service=service)
        blockers = [
            issue
            for issue in check_capabilities(board, service, options=options)
            if issue.severity.value == "blocker"
        ]
        verdict = "cannot build" if blockers else "can build"
        print(f"  {service.house + ' ' + service.service:<34} "
              f"{priced.total_text():>14}  {verdict}")
        for issue in blockers:
            print(f"      blocked: {issue.title}")

    rule("6. The fab package")
    for layer in order.files:
        print(f"  {layer.filename:<32} {len(layer.content):>7} bytes")

    rule("7. Where this stops")
    print(f"  ready for a human to review: {order.ready_for_human_review}")
    print(f"  human approval required:     {order.requires_human_approval}")
    print(f"  price to beat:               {order.quote.total_text()}")
    print(f"  upload it yourself at:       {order.service.quote_url}")
    print(
        "\n  Silkscreen prepares orders. It does not place them, and there is\n"
        "  no flag, argument or configuration that changes that."
    )
    return 0 if order.gate.go else 2


if __name__ == "__main__":
    raise SystemExit(main())
