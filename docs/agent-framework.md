# Agent framework

## What the requirement asks for

The hackathon requires that a project use at least one Google agent framework. Four are
named as acceptable: the Agent Development Kit (ADK), the Google Gen AI SDK, the
Antigravity SDK, and Genkit. A project satisfies the requirement by building its
model-facing layer on one of those four, rather than on a third-party abstraction or a
hand-rolled HTTP client against a model endpoint.

## What Silkscreen uses

Silkscreen uses the **Google Gen AI SDK**, the Python package published as `google-genai`.
It is declared in `pyproject.toml` under two optional dependency groups:

```toml
agents = ["google-genai>=1.0"]
cloud  = ["google-genai>=1.0", "google-cloud-firestore>=2.16"]
```

The base install deliberately does not include it. The engine that computes footprints,
packs parts, and writes KiCad files has no model dependency at all, and the split is stated
in `engine/silkscreen/agents/__init__.py`: "The engine below this package is deliberately
model-free so the parts that must be _correct_ can be tested without a network."

### Where the SDK is called

There are exactly two places in the repository that import the SDK, and both are inside
`engine/silkscreen/agents/`.

The first is `engine/silkscreen/agents/model.py`. The class `GeminiModel` imports
`from google import genai`, constructs a `genai.Client(api_key=key)` from `GOOGLE_API_KEY`,
and calls `self._client.models.generate_content(model=self.model, contents=parts,
config=config)`. The `contents` list is assembled from an ordered sequence of document
parts followed by the prompt text. A document with a URL becomes a `file_data` part
carrying a `file_uri` and MIME type; a document supplied as raw bytes becomes an
`inline_data` part. The `config` dictionary carries `temperature`, `max_output_tokens`,
an optional `system_instruction`, and `media_resolution` whenever documents are attached.
Any exception from the SDK is re-raised as the module's own `ModelError`, and an empty
response text is treated as a failure rather than an empty answer.

The second is `engine/silkscreen/agents/retrieval.py`. The class `GeminiEmbedder` builds
the same `genai.Client` and calls `self._client.models.embed_content(...)` against
`gemini-embedding-001`, passing a `task_type` of `RETRIEVAL_QUERY` for questions and
`RETRIEVAL_DOCUMENT` for passages, plus an `output_dimensionality`. The asymmetry is
deliberate and commented as such: using one task type for both sides measurably degrades
retrieval quality.

