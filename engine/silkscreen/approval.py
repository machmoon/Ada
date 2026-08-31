"""The prepared order, and the human it stops in front of.

This module assembles everything a person needs in order to decide -- the
board, the twelve checks and their evidence, the fab package, the house, the
options, the quote -- into one object, renders it as something readable, and
then does nothing further.

That last part is the design, not an unfinished edge. A :class:`PreparedOrder`
has no ``submit``, no ``confirm``, no ``place``, and no field a caller can set
to mean "approved". The only way to buy the board it describes is for a person
to open the fab's own site and upload the package, which is the point:

    An agent that clears its own checks and then spends money has converted
    every remaining class of design error -- a wrong part value, a misread
    datasheet pin, a topology that validates and does not work -- into money,
    silently and at machine speed.

:data:`silkscreen.fabhouse.SUBMISSION_BOUNDARY` states that at length, and
:func:`silkscreen.fabhouse.submit_order` refuses at the one call site anyone
would reach for. Nothing in this module contacts a fabricator, holds a
credential, or touches a payment path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .board import BoardResult, emit_kicad_pcb
from .fab import FabLayer, fab_files
from .fabhouse import (
    DEFAULT_SERVICE_ID,
    SUBMISSION_BOUNDARY,
    FabService,
    PriceBasis,
    Quote,
    quote,
    service_by_id,
)
from .gate import GateReport, run_gate
from .netlist import CircuitSpec
from .order import (
    MANIFEST_FILENAME,
    OrderOptions,
    OrderPreflight,
    board_summary,
    order_manifest,
    package_zip,
    preflight,
)

__all__ = [
    "PreparedOrder",
    "prepare_order",
    "BOARD_FILENAME",
    "GATE_FILENAME",
]

BOARD_FILENAME = "board.kicad_pcb"
GATE_FILENAME = "preflight-gate.json"


@dataclass(frozen=True)
class PreparedOrder:
    """Everything about an order except the act of placing it.

    ``ready_for_human_review`` is not ``approved``. It says the machine has
    finished its half and the package is worth a person's time; the person's
    half has not happened and cannot happen here.
    """

    board: BoardResult
    options: OrderOptions
    service: FabService
    gate: GateReport
    quote: Quote
    preflight: OrderPreflight
    files: tuple[FabLayer, ...]
    board_text: str
    spec: CircuitSpec | None = None
    #: Repeated from the gate so no reader has to reconstruct it, and derived
    #: rather than stored so it cannot drift from the checks underneath.
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ready_for_human_review(self) -> bool:
        """True when every check cleared. Still not an approval to buy."""
        return self.gate.go

    @property
    def requires_human_approval(self) -> bool:
        """Always. There is no state of this object in which it is False."""
        return True

    def manifest(self) -> dict:
        """The JSON-safe description of the order, gate and price included.

        Built on :func:`silkscreen.order.order_manifest` rather than beside it,
        so the compatibility surface that already exists keeps its shape and
        this adds to it.
        """
        base = order_manifest(self.board, self.options, self.preflight)
        base["fab"] = {
            "house": self.service.house,
            "service": self.service.service,
            "service_id": self.service.id,
            "quote_url": self.service.quote_url,
            "capabilities_source": self.service.source_url,
            "lead_time_days": list(self.service.lead_time_days),
        }
        base["quote"] = self.quote.as_dict()
        base["gate"] = self.gate.as_dict()
        base["ready_for_human_review"] = self.ready_for_human_review
        base["files"] = [f.filename for f in self.files]
        # order_manifest's own "orderable" reads the older, narrower preflight.
        # The gate is strictly stronger, so the manifest must not be able to
        # say yes where the gate says no.
        base["orderable"] = base["orderable"] and self.gate.go
        base["submission_boundary"] = SUBMISSION_BOUNDARY
        return base

    def package(self) -> bytes:
        """The whole order as one deterministic zip, ready to hand over."""
        payload = [(f.filename, f.content) for f in self.files]
        payload.append((BOARD_FILENAME, self.board_text))
        gate_json = json.dumps(self.gate.as_dict(), indent=2, sort_keys=True) + "\n"
        payload.append((GATE_FILENAME, gate_json))
        return package_zip(payload, self.manifest())

    def write(self, out_dir: str | Path) -> list[Path]:
        """Write every file, the manifest and the zip into ``out_dir``."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for layer in self.files:
            path = out_dir / layer.filename
            path.write_text(layer.content, encoding="utf-8")
            written.append(path)
        for name, text in (
            (BOARD_FILENAME, self.board_text),
            (
                GATE_FILENAME,
                json.dumps(self.gate.as_dict(), indent=2, sort_keys=True) + "\n",
            ),
            (
                MANIFEST_FILENAME,
                json.dumps(self.manifest(), indent=2, sort_keys=True) + "\n",
            ),
            ("order-summary.txt", self.render()),
        ):
            path = out_dir / name
            path.write_text(text, encoding="utf-8")
            written.append(path)
        zip_path = out_dir / "order-package.zip"
        zip_path.write_bytes(self.package())
        written.append(zip_path)
        return written

    def render(self) -> str:
        """The order as a person reads it: verdict, evidence, price, then stop.

        Deliberately ordered worst-news-first within each section. A summary
        that opens with the price and buries the failing check is optimised for
        the answer the reader wants rather than the one they need.
        """
        summary = board_summary(self.board, self.options)
        verdict = "GO" if self.gate.go else "NO-GO"
        lines = [
            "ORDER SUMMARY -- prepared, not placed",
            "=" * 70,
            "",
            f"VERDICT: {verdict}",
            f"  {self.gate.headline()}",
            "",
            "PRE-FLIGHT CHECKS",
        ]
        for check in self.gate.checks:
            lines.append(
                f"  [{str(check.status).upper():<7}] {check.title}"
            )
            lines.append(f"            {check.summary}")
            for item in check.evidence:
                lines.append(f"              - {item}")
        lines += [
            "",
            "BOARD",
            f"  Size            {summary['width_mm']} x {summary['height_mm']} mm"
            f"  ({summary['area_mm2']} mm2)",
            f"  Parts           {summary['part_count']}"
            f"  (top {summary['parts_by_side'].get('top', 0)},"
            f" bottom {summary['parts_by_side'].get('bottom', 0)})",
            f"  Nets            {summary['net_count']}"
            f"  ({summary['nets_with_two_or_more_pads']} needing copper)",
            f"  Placement       {summary['solver_status']}",
            f"  Routing         {len(self.board.tracks)} track(s),"
            f" {len(self.board.vias)} via(s),"
            f" {len(self.board.unrouted_nets)} net(s) open",
            "",
            "ORDER",
            f"  House           {self.service.house} -- {self.service.service}",
            f"  Quantity        {self.options.quantity} requested",
            f"  Layers          {self.options.layers}",
            f"  Thickness       {self.options.thickness_mm} mm",
            f"  Finish          {self.service.finish}",
            f"  Mask            {self.service.mask_colour}",
            f"  Assembly        {'yes' if self.options.assembly else 'no'}",
            f"  Panel           {self.options.panel_columns}"
            f" x {self.options.panel_rows}",
            "",
            "PRICE",
        ]
        if self.quote.basis is PriceBasis.UNAVAILABLE:
            lines.append("  No price.")
            lines.append(f"  {self.quote.unavailable_reason}")
            lines.append(f"  Quote it yourself at: {self.service.quote_url}")
        else:
            lines += [
                f"  Boards          {self.quote.boards_ordered}"
                f" (from {self.quote.quantity} requested)",
                f"  Area billed     {self.quote.area_sq_in:.4f} sq in",
                f"  Subtotal        ${self.quote.subtotal_cents / 100:,.2f}",
                f"  Shipping        ${self.quote.shipping_cents / 100:,.2f}",
                f"  TOTAL           {self.quote.total_text()}",
                f"  Lead time       {self.quote.lead_time_days[0]}-"
                f"{self.quote.lead_time_days[1]} days",
                f"  Price basis     {self.quote.basis}",
            ]
        for note in self.quote.notes:
            lines.append(f"    - {note}")

        lines += [
            "",
            "PACKAGE",
        ]
        for layer in self.files:
            lines.append(f"  {layer.filename:<32} {len(layer.content):>8} bytes")
        lines += [
            f"  {BOARD_FILENAME:<32} {len(self.board_text):>8} bytes",
            "",
            "NEXT STEP -- a person, not this program",
        ]
        if self.gate.go:
            lines += [
                "  The checks above all cleared. Review them, then upload the",
                f"  package yourself at {self.service.quote_url} if you agree.",
            ]
        else:
            lines += [
                "  This order is NOT ready. Fix the blocking checks above and",
                "  run the gate again before showing this to anyone.",
            ]
        lines += ["", "-" * 70, SUBMISSION_BOUNDARY.rstrip()]
        return "\n".join(lines) + "\n"


def prepare_order(
    board: BoardResult,
    *,
    spec: CircuitSpec | None = None,
    options: OrderOptions | None = None,
    service: FabService | str = DEFAULT_SERVICE_ID,
) -> PreparedOrder:
    """Render the package, run the gate, price the board, and stop.

    The gate runs over the *same* rendered package and board text that go into
    the result, not over a second rendering of the same inputs. A check that
    passes on one rendering and ships another has verified nothing.

    A failing gate still produces a full :class:`PreparedOrder`. Refusing to
    build one would leave the reader with a verdict and no way to see what
    produced it, and the evidence is the part worth reading.
    """
    options = options or OrderOptions()
    if isinstance(service, str):
        service = service_by_id(service)

    files = tuple(fab_files(board))
    board_text = emit_kicad_pcb(board)

    return PreparedOrder(
        board=board,
        options=options,
        service=service,
        gate=run_gate(
            board,
            spec=spec,
            options=options,
            service=service,
            files=files,
            board_text=board_text,
        ),
        quote=quote(board, options, service=service),
        preflight=preflight(board, spec=spec, options=options),
        files=files,
        board_text=board_text,
        spec=spec,
    )
