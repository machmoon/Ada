"""Drive the pipeline stages as an ADK workflow.

The workflow graph in :mod:`.workflow` carries one string -- a run token -- and
nothing else. Everything a stage actually needs (the wrapped model, the intent,
the emit and enter closures, and the slots its results land in) lives in a
:class:`_RunContext` held in this module's registry, which the nodes look up by
token. ADK state is session state: it is serialised, persisted and echoed into
ADK's own event stream, so a model object or a solved board must never enter it.

ADK's events are not silkscreen's. They are drained and dropped here; the
pipeline's stream is the one the stage bodies push through ``emit``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import secrets
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from google.adk import Runner
from google.adk.sessions import InMemorySessionService

from ...board import BoardResult
from ...netlist import CircuitSpec
from ...routing import RouteResult
from ..datasheet import PartFacts
from ..model import Model
from ..pipeline import PipelineResult, _finish, _wire_events
from ..propose import ProposalAttempt
from ..review import Finding
from ..stages import NO_ARTIFACTS, SchematicArtifacts

__all__ = ["generate_pcb_adk"]

_APP_NAME = "silkscreen"
_USER_ID = "silkscreen"

# ADK logs a full traceback at ERROR for every failed node and every failed
# run -- three dumps and ~5 kB of stderr for a routine client disconnect that
# the service reports in one line. The exception itself still propagates to
# the caller, so nothing is lost; it is just not duplicated onto stderr per
# request. Scoped to ADK's own namespace, set when this driver is imported.
logging.getLogger("google_adk").setLevel(logging.CRITICAL)


@dataclass
class _RunContext:
    """One run's inputs, outputs and event plumbing, keyed out of ADK state."""

    agent_model: Model
    intent: str
    datasheets: dict[str, str] | None
    preloaded_facts: list[PartFacts] | None
    max_repairs: int
    time_limit_s: float | None
    review: bool
    route: bool
    output: str | Path | None
    emit_stages: bool
    emit: Callable[[dict[str, Any]], None]
    enter: Callable[[str], None]
    propose_on_event: Callable[[dict[str, Any]], None] | None
    facts: list[PartFacts] = field(default_factory=list)
    spec: CircuitSpec | None = None
    attempts: list[ProposalAttempt] = field(default_factory=list)
    board: BoardResult | None = None
    artifacts: SchematicArtifacts = NO_ARTIFACTS
    route_result: RouteResult | None = None
    findings: list[Finding] = field(default_factory=list)
    error: BaseException | None = None
    # The chain as it stood at the raise site. ADK re-raises the original
    # exception object, but from inside its own except block, which rewrites
    # __context__ -- and an implicitly-chained ModelError that vanishes turns
    # the service's 502 into a 500. Snapshot on the way out, restore on return.
    error_context: BaseException | None = None
    error_cause: BaseException | None = None
    error_suppress: bool = False


_RUNS: dict[str, _RunContext] = {}


def run_context(token: str) -> _RunContext:
    """The context a node is running for. Raises rather than inventing one."""
    try:
        return _RUNS[token]
    except KeyError:
        raise RuntimeError(f"no ADK run registered for token {token!r}") from None


@contextmanager
def recording(run: _RunContext) -> Iterator[None]:
    """Keep a stage's exception, and its chain, where the driver can find them.

    Two jobs. First, insurance against a runner that reports a failure as an
    error event instead of raising: a swallowed ``ProposalError`` would
    otherwise surface as a board that was never built. Second, the snapshot of
    ``__context__``/``__cause__`` taken here, at the raise site, is what lets
    the driver undo ADK's re-raise rewriting the chain (see ``_restore_chain``).
    ``BaseException`` on purpose: a ``KeyboardInterrupt`` must not slip past
    the record either.
    """
    try:
        yield
    except BaseException as exc:
        run.error = exc
        run.error_context = exc.__context__
        run.error_cause = exc.__cause__
        run.error_suppress = exc.__suppress_context__
        raise


