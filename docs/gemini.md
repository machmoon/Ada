# Gemini models and how Silkscreen calls them

This document covers one hackathon requirement: the project must use Gemini 3.5 or newer,
called through the Gemini API or Vertex AI. It records what the requirement asks for, what
the code actually does today, and where the gaps are. The requirement is restated in the
repo's own `CLAUDE.md:39`.

## What the requirement asks for

Two things have to be true at once. The model has to be at least Gemini 3.5, so a 2.5-family
or 2.0-family model would not qualify. And the call has to go through one of Google's two
first-party surfaces: the Gemini Developer API, which authenticates with an API key, or
Vertex AI (recently rebranded by Google as the Gemini Enterprise Agent Platform), which
authenticates with Google Cloud credentials scoped to a project and a location. Both
surfaces are reachable from the same `google-genai` Python SDK, which is the package
Silkscreen depends on.

## How Silkscreen satisfies it today

The short answer is that it satisfies the model-version half cleanly and the call-surface
half only through the Gemini API, not Vertex AI.

Every model call in the repository funnels through a single class. `GeminiModel` in
`engine/silkscreen/agents/model.py:103` imports `google.genai`, constructs a client, and
issues one `generate_content` request per call at `model.py:163`. Nothing else in the
codebase talks to a vendor SDK for text generation. The one other place that reaches Google
is `GeminiEmbedder` in `engine/silkscreen/agents/retrieval.py:121`, which calls
`embed_content` at `retrieval.py:150` for the datasheet retrieval index.

The agent stages sit above that seam and never see the SDK. The datasheet reader calls
`model.generate` at `engine/silkscreen/agents/datasheet.py:109`, the circuit proposer at
`engine/silkscreen/agents/propose.py:135`, and the adversarial reviewer at
`engine/silkscreen/agents/review.py:128`. All three receive a `Model` — the structural
protocol declared at `model.py:57` — rather than a concrete client, and `generate_pcb` in
`engine/silkscreen/agents/pipeline.py:65` threads one model object through the whole run.

The dependency is declared as an optional extra rather than a base requirement. In
`pyproject.toml:20` the `agents` extra pins `google-genai>=2.19,<3`, and the `cloud` extra
at `pyproject.toml:21` repeats that pin and adds Firestore alongside it. The upper bound
is deliberate: the SDK has an announced breaking 3.0, and the bound is what stops it
arriving in a fresh install without anyone deciding to take it. The base dependency list at
`pyproject.toml:13` contains only OR-Tools and kiutils, which is what keeps the deterministic
engine free of any model dependency at all.

### Keeping the tests offline

`ScriptedModel` at `model.py:175` is the reason the test suite needs no key and no network.
It satisfies the same `Model` protocol, returns canned strings either in order from
`responses` or by matching a substring of the prompt against `by_marker`, and records every
call it received in `calls` so a test can assert on what was actually asked. The
substring-matching path is what lets one model object drive several different agent stages
inside a single test.

`engine/tests/test_agents.py` uses it for all 22 of its tests and says so in its module
docstring. `service/tests/test_app.py:9` imports it for the HTTP service tests. No test in
the repository constructs a `GeminiModel` or reads `GOOGLE_API_KEY`, which is why the CI
workflow at `.github/workflows/ci.yml:20` can install the `agents` and `cloud` extras and run
`pytest -q` on three operating systems without any secret configured.

The failover layer is tested the same way. `engine/silkscreen/agents/resilience.py:74`
defines `FallbackModel`, which tries each `Provider` in order, validates the returned value
through `_validate` at `resilience.py:57`, and moves to the next provider when a response is
empty or is not a string. `engine/tests/test_resilience.py` forces each of those failure
shapes with small stub classes — one that raises, one that returns a streaming iterator, one
that returns whitespace, one that returns `None` — so every fallback branch is executed
rather than assumed.

## Which model IDs the code names

Two constants sit at the top of `model.py`.

| Constant | Value | Where it is used |
|---|---|---|
| `DEFAULT_MODEL` (`model.py:30`) | `gemini-3.7-flash` | CLI default, primary provider in the service |
| `CHEAP_MODEL` (`model.py:33`) | `gemini-3.5-flash-lite` | second-tier provider in the service |

Both satisfy the requirement. Google's model list documents `gemini-3.7-flash` as the
current, most capable Flash model, released August 2026, accepting text, image, video, audio
and PDF input with a 1,048,576-token input window
([model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.7-flash)). It documents
`gemini-3.5-flash-lite` as a stable, low-latency, cost-oriented multimodal model that also
accepts PDF input and carries the same 1,048,576-token input window
([model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)). Neither
is a 2.5-family or older model, so the version floor is met with room to spare.

