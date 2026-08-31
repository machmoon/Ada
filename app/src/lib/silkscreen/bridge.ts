/**
 * The run-state bridge between Kaleo's two webviews.
 *
 * The overlay (window `main`, route `/`) owns the run: it holds the only
 * `AbortController`, the only in-flight guard, and therefore the only state
 * machine that may spend money. The workbench (window `dashboard`, route
 * `/workbench`) is a *different JS realm* with its own React root, so it gets
 * its own `RunProvider` instance, which is idle and always will be. This module
 * is the wire between them.
 *
 * Three rules shaped it:
 *
 * 1. **One result type.** Nothing here invents a parallel shape for a finished
 *    board. The payload carries `RunResult`, `RunProgress` and
 *    `RunRequestDraft` exactly as the engine and the run hook already define
 *    them, and {@link mirrorRun} hands the workbench a plain `SilkscreenRun` —
 *    the same interface `useSilkscreenRunState` returns. The workbench does not
 *    know it is looking at another window's run.
 *
 * 2. **Snapshots, never deltas.** Every message is the publisher's *complete*
 *    accumulated state. A late subscriber that missed fifty frames does not
 *    reconstruct them and does not guess at them: it receives the same
 *    `RunProgress` the overlay is holding, which is the real record of what the
 *    engine actually said. There is no code path here that can manufacture a
 *    stage, a feed line or an elapsed time that no frame reported.
 *
 * 3. **Absence is not an error.** Every transport call is guarded. With no
 *    Tauri host, no permission, or no publisher, the mirror simply never
 *    receives a snapshot, `mirrorRun` falls through to the local idle state,
 *    and the workbench renders its own "No board on the bench yet" empty
 *    state. Nothing spins.
 */

import { getCurrentWindow } from "@tauri-apps/api/window";
import { emit as tauriEmit, listen as tauriListen } from "@tauri-apps/api/event";

import { SilkscreenError, type ErrorKind } from "./client";
import type { RunProgress } from "./describe";
import type { RunResult } from "./types";
import type {
  RunHistoryEntry,
  RunRequestDraft,
  RunStatus,
  SilkscreenRun,
} from "@/hooks/useSilkscreenRun";

/**
 * Event names.
 *
 * Tauri restricts these to alphanumerics plus `-`, `/`, `:` and `_`; the
 * `kaleo://` prefix keeps them clear of Pluely's inherited event vocabulary
 * (icons, shortcuts, capture, audio), none of which carries run state.
 */
export const RUN_STATE_EVENT = "kaleo://run-state";
export const RUN_HISTORY_EVENT = "kaleo://run-history";
export const RUN_REQUEST_EVENT = "kaleo://run-request";
/** Emitted by Rust (`open_dashboard`), not by this module. */
export const NAVIGATE_EVENT = "kaleo://navigate";

/**
 * `SilkscreenError` is a class, and a class does not survive a structured
 * clone through the IPC. This is its data, and {@link fromBridgedError} builds
 * the instance back on the far side so the workbench keeps switching on `kind`
 * exactly as it does in the overlay.
 */
export interface BridgedError {
  kind: ErrorKind;
  message: string;
  status: number;
  errorId: string;
  detail: string;
}

/**
 * A finished run as it crosses the wire.
 *
 * `frames` is deliberately dropped: it is up to 2000 raw stream frames per run,
 * nothing on the review surface reads it (the only reader is the run hook's own
 * passthrough), and re-sending it would dominate the payload. The mirror
 * rehydrates it as `[]`, which is the truth — that window received no frames.
 */
export type BridgedHistoryEntry = Omit<RunHistoryEntry, "frames">;

/** The live run. Small while a run is streaming: `result` is null until the end. */
export interface RunStateSnapshot {
  /** Monotonic per publisher. A snapshot that arrives out of order is dropped. */
  v: number;
  /** The window label that produced it, so a receiver can ignore its own echo. */
  from: string;
  status: RunStatus;
  submitted: RunRequestDraft | null;
  progress: RunProgress;
  result: RunResult | null;
  error: BridgedError | null;
  startedAt: number | null;
  /**
   * The publisher's final measurement. While a run is live the mirror derives
   * elapsed time from `startedAt` against its own clock instead, so the number
   * keeps moving without the publisher emitting five snapshots a second.
   */
  elapsedMs: number;
}

/**
 * Past runs. Sent on its own event because it only changes when a run
 * finishes — folding it into {@link RunStateSnapshot} would re-send every
 * stored board on every progress frame.
 */
export interface RunHistorySnapshot {
  v: number;
  from: string;
  entries: BridgedHistoryEntry[];
}

/** A mirror asking any publisher to re-send. The late-subscriber handshake. */
export interface RunRequestPayload {
  from: string;
}

