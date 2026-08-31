"""Real fabricators: what they can build, what they charge, and where we stop.

A fab package is not an order. An order names a *house*, a *service*, a
quantity, a set of options that house actually offers, a price and a lead time.
This module is where the engine meets three real ones -- OSH Park, JLCPCB and
PCBWay -- and it is deliberately the only module in the package that knows any
of them exist.

Three things are worth reading before the code.

**The capability check is not the design rule check.** :mod:`silkscreen.audit`
asks whether the board is *right*: are courtyards clear, are tracks clear of
each other, is copper inside the outline. This module asks the different and
narrower question of whether *this house* will build it -- a 0.13 mm track is a
perfectly good track and OSH Park will refuse it, while JLCPCB will not. Neither
check subsumes the other, and a board can pass one and fail the other.

**Only one of the three can be priced.** OSH Park publishes a price *rule* --
dollars per square inch, three copies included -- so a quote is arithmetic over
the board's own geometry and is exact. JLCPCB and PCBWay both quote through
APIs that require a registered application's key and secret. This project does
not create accounts at fabricators and holds no credentials, so for those two
houses the honest output is a complete, validated order specification, a link
to their own quote page, and no number at all. :class:`PriceBasis` carries that
distinction in the type rather than in a comment, so nothing downstream can
render an estimate as though it were a quote.

**Nothing here submits.** There is no code path from this module to a purchase,
by design and not by omission -- see :func:`submit_order`, which exists solely
to refuse and to say why.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum

from .board import DEFAULT_BOARD_MARGIN_NM, BoardResult
from .fab import SILK_WIDTH_NM
from .order import OrderIssue, OrderIssueSeverity, OrderOptions
from .units import mil, mm, to_mm

__all__ = [
    "PriceBasis",
    "FabService",
    "Quote",
    "SubmissionRefused",
    "SERVICES",
    "service_by_id",
    "check_capabilities",
    "quote",
    "submit_order",
    "SUBMISSION_BOUNDARY",
]

#: Square nanometres in one square inch, exactly: 25 400 nm/mil x 1000 mil.
_NM2_PER_SQ_IN = 25_400_000 * 25_400_000


class PriceBasis(StrEnum):
    """Where a price came from, which decides how much it may be trusted."""

    #: Computed from the house's own published price rule. Exact arithmetic
    #: over the board's geometry; no interpolation and no guessing.
    PUBLISHED_RULE = "published-rule"
    #: Returned by the house's own quoting API for these exact files.
    LIVE_API = "live-api"
    #: No price. The house quotes only through an authenticated API, and this
    #: project holds no credentials there.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class FabService:
    """One purchasable service at one house, with its published limits.

    Every number here is quoted from the house's own documentation and carries
    ``source_url`` so a reviewer can check it rather than trust it. They do go
    stale -- fabs revise capabilities and prices -- which is exactly why the
    URL travels with the number instead of the number travelling alone.
    """

    id: str
    house: str
    service: str
    layers: int
    thickness_mm: float
    finish: str
    mask_colour: str
    #: Boards per purchasable unit. OSH Park sells prototypes in threes; a
    #: request for four boards buys six and the quote has to say so.
    copies_per_unit: int
    min_track_nm: int
    min_clearance_nm: int
    min_drill_nm: int
    min_annular_ring_nm: int
    min_silk_width_nm: int
    min_side_nm: int
    max_width_nm: int
    max_height_nm: int
    lead_time_days: tuple[int, int]
    quote_url: str
    source_url: str
    #: Price in US cents per square inch of board, for one unit of
    #: ``copies_per_unit`` boards. ``None`` where the house does not publish a
    #: rule that can be evaluated without contacting it.
    cents_per_sq_in: int | None = None
    shipping_cents: int | None = None
    shipping_note: str = ""
    offers_assembly: bool = False
    #: Why this service cannot be priced here. Empty when it can.
    price_unavailable_reason: str = ""


#: The three houses, as documented on 2026-08-31. OSH Park's two-layer
#: prototype service is first because it is the only one of the three whose
#: price is a published rule rather than an authenticated API call, which makes
#: it the only one this code can quote honestly.
SERVICES: tuple[FabService, ...] = (
    FabService(
        id="oshpark-2layer",
        house="OSH Park",
        service="2 Layer Prototype",
        layers=2,
        thickness_mm=1.6,
        finish="ENIG",
        mask_colour="purple",
        copies_per_unit=3,
        min_track_nm=mil(6),
        min_clearance_nm=mil(6),
        min_drill_nm=mil(10),
        min_annular_ring_nm=mil(5),
        min_silk_width_nm=mil(5),
        min_side_nm=mil(250),
        max_width_nm=mil(16_000),
        max_height_nm=mil(22_000),
        lead_time_days=(9, 12),
        cents_per_sq_in=500,
        shipping_cents=0,
        shipping_note="Free shipping worldwide, included in the total.",
        quote_url="https://oshpark.com/",
        source_url="https://docs.oshpark.com/services/two-layer/",
    ),
    FabService(
        id="oshpark-2layer-swift",
        house="OSH Park",
        service="2 Layer Super Swift",
        layers=2,
        thickness_mm=1.6,
        finish="ENIG",
        mask_colour="purple",
        copies_per_unit=3,
        min_track_nm=mil(6),
        min_clearance_nm=mil(6),
        min_drill_nm=mil(10),
        min_annular_ring_nm=mil(5),
        min_silk_width_nm=mil(5),
        min_side_nm=mil(250),
        max_width_nm=mil(16_000),
        max_height_nm=mil(22_000),
        lead_time_days=(4, 5),
        cents_per_sq_in=1000,
        shipping_cents=0,
        shipping_note="Free shipping worldwide, included in the total.",
        quote_url="https://oshpark.com/",
        source_url="https://docs.oshpark.com/services/super-swift/",
    ),
    FabService(
        id="jlcpcb-2layer",
        house="JLCPCB",
        service="2 Layer FR-4, standard",
        layers=2,
        thickness_mm=1.6,
        finish="lead-free HASL",
        mask_colour="green",
        copies_per_unit=1,
        min_track_nm=mm(0.10),
        min_clearance_nm=mm(0.10),
        min_drill_nm=mm(0.15),
        min_annular_ring_nm=mm(0.18),
        min_silk_width_nm=mm(0.15),
        min_side_nm=mm(3),
        max_width_nm=mm(1020),
        max_height_nm=mm(600),
        lead_time_days=(2, 5),
        quote_url="https://cart.jlcpcb.com/quote",
        source_url="https://jlcpcb.com/capabilities/pcb-capabilities",
        offers_assembly=True,
        price_unavailable_reason=(
            "JLCPCB quotes through its API platform, which requires a "
            "registered application's key and secret. Silkscreen holds no "
            "JLCPCB credentials and does not create accounts at fabricators, "
            "so no price is produced here. Upload this package at the quote "
            "URL to get a real one."
        ),
    ),
    FabService(
        id="pcbway-2layer",
        house="PCBWay",
        service="2 Layer FR-4, standard",
        layers=2,
        thickness_mm=1.6,
        finish="lead-free HASL",
        mask_colour="green",
        copies_per_unit=1,
        min_track_nm=mm(0.10),
        min_clearance_nm=mm(0.10),
        min_drill_nm=mm(0.20),
        min_annular_ring_nm=mm(0.15),
        min_silk_width_nm=mm(0.15),
        min_side_nm=mm(5),
        max_width_nm=mm(500),
        max_height_nm=mm(500),
        lead_time_days=(2, 5),
        quote_url="https://www.pcbway.com/orderonline.aspx",
        source_url="https://www.pcbway.com/capabilities.html",
        offers_assembly=True,
        price_unavailable_reason=(
            "PCBWay quotes through an authenticated OpenAPI. Silkscreen holds "
            "no PCBWay credentials and does not create accounts at "
            "fabricators, so no price is produced here. Upload this package "
            "at the quote URL to get a real one."
        ),
    ),
)

#: Default house. OSH Park, because it is the only one of the three that can be
#: quoted without an account, so the default path produces a real number.
DEFAULT_SERVICE_ID = "oshpark-2layer"


def service_by_id(service_id: str) -> FabService:
    """Look up a service, listing the alternatives when the id is wrong."""
    for service in SERVICES:
        if service.id == service_id:
            return service
    raise ValueError(
        f"unknown fab service {service_id!r}; known: "
        f"{[s.id for s in SERVICES]}"
    )


# ------------------------------------------------------------- capabilities


def _narrowest_track_nm(board: BoardResult) -> int | None:
    widths = [track.width_nm for track in board.tracks]
    return min(widths) if widths else None


def _smallest_drill_nm(board: BoardResult) -> int | None:
    drills = [via.drill_nm for via in board.vias]
    return min(drills) if drills else None


def _smallest_annular_ring_nm(board: BoardResult) -> int | None:
    """Half the difference between a via's pad and its hole, at the worst via.

    The annular ring is what is left of the pad once the drill has been through
    it, and it is what the plating actually bonds to. Too little of it and the
    barrel tears out of the pad under thermal stress -- a board that works on
    the bench and fails in the field, which is the worst failure to ship.
    """
    rings = [(via.diameter_nm - via.drill_nm) // 2 for via in board.vias]
    return min(rings) if rings else None


def _mm3(value_nm: int) -> float:
    return round(to_mm(value_nm), 3)


def check_capabilities(
    board: BoardResult,
    service: FabService,
    *,
    options: OrderOptions | None = None,
    silk_width_nm: int = SILK_WIDTH_NM,
    margin_nm: int = DEFAULT_BOARD_MARGIN_NM,
) -> tuple[OrderIssue, ...]:
    """Compare the board and the requested options against one house's limits.

    Returns :class:`~silkscreen.order.OrderIssue` values so the results fold
    into the same severity model the rest of the order path uses. A limit the
    house states as absolute produces a blocker; a mismatch the house would
    silently substitute produces a warning, because a substitution the buyer
    did not ask for is a surprise on arrival rather than a refusal at checkout.

    The dimensions checked are the emitted profile -- the placement plus the
    outline margin on all four sides -- because that is the rectangle the fab
    actually cuts, the same one :func:`quote` bills. Checking the placement
    alone approves a board whose shipped outline exceeds the house maximum.
    """
    options = options or OrderOptions()
    issues: list[OrderIssue] = []
    where = f"{service.house} {service.service}"

    width_nm = board.width_nm + 2 * margin_nm
    height_nm = board.height_nm + 2 * margin_nm
    if width_nm > service.max_width_nm or height_nm > service.max_height_nm:
        issues.append(
            OrderIssue(
                code="board-too-large",
                severity=OrderIssueSeverity.BLOCKER,
                title=f"Board exceeds the maximum panel {where} will build",
                detail=(
                    f"The board is {_mm3(width_nm)} x {_mm3(height_nm)} mm and "
                    f"{where} builds up to {_mm3(service.max_width_nm)} x "
                    f"{_mm3(service.max_height_nm)} mm ({service.source_url})."
                ),
            )
        )
    if min(width_nm, height_nm) < service.min_side_nm:
        issues.append(
            OrderIssue(
                code="board-too-small",
                severity=OrderIssueSeverity.BLOCKER,
                title=f"Board is under the minimum side {where} accepts",
                detail=(
                    f"The board is {_mm3(width_nm)} x {_mm3(height_nm)} mm and "
                    f"{where} needs at least {_mm3(service.min_side_nm)} mm on "
                    f"each side ({service.source_url})."
                ),
            )
        )

    track_nm = _narrowest_track_nm(board)
    if track_nm is not None and track_nm < service.min_track_nm:
        issues.append(
            OrderIssue(
                code="track-below-fab-minimum",
                severity=OrderIssueSeverity.BLOCKER,
                title=f"A track is narrower than {where} can etch",
                detail=(
                    f"The narrowest track is {_mm3(track_nm)} mm; {where} "
                    f"guarantees {_mm3(service.min_track_nm)} mm "
                    f"({service.source_url}). A track under the house minimum "
                    f"is not a thin track, it is a track that may not be there "
                    f"at all after etching."
                ),
            )
        )

    drill_nm = _smallest_drill_nm(board)
    if drill_nm is not None and drill_nm < service.min_drill_nm:
        issues.append(
            OrderIssue(
                code="drill-below-fab-minimum",
                severity=OrderIssueSeverity.BLOCKER,
                title=f"A hole is smaller than {where} will drill",
                detail=(
                    f"The smallest hole is {_mm3(drill_nm)} mm; {where} drills "
                    f"down to {_mm3(service.min_drill_nm)} mm "
                    f"({service.source_url})."
                ),
            )
        )

    ring_nm = _smallest_annular_ring_nm(board)
    if ring_nm is not None and ring_nm < service.min_annular_ring_nm:
        issues.append(
            OrderIssue(
                code="annular-ring-below-fab-minimum",
                severity=OrderIssueSeverity.BLOCKER,
                title=f"A via's annular ring is thinner than {where} allows",
                detail=(
                    f"The worst via leaves {_mm3(ring_nm)} mm of copper around "
                    f"its hole; {where} requires {_mm3(service.min_annular_ring_nm)} "
                    f"mm ({service.source_url}). Under that, the plated barrel "
                    f"can tear out of the pad -- a board that passes on the "
                    f"bench and fails later in the field."
                ),
            )
        )

    if silk_width_nm < service.min_silk_width_nm:
        issues.append(
            OrderIssue(
                code="silkscreen-below-fab-minimum",
                severity=OrderIssueSeverity.WARNING,
                title=f"Silkscreen strokes are thinner than {where} prints",
                detail=(
                    f"The legend is drawn with a {_mm3(silk_width_nm)} mm pen; "
                    f"{where} prints down to {_mm3(service.min_silk_width_nm)} "
                    f"mm ({service.source_url}). Thinner ink is not refused, it "
                    f"is printed badly or dropped."
                ),
            )
        )

    if options.layers != service.layers:
        issues.append(
            OrderIssue(
                code="layer-count-mismatch",
                severity=OrderIssueSeverity.BLOCKER,
                title=f"{options.layers}-layer order against a "
                f"{service.layers}-layer service",
                detail=(
                    f"The order asks for {options.layers} layers and this "
                    f"service builds {service.layers}. Pick the service that "
                    f"matches, rather than letting the house choose."
                ),
            )
        )

    if abs(float(options.thickness_mm) - service.thickness_mm) > 1e-9:
        issues.append(
            OrderIssue(
                code="thickness-substituted",
                severity=OrderIssueSeverity.WARNING,
                title=f"{where} builds this service at "
                f"{service.thickness_mm} mm",
                detail=(
                    f"The order asks for {options.thickness_mm} mm. This "
                    f"service has a fixed {service.thickness_mm} mm stack-up, "
                    f"so what arrives will be {service.thickness_mm} mm."
                ),
            )
        )

    if options.assembly and not service.offers_assembly:
        issues.append(
            OrderIssue(
                code="assembly-not-offered",
                severity=OrderIssueSeverity.BLOCKER,
                title=f"{where} does not assemble boards",
                detail=(
                    f"The order asks for SMT assembly and {where} supplies "
                    f"bare boards only. The BOM and pick-and-place files are "
                    f"still in the package, but this house will not populate "
                    f"them."
                ),
            )
        )

    return tuple(issues)


# -------------------------------------------------------------------- quotes


@dataclass(frozen=True)
class Quote:
    """A price for a specific board at a specific service, or the reason there is none.

    ``basis`` is the field to read first. A quote whose basis is
    :attr:`PriceBasis.UNAVAILABLE` has no numbers in it at all -- not zeroes,
    not estimates -- because a zero in a money field is indistinguishable from
    "free" at a glance, and the whole point of this object is that a human
    reads it before spending anything.
    """

    service: FabService
    basis: PriceBasis
    #: Boards the buyer asked for.
    quantity: int
    #: Boards actually purchased, which is the request rounded up to the
    #: house's purchasable unit.
    boards_ordered: int = 0
    area_sq_in: float = 0.0
    subtotal_cents: int | None = None
    shipping_cents: int | None = None
    total_cents: int | None = None
    currency: str = "USD"
    lead_time_days: tuple[int, int] = (0, 0)
    unavailable_reason: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def priced(self) -> bool:
        return self.basis is not PriceBasis.UNAVAILABLE

    def total_text(self) -> str:
        """The total as money, or a plain statement that there is none."""
        if self.total_cents is None:
            return "no price"
        return f"${self.total_cents / 100:,.2f} {self.currency}"

    def as_dict(self) -> dict:
        return {
            "house": self.service.house,
            "service": self.service.service,
            "service_id": self.service.id,
            "basis": str(self.basis),
            "priced": self.priced,
            "quantity": self.quantity,
            "boards_ordered": self.boards_ordered,
            "area_sq_in": round(self.area_sq_in, 4),
            "subtotal_cents": self.subtotal_cents,
            "shipping_cents": self.shipping_cents,
            "total_cents": self.total_cents,
            "total_text": self.total_text(),
            "currency": self.currency,
            "lead_time_days": list(self.lead_time_days),
            "unavailable_reason": self.unavailable_reason,
            "notes": list(self.notes),
            "quote_url": self.service.quote_url,
            "source_url": self.service.source_url,
        }


def _area_sq_in(board: BoardResult, *, margin_nm: int) -> float:
    """Billable area: the outline the fab cuts, margin included.

    The fab bills the rectangle it routes out, which is the profile in the
    Gerbers -- the placement plus the outline margin on all four sides, not the
    placement alone. Quoting the placed area under-prices every board.
    """
    width_nm = board.width_nm + 2 * margin_nm
    height_nm = board.height_nm + 2 * margin_nm
    return (width_nm * height_nm) / _NM2_PER_SQ_IN


def quote(
    board: BoardResult,
    options: OrderOptions | None = None,
    *,
    service: FabService | str = DEFAULT_SERVICE_ID,
    margin_nm: int = mm(2.0),
) -> Quote:
    """Price ``board`` at ``service``, or say why it cannot be priced.

    No network call is made. The only house here with a price is OSH Park, and
    OSH Park's price is a published rule over the board's own area, so
    contacting them would tell us nothing the arithmetic does not. The other
    two return an unpriced quote naming the credential they would need.
    """
    if isinstance(service, str):
        service = service_by_id(service)
    options = options or OrderOptions()

    if service.cents_per_sq_in is None:
        return Quote(
            service=service,
            basis=PriceBasis.UNAVAILABLE,
            quantity=options.quantity,
            lead_time_days=service.lead_time_days,
            unavailable_reason=service.price_unavailable_reason,
            notes=(
                "The order specification and the fab package are complete; "
                "only the price is missing.",
            ),
        )

    area = _area_sq_in(board, margin_nm=margin_nm)
    units = math.ceil(options.quantity / service.copies_per_unit)
    boards = units * service.copies_per_unit
    # Rounded once, at the end, in cents. Rounding per unit and multiplying
    # would drift from what the house's own cart shows.
    subtotal = int(round(service.cents_per_sq_in * area * units))
    shipping = service.shipping_cents or 0

    notes = [
        f"{service.house} sells this service in units of "
        f"{service.copies_per_unit} board(s) at "
        f"${service.cents_per_sq_in / 100:.2f} per square inch "
        f"({service.source_url}).",
        f"Billed area is the routed outline, {area:.4f} sq in, which includes "
        f"the {to_mm(margin_nm):g} mm margin around the placement.",
    ]
    if boards != options.quantity:
        notes.append(
            f"{options.quantity} board(s) requested; {boards} will arrive, "
            f"because this service is not sold in smaller units than "
            f"{service.copies_per_unit}."
        )
    if service.shipping_note:
        notes.append(service.shipping_note)

    return Quote(
        service=service,
        basis=PriceBasis.PUBLISHED_RULE,
        quantity=options.quantity,
        boards_ordered=boards,
        area_sq_in=area,
        subtotal_cents=subtotal,
        shipping_cents=shipping,
        total_cents=subtotal + shipping,
        lead_time_days=service.lead_time_days,
        notes=tuple(notes),
    )


# --------------------------------------------------------- the hard boundary


SUBMISSION_BOUNDARY = """\
Silkscreen prepares orders. It does not place them.

