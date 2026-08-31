// The only place Kaleo talks to the silkscreen engine.
//
// The boundary is HTTP and it is deliberate. This directory is GPL-3.0 and the
// Python side is MIT; they stay separable because they are two programs that
// communicate, never one program in two languages (see `app/NOTICE.md`). So
// everything here goes through the documented `/healthz`, `/generate` and
// `/generate/stream` surface, and nothing here imports, embeds, or vendors
// engine source.
//
// `fetch` is Tauri's, not the webview's: the app origin is `tauri://localhost`
// and the service is `http://127.0.0.1:PORT`, which is cross-origin. The
// service ships no CORS headers on purpose and must not grow any, so the
// request goes through Rust instead, where the same-origin policy does not
// apply. That is also why `http:default` in `src-tauri/capabilities` allows
// `http://**`.

import { fetch as tauriFetch } from "@tauri-apps/plugin-http";
import type { GenerateRequest, RunError, RunResult, StreamFrame } from "./types";

export const DEFAULT_BASE_URL = "http://127.0.0.1:8081";

/** A solve can legitimately run for minutes; 300 s is this client's own ceiling. */
export const REQUEST_TIMEOUT_MS = 300_000;
export const MIN_TIME_LIMIT_S = 5;
export const MAX_TIME_LIMIT_S = 60;

/**
 * What went wrong, in the terms the UI switches on.
 *
 * `kind` is the whole point: "the engine is not running" and "the engine ran
 * and refused your prompt" are different conversations with the user, and a
 * bare status code cannot tell them apart.
 */
export type ErrorKind =
  | "offline" // nothing is listening; the engine was never reached
  | "setup" // reached it, but it has no GOOGLE_API_KEY
  | "request" // 400/413 — the prompt or its options were rejected
  | "upstream" // 502/503 — the model provider failed
  | "server" // 500 — a bug on the engine side, carries an error_id
  | "timeout"
  | "cancelled";

export class SilkscreenError extends Error {
  kind: ErrorKind;
  status: number;
  errorId: string;
  detail: string;

  constructor(
    kind: ErrorKind,
    message: string,
    { status = 0, errorId = "", detail = "" } = {}
  ) {
    super(message);
    this.name = "SilkscreenError";
    this.kind = kind;
    this.status = status;
    this.errorId = errorId;
    this.detail = detail;
  }
}

/**
 * The caller's abort signal combined with this client's own ceiling. Passing
 * a signal must not silently remove the timeout — the app always passes one,
 * so `signal ?? timeout` would leave every real request without a deadline.
 */
function withTimeout(signal?: AbortSignal): AbortSignal {
  const timeout = AbortSignal.timeout(REQUEST_TIMEOUT_MS);
  return signal ? AbortSignal.any([signal, timeout]) : timeout;
}

function clampTimeLimit(value: number | undefined): number {
  const n = Number(value);
  if (!Number.isFinite(n)) return MIN_TIME_LIMIT_S;
  return Math.min(MAX_TIME_LIMIT_S, Math.max(MIN_TIME_LIMIT_S, Math.round(n)));
}

/** Drop half-filled datasheet rows and clamp the solver budget to this app's 5–60 s range. */
export function normalizeRequest(request: GenerateRequest): GenerateRequest {
  const datasheets: Record<string, string> = {};
  for (const [part, url] of Object.entries(request.datasheets ?? {})) {
    const p = String(part).trim();
    const u = String(url).trim();
    if (p && u) datasheets[p] = u;
  }
  return {
    intent: String(request.intent ?? "").trim(),
    datasheets,
    time_limit_s: clampTimeLimit(request.time_limit_s),
    review: request.review !== false,
    // Grounding is opt-in and only sent when asked for: an absent flag is the
    // service's default, so a stray `ground: false` would say nothing. And
    // never after normalization emptied `datasheets` — the service 400s a
    // ground request with nothing to ground on.
    ...(request.ground === true && Object.keys(datasheets).length > 0
      ? { ground: true }
      : {}),
    ...(request.debug === true ? { debug: true } : {}),
  };
}

/**
 * A missing API key is a setup problem, not an outage.
 *
 * The service answers it as a 502 like any other upstream failure, so the only
 * thing separating "go export your key" from "Gemini is down" is the message
 * text. Getting this wrong sends the user to a status page over a five-second
 * fix.
 */
function kindForStatus(status: number, body: Partial<RunError>): ErrorKind {
  const text = `${body.error ?? ""} ${body.detail ?? ""}`;
  if (status === 502 || status === 503) {
    return /GOOGLE_API_KEY|api key/i.test(text) ? "setup" : "upstream";
  }
  if (status === 400 || status === 413) return "request";
  if (status >= 500) return "server";
  return "server";
}

