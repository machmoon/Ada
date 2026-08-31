"""``silkscreen-order``: design a board from a circuit file and prepare an order.

    silkscreen-order circuit.json --quantity 10 --out order/

Reads a circuit specification, places it, routes it, renders the fab package,
runs the pre-flight gate and prints the order summary. It exits ``0`` on a GO
and ``2`` on a NO-GO, so a script or an agent can branch on the gate's verdict
without parsing prose -- and so that the *only* thing an automated caller can
do with a passing gate is notice that it passed.

It never submits an order, and there is no flag that makes it. See
:data:`silkscreen.fabhouse.SUBMISSION_BOUNDARY`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .approval import prepare_order
from .board import build_board, route_board
from .fabhouse import DEFAULT_SERVICE_ID, SERVICES
from .netlist import ValidationError, parse_circuit_spec
from .order import OrderOptions

__all__ = ["main", "build_parser"]

#: Exit code for a board the gate refuses. Distinct from 1, which is reserved
#: for the program failing, because "the board is not orderable" is a
#: successful run that reached a negative answer.
NO_GO = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="silkscreen-order",
        description=(
            "Prepare -- never place -- a board order from a circuit "
            "specification."
        ),
    )
    parser.add_argument(
        "circuit",
        nargs="?",
        help="JSON circuit specification, the same shape the model proposes",
    )
    parser.add_argument(
        "--list-services",
        action="store_true",
        help="print the known fab services and exit",
    )
    parser.add_argument("--service", default=DEFAULT_SERVICE_ID)
    parser.add_argument("--quantity", type=int, default=5)
    parser.add_argument("--layers", type=int, default=2)
    parser.add_argument("--thickness", type=float, default=1.6)
    parser.add_argument(
        "--assembly",
        action="store_true",
        help="ask the house to populate the board as well as build it",
    )
    parser.add_argument("--panel-columns", type=int, default=1)
    parser.add_argument("--panel-rows", type=int, default=1)
    parser.add_argument(
        "--no-route",
        action="store_true",
        help="skip routing, to see the gate refuse an unrouted board",
    )
    parser.add_argument("--time-limit", type=float, default=20.0)
    parser.add_argument(
        "--out", type=Path, help="write the package, manifest and zip here"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the manifest as JSON instead of the readable summary",
    )
    return parser


def _list_services() -> int:
    for service in SERVICES:
        price = (
            f"${service.cents_per_sq_in / 100:.2f}/sq in"
            if service.cents_per_sq_in is not None
            else "no published price rule"
        )
        print(f"{service.id:<24} {service.house} -- {service.service}")
        print(f"{'':<24} {price}, {service.lead_time_days[0]}-"
              f"{service.lead_time_days[1]} days, {service.source_url}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_services:
        return _list_services()
    if not args.circuit:
        build_parser().error("a circuit file is required unless --list-services")

    try:
        spec = parse_circuit_spec(Path(args.circuit).read_text(encoding="utf-8"))
    except ValidationError as exc:
        print(f"the circuit does not validate:\n{exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"cannot read {args.circuit}: {exc}", file=sys.stderr)
        return 1

    try:
        options = OrderOptions(
            quantity=args.quantity,
            layers=args.layers,
            thickness_mm=args.thickness,
            assembly=args.assembly,
            panel_columns=args.panel_columns,
            panel_rows=args.panel_rows,
        )
    except ValueError as exc:
        print(f"bad order options: {exc}", file=sys.stderr)
        return 1

    board = build_board(spec, time_limit_s=args.time_limit)
    if not args.no_route:
        route_board(board)

    order = prepare_order(board, spec=spec, options=options, service=args.service)

    if args.json:
        print(json.dumps(order.manifest(), indent=2, sort_keys=True))
    else:
        print(order.render())

    if args.out:
        written = order.write(args.out)
        print(f"wrote {len(written)} file(s) to {args.out}", file=sys.stderr)

    return 0 if order.gate.go else NO_GO


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