Everything up to this line is reversible: a board can be re-placed, re-routed,
re-checked and re-packaged at no cost but time. Submitting an order is not. It
spends money, it consumes a fab slot, and a mistake in it arrives as a box of
scrap copper a week later.

The gate in silkscreen.gate is good, and it is not good enough to close that
loop. It proves what it measures and says so; it cannot prove the circuit is
the circuit the buyer meant. An agent that clears its own checks and then buys
the board has converted every remaining class of design error -- a wrong part
value, a mis-read datasheet pin, a topology that validates and does not work --
into money, silently and at machine speed. That is a categorically different
failure from a misplaced footprint, and no amount of additional checking makes
it the same one.

So the last step is a person. Silkscreen hands them the package, the evidence
and the price, and stops.
"""


class SubmissionRefused(RuntimeError):
    """Raised by :func:`submit_order`. There is no argument that clears it."""


def submit_order(*args: object, **kwargs: object) -> None:
    """Refuse to place an order, always, and explain why.

    This function exists rather than being absent on purpose. An absent
    function is an oversight someone fills in later; a function that refuses is
    a decision, and it is discoverable from the call site that wants it. It
    takes and ignores any arguments so that no caller can believe it failed
    only because it was called wrongly.

    Raises:
        SubmissionRefused: unconditionally.
    """
    raise SubmissionRefused(SUBMISSION_BOUNDARY)