export type BridgeRole =
  /** Owns the run and broadcasts it. The overlay. */
  | "publisher"
  /** Shows another window's run and cannot start one. The dashboard. */
  | "mirror"
  /** No bridge: no Tauri host, or a webview that renders no run surface. */
  | "off";

/** Window labels that own runs. `window.rs` accepts either for the overlay. */
const PUBLISHER_LABELS = ["main", "kaleo"];

/**
 * Which end of the bridge this webview is.
 *
 * Unknown labels default to `mirror`: a window that renders the app shell but
 * does not own the state machine may read, never write. The capture overlays
 * render no run surface at all (see `main.tsx`) and are excluded outright.
 */
export function roleFor(label: string | null | undefined): BridgeRole {
  if (!label) return "off";
  if (PUBLISHER_LABELS.includes(label)) return "publisher";
  if (label.startsWith("capture-overlay-")) return "off";
  return "mirror";
}

/**
 * The transport, narrowed to the two calls the bridge makes.
 *
 * An interface rather than direct Tauri calls so the whole bridge is testable
 * without a Tauri host, and so `null` is a first-class "the bridge is not
 * available" value rather than an exception somewhere deep in an effect.
 */
export interface BridgeTransport {
  /** This webview's window label. */
  label: string;
  /**
   * Broadcast. Tauri delivers this to every webview, the sender's included,
   * which is why receivers filter on `from`.
   */
  emit: (event: string, payload: unknown) => Promise<void>;
  /**
   * Resolves to an unlisten function; a failed listen resolves to a no-op.
   *
   * `options.target` matters more than it looks. A listener registered with
   * Tauri's default target (`Any`) is delivered *every* event of that name,
   * including ones addressed to a different window — `match_any_or_filter` in
   * `tauri::event::listener` short-circuits the address filter for `Any`.
   * Passing this window's own label makes an addressed emit (Rust's
   * `emit_to`) actually addressed, and costs nothing for a broadcast, which
   * ignores handler targets either way.
   */
  listen: (
    event: string,
    handler: (payload: unknown) => void,
    options?: { target?: string }
  ) => Promise<() => void>;
}

const noop = () => {};

/**
 * The real transport, or null when there is no Tauri host.
 *
 * Reading the window label is the cheapest possible probe: outside a Tauri
 * webview `getCurrentWindow()` throws, and the caller degrades to `off`.
 * Failures after that point are swallowed rather than thrown — a bridge that
 * cannot emit must leave the workbench in its honest empty state, not break
 * the window that was trying to publish.
 */
export function tauriTransport(): BridgeTransport | null {
  let label: string;
  try {
    label = getCurrentWindow().label;
  } catch {
    return null;
  }
  if (!label) return null;

  return {
    label,
    emit: async (event, payload) => {
      try {
        await tauriEmit(event, payload);
      } catch {
        // Capability denied, or the IPC is gone. The mirror keeps whatever it
        // last received and the overlay keeps running; neither is a failure
        // the user needs to see.
      }
    },
    listen: async (event, handler, options) => {
      try {
        return await tauriListen(
          event,
          (e) => handler(e.payload),
          options?.target ? { target: options.target } : undefined
        );
      } catch {
        // No capability, or no IPC. The caller must carry on without it.
        return noop;
      }
    },
  };
}

export function toBridgedError(
  error: SilkscreenError | null | undefined
): BridgedError | null {
  if (!error) return null;
  return {
    kind: error.kind,
    message: error.message,
    status: error.status,
    errorId: error.errorId,
    detail: error.detail,
  };
}

export function fromBridgedError(
  error: BridgedError | null | undefined
): SilkscreenError | null {
  if (!error) return null;
  return new SilkscreenError(error.kind, error.message, {
    status: error.status,
    errorId: error.errorId,
    detail: error.detail,
  });
}

/** Project the live run onto the wire. Reads the run; never mutates it. */
export function toStateSnapshot(
  run: SilkscreenRun,
  from: string,
  v: number
): RunStateSnapshot {
  return {
    v,
    from,
    status: run.status,
    submitted: run.submitted,
    progress: run.progress,
    result: run.result,
    error: toBridgedError(run.error),
    startedAt: run.startedAt,
    elapsedMs: run.elapsedMs,
  };
}