function errorFromBody(status: number, body: Partial<RunError>): SilkscreenError {
  return new SilkscreenError(
    kindForStatus(status, body),
    body.error || `The engine answered ${status}.`,
    { status, errorId: body.error_id ?? "", detail: body.detail ?? "" }
  );
}

async function readJson(response: Response): Promise<Record<string, unknown>> {
  try {
    return (await response.json()) as Record<string, unknown>;
  } catch {
    return {};
  }
}

/**
 * Is the engine up?
 *
 * Returns the reason rather than throwing: this runs on a timer behind a
 * status dot, and an unreachable engine is an ordinary state for this app to
 * be in, not an exception.
 */
export async function health(
  baseUrl: string
): Promise<{ ok: boolean; detail: string }> {
  try {
    const response = await tauriFetch(`${baseUrl}/healthz`, {
      method: "GET",
      signal: AbortSignal.timeout(4000),
    });
    if (!response.ok) return { ok: false, detail: `answered ${response.status}` };
    const body = await readJson(response);
    return body?.ok === true
      ? { ok: true, detail: "" }
      : { ok: false, detail: "answered without ok:true" };
  } catch (error) {
    return { ok: false, detail: (error as Error)?.message ?? "unreachable" };
  }
}

/** One-shot generation. Used as the fallback when the stream never begins. */
export async function generate(
  baseUrl: string,
  request: GenerateRequest,
  signal?: AbortSignal
): Promise<RunResult> {
  const response = await tauriFetch(`${baseUrl}/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(normalizeRequest(request)),
    signal: withTimeout(signal),
  });
  const body = await readJson(response);
  if (!response.ok) throw errorFromBody(response.status, body as Partial<RunError>);
  return body as RunResult;
}

/**
 * Streaming generation: NDJSON frames, one per engine event.
 *
 * `onFrame` is called for every parsed frame in arrival order and the promise
 * resolves with the run's result, taken from the terminal `run.done`.
 *
 * The fallback to `generate()` fires **only** when the stream never began — a
 * 404 from a service too old to have the route. Every other failure mode
 * happens after a 200, by which point the engine has already started a paid
 * run, and quietly re-running it would bill the user twice for one prompt.
 */
export async function generateStream(
  baseUrl: string,
  request: GenerateRequest,
  onFrame: (frame: StreamFrame) => void,
  signal?: AbortSignal
): Promise<RunResult> {
  const payload = normalizeRequest(request);
  let response: Response;
  try {
    response = await tauriFetch(`${baseUrl}/generate/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: withTimeout(signal),
    });
  } catch (error) {
    throw new SilkscreenError(
      "offline",
      "Could not reach the silkscreen engine.",
      { detail: (error as Error)?.message ?? "" }
    );
  }

  // The one safe fallback: the route does not exist, so no run has started.
  if (response.status === 404) return generate(baseUrl, request, signal);

  if (!response.ok) {
    // Pre-stream validation (400/413) still answers plain JSON.
    throw errorFromBody(response.status, (await readJson(response)) as Partial<RunError>);
  }

  const body = response.body;
  if (!body) {
    throw new SilkscreenError(
      "server",
      "The engine accepted the run but sent no stream to follow.",
      { status: response.status }
    );
  }

  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffered = "";
  let result: RunResult | null = null;
  let failure: SilkscreenError | null = null;

  const handle = (line: string) => {
    const frame = parseFrame(line);
    if (!frame) return;
    onFrame(frame);
    if (frame.event === "run.done") result = (frame.result ?? {}) as RunResult;
    if (frame.event === "run.error") {
      failure = errorFromBody(
        Number(frame.status ?? 500),
        frame as unknown as Partial<RunError>
      );
    }
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffered += decoder.decode(value, { stream: true });
    let newline = buffered.indexOf("\n");
    while (newline !== -1) {
      handle(buffered.slice(0, newline));
      buffered = buffered.slice(newline + 1);
      newline = buffered.indexOf("\n");
    }
  }
  handle(buffered);

  if (failure) throw failure;
  if (!result) {
    // The connection closed before `run.done`. Something did happen on the
    // engine side, so this must not silently re-run.
    throw new SilkscreenError(
      "server",
      "The engine closed the stream before the run finished."
    );
  }
  return result;
}

/**
 * Parse one NDJSON line, never throwing.
 *
 * A malformed frame must not take down a run that is otherwise fine, so this
 * returns null and the caller skips it. Blank lines are ordinary: the service
 * flushes per event and the last chunk usually ends in a newline.
 */
export function parseFrame(line: string): StreamFrame | null {
  const trimmed = line.trim();
  if (!trimmed) return null;
  try {
    const parsed = JSON.parse(trimmed);
    if (!parsed || typeof parsed !== "object") return null;
    if (typeof parsed.event !== "string") return null;
    return parsed as StreamFrame;
  } catch {
    return null;
  }
}
