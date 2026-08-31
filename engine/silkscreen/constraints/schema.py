"""The constraint schema. This file is the artifact; everything else serves it.

A datasheet locks its requirements in prose and tables. This schema is the
machine-readable form those requirements convert into: absolute maximum
ratings, operating conditions, decoupling, power sequencing, strap pins,
thermal limits, pin-level electrical characteristics. Once a requirement is
here, a deterministic checker can enforce it; while it stays in the PDF, only
an engineer re-reading page 47 can.

Three rules shape every type in this module:

* **Provenance is mandatory.** Every constraint carries the page, section and
  verbatim quote it came from. A constraint that cannot be traced back is
  worse than none, because it will be trusted -- so a missing or unverified
  quote forces ``needs_review`` on, and nothing here lets it be forced off
  while the reason stands.
* **Extraction is fallible and says so.** Constraints carry the extractor's
  confidence and an explicit ``needs_review`` flag with a reason. ``confirmed``
  means a human checked the constraint against the PDF; it is the only field
  that upgrades a constraint to fully trusted, and only a human sets it.
* **The schema is versioned.** ``schema_version`` is written into every file
  and checked on load: same major version reads (unknown keys dropped, the
  compatibility rule :class:`~silkscreen.agents.datasheet.PartFacts` set),
  a different major version refuses rather than misreading.

Values keep the datasheet's own units (``"V"``, ``"mA"``, ``"nF"``, ``"mm"``)
as strings next to floats. The engine's integer-nanometre convention applies
to board geometry; a constraint file is a transcription of a document, and
transcriptions do not convert. Conversion happens once, in the checker.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, fields
from enum import StrEnum
from typing import Any

__all__ = [
    "SCHEMA_VERSION",
    "Provenance",
    "Limit",
    "RatingKind",
    "Rating",
    "Decoupling",
    "PowerSequencing",
    "StrapPin",
    "DocumentInfo",
    "ConstraintSet",
]

#: major.minor. Minor bumps add fields (old readers drop them); major bumps
#: change meaning and refuse to cross.
SCHEMA_VERSION = "1.0"


def _major(version: str) -> str:
    return str(version).split(".", 1)[0]


@dataclass(frozen=True)
class Provenance:
    """Where a constraint came from, precisely enough to check it by hand.

    ``verified`` is set mechanically by :mod:`.extract`: the quote was found
    in the extracted text of the claimed PDF page. It is evidence the model
    did not invent the citation, not evidence the constraint is *right* --
    that is what ``Constraint.confirmed`` is for.
    """

    #: 1-based PDF page index (not the printed page number).
    page: int
    #: The heading or table the text sits under, as the datasheet names it.
    section: str = ""
    #: Verbatim source text. The thing a human checks against the PDF.
    quote: str = ""
    #: The quote was mechanically found on the claimed page.
    verified: bool = False


@dataclass(frozen=True)
class Limit:
    """A min/typ/max triple in the datasheet's own unit.

    Any of the three may be absent -- an absolute-maximum row usually has only
    a max, a typical-only row only a typ. ``conditions`` is the test-condition
    column verbatim ("VDD = 3.3 V, TA = 25 °C"), because a limit quoted
    without its conditions is a different, wrong claim.
    """

    unit: str
    min: float | None = None
    typ: float | None = None
    max: float | None = None
    conditions: str = ""


class RatingKind(StrEnum):
    #: Stress beyond this damages the part. Table "Absolute maximum ratings".
    ABSOLUTE_MAXIMUM = "absolute_maximum"
    #: The envelope the part is specified to work inside.
    OPERATING_CONDITION = "operating_condition"
    #: Junction temperature, thermal resistance, power dissipation.
    THERMAL = "thermal"
    #: Per-pin electricals: VIH/VIL/VOH/VOL, leakage, drive strength.
    PIN_ELECTRICAL = "pin_electrical"


@dataclass(frozen=True)
class _Constraint:
    """Fields every constraint kind shares. Not serialized on its own."""

    #: Stable within a set, e.g. ``abs-max.vdd`` -- how a checker, a diff or
    #: a human review refers to one constraint. Duplicate subjects get a
    #: positional ``-2``/``-3`` suffix, so those ids are only as stable as
    #: the extraction order that produced them; diff by subject, not suffix.
    id: str
    provenance: Provenance
    #: The extractor's own 0..1 estimate. Advisory; ``needs_review`` governs.
    confidence: float = 0.0
    #: Must a human look before a checker trusts this? Never silently False:
    #: :func:`.extract.gate` derives it from confidence and verification.
    needs_review: bool = True
    #: Why it needs review, when it does ("quote not found on page 12").
    review_reason: str = ""
    #: A human checked this constraint against the PDF. Only ever set by a
    #: person (or a tool acting on a person's explicit say-so).
    confirmed: bool = False
    notes: str = ""


@dataclass(frozen=True)
class Rating(_Constraint):
    """One row of a ratings/characteristics table."""

    kind: RatingKind = RatingKind.OPERATING_CONDITION
    #: The parameter as the datasheet names it ("Supply voltage").
    parameter: str = ""
    #: The symbol column ("VDD", "Tj"), when the table has one.
    symbol: str = ""
    limit: Limit = field(default_factory=lambda: Limit(unit=""))
    #: Datasheet pin names this applies to, empty when it is part-wide.
    pins: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decoupling(_Constraint):
    """A required decoupling/bypass capacitor."""

    #: The rail or pin it decouples, as the datasheet names it ("VDD", "VDDA").
    rail: str = ""
    #: e.g. 100.0 with unit "nF". None when the datasheet names no value.
    value: float | None = None
    unit: str = ""
    #: How many, when stated ("one 100 nF per VDD pin" -> per_pin=True).
    count: int | None = None
    per_pin: bool = False
    #: A hard placement distance, when the datasheet gives one.
    max_distance_mm: float | None = None
    #: The qualitative demand when it does not ("as close as possible").
    placement: str = ""
    #: Dielectric/type demands ("X7R ceramic", "low ESR tantalum").
    cap_type: str = ""


@dataclass(frozen=True)
class PowerSequencing(_Constraint):
    """An ordering or timing requirement between supply rails."""

    #: Rails in required order, first up first. Empty when the requirement is
    #: "no sequencing required" -- which is itself worth recording.
    rails: tuple[str, ...] = ()
    #: The requirement in the datasheet's words; timing lives here too.
    requirement: str = ""


@dataclass(frozen=True)
class StrapPin(_Constraint):
    """A configuration/boot/mode pin that must be tied to a defined level."""

    pin: str = ""
    #: high | low | pull-up | pull-down | no-float | external-resistor
    required_state: str = ""
    #: Pull resistor value when the datasheet names one.
    resistor_value: float | None = None
    resistor_unit: str = ""
    #: When the requirement applies ("to boot from flash", "always").
    condition: str = ""


@dataclass(frozen=True)
class DocumentInfo:
    """The document a set was extracted from, pinned hard enough to diff."""

    title: str = ""
    revision: str = ""
    url: str = ""
    sha256: str = ""
    page_count: int = 0


@dataclass
class ConstraintSet:
    """Everything extracted from one datasheet for one part."""

    part_number: str
    schema_version: str = SCHEMA_VERSION
    manufacturer: str = ""
    document: DocumentInfo = field(default_factory=DocumentInfo)
    ratings: list[Rating] = field(default_factory=list)
    decoupling: list[Decoupling] = field(default_factory=list)
    power_sequencing: list[PowerSequencing] = field(default_factory=list)
    strap_pins: list[StrapPin] = field(default_factory=list)
    #: ISO date of extraction and the model that did it.
    extracted_at: str = ""
    extractor: str = ""

    def all_constraints(self) -> list[_Constraint]:
        return [
            *self.ratings,
            *self.decoupling,
            *self.power_sequencing,
            *self.strap_pins,
        ]

    def trusted(self) -> list[_Constraint]:
        """Constraints a checker may enforce without a human in the loop."""
        return [c for c in self.all_constraints() if not c.needs_review]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ConstraintSet:
        """Rebuild from :meth:`to_dict` output.

        Unknown keys are dropped (a file written by a newer minor version must
        load), a different *major* version raises, and malformed input raises
        :class:`ValueError` -- one type, so a caller can treat a corrupt file
        as absent without enumerating every way a dict can be wrong.
        """
        if not isinstance(data, dict):
            raise ValueError(
                f"expected a constraint-set dict, got {type(data).__name__}"
            )
        version = str(data.get("schema_version", SCHEMA_VERSION))
        if _major(version) != _major(SCHEMA_VERSION):
            raise ValueError(
                f"constraint schema {version} is a different major version "
                f"than this reader ({SCHEMA_VERSION}); refusing to misread it"
            )
        if not data.get("part_number"):
            raise ValueError("a constraint set needs a part_number")

        try:
            return cls(
                part_number=str(data["part_number"]),
                schema_version=version,
                manufacturer=str(data.get("manufacturer", "")),
                document=_load(DocumentInfo, data.get("document") or {}),
                ratings=[_load_rating(r) for r in _list(data, "ratings")],
                decoupling=[
                    _load_constraint(Decoupling, d)
                    for d in _list(data, "decoupling")
                ],
                power_sequencing=[
                    _load_constraint(PowerSequencing, p)
                    for p in _list(data, "power_sequencing")
                ],
                strap_pins=[
                    _load_constraint(StrapPin, s)
                    for s in _list(data, "strap_pins")
                ],
                extracted_at=str(data.get("extracted_at", "")),
                extractor=str(data.get("extractor", "")),
            )
        except (TypeError, KeyError) as exc:
            raise ValueError(f"malformed constraint set: {exc}") from exc


def _list(data: dict, key: str) -> list:
    value = data.get(key) or []
    if not isinstance(value, list):
        raise ValueError(f"'{key}' must be a list, got {type(value).__name__}")
    return value


def _load(cls: type, data: Any) -> Any:
    """Build a plain frozen dataclass from a dict, dropping unknown keys."""
    if not isinstance(data, dict):
        raise ValueError(f"expected an object for {cls.__name__}, got "
                         f"{type(data).__name__}")
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


def _check(cond: bool, what: str) -> None:
    if not cond:
        raise ValueError(f"malformed constraint set: {what}")


def _valid_real(value: Any, what: str) -> None:
    """None or a finite non-bool number. NaN is rejected explicitly because
    ``json`` emits and accepts it, and every comparison against NaN is False
    -- including the gate's confidence floor."""
    if value is None:
        return
    _check(
        not isinstance(value, bool) and isinstance(value, (int, float))
        and math.isfinite(value),
        f"{what} must be a finite number or null, got {value!r}",
    )