export function toHistorySnapshot(
  history: RunHistoryEntry[],
  from: string,
  v: number
): RunHistorySnapshot {
  return {
    v,
    from,
    // Built field by field rather than by spreading: the wire shape is a
    // contract, and a new field on `RunHistoryEntry` should have to be added
    // here deliberately instead of riding along by accident.
    entries: history.map((entry) => ({
      id: entry.id,
      intent: entry.intent,
      at: entry.at,
      request: entry.request,
      result: entry.result,
      progress: entry.progress,
      startedAt: entry.startedAt,
      finishedAt: entry.finishedAt,
      elapsedS: entry.elapsedS,
    })),
  };
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null;

/**
 * Payloads arrive from another webview, so they are data and not to be
 * trusted. A snapshot that does not carry the fields the mirror is about to
 * read is dropped whole rather than merged in half.
 */
export function isRunStateSnapshot(value: unknown): value is RunStateSnapshot {
  if (!isRecord(value)) return false;
  if (typeof value.v !== "number" || typeof value.from !== "string") return false;
  if (typeof value.status !== "string") return false;
  const progress = value.progress;
  if (!isRecord(progress)) return false;
  return Array.isArray(progress.stages) && Array.isArray(progress.feed);
}

export function isRunHistorySnapshot(
  value: unknown
): value is RunHistorySnapshot {
  if (!isRecord(value)) return false;
  if (typeof value.v !== "number" || typeof value.from !== "string") return false;
  return Array.isArray(value.entries);
}

export function isRunRequest(value: unknown): value is RunRequestPayload {
  return isRecord(value) && typeof value.from === "string";
}

/**
 * Should `next` replace `previous`?
 *
 * Snapshots are idempotent, so the only thing worth guarding is order: a
 * duplicate or out-of-order delivery must not rewind the bench to a run that
 * has already finished. A snapshot from a different publisher always wins —
 * that is a new owner, not a stale message.
 */
export function acceptsSnapshot(
  previous: { v: number; from: string } | null,
  next: { v: number; from: string }
): boolean {
  if (previous === null) return true;
  if (previous.from !== next.from) return true;
  return next.v > previous.v;
}

export interface MirrorView {
  state: RunStateSnapshot | null;
  history: RunHistorySnapshot | null;
  viewingId: string | null;
  selectRun: (id: string | null) => void;
  /** Injected so a test is not at the mercy of the wall clock. */
  now?: () => number;
}

/**
 * Merge what arrived over the bridge onto this window's own (idle) run state.
 *
 * The result is a `SilkscreenRun`, so `pages/workbench` reads it through
 * `useSilkscreenRun()` with no idea a second window is involved.
 *
 * What is mirrored is exactly the *observation* surface — what a run said about
 * itself. What is kept local is the *configuration* surface: `baseUrl`,
 * `token`, `engine` health and the request draft all belong to the window
 * showing them (the Engine page writes them here).
 *
 * Run control is inert in the mirror on purpose. The overlay holds the only
 * in-flight guard, so a `start()` from this window would be a second paid run
 * for one prompt. Nothing in the dashboard calls it today; this makes that
 * true by construction rather than by luck.
 */
export function mirrorRun(local: SilkscreenRun, view: MirrorView): SilkscreenRun {
  const entries = view.history?.entries ?? [];
  // `frames: []` is the honest value: this window received no stream frames.
  const history: RunHistoryEntry[] = entries.map((entry) => ({
    ...entry,
    frames: [],
  }));

  const viewed = view.viewingId
    ? history.find((entry) => entry.id === view.viewingId) ?? null
    : null;
  const live = view.state;

  const busy =
    live !== null && (live.status === "running" || live.status === "starting");
  const nowMs = (view.now ?? Date.now)();

  // A live run's clock ticks from `startedAt`, which is a fact the publisher
  // sent. A settled one uses the publisher's own final measurement. Neither is
  // invented here, and with no snapshot at all the answer is zero, not a guess.
  const elapsedMs = viewed
    ? viewed.finishedAt - viewed.startedAt
    : live === null
      ? 0
      : busy && live.startedAt !== null
        ? Math.max(0, nowMs - live.startedAt)
        : live.elapsedMs;

  const progress = viewed
    ? viewed.progress
    : live
      ? live.progress
      : local.progress;

  return {
    ...local,

    status: viewed ? "done" : live ? live.status : "idle",
    viewingHistory: viewed !== null,
    submitted: viewed ? viewed.request : live ? live.submitted : null,

    progress,
    stages: progress.stages,
    lines: progress.feed,
    // Raw frames do not cross the bridge; see `BridgedHistoryEntry`.
    frames: [],

    elapsedS: elapsedMs / 1000,
    elapsedMs,
    startedAt: viewed ? viewed.startedAt : live ? live.startedAt : null,
    result: viewed ? viewed.result : live ? live.result : null,
    error: viewed ? null : fromBridgedError(live?.error ?? null),

    history,
    // In a mirror, "have we looked?" means "has a history snapshot arrived?" —
    // this window's own store is irrelevant, because what it reads is thrown
    // away in favour of the publisher's list.
    historyReady: view.history !== null,
    viewingId: view.viewingId,
    selectRun: view.selectRun,

    canStart: false,
    start: noop,
    cancel: noop,
    reset: noop,
    clearHistory: noop,
  };
}

export type { RunResult, RunProgress };