Two SDK capabilities are load-bearing for this project specifically. Native PDF vision is
what makes datasheet reading possible, because a pinout table and a package drawing are
pictures and text extraction discards exactly the information the pipeline needs; the
Gemini API documents support for PDFs up to 50 MB or 1000 pages at 258 tokens per page,
including interpretation of diagrams, charts, and tables
([ai.google.dev](https://ai.google.dev/gemini-api/docs/document-processing)). The
`media_resolution` parameter, documented on the same page as a Gemini 3 control accepting
low, medium, or high, is set to `high` by default in `GeminiModel.__init__` because
datasheet pin tables are set in small type.

### Where the agent structure lives

The SDK is the transport. The agent architecture sits above it, and it is worth describing
because it is the substance of the claim that this is an agentic project rather than a
project that happens to call a model.

`engine/silkscreen/agents/model.py` defines a `Model` protocol with a single `generate`
method. Every stage in the package talks to that protocol and never to the vendor SDK
directly. Three types satisfy it: `GeminiModel` for the live path, `ScriptedModel` for
tests, and `FallbackModel` in `engine/silkscreen/agents/resilience.py` for provider
failover. Because the seam is a protocol rather than a base class, a caller can substitute
any of the three without the calling code knowing which it has.

`engine/silkscreen/agents/pipeline.py` runs the multi-stage pipeline. `generate_pcb` reads
each supplied datasheet into structured facts, proposes a circuit from those facts, builds
and places a board with the constraint solver, runs an adversarial review pass, and
optionally writes the `.kicad_pcb` file. Each stage is a separate model call with its own
prompt and its own output contract, and the output of one stage is the typed input to the
next.

`engine/silkscreen/agents/datasheet.py` is the extraction stage. `read_datasheet` puts a
PDF in front of the model with a prompt that demands a page number on every requirement and
every recommended auxiliary component, and that instructs the model to omit anything the
document does not state. The result is parsed into `PartFacts`, and the function raises
rather than continuing if no pins were extracted. When the reported package pin count
disagrees with the number of pins actually extracted, a warning is appended to the notes,
because choosing a footprint from a wrong pin count produces a dead board.

`engine/silkscreen/agents/propose.py` is the propose-and-repair loop, and it is the part
that most clearly makes this an agent rather than a single completion. The model's proposal
never reaches the board builder directly. It goes through
`silkscreen.netlist.parse_circuit_spec`, which collects structural problems into a list and
then calls `CircuitSpec.validate()` to collect semantic ones. `ValidationError` carries the
entire list, one human-readable message per problem, and the loop feeds all of them back at
once:

```python
problems = "\n".join(f"  - {e}" for e in exc.errors)
prompt = (
    ...
    f"Your previous proposal was rejected. Fix ALL of these and "
    f"return the corrected JSON object:\n{problems}\n\n"
    f"Your previous proposal was:\n{raw}\n"
)
```

Batching every failure into one repair prompt is a design decision with a reason behind it.
Returning only the first error would make each round fix one problem and often introduce
another, so the loop would burn its budget without converging. The loop is bounded by
`max_repairs`, every round is informed by the previous one, and the full attempt history is
returned so a caller can report how many rounds it took. That count is exposed as
`PipelineResult.repair_rounds` and is a genuine quality signal about the proposal.

The loop also distinguishes two failure modes that a naive implementation would conflate.
A transport failure raises `ModelError` and is deliberately not wrapped in `ProposalError`,
because "the model was unreachable" and "the model could not produce a valid circuit" call
for different responses, and the Cloud Run service in `service/app.py` walks the exception
cause chain in `caused_by_model_failure` precisely so an upstream outage is not reported as
an internal error.

`engine/silkscreen/agents/review.py` is the critic. The reviewer is given the accepted
circuit and the datasheet facts and is prompted to refute the design rather than assess it,
on the stated reasoning that a model asked "is this correct?" says yes. Findings come back
as JSON with a severity of blocker, marginal, or note. The parser then filters the model's
own output against the circuit: any part reference in a finding that does not name a device
or passive actually present in the spec is dropped, so a finding never points at a component
that does not exist. Findings are sorted most severe first.

`engine/silkscreen/agents/resilience.py` handles failover. `FallbackModel` walks a list of
named `Provider` entries, retries each with exponential backoff capped at `max_backoff_s`,
and validates every response before accepting it — a provider that returns a non-string or
an empty string is treated as a failure and the chain moves on. `service/app.py` uses this
to put a cheaper model tier behind the primary one. Every attempt is logged, so a caller
can see which provider actually served a request.

### What keeps it testable

`ScriptedModel` in `model.py` is a deterministic stand-in that returns canned responses,
either in order or matched by a substring marker in the prompt, and records every call.
Because the whole package speaks to the `Model` protocol, `engine/tests/test_agents.py`
drives the entire prompt-to-PCB pipeline — including the repair loop and the failure paths
that only fire against a badly behaved model — with no network and no API key. The same
approach applies to retrieval, where `HashEmbedder` is a deterministic offline stand-in
that hashes token trigrams into a fixed-width bag of counts. Its docstring is explicit that
it is not a semantic model, and nothing in the codebase pretends otherwise. CI installs the
`agents` and `cloud` extras and runs `pytest` on Linux, macOS, and Windows, so the SDK is
present in CI while no test requires a live call.

### Tool exposure over MCP

`engine/silkscreen/mcp/server.py` implements a Model Context Protocol server as JSON-RPC 2.0
over stdin and stdout, speaking `initialize`, `ping`, `tools/list`, and `tools/call` at
protocol version `2024-11-05`. It exposes five engine operations as tools with JSON
schemas: `validate_circuit`, `build_board`, `emit_kicad_pcb`, `place_parts`, and
`generate_footprint`. The entry point is registered in `pyproject.toml` as
`silkscreen-mcp`. The transport is separated from the dispatch — `handle` maps one request
dictionary to one response dictionary and never touches a stream — which is what makes the
protocol testable without spawning a process.

This is not itself one of the four named frameworks, and it should not be presented as one.
It matters here because tool exposure is the interoperability surface those frameworks
consume. ADK lists MCP tools among its supported tool types
([adk.dev](https://adk.dev/)), and the Antigravity SDK documents integration with external
MCP servers ([antigravity.google](https://antigravity.google/docs/sdk/overview/)). Any of
those frameworks could call Silkscreen's engine today without a line of adapter code.

## Is the Gen AI SDK an "agent framework"?

This deserves a direct answer rather than a deflection, because a judge could reasonably
raise it.

On the narrow reading, the question does not arise: the requirement names the GenAI SDK as
one of four acceptable frameworks, and Silkscreen uses it as its only model interface. That
is the requirement met on its own terms.

On the broader reading — whether an SDK counts as an *agent* framework — the honest answer
is that the Gen AI SDK is a model-access SDK with agent-shaped primitives, not an
opinionated agent runtime. It provides a client, content generation, embeddings, file
handling, multi-turn chat, a live streaming API, and function calling, against either the
Gemini Developer API or Google Cloud
([github.com/googleapis/python-genai](https://github.com/googleapis/python-genai)). Its
function-calling support does include an autonomous loop: the Python SDK reference states
that "You can pass a Python function directly and it will be automatically called and
responded by default," with an automatic-function-calling config exposing a `disable` flag
and a `maximum_remote_calls` limit that defaults to 10
([googleapis.github.io/python-genai](https://googleapis.github.io/python-genai/)). What it
does not provide is the layer above that: agent hierarchies, session services, workflow
composition, and an evaluation harness. ADK provides those, which is why ADK exists as a
separate package that depends on the Gen AI SDK rather than replacing it.

The substantive claim is therefore not that the SDK supplies the agent architecture. It is
that Silkscreen builds the agent architecture on top of the SDK, in code that can be read
and checked. Concretely, that architecture consists of a multi-stage pipeline in
`pipeline.py` where each stage is a distinct model call with its own contract; a
propose-and-repair loop in `propose.py` where a deterministic validator rejects the model's
output and returns every error for a bounded, converging repair cycle; an adversarial review
stage in `review.py` where a second model call is prompted to refute the first one's work
and its findings are filtered against ground truth; a provider failover chain in
`resilience.py` with validated responses; a retrieval pipeline in `retrieval.py` with
page-level citations; and a tool surface in `mcp/server.py` that other agents can call.
Those are the things an agent framework would otherwise supply, written here against the
project's own domain constraints.

There is one thing worth being candid about. Because Silkscreen's engine is deterministic
and its correctness checks are exact, the model is never given a tool-calling loop over the
engine within a single generation. Validation happens in Python between calls rather than
through the SDK's automatic function calling. That is a deliberate choice — a solver result
should not depend on whether the model decided to invoke the solver — but it does mean the
project does not exercise the SDK's own agentic loop. The agent loop is the one in
`propose.py`.

## The four options, compared for this project

| Framework | Package | Languages | Status | What it gives you |
| --- | --- | --- | --- | --- |
| Gen AI SDK | `google-genai` | Python (and other language SDKs) | 2.20.0, released 25 Aug 2026, Python ≥3.10 | Model access, embeddings, files, chat, function calling |
| ADK | `google-adk` | Python, TypeScript, Go, Java, Kotlin | 2.8.0, released 26 Aug 2026, Python ≥3.10 | Agent hierarchies, sessions, workflows, evaluation, deployment |
| Genkit | `genkit` | TypeScript and Go stable; Python and Dart in preview | Python announced as Alpha in April 2025 | Flows, tools, plugins, browser dev UI |
| Antigravity SDK | `google-antigravity` | Python, with TypeScript and Go planned | Preview, announced 19 May 2026, docs show v0.1.15 | Stateful agent runtime, safety policies, subagent delegation |

**The Gen AI SDK** is what the project uses. It fits because the pipeline's requirements are
narrow and specific rather than broad. The project needs exactly three things from a model
layer: native PDF vision at controllable resolution, deterministic JSON output at
temperature zero, and asymmetric retrieval embeddings. All three are direct SDK calls. The
project does not need conversation state, because every stage is a single-shot call with a
typed input and a typed output. It does not need agent-to-agent delegation, because the
stages form a fixed sequence. Adopting a heavier framework would add a dependency without
removing any code, since the parts that would be replaced — the repair loop and the review
pass — are the parts encoding the domain knowledge that makes the project worth anything.

**ADK** is Google's dedicated agent framework, described on its site as "the open-source
agent development framework that lets you build, debug, and deploy reliable AI agents at
enterprise scale" ([adk.dev](https://adk.dev/)). It was announced at Google Cloud Next in
April 2025 as a framework for "the full stack end-to-end development of agents and
multi-agent systems"
([developers.googleblog.com](https://developers.googleblog.com/en/agent-development-kit-easy-to-build-multi-agent-applications/)).
It supports Python, TypeScript, Go, Java, and Kotlin, is model-agnostic — the site states
that "ADK can work with almost any generative AI model" — and provides agents, tools
including MCP and OpenAPI tools, session management, graph and multi-agent workflows,
criteria-based evaluation, and one-command deployment to Google Cloud. It is the natural
choice for a system where several agents negotiate with each other or where conversation
state must survive across turns. Silkscreen's pipeline is a fixed five-stage sequence with
no delegation and no persistent conversation, so most of that surface would sit unused.
Notably, ADK does not replace the Gen AI SDK; `google-adk` declares `google-genai>=2.19,<3`
as a direct dependency
([adk-python pyproject.toml](https://raw.githubusercontent.com/google/adk-python/main/pyproject.toml)),
so adopting ADK would layer on top of what Silkscreen already uses rather than displace it.

**Genkit** describes itself as "Google's open-source framework for building full-stack,
AI-powered and agentic applications for any platform" ([genkit.dev](https://genkit.dev/)),
offering flows, tools, plugins, and a browser-based developer UI for tracing and debugging
runs. Its centre of gravity is TypeScript and JavaScript, with Go also stable; Python and
Dart are marked preview on the site. Google announced Genkit for Python as Alpha in April
2025 and was explicit that both new SDKs were "in versions <1.0.0 and may have breaking
changes" ([firebase.blog](https://firebase.blog/posts/2025/04/genkit-python-go/)).
Silkscreen is a Python project whose engine depends on OR-Tools and kiutils, so the
strongest part of Genkit is in the wrong language, and the Python surface carries a
stability warning that a project shipping deterministic geometry should not take on.

**The Antigravity SDK** is a Python SDK "for building autonomous AI agents powered by
Antigravity and Gemini," providing "a secure, stateful runtime harness that handles tool
execution, context management, safety policies, and subagent delegation"
([antigravity.google](https://antigravity.google/docs/sdk/overview/)). It was announced on
19 May 2026 as a preview that gives "programmatic access to Google's premier Antigravity
coding agent," Python-only for now with TypeScript and Go on the roadmap
([antigravity.google blog](https://antigravity.google/blog/introducing-google-antigravity-sdk)).
Architecturally, the Python layer acts as a control plane over a bundled Go harness that
runs the agentic loop and sandboxes tool execution, and it can consume external MCP servers.
The fit question is about shape rather than quality. Its built-in toolset is oriented around
filesystem and terminal work for coding agents, and its value proposition is a managed
runtime for open-ended autonomous work. Silkscreen's pipeline is closed-ended: five known
stages, a solver in the middle, and a file at the end. The documentation version shown is
v0.1.15, which is early enough that a demo-critical path should not depend on it.

## What a second framework would add, and what it would cost

If the team wanted to add a second Google framework, ADK is the better candidate and the
`Model` protocol in `model.py` is where it would attach. Because every stage already talks
to that protocol rather than to the SDK, an ADK-backed implementation of `generate` would
drop in beside `GeminiModel`, `ScriptedModel`, and `FallbackModel` without touching any
stage.

ADK would add four things Silkscreen does not have today. Its evaluation harness would let
the propose-and-repair loop be scored against a fixture set of intents, turning
`repair_rounds` from a number printed at the end of a run into a tracked regression metric.
Its session service would give the review stage memory across runs, so a finding raised once
about a part could be recalled when that part appears again. Its deployment integration
would replace the hand-rolled `http.server` handler in `service/app.py` with a managed
runtime. Its multi-agent workflow support would matter if the review stage were split into
several specialised reviewers — a power reviewer, a signal-integrity reviewer, a
manufacturability reviewer — arguing separately and having their findings merged.

The costs are concrete. ADK is a large dependency with its own transitive tree, including
FastAPI, Starlette, uvicorn, OpenTelemetry, and a dozen others, against a project whose
current runtime dependencies are two packages. Adopting it would mean either restructuring
the pipeline into ADK agents, which would put a framework abstraction between the code and
the domain logic that is currently the point of the code, or wrapping ADK behind the
existing `Model` protocol, which would use almost none of what ADK offers and would be
difficult to defend as a real adoption. The `ScriptedModel` testing story would need an ADK
equivalent, or the offline test guarantee would erode. And ADK's requirement of
`google-genai>=2.19,<3` is tighter than Silkscreen's own unbounded `>=1.0`, which would
force a version decision the project has so far not had to make.

The reasonable summary is that ADK is the right tool for a different shape of problem.
Silkscreen's agentic value is in the domain-specific checks between stages, not in
orchestration, and orchestration is what ADK sells.

## What this document does not establish

Several things were checked in the code or against published documentation, and a few were
not. Stating which is which matters more than a clean-looking claim.

The model identifiers `gemini-3.7-flash` and `gemini-3.5-flash-lite` in `model.py` were read
from the source but were not verified against a current Gemini model list, so this document
makes no claim that they resolve. The embedding model `gemini-embedding-001` does appear in
the Gen AI SDK's own `embed_content` example, so that one is corroborated.

No live model call was made while writing this. Every statement about the pipeline's
behaviour comes from reading the source and the offline tests, not from observing a run
against the API.

The `google-genai>=1.0` constraint in `pyproject.toml` is unbounded on the upper end. PyPI
currently shows 2.20.0 with a notice of breaking changes planned for 3.0.0, so a fresh
install could eventually pick up a major version the code was not written against. This is
an observation about the pin, not a claim that anything is broken today.

Genkit's Python stability was cross-checked in two places that agree in substance but not in
wording: genkit.dev marks Python as preview in its language selector, while Google's own
April 2025 announcement calls Genkit for Python Alpha. Either label supports the conclusion
drawn above, which is that it is not stable.

## Sources

- Google Gen AI Python SDK repository — <https://github.com/googleapis/python-genai>
- Gen AI Python SDK reference, including automatic function calling and `embed_content` — <https://googleapis.github.io/python-genai/>
- `google-genai` on PyPI — <https://pypi.org/project/google-genai/>
- Gemini API document processing, page limits and `media_resolution` — <https://ai.google.dev/gemini-api/docs/document-processing>
- Gemini API function calling — <https://ai.google.dev/gemini-api/docs/function-calling>
- Agent Development Kit documentation — <https://adk.dev/>
- ADK announcement, Google Developers Blog, 9 April 2025 — <https://developers.googleblog.com/en/agent-development-kit-easy-to-build-multi-agent-applications/>
- `google-adk` on PyPI — <https://pypi.org/project/google-adk/>
- `adk-python` dependency declaration — <https://raw.githubusercontent.com/google/adk-python/main/pyproject.toml>
- Genkit — <https://genkit.dev/>
- Genkit for Python and Go announcement, Firebase Blog, April 2025 — <https://firebase.blog/posts/2025/04/genkit-python-go/>
- Google Antigravity SDK overview — <https://antigravity.google/docs/sdk/overview/>
- Google Antigravity SDK announcement, 19 May 2026 — <https://antigravity.google/blog/introducing-google-antigravity-sdk>