def _valid_bool(value: Any, what: str) -> None:
    _check(isinstance(value, bool), f"{what} must be a boolean, got {value!r}")


def _load_constraint(cls: type, data: Any) -> Any:
    """Build a constraint, rebuilding nested objects and validating scalars.

    The contract is "malformed input raises ValueError -- one type"; without
    these checks a corrupt file loads silently and detonates later as a
    TypeError inside the gate or the checker, far from the file that caused
    it. Worse, several corrupt shapes would load into *more* trust than the
    pipeline granted (``"needs_review": 0`` in ``trusted()``, NaN confidence
    sailing past the floor), so validation here is part of the trust ladder,
    not politeness.
    """
    if not isinstance(data, dict):
        raise ValueError(f"expected an object for {cls.__name__}, got "
                         f"{type(data).__name__}")
    known = {f.name for f in fields(cls)}
    kwargs = {k: v for k, v in data.items() if k in known}
    kwargs["provenance"] = _load(Provenance, data.get("provenance") or {"page": 0})
    prov = kwargs["provenance"]
    _check(isinstance(prov.page, int) and not isinstance(prov.page, bool),
           f"provenance.page must be an integer, got {prov.page!r}")
    _valid_bool(prov.verified, "provenance.verified")
    _check(isinstance(prov.quote, str) and isinstance(prov.section, str),
           "provenance quote and section must be strings")

    _check(isinstance(kwargs.get("id"), str) and kwargs["id"] != "",
           f"a {cls.__name__} constraint needs a non-empty string id")
    for name in ("confidence",):
        if name in kwargs:
            _valid_real(kwargs[name], name)
            _check(0.0 <= kwargs[name] <= 1.0,
                   f"confidence must be within 0..1, got {kwargs[name]!r}")
    for name in ("needs_review", "confirmed", "per_pin"):
        if name in kwargs:
            _valid_bool(kwargs[name], name)
    for name in ("pins", "rails"):
        if name in kwargs:
            _check(isinstance(kwargs[name], (list, tuple))
                   and all(isinstance(v, str) for v in kwargs[name]),
                   f"{name} must be a list of strings, got {kwargs[name]!r}")
            kwargs[name] = tuple(kwargs[name])
    # These four are *physical magnitudes*, and the checker divides, compares
    # and counts with them. A non-positive one does not merely look odd: it
    # silently changes what the checker decides. ``count: 0`` makes the
    # decoupling count unfalsifiable (``len(caps) < 0`` is never true), and
    # ``max_distance_mm: 0`` fabricates a distance finding on every board
    # ever checked, because no capacitor is within 0 mm of a pad. Both read
    # afterwards as a confident verdict. Rejecting them here is the only
    # place that can tell the difference between "the datasheet says none"
    # (null, which stays legal) and "something produced a zero".
    for name in ("value", "max_distance_mm", "resistor_value"):
        if name in kwargs:
            _valid_real(kwargs[name], name)
            _check(kwargs[name] is None or kwargs[name] > 0,
                   f"{name} must be positive or null, got {kwargs[name]!r}")
    if kwargs.get("count") is not None and "count" in kwargs:
        _check(isinstance(kwargs["count"], int)
               and not isinstance(kwargs["count"], bool),
               f"count must be an integer or null, got {kwargs['count']!r}")
        _check(kwargs["count"] > 0,
               f"count must be positive or null, got {kwargs['count']!r}")
    if "limit" in kwargs:
        kwargs["limit"] = _load(Limit, kwargs["limit"])
        for name in ("min", "typ", "max"):
            _valid_real(getattr(kwargs["limit"], name), f"limit.{name}")
        _check(isinstance(kwargs["limit"].unit, str), "limit.unit must be a string")
    if "kind" in kwargs:
        # StrEnum serializes as its value; rebuild the enum member. An unknown
        # kind is malformed data, and ValueError is already our contract.
        kwargs["kind"] = RatingKind(str(kwargs["kind"]))
    return cls(**kwargs)


def _load_rating(data: Any) -> Rating:
    return _load_constraint(Rating, data)
