"""Propose an enclosure, then make the model fix its own mistakes.

The :mod:`silkscreen.agents.propose` loop, applied to 3D: the model's output
goes through :func:`silkscreen.enclosure.ir.parse_enclosure_spec` and then
:func:`silkscreen.enclosure.verify.verify_fit`. How hard the loop pushes back
is the ``rigorous`` flag's call. The **fast default** (``rigorous=False``,
demo speed) repairs only JSON/spec validation failures, allows one repair
round, and runs ``verify_fit(strict=False)`` purely for the receipt -- a fit
failure is downgraded to a warning on the returned report and the artifact
still ships. **Rigorous mode** (``rigorous=True``) is the strict loop: every
failure -- JSON shape, unprintable wall, a cutout naming a part the board does
not have, a fit warning -- is batched back as a single repair prompt over
three repair rounds, and a hard fit failure blocks. The loop is bounded and
gives up loudly with :class:`EnclosureProposalError`.

The model never receives or invents a raw dimension to transcribe (plan
decision 3): the prompt carries the *measured* board facts -- outline size,
part rectangles, heights, edge-adjacent refs with their faces -- purely so the
model can choose style within bounds, and the deterministic emitter injects
every millimetre from the envelope, not from the model's answer.

Intended live tier: :data:`~silkscreen.agents.model.CHEAP_MODEL` -- choosing a
lid style is a mechanical pass, not a reasoning one. The tier is the caller's
to construct; this module only ever sees the :class:`Model` protocol.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..enclosure.board_shape import BoardEnvelope, PartExtent
from ..enclosure.errors import (
    CavityFitError,
    CutoutError,
    EnclosureError,
    EnclosureValidationError,
    WallError,
)
from ..enclosure.ir import EnclosureSpec, parse_enclosure_spec
from ..enclosure.verify import FitReport, verify_fit
from ..units import mm, to_mm
from .model import Model

__all__ = ["ENCLOSURE_PROMPT", "EnclosureProposalError", "propose_enclosure"]

#: A part whose courtyard sits within this of a board edge is offered to the
#: model as a cutout candidate on that face.
_EDGE_NEAR_NM: int = mm(3.0)

#: Repair budgets per mode, applied when the caller does not pass
#: ``max_repairs`` explicitly: fast mode gets one round (speed over rigor,
#: by default), rigorous mode keeps the strict loop's three.
_FAST_MAX_REPAIRS: int = 1
_RIGOROUS_MAX_REPAIRS: int = 3


class EnclosureProposalError(EnclosureError):
    """The model could not produce a valid enclosure within the repair budget.

    ``attempts`` counts every round made, so the caller can report honestly
    how hard the model tried before the run degraded (plan decision 5).
    """

    def __init__(self, message: str, attempts: int):
        self.attempts = attempts
        super().__init__(message)


#: The marker ``"ENCLOSURE-SPEC v1"`` is frozen (docs/ai-cad-plan.md) so any
#: workstream's ``ScriptedModel.by_marker`` can key on it.
ENCLOSURE_PROMPT = """\
You are choosing the style of a 3D-printed enclosure for a finished PCB
(ENCLOSURE-SPEC v1). Respond with ONE JSON object -- no prose, no code fence.

{
  "wall_mm": <number, >= 1.2>,
  "clearance_mm": <number, board-to-cavity gap; 1.0 is typical, below 0.5 binds>,
  "corner_radius_mm": <number, 0 for square corners>,
  "lid": "friction" | "screw" | "none",
  "cutouts": [
    {"id": "<unique identifier>", "ref": "<board ref, e.g. J1>",
     "face": "left" | "right" | "front" | "back" | "top",
     "margin_mm": <number, opening margin around the part>}
  ],
  "standoffs": true | false,
  "vents": true | false,
  "label": "<short text embossed on the lid>" | null
}

Hard rules -- a proposal breaking any of these is rejected automatically:

1. You choose STYLE only. Every board dimension is measured from the PCB file
   and injected by the emitter; never invent or repeat a board measurement.
2. A cutout's "ref" must name a part listed in the board facts below, and its
   opening is sized from that part's real courtyard -- you only pick the part,
   the face, and the margin.