The tiering is a cost decision. On the paid tier Google lists `gemini-3.7-flash` at $0.75 per
million input tokens and $3.75 per million output tokens through 31 December 2026, against
$0.30 and $2.50 for `gemini-3.5-flash-lite`
([pricing](https://ai.google.dev/gemini-api/docs/pricing)). The comment at `model.py:32`
describes the cheaper tier as being for high-volume mechanical passes, which matches how
Google positions Flash-Lite for document parsing and subagent work.

Retrieval uses a third model. `EMBED_MODEL` at `retrieval.py:40` is `gemini-embedding-001`,
which is generally available in both the Gemini API and Vertex AI
([announcement](https://developers.googleblog.com/gemini-embedding-available-gemini-api/)).
It is an embedding model rather than a generative one, so it sits outside the "Gemini 3.5 or
newer" version scheme and does not affect the requirement either way.

### Changing the model

There are three places to change, depending on scope. Passing `--model` on the command line
overrides it for one run; the flag is declared at `engine/silkscreen/cli.py:44` and its value
is handed straight to `GeminiModel` at `cli.py:63`. Constructing `GeminiModel("some-model-id")`
directly overrides it for one call site, which is what `service/app.py:92` does for both
providers in the fallback chain. Editing `DEFAULT_MODEL` or `CHEAP_MODEL` at `model.py:30`
and `model.py:33` changes it everywhere at once, since the CLI default, `default_model` at
`model.py:212`, and the service's `build_model` at `service/app.py:88` all read those
constants. There is no environment variable for the model ID, so a deployment cannot change
models without a code change or an explicit `--model` argument.

## Gemini API versus Vertex AI

The `google-genai` SDK is a single client that can point at either backend, and which one it
picks depends entirely on how `genai.Client` is constructed.

For the Gemini Developer API you pass an API key, either explicitly as
`genai.Client(api_key=...)` or implicitly by setting `GOOGLE_API_KEY` or `GEMINI_API_KEY` in
the environment. The SDK's own key resolution reads `GOOGLE_API_KEY` first and falls back to
`GEMINI_API_KEY`, warning if both are set
([`_api_client.py`](https://raw.githubusercontent.com/googleapis/python-genai/main/google/genai/_api_client.py),
lines 130-141).

For Vertex AI you pass `enterprise=True` together with `project` and `location`, or set
`GOOGLE_GENAI_USE_ENTERPRISE=true`, `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` and
construct the client with no arguments
([SDK README](https://github.com/googleapis/python-genai)). The older spelling still works:
the `vertexai` keyword is documented in the client source as a legacy alias for `enterprise`,
and the SDK raises if the two are set to conflicting values
([`client.py`](https://raw.githubusercontent.com/googleapis/python-genai/main/google/genai/client.py),
lines 271-380). The same holds for the environment variables, since `GOOGLE_GENAI_USE_VERTEXAI`
is still read alongside `GOOGLE_GENAI_USE_ENTERPRISE`, with the enterprise variable winning a
conflict (`_api_client.py`, lines 656-676). Google began rebranding Vertex AI to the Gemini
Enterprise Agent Platform in 2026 while leaving the `google-genai` package name and proto
namespaces unchanged, which is why both spellings coexist.

Silkscreen uses the API-key path only. `GeminiModel.__init__` reads `GOOGLE_API_KEY` at
`model.py:120`, raises `ModelError` if it is absent, and constructs `genai.Client(api_key=key)`
at `model.py:126`. `GeminiEmbedder` does the same at `retrieval.py:135` and `retrieval.py:138`.
Neither class accepts a `project` or `location` argument, and neither passes `vertexai` or
`enterprise`, so there is no code path in the repository that reaches Vertex AI.

The environment file is misleading on this point. `.env.example` lists `GOOGLE_API_KEY`,
`GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION`, which reads like Vertex AI configuration.
It is not. `GOOGLE_CLOUD_PROJECT` is consumed at `service/app.py:75`, where its presence is
the signal to use Firestore instead of an in-memory fact cache. `GOOGLE_CLOUD_LOCATION` is not
read anywhere in the codebase at all; grepping the repository finds it only in
`.env.example:4` and in a prose sentence in `CLAUDE.md:43`. The CLI loads `.env` into the
process environment at `cli.py:53` using a small hand-rolled reader that calls
`os.environ.setdefault`, so an already-exported variable wins over the file.

One consequence is worth stating plainly. Because `GOOGLE_CLOUD_PROJECT` and
`GOOGLE_CLOUD_LOCATION` land in the environment but `GOOGLE_GENAI_USE_ENTERPRISE` is never
set, the SDK still selects the Developer API. Setting the project and location alone does not
switch backends.

### What a Vertex AI path would need

Adding one is small. `GeminiModel.__init__` would need to accept a project and location, and
to build the client as `genai.Client(vertexai=True, project=..., location=...)` when those are
present instead of requiring `GOOGLE_API_KEY`. The provider chain in `resilience.py` would
then let a Gemini API provider and a Vertex AI provider sit in the same list, which would make
the "no single point of failure" framing in `resilience.py:1` true across surfaces rather than
only across model tiers. Vertex AI also authenticates through Application Default Credentials
rather than a key, which matters on Cloud Run because the service account is already attached
and no secret would need to be set.

## Things worth flagging

The `media_resolution` value is probably wrong. `GeminiModel` defaults `media_resolution` to
the string `"high"` at `model.py:111` and puts it in the request config at `model.py:160`.
Google documents the accepted values as `MEDIA_RESOLUTION_LOW`, `MEDIA_RESOLUTION_MEDIUM`,
`MEDIA_RESOLUTION_HIGH` and `MEDIA_RESOLUTION_UNSPECIFIED`
([media resolution docs](https://ai.google.dev/gemini-api/docs/generate-content/media-resolution)),
and the SDK's `MediaResolution` enum defines exactly those four names and values
([`types.py`](https://raw.githubusercontent.com/googleapis/python-genai/main/google/genai/types.py),
lines 661-671). That enum subclasses `CaseInSensitiveEnum`, whose `_missing_` hook tries the
supplied value as an uppercased and then a lowercased member name
([`_common.py`](https://raw.githubusercontent.com/googleapis/python-genai/main/google/genai/_common.py),
lines 657-677). Neither `HIGH` nor `high` is a member name, so the lookup falls through to the
branch that emits a "high is not a valid MediaResolution" warning and constructs an unknown
enum member carrying the literal string, which is then what goes on the wire. I could not run
this against the live API, because `google-genai` is not installed in this working copy, so I
have not confirmed the server's response. The safe change is to pass `"MEDIA_RESOLUTION_HIGH"`,
and the safest is to import `types.MediaResolution` and use the enum member. This matters more
than it sounds, because the comment at `model.py:128` says high resolution is the lever that
makes datasheet pin tables legible; if the value is being dropped, the pipeline is silently
reading datasheets at the default resolution.

There is no live-path test. Every test uses `ScriptedModel` or a stub, and nothing exercises
`GeminiModel` against the real SDK, not even a skipped-by-default integration test gated on
the presence of a key. The pattern already exists in the repo, since
`engine/tests/test_kicad.py:29` uses a `pytestmark = pytest.mark.skipif(...)` guard, so a
key-gated smoke test would fit existing conventions. As things stand, a typo in a model ID
constant or a breaking SDK change would not be caught by CI.

The `generate_content` surface is being repositioned. Google's current documentation labels
several `generate_content` pages as "Gemini Generate Content API (Legacy)" and shows a newer
`client.interactions.create` surface in the Gemini 3 developer guide
([Gemini 3 guide](https://ai.google.dev/gemini-api/docs/gemini-3)). The `generate_content`
method Silkscreen calls at `model.py:163` is still documented and still shown in current model
pages, so nothing is broken today, but the naming suggests the surface will eventually move.
Because every call is behind the `Model` protocol at `model.py:57`, migrating would touch one
method body.

There is no retry or backoff inside `GeminiModel` itself. The class wraps any SDK exception in
`ModelError` at `model.py:166` and re-raises. Retry lives one layer up in `FallbackModel`,
which is a reasonable split, but it means a bare `GeminiModel` — which is what the CLI
constructs at `cli.py:63` — has no retry at all. The CLI is therefore less resilient to a
transient 5xx than the HTTP service is.

The README and DEVPOST claims match the code. `DEVPOST.md:127` describes the tiering as
`gemini-3.7-flash` for datasheet vision and reasoning dropping to `gemini-3.5-flash-lite` for
high-volume passes, which is exactly what `service/app.py:92` wires up. The README's setup
instructions at `README.md:102` and `README.md:205` ask for `GOOGLE_API_KEY` and nothing else,
which is honest about the Developer-API-only reality even though `.env.example` is not.

## Verdict

The requirement is met on both counts as written. The models named in the code are
`gemini-3.7-flash` and `gemini-3.5-flash-lite`, both newer than the 3.5 floor, and they are
called through the Gemini API using the official `google-genai` SDK. The requirement offers
Vertex AI as an alternative rather than an addition, so not supporting it is not a failure.
The two things genuinely worth fixing are the `media_resolution` string, which looks like a
real bug rather than a documentation gap, and the stale `GOOGLE_CLOUD_LOCATION` entry in
`.env.example`, which implies a Vertex AI configuration path that does not exist.
