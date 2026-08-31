"""Datasheet constraints: PDF prose in, checkable rules out.

The band between geometry (DRC solves it) and judgment (an engineer owns it)
is full of questions with exactly one right answer that happens to be locked
in a PDF: required decoupling, strap-pin states, maximum ratings, sequencing.
This package converts that band into a versioned, provenance-carrying schema
(:mod:`.schema`), extracts into it with a model that is then mechanically
distrusted (:mod:`.extract`, :mod:`.pipeline`), and enforces what survives
against a real board (:mod:`.check`) in the audit package's Finding shape.

    from silkscreen.constraints import extract_constraints, check_board
    cset = extract_constraints(model, "STM32F030F4", pdf_bytes=data)
    result = check_board(load_audit_board("board.kicad_pcb"), cset, ref="U1")

The load-bearing property end to end: nothing is presented as more certain
than it is. Every constraint carries page + section + verbatim quote, quotes
are verified against the PDF text, unverified or low-confidence constraints
demand human review, and findings from unconfirmed constraints stay
``SUGGESTED``.
"""

from .check import CheckResult, check_board, parse_farads
from .extract import gate, quote_on_page, verify_provenance
from .pipeline import extract_constraints
from .schema import (
    SCHEMA_VERSION,
    ConstraintSet,
    Decoupling,
    DocumentInfo,
    Limit,
    PowerSequencing,
    Provenance,
    Rating,
    RatingKind,
    StrapPin,
)

__all__ = [
    "SCHEMA_VERSION",
    "ConstraintSet",
    "DocumentInfo",
    "Provenance",
    "Limit",
    "Rating",
    "RatingKind",
    "Decoupling",
    "PowerSequencing",
    "StrapPin",
    "extract_constraints",
    "verify_provenance",
    "quote_on_page",
    "gate",
    "check_board",
    "CheckResult",
    "parse_farads",
]