3. Only put a side cutout on a face the part actually sits near (the board
   facts name each part's nearby faces); a part far from a wall gets a tunnel
   to nowhere and is rejected.
4. Walls below 1.2 mm do not print. Clearance below 0.5 mm binds.
5. Omit "cutouts" entries you are not sure about -- a solid case that fits is
   better than an opening onto the wrong part.
"""


def _facts_block(envelope: BoardEnvelope) -> str:
    """The measured board, rendered for the prompt. Deterministic text.

    Everything here is derived from the envelope so two runs over the same
    board produce byte-identical prompts. Faces use the emitter's frame map:
    ``front`` is the board edge at maximum KiCad Y, ``back`` at minimum.
    """
    size_x = to_mm(envelope.x_max_nm - envelope.x_min_nm)
    size_y = to_mm(envelope.y_max_nm - envelope.y_min_nm)
    lines = [
        f"Board outline: {size_x:.2f} x {size_y:.2f} mm, "
        f"substrate {to_mm(envelope.thickness_nm):.2f} mm thick.",
        f"Tallest part: {to_mm(envelope.max_height_nm):.2f} mm above the board.",
        "Parts (sizes measured from the board file; you never restate them):",
    ]
    for part in envelope.parts:
        width = to_mm(part.x_max_nm - part.x_min_nm)
        depth = to_mm(part.y_max_nm - part.y_min_nm)
        faces = _near_faces(part, envelope)
        near = f"; near faces: {', '.join(faces)}" if faces else ""
        lines.append(
            f"  {part.ref}: {width:.2f} x {depth:.2f} mm footprint, "
            f"{to_mm(part.height_nm):.2f} mm tall{near}"
        )
    return "\n".join(lines)


def _near_faces(part: PartExtent, envelope: BoardEnvelope) -> list[str]:
    """Which enclosure faces this part sits close enough to for a cutout."""
    faces: list[str] = []
    if part.x_min_nm - envelope.x_min_nm <= _EDGE_NEAR_NM:
        faces.append("left")
    if envelope.x_max_nm - part.x_max_nm <= _EDGE_NEAR_NM:
        faces.append("right")
    # front = the KiCad max-Y edge; back = min-Y (the emit.py frame map).
    if envelope.y_max_nm - part.y_max_nm <= _EDGE_NEAR_NM:
        faces.append("front")
    if part.y_min_nm - envelope.y_min_nm <= _EDGE_NEAR_NM:
        faces.append("back")
    return faces


def _degraded_report(
    spec: EnclosureSpec, envelope: BoardEnvelope, exc: EnclosureError
) -> FitReport:
    """The honest receipt for a fast-mode spec whose fit check failed.

    Fast mode ships the artifact anyway (speed over rigor, by default), so the
    failure must ride the report rather than vanish: the first warning names
    it. A :class:`CavityFitError` carries its signed margins and they are kept;
    other failures have none to report. ``params_mm`` mirrors what the emitter
    will write, best-effort -- a spec broken enough that even the parameter
    dump raises still gets its warning.
    """
    from ..enclosure.emit import scad_params

    margins = dict(getattr(exc, "margins_nm", None) or {})
    try:
        params = scad_params(spec, envelope)
    except EnclosureError:
        params = {}
    return FitReport(
        margins_nm=margins,
        warnings=(f"fit verification failed: {exc}",),
        params_mm=params,
    )


def propose_enclosure(
    model: Model,
    envelope: BoardEnvelope,
    *,
    style_hint: str = "",
    rigorous: bool = False,
    max_repairs: int | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[EnclosureSpec, FitReport, int]:
    """Ask for an enclosure spec and repair it until it validates.

    Returns ``(spec, fit, repair_rounds)``. ``fit`` is the
    :class:`~silkscreen.enclosure.verify.FitReport` the accepting round
    produced -- returned rather than discarded so callers never re-verify
    what the loop already verified. ``repair_rounds`` -- how many corrections
    the model needed -- is a genuinely useful quality signal, mirrored into
    the ``stage.done`` event.

    ``rigorous`` selects how much the loop pushes back; **fast is the
    default** because a demo wants its ``.scad`` now:

    * ``rigorous=False`` (fast): only :func:`parse_enclosure_spec` failures
      feed the repair loop, with a budget of one repair round unless
      ``max_repairs`` says otherwise. ``verify_fit(strict=False)`` still runs
      -- the receipt is never skipped -- but it can neither block nor trigger
      a repair: a hard fit failure (:class:`CavityFitError` included) is
      downgraded to a ``"fit verification failed: ..."`` warning on the
      returned report, and the spec is accepted so the artifact ships. The
      receipt stays honest -- it says the fit failed -- but the demo gets its
      case.
    * ``rigorous=True``: the strict loop. Each round runs
      :func:`verify_fit(strict=True)
      <silkscreen.enclosure.verify.verify_fit>`, so fit failures and
      spec-fixable fit *warnings* feed the repair loop rather than shipping
      silently, over three repair rounds unless ``max_repairs`` says
      otherwise. Warnings the model cannot fix -- a defaulted part height is
      measured from the board, not chosen by the spec -- stay on the returned
      report's ``warnings`` instead of burning repair rounds.

    ``on_event`` receives one ``enclosure.round`` event per rejected round.

    Raises:
        EnclosureProposalError: the model answered, but never with a spec that
            validated (and, in rigorous mode, fit) within the budget. Carries
            ``attempts``.
        ModelError: the model could not be reached at all. Deliberately not
            wrapped -- an upstream outage is a different condition from a bad
            proposal, and callers route them differently (the
            :func:`~silkscreen.agents.propose.propose_circuit` convention).
    """
    if max_repairs is None:
        max_repairs = _RIGOROUS_MAX_REPAIRS if rigorous else _FAST_MAX_REPAIRS
    facts = _facts_block(envelope)
    hint = f"\nStyle the user asked for:\n{style_hint}\n" if style_hint else ""
    prompt = f"{ENCLOSURE_PROMPT}\n{hint}\nBoard facts:\n{facts}\n"

    last_errors: list[str] = []
    for round_no in range(max_repairs + 1):
        # A transport failure is NOT wrapped: ModelError propagates so a
        # FallbackModel's failover -- and the service's 502 -- stay intact.
        raw = model.generate(prompt, temperature=0.0, max_output_tokens=4096)

        errors: list[str] = []
        try:
            spec = parse_enclosure_spec(raw)
        except EnclosureValidationError as exc:
            errors = list(exc.errors)
        else:
            if not rigorous:
                # Fast mode: the receipt is produced but never enforced. A
                # hard fit failure is downgraded to a warning on the report,
                # and the spec is accepted as-is -- the artifact ships either
                # way.
                try:
                    fit = verify_fit(spec, envelope, strict=False)
                except (CavityFitError, CutoutError, WallError) as exc:
                    fit = _degraded_report(spec, envelope, exc)
                return spec, fit, round_no
            try:
                fit = verify_fit(spec, envelope, strict=True)
            except EnclosureValidationError as exc:
                errors = list(exc.errors)
            except (CavityFitError, CutoutError, WallError) as exc:
                errors = [str(exc)]
            else:
                return spec, fit, round_no

        last_errors = errors
        if on_event is not None:
            # Engine-generated messages, not model text: safe on the wire,
            # truncated all the same.
            on_event(
                {
                    "event": "enclosure.round",
                    "round": round_no + 1,
                    "errors": len(errors),
                    "first_error": str(errors[0])[:160] if errors else "",
                }
            )
        if round_no == max_repairs:
            break
        # Feed every problem back at once so one round can fix all of them.
        problems = "\n".join(f"  - {e}" for e in errors)
        prompt = (
            f"{ENCLOSURE_PROMPT}\n{hint}\nBoard facts:\n{facts}\n\n"
            f"Your previous proposal was rejected. Fix ALL of these and "
            f"return the corrected JSON object:\n{problems}\n\n"
            f"Your previous proposal was:\n{raw}\n"
        )

    detail = "\n".join(f"  - {e}" for e in last_errors)
    raise EnclosureProposalError(
        f"No valid enclosure after {max_repairs + 1} attempts. "
        f"Final errors:\n{detail}",
        attempts=max_repairs + 1,
    )