def _restore_chain(run: _RunContext) -> None:
    """Put a recorded exception's chain back the way the stage raised it.

    ADK 2.8's ``raise e.error`` happens inside its own ``except`` block, so
    Python rewrites ``__context__`` to ADK's wrapper on the way out. The walk
    in ``service.app.caused_by_model_failure`` follows ``__cause__`` and
    ``__context__``, so a ``ModelError`` reachable only implicitly would
    disappear -- a Gemini outage answered as a 500 instead of a 502.
    """
    exc = run.error
    if exc is None:
        return
    exc.__context__ = run.error_context
    exc.__cause__ = run.error_cause  # the setter flips __suppress_context__ on
    exc.__suppress_context__ = run.error_suppress


async def _run_workflow(token: str) -> None:
    # Imported here, not at module scope: workflow.py imports the registry from
    # this module, so the graph is built on demand rather than in a cycle.
    from .workflow import build_workflow

    session_service = InMemorySessionService()
    runner = Runner(
        node=build_workflow(),
        session_service=session_service,
        app_name=_APP_NAME,
    )
    # auto_create_session is off by default, so the session exists before the run.
    created = session_service.create_session(
        app_name=_APP_NAME,
        user_id=_USER_ID,
        session_id=token,
        state={"token": token},
    )
    if inspect.isawaitable(created):
        await created
    async for _event in runner.run_async(
        user_id=_USER_ID,
        session_id=token,
        new_message=None,
        state_delta={"token": token},
    ):
        pass


def _loop_is_running() -> bool:
    """Whether this thread already has an event loop.

    The lookup fails by raising, and it is caught here rather than around the
    run below so that a stage's own exception does not come out chained to an
    irrelevant "no running event loop" as its context.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _drive(token: str) -> None:
    """Run the workflow to completion from synchronous code."""
    if not _loop_is_running():
        asyncio.run(_run_workflow(token))
        return

    # Called from inside someone else's event loop: asyncio.run would refuse and
    # blocking it would deadlock, so the run gets a thread and a loop of its own.
    raised: list[BaseException] = []

    def target() -> None:
        try:
            asyncio.run(_run_workflow(token))
        except BaseException as exc:  # noqa: BLE001 -- re-raised by the caller below
            raised.append(exc)

    thread = threading.Thread(target=target, name=f"silkscreen-adk-{token}")
    thread.start()
    thread.join()
    if raised:
        raise raised[0]


def generate_pcb_adk(
    model: Model,
    intent: str,
    *,
    datasheets: dict[str, str] | None = None,
    preloaded_facts: list[PartFacts] | None = None,
    output: str | Path | None = None,
    max_repairs: int = 3,
    time_limit_s: float | None = 20.0,
    review: bool = True,
    route: bool = True,
    emit_stages: bool = True,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    include_responses: bool = False,
) -> PipelineResult:
    """Run the stages as an ADK workflow. See :func:`silkscreen.agents.generate_pcb`.

    Behaviour matches the straight-line driver exactly, exceptions included: a
    ``ModelError``, a ``ProposalError`` or an event callback that hangs up all
    leave here as themselves, with their cause chain intact.
    """
    emit, agent_model, enter = _wire_events(model, on_event, include_responses)
    run = _RunContext(
        agent_model=agent_model,
        intent=intent,
        datasheets=datasheets,
        preloaded_facts=preloaded_facts,
        max_repairs=max_repairs,
        time_limit_s=time_limit_s,
        review=review,
        route=route,
        output=output,
        emit_stages=emit_stages,
        emit=emit,
        enter=enter,
        propose_on_event=emit if on_event is not None else None,
    )
    token = secrets.token_hex(8)
    _RUNS[token] = run
    try:
        try:
            _drive(token)
        except BaseException as exc:
            if exc is run.error:
                _restore_chain(run)
            raise
        if run.error is not None:
            # The runner reported the failure as an event instead of raising.
            # No exception is in flight here, so the restored chain survives.
            _restore_chain(run)
            raise run.error
    finally:
        _RUNS.pop(token, None)

    if run.spec is None or run.board is None:
        raise RuntimeError("the ADK workflow finished without producing a board")

    return _finish(
        intent=intent,
        spec=run.spec,
        board=run.board,
        facts=run.facts,
        findings=run.findings,
        attempts=run.attempts,
        output=output,
        route=run.route_result,
        artifacts=run.artifacts,
    )
