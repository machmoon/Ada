"""The pipeline as an ADK dynamic workflow.

One orchestrator node runs read, propose, place and review in order, each as its
own child node. The nodes hold no logic of their own: every one of them calls
the matching body in :mod:`silkscreen.agents.stages`, which is what keeps the
two drivers emitting the same events. ``review`` is always run -- its stage body
owns the decision to do nothing when review is off, so the graph never has to.

Each node is handed the run token as ``node_input`` and returns it, so the token
is the only value ADK ever sees; the rest is looked up from the registry in
:mod:`.runner`.
"""

from __future__ import annotations

from google.adk import Context, Workflow
from google.adk.workflow import node

from ..stages import place_stage, propose_stage, read_stage, review_stage
from .runner import recording, run_context

__all__ = ["build_workflow"]


@node(name="read")
def read(node_input: str) -> str:
    run = run_context(node_input)
    with recording(run):
        run.facts = read_stage(
            run.agent_model,
            sheets=run.datasheets,
            preloaded_facts=run.preloaded_facts,
            emit=run.emit,
            enter=run.enter,
        )
    return node_input


@node(name="propose")
def propose(node_input: str) -> str:
    run = run_context(node_input)
    with recording(run):
        run.spec, run.attempts = propose_stage(
            run.agent_model,
            intent=run.intent,
            facts=run.facts,
            max_repairs=run.max_repairs,
            emit=run.emit,
            enter=run.enter,
            propose_on_event=run.propose_on_event,
        )
    return node_input


@node(name="place")
def place(node_input: str) -> str:
    run = run_context(node_input)
    with recording(run):
        run.board = place_stage(
            run.spec,
            time_limit_s=run.time_limit_s,
            emit=run.emit,
            enter=run.enter,
        )
    return node_input


@node(name="review")
def review(node_input: str) -> str:
    run = run_context(node_input)
    with recording(run):
        run.findings = review_stage(
            run.agent_model,
            run.spec,
            facts=run.facts,
            review=run.review,
            emit=run.emit,
            enter=run.enter,
        )
    return node_input


# rerun_on_resume is ADK's condition for scheduling child nodes dynamically:
# an interrupted child wakes its parent up again to collect the result.
@node(name="silkscreen", rerun_on_resume=True)
async def silkscreen(ctx: Context, token: str) -> str:
    """The whole pipeline, in order. ``token`` binds from session state."""
    for stage in (read, propose, place, review):
        await ctx.run_node(stage, node_input=token)
    return token


def build_workflow() -> Workflow:
    """The workflow object, built fresh per run so no state is shared."""
    return Workflow(name="silkscreen", edges=[("START", silkscreen)])
