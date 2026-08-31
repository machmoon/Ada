"""Turn extracted constraints into review findings against a real board.

This is where the schema pays for itself: a requirement that used to live on
page 33 of a PDF becomes a measurement against the board file, in the same
:class:`~silkscreen.audit.findings.Finding` shape the visual review renders
and reports.

Trust is inherited, not invented. The *measurement* here is deterministic,
but the *threshold* came out of a model reading a PDF -- so a finding is
``PROVEN`` only when its constraint is both mechanically quote-verified and
human-confirmed, and ``SUGGESTED`` otherwise, with the provenance quoted so a
reader can check the source themselves. A constraint the checker cannot apply
to this board (no matching net, no pin map) is returned in ``unchecked`` with
the reason -- never silently dropped, because a silent drop reads as "checked
and fine".

What is checkable today, deliberately narrow:

* decoupling -- count, per-pin coverage, capacitor value, placement distance;
* strap pins -- the pin is tied to a defined level, not floating.

Ratings (voltages, temperatures) describe operating conditions a board file
does not contain; they are returned as ``unchecked`` so the caller knows the
schema carries more than the checker can yet enforce.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from ..audit.findings import Finding, Origin, Severity, fmt_mm
from ..audit.geometry import AuditBoard, AuditPad, AuditPart
from ..audit.rules import is_ground, is_supply
from ..units import mm
from .schema import ConstraintSet, Decoupling, StrapPin

__all__ = ["CheckResult", "check_board", "parse_farads"]


@dataclass
class CheckResult:
    findings: list[Finding] = field(default_factory=list)
    #: ``(constraint_id, reason)`` for everything not checked -- constraints
    #: needing review, kinds the checker cannot enforce, rails with no net.
    unchecked: list[tuple[str, str]] = field(default_factory=list)


# --------------------------------------------------------------------------
# value parsing
# --------------------------------------------------------------------------

_FARAD_MULT = {
    "f": 1.0, "mf": 1e-3, "uf": 1e-6, "µf": 1e-6, "μf": 1e-6,
    "nf": 1e-9, "pf": 1e-12,
    "m": 1e-3, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "n": 1e-9, "p": 1e-12,
}

_VALUE_RE = re.compile(
    r"^\s*(\d+(?:[.,]\d+)?|[.,]\d+)\s*([a-zµμ]+)\s*$", re.IGNORECASE
)
# RKM style: 4u7, 2n2, 0R1-alike for caps.
_RKM_RE = re.compile(r"^\s*(\d+)([umµμnpf])(\d+)\s*$", re.IGNORECASE)


def parse_farads(text: str) -> float | None:
    """``"100nF"``/``".1uF"``/``"4u7"`` -> farads. None when unparseable.

    A bare number ("100", or the marking code "104") is unparseable on
    purpose: without a unit it could be picofarads, a marking code, or a
    typo, and guessing farads would turn every such capacitor into a false
    value-mismatch finding. Unparseable lands in ``unchecked``, where a
    human can see it.
    """
    if not text:
        return None
    s = text.strip()
    match = _RKM_RE.match(s)
    if match:
        whole, prefix, frac = match.groups()
        mult = _FARAD_MULT.get(prefix.lower())
        if mult is None:
            return None
        return (float(whole) + float(frac) / 10 ** len(frac)) * mult
    match = _VALUE_RE.match(s)
    if not match:
        return None
    number, unit = match.groups()
    mult = _FARAD_MULT.get(unit.lower())
    if mult is None:
        return None
    return float(number.replace(",", ".")) * mult


def _close(a: float, b: float, rel: float = 0.05) -> bool:
    return math.isclose(a, b, rel_tol=rel)


# --------------------------------------------------------------------------
# the checks
# --------------------------------------------------------------------------


def _finding(constraint, **kwargs) -> Finding:
    """A finding whose trust mirrors its constraint's trust.

    ``PROVEN`` needs the whole ladder: quote mechanically verified, human
    confirmed, and not awaiting review. Anything less is ``SUGGESTED`` --
    and per the audit package's contract a suggested finding carries no
    ``evidence``, so the measurement moves into ``detail`` where the report
    will not label a model-sourced threshold's comparison as a measurement.
    """
    prov = constraint.provenance
    origin = (
        Origin.PROVEN
        if constraint.confirmed and prov.verified and not constraint.needs_review
        else Origin.SUGGESTED
    )
    cite = f'datasheet p.{prov.page}'
    if prov.section:
        cite += f", {prov.section}"
    kwargs["detail"] = kwargs.get("detail", "") + (
        f' Source: {cite}: "{prov.quote}"' if prov.quote else f" Source: {cite}."
    )
    kwargs.setdefault("origin", origin)
    if kwargs["origin"] is Origin.SUGGESTED and kwargs.get("evidence"):
        kwargs["detail"] += f" Comparison: {kwargs.pop('evidence')}."
    kwargs.setdefault("source", f"constraint:{constraint.id}")
    return Finding(rule=f"constraint:{constraint.id}", **kwargs)


def _match_net(rail: str, net_names: list[str]) -> tuple[str | None, str]:
    """``(net, "")`` for the board net a rail name refers to, or
    ``(None, why)``.

    Exact (case-insensitive) first; then a net whose name contains the rail
    as a word ("+3V3_VDD" for "VDD"). Ground nets are excluded -- a
    decoupling constraint's rail is the supply side by construction. Two
    plausible nets is an honest failure of its own kind, and the reason says
    which nets tied.
    """
    rail_l = rail.strip().lower()
    if not rail_l:
        return None, "the constraint names no rail"
    supply_nets = [n for n in net_names if not is_ground(n)]
    for net in supply_nets:
        if net.lower() == rail_l:
            return net, ""
    word = re.compile(rf"(?:^|[^a-z0-9]){re.escape(rail_l)}(?:$|[^a-z0-9])")
    candidates = [n for n in supply_nets if word.search(n.lower())]
    if len(candidates) == 1:
        return candidates[0], ""
    if candidates:
        return None, (
            f"rail {rail!r} is ambiguous on this board: "
            f"{', '.join(sorted(candidates))} all match"
        )
    return None, f"no board net matches rail {rail!r}"


def _decoupling_caps(
    board: AuditBoard, net: str
) -> list[tuple[AuditPart, AuditPad]]:
    """Two-pad C-parts bridging ``net`` to a ground net, with the rail pad."""
    out = []
    for part in board.parts:
        if not part.ref.upper().startswith("C") or len(part.pads) != 2:
            continue
        nets = {p.net for p in part.pads}
        if net in nets and any(is_ground(n) for n in nets):
            rail_pad = next(p for p in part.pads if p.net == net)
            out.append((part, rail_pad))
    return out


def _supply_pads(board: AuditBoard, ref: str, net: str) -> list[AuditPad]:
    part = board.part_by_ref(ref)
    if part is None:
        return []
    return [p for p in part.pads if p.net == net]


def _consumers(board: AuditBoard, net: str,
               caps: list[tuple[AuditPart, AuditPad]]) -> list[AuditPart]:
    """Parts drawing off ``net`` -- everything on it that is not one of the
    decoupling capacitors themselves."""
    cap_refs = {part.ref for part, _pad in caps}
    return [
        part for part in board.parts
        if part.ref not in cap_refs and any(p.net == net for p in part.pads)
    ]


def _attributed_caps(
    board: AuditBoard, net: str, ref: str,
    caps: list[tuple[AuditPart, AuditPad]],
) -> list[tuple[AuditPart, AuditPad]]:
    """The caps on ``net`` that belong to ``ref`` rather than to another part.

    A capacitor is attributed to whichever consumer of the rail it sits
    nearest. Without this the count is board-wide, and a board-wide count
    against one part's pin requirement is the checker's worst failure mode:
    three capacitors clustered around a second regulator satisfy "one 100 nF
    per VDD pin" for an MCU that has none, and the board is reported
    compliant. Distance is the right discriminator because proximity is what
    the requirement is actually about -- a bypass capacitor two inches away
    is not decoupling this part whatever the netlist says.

    With one consumer on the rail this returns every cap, which is the same
    answer the board-wide count gave.
    """
    consumers = _consumers(board, net, caps)
    if len(consumers) <= 1:
        return list(caps)
    mine = []
    for cap_part, cap_pad in caps:
        nearest = min(
            consumers,
            key=lambda part: min(
                (math.hypot(pad.centre[0] - cap_pad.centre[0],
                            pad.centre[1] - cap_pad.centre[1])
                 for pad in part.pads if pad.net == net),
                default=float("inf"),
            ),
        )
        if nearest.ref == ref:
            mine.append((cap_part, cap_pad))
    return mine


def _uncovered_pins(
    supply_pads: list[AuditPad],
    caps: list[tuple[AuditPart, AuditPad]],
    limit_nm: int | None,
) -> list[AuditPad]:
    """Supply pads left with no capacitor of their own.

    One capacitor covers one pin: a part is matched to at most one pad, so
    a single 100 nF cannot satisfy four VDD pins by being counted four
    times. Pads are served nearest-first, and when the constraint carries a
    distance, a capacitor beyond it does not cover the pad at all.
    """
    pairs = sorted(
        (
            (math.hypot(cap_pad.centre[0] - pad.centre[0],
                        cap_pad.centre[1] - pad.centre[1]), i, j)
            for i, pad in enumerate(supply_pads)
            for j, (_cp, cap_pad) in enumerate(caps)
        ),
    )
    taken_pads: set[int] = set()
    taken_caps: set[int] = set()
    for dist, i, j in pairs:
        if i in taken_pads or j in taken_caps:
            continue
        if limit_nm is not None and dist > limit_nm:
            continue
        taken_pads.add(i)
        taken_caps.add(j)
    return [pad for i, pad in enumerate(supply_pads) if i not in taken_pads]


def _check_decoupling(
    board: AuditBoard, c: Decoupling, ref: str, result: CheckResult
) -> None:
    net, why = _match_net(c.rail, board.net_names)
    if net is None:
        result.unchecked.append((c.id, why))
        return

    all_caps = _decoupling_caps(board, net)
    part = board.part_by_ref(ref)
    supply_pads = _supply_pads(board, ref, net)
    # Only the capacitors that serve *this* part. See _attributed_caps: a
    # board-wide count is how a part with no decoupling at all passes.
    caps = _attributed_caps(board, net, ref, all_caps)
    limit_nm = mm(c.max_distance_mm) if c.max_distance_mm is not None else None

    if c.per_pin and supply_pads:
        # Per-pin is a coverage question, not a counting one.
        uncovered = _uncovered_pins(supply_pads, caps, limit_nm)
        if uncovered:
            names = ", ".join(pad.name for pad in uncovered)
            near = (f" within {c.max_distance_mm:g} mm"
                    if limit_nm is not None else "")
            result.findings.append(
                _finding(
                    c,
                    severity=Severity.MARGINAL,
                    title=(
                        f"{len(uncovered)} of {len(supply_pads)} {c.rail} pin(s) "
                        f"on {ref} have no decoupling capacitor of their own"
                    ),
                    detail=(
                        f"The datasheet requires one capacitor per {c.rail} "
                        f"pin. {names} {'has' if len(uncovered) == 1 else 'have'} "
                        f"none{near}; {len(caps)} capacitor(s) on {net} are "
                        f"placed nearest {ref}"
                        + (f" (of {len(all_caps)} on the rail board-wide)"
                           if len(all_caps) != len(caps) else "") + "."
                    ),
                    refs=(ref,),
                    nets=(net,),
                    extent=part.extent if part else None,
                    point=uncovered[0].centre,
                    evidence=(
                        f"{len(supply_pads) - len(uncovered)} of "
                        f"{len(supply_pads)} pin(s) covered"
                    ),
                    fix=f"Add decoupling beside {names} on {ref}.",
                )
            )
    else:
        required = c.count if c.count is not None else 1
        if len(caps) < required:
            result.findings.append(
                _finding(
                    c,
                    severity=Severity.MARGINAL,
                    title=(
                        f"{c.rail} has {len(caps)} decoupling capacitor(s) "
                        f"serving {ref}; the datasheet requires {required}"
                    ),
                    detail=(
                        f"{len(caps)} capacitor(s) bridging {net} to ground "
                        f"are placed nearest {ref}"
                        + (f", of {len(all_caps)} on the rail board-wide"
                           if len(all_caps) != len(caps) else "")
                        + f", against a required {required}."
                    ),
                    refs=(ref,) if part else (),
                    nets=(net,),
                    extent=part.extent if part else None,
                    evidence=f"{len(caps)} found, {required} required",
                    fix=f"Add decoupling on {net} next to {ref}.",
                )
            )

    # Value: the datasheet naming 100 nF is satisfied when SOME cap on the
    # rail carries it. Other caps on the same rail (a bulk 4.7 uF beside the
    # 100 nF) are not violations of this constraint -- flagging every
    # non-matching cap would make any correctly built multi-cap rail read as
    # riddled with errors.
    if c.value is not None:
        want = parse_farads(f"{c.value}{c.unit}")
        if want is None:
            result.unchecked.append(
                (c.id,
                 f"the constraint's value {c.value:g} {c.unit!r} is not a "
                 f"capacitance this checker can parse")
            )
        elif caps:
            fitted = [(cap_part, parse_farads(cap_part.value))
                      for cap_part, _pad in caps]
            unparseable = [cp for cp, v in fitted if v is None]
            satisfied = any(v is not None and _close(v, want)
                            for _cp, v in fitted)
            if satisfied:
                pass
            elif unparseable:
                names = ", ".join(
                    f"{cp.ref}={cp.value!r}" for cp in unparseable
                )
                result.unchecked.append(
                    (c.id,
                     f"no cap on {net} parses to {c.value:g} {c.unit}, but "
                     f"{names} could not be read as capacitance")
                )
            else:
                values = ", ".join(
                    f"{cp.ref}={cp.value}" for cp, _v in fitted
                )
                result.findings.append(
                    _finding(
                        c,
                        severity=Severity.NOTE,
                        title=(
                            f"no capacitor on {c.rail} carries the "
                            f"datasheet's {c.value:g} {c.unit}"
                        ),
                        detail=(
                            f"The capacitors bridging {net} to ground are "
                            f"{values}; none is the stated "
                            f"{c.value:g} {c.unit}."
                        ),
                        refs=tuple(cp.ref for cp, _v in fitted),
                        nets=(net,),
                        extent=fitted[0][0].extent,
                        evidence=(
                            f"fitted {values}; {c.value:g} {c.unit} specified"
                        ),
                        fix=(
                            f"Fit a {c.value:g} {c.unit} capacitor on {net} "
                            f"or confirm the substitution."
                        ),
                    )
                )

    if c.max_distance_mm is not None and not supply_pads:
        # The constraint carries a measurable distance and this board gives
        # us nothing to measure from. Saying nothing here would read as
        # "checked and fine".
        result.unchecked.append(
            (c.id,
             f"cannot measure the {c.max_distance_mm:g} mm placement "
             f"distance: {ref!r} has no pads on {net}")
        )
    if c.max_distance_mm is not None and supply_pads and caps:
        for pad in supply_pads:
            nearest_nm, cap_part = min(
                (
                    (
                        int(round(math.hypot(
                            cp.centre[0] - pad.centre[0],
                            cp.centre[1] - pad.centre[1],
                        ))),
                        part_,
                    )
                    for part_, cp in caps
                ),
                key=lambda t: t[0],
            )
            if nearest_nm > limit_nm:
                result.findings.append(
                    _finding(
                        c,
                        severity=Severity.MARGINAL,
                        title=(
                            f"{cap_part.ref} sits {fmt_mm(nearest_nm)} from "
                            f"{pad.name}; the datasheet allows "
                            f"{c.max_distance_mm:g} mm"
                        ),
                        detail=(
                            f"The nearest {net}-to-ground capacitor to "
                            f"{pad.name} is {cap_part.ref} at "
                            f"{fmt_mm(nearest_nm)}, beyond the stated "
                            f"{c.max_distance_mm:g} mm."
                        ),
                        refs=(ref, cap_part.ref),
                        nets=(net,),
                        extent=pad.rect.union(cap_part.extent),
                        point=cap_part.centre,
                        evidence=(
                            f"{fmt_mm(nearest_nm)} measured, limit "
                            f"{fmt_mm(limit_nm)}"
                        ),
                        fix=f"Move {cap_part.ref} beside {pad.name}.",
                    )
                )


def _tied_level(board: AuditBoard, pad: AuditPad) -> str:
    """What defined level, if any, this pad's net is tied to.

    ``"high"``/``"low"`` for a direct supply/ground net;
    ``"pulled-high"``/``"pulled-low"`` when a two-pad R-part bridges the net
    to a supply or ground -- the direction matters, because a strap pulled to
    the wrong rail is exactly the bug class strap checking exists for;
    ``""`` for a net that defines nothing.
    """
    if is_supply(pad.net):
        return "high"
    if is_ground(pad.net):
        return "low"
    pulled = ""
    for part in board.parts:
        if not part.ref.upper().startswith("R") or len(part.pads) != 2:
            continue
        nets = [p.net for p in part.pads]
        if pad.net in nets:
            other = nets[1] if nets[0] == pad.net else nets[0]
            if is_supply(other):
                pulled = "pulled-low" if pulled == "pulled-low" else "pulled-high"
            elif is_ground(other):
                pulled = "pulled-high" if pulled == "pulled-high" else "pulled-low"
    return pulled


#: Required states that name a direction, and those that only demand a
#: defined level. Anything else is a state this checker cannot enforce, and
#: it says so rather than passing the pin in silence.
_DIRECTED = {"high", "low", "pull-up", "pull-down"}
_UNDIRECTED = {"no-float", "not floating", "defined", "tied", "connected"}


def _check_strap(
    board: AuditBoard,
    c: StrapPin,
    ref: str,
    pin_map: dict[str, str],
    result: CheckResult,
) -> None:
    part = board.part_by_ref(ref)
    if part is None:
        result.unchecked.append((c.id, f"no part {ref!r} on the board"))
        return
    number = pin_map.get(c.pin) or pin_map.get(c.pin.upper())
    if number is None:
        result.unchecked.append(
            (c.id, f"pin {c.pin!r} is not in the supplied pin map")
        )
        return
    pad = next((p for p in part.pads if p.number == number), None)
    if pad is None:
        result.unchecked.append(
            (c.id, f"{ref} has no pad {number} (pin {c.pin})")
        )
        return

    peers = board.pads_by_net().get(pad.net, [])
    floating = not pad.net or len(peers) < 2
    level = "" if floating else _tied_level(board, pad)

    # A strap requirement that carries a condition ("when unused", "to boot
    # from flash") applies to *some* configurations. The condition is prose
    # this checker cannot evaluate, so enforcing it on every board turns a
    # legitimate design into a blocker -- an NE555 whose RESET is driven by
    # an MCU is correct, and the datasheet's "connect RESET to VCC when not
    # used" does not apply to it. A false blocker costs more than a missed
    # finding here: it is the finding that teaches an engineer to stop
    # reading the checker. So a conditional requirement on a pin that *is*
    # driven to some defined state goes to unchecked, naming the condition
    # for a human. Floating is still a blocker whatever the condition says,
    # because no configuration wants a floating strap pin.
    if c.condition and not floating and not level:
        result.unchecked.append(
            (c.id,
             f"{ref} {c.pin} is driven by net {pad.net}, and this requirement "
             f"applies only {c.condition}; confirm which configuration this "
             f"board uses")
        )
        return

    if floating or not level:
        state = "is not connected" if floating else (
            f"is on net {pad.net}, which ties it to no defined level"
        )
        result.findings.append(
            _finding(
                c,
                severity=Severity.BLOCKER,
                title=f"{ref} {c.pin} (pad {number}) {state}",
                detail=(
                    f"The datasheet requires {c.pin} "
                    f"{c.required_state or 'tied to a defined level'}"
                    + (f" {c.condition}" if c.condition else "")
                    + f", but pad {number} {state}."
                ),
                refs=(ref,),
                nets=(pad.net,) if pad.net else (),
                extent=pad.rect,
                evidence=(
                    f"net {pad.net!r}, {len(peers)} pad(s) on it"
                    if pad.net else "no net"
                ),
                fix=(
                    f"Tie {c.pin} {c.required_state}"
                    + (f" ({c.resistor_value:g} {c.resistor_unit})"
                       if c.resistor_value is not None else "")
                    + "."
                ),
            )
        )
        return

    # Tied to something defined. Direction checks need the required state.
    want = (c.required_state or "").strip().lower()
    if want not in _DIRECTED and want not in _UNDIRECTED:
        # An unrecognised state used to fall through every branch below and
        # return quietly, which reads as "checked, and the pin is fine".
        result.unchecked.append(
            (c.id,
             f"required_state {c.required_state!r} is not one this checker "
             f"understands ({', '.join(sorted(_DIRECTED | _UNDIRECTED))})")
        )
        return
    if want in _UNDIRECTED:
        # "no-float" and friends ask only for a defined level, which the pin
        # has by this point. Nothing further to check.
        return
    tied_low = level in ("low", "pulled-low")
    tied_high = level in ("high", "pulled-high")
    if want in ("high", "pull-up") and tied_low or \
       want in ("low", "pull-down") and tied_high:
        # Tied the other way is a real contradiction, but under a condition
        # this checker cannot evaluate it may be a deliberate alternative
        # configuration -- reported, and not as a blocker.
        result.findings.append(
            _finding(
                c,
                severity=Severity.MARGINAL if c.condition else Severity.BLOCKER,
                title=(
                    f"{ref} {c.pin} is tied {level}; the datasheet requires "
                    f"{c.required_state}"
                    + (f" {c.condition}" if c.condition else "")
                ),
                detail=(
                    f"Pad {number} is tied {level.replace('-', ' ')} via net "
                    f"{pad.net}, but the datasheet requires {c.pin} "
                    f"{c.required_state}"
                    + (f" {c.condition}" if c.condition else "") + "."
                ),
                refs=(ref,),
                nets=(pad.net,),
                extent=pad.rect,
                evidence=f"net {pad.net} is tied {level}",
                fix=f"Retie {c.pin} {c.required_state}.",
            )
        )


def check_board(
    board: AuditBoard,
    cset: ConstraintSet,
    *,
    ref: str,
    pin_map: dict[str, str] | None = None,
    include_needs_review: bool = False,
) -> CheckResult:
    """Check one board against one part's constraint set.

    Args:
        board: The loaded board (``silkscreen.audit.load_audit_board``).
        ref: The board reference the constraint set's part carries ("U1").
        pin_map: Datasheet pin name -> pad number, e.g. from
            :meth:`~silkscreen.agents.datasheet.PartFacts.pin_map`. Without
            it, strap-pin constraints go to ``unchecked``.
        include_needs_review: Also check constraints still awaiting human
            review. Their findings are ``SUGGESTED`` regardless, but the
            default keeps unreviewed extractions out of reports entirely.
    """
    result = CheckResult()
    pin_map = pin_map or {}

    for c in cset.all_constraints():
        if c.needs_review and not include_needs_review:
            result.unchecked.append(
                (c.id, f"needs human review: {c.review_reason}")
            )
            continue
        if isinstance(c, Decoupling):
            _check_decoupling(board, c, ref, result)
        elif isinstance(c, StrapPin):
            _check_strap(board, c, ref, pin_map, result)
        else:
            result.unchecked.append(
                (c.id,
                 "not board-checkable: describes operating conditions, not "
                 "board geometry")
            )
    return result
