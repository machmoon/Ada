"""One call: PDF in, gated :class:`~.schema.ConstraintSet` out.

The sequence is fixed and each step distrusts the last: the model transcribes
(two focused calls), pypdf extracts the page text, every quote is searched for
on its claimed page, and the gate decides -- per constraint -- whether a
checker may trust it or a human must look first. The set that comes out is
honest by construction: nothing in it claims more certainty than the pipeline
established.

Page-text extraction failing (a scanned datasheet, a broken PDF) does not
abort the run; it means no quote can verify, so every constraint gates to
``needs_review``. Degraded and saying so beats refusing to extract at all.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import date
from typing import Any

from ..agents.grounding import GroundingError, extract_pages, fetch_pdf
from ..agents.model import Document, Model
from .extract import (
    extract_design_requirements,
    extract_ratings,
    gate,
    verify_provenance,
)
from .schema import SCHEMA_VERSION, ConstraintSet, DocumentInfo

__all__ = ["extract_constraints"]


def extract_constraints(
    model: Model,
    part_number: str,
    *,
    pdf_bytes: bytes | None = None,
    pdf_url: str | None = None,
    manufacturer: str = "",
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> ConstraintSet:
    """Extract, verify and gate every constraint from one datasheet.

    Give ``pdf_bytes`` when you have the file (preferred: the model and the
    provenance check then read the *same bytes*, and the hash pins them);
    ``pdf_url`` alone makes the pipeline fetch it first.
    """
    if pdf_bytes is None:
        if not pdf_url:
            raise ValueError("extract_constraints needs pdf_bytes or a pdf_url")
        pdf_bytes = fetch_pdf(pdf_url)

    def emit(**evt: Any) -> None:
        if on_event is not None:
            on_event(evt)

    # Page text for the provenance check. Failure degrades, loudly.
    pages: list[str] = []
    try:
        pages = extract_pages(pdf_bytes)
    except GroundingError as exc:
        emit(event="constraints.pages_failed", error=str(exc)[:160])

    doc = Document(data=pdf_bytes)

    emit(event="constraints.stage", stage="ratings")
    ratings = extract_ratings(model, doc, part_number)
    emit(event="constraints.stage", stage="design")
    decoupling, sequencing, straps = extract_design_requirements(
        model, doc, part_number
    )

    def settle(items: list) -> list:
        return [gate(verify_provenance(c, pages), pages) for c in items]

    cset = ConstraintSet(
        part_number=part_number,
        schema_version=SCHEMA_VERSION,
        manufacturer=manufacturer,
        document=DocumentInfo(
            url=pdf_url or "",
            sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            page_count=len(pages),
        ),
        ratings=settle(ratings),
        decoupling=settle(decoupling),
        power_sequencing=settle(sequencing),
        strap_pins=settle(straps),
        extracted_at=date.today().isoformat(),
        extractor=getattr(model, "model", type(model).__name__),
    )
    emit(
        event="constraints.done",
        total=len(cset.all_constraints()),
        trusted=len(cset.trusted()),
    )
    return cset
