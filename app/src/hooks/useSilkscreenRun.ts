import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  DEFAULT_BASE_URL,
  MAX_TIME_LIMIT_S,
  MIN_TIME_LIMIT_S,
  SilkscreenError,
  generateStream,
} from "@/lib/silkscreen/client";
import type {
  GenerateRequest,
  RunResult,
  StreamFrame,
} from "@/lib/silkscreen/types";
import {
  describeFrame,
  initialRunProgress,
  reduceFrame,
  type FeedLine,
  type RunProgress,
  type StageState,
} from "@/lib/silkscreen/describe";
import type { RunPlan } from "@/lib/silkscreen/stages";
import { useEngineHealth, type EngineHealth } from "@/hooks/useEngineHealth";
import { logError, logEvent, logServer } from "@/lib/silkscreen/log";

/**
 * `cancelled` is deliberately its own status and not an error: the user asked
 * for it, so nothing went wrong and nothing should be dressed up as a failure.
 *
 * Distinct from `RunProgress["status"]` in `describe.ts`, which tracks what the
 * *engine* has said about itself. This one is what the *app* is doing, and only
 * this one knows about cancelling.
 */
export type RunStatus =
  | "idle"
  /** Asked for, not yet acknowledged. `run.accepted` has not arrived. */
  | "starting"
  | "running"
  | "done"
  | "error"
  | "cancelled";

/** The form the user fills in. Mirrors `GenerateRequest` with no optionals. */
export interface RunRequestDraft {
  intent: string;
  /** part ref -> datasheet URL. Half-filled rows are dropped by the client. */
  datasheets: Record<string, string>;
  time_limit_s: number;
  review: boolean;
  /** Only meaningful with at least one datasheet — retrieval needs a source. */
  ground: boolean;
  /** Asks the service for `model.response` frames, for the debug console. */
  debug: boolean;
}

export interface RunHistoryEntry {
  id: string;
  /** `request.intent`, lifted so a history row can label itself. */
  intent: string;
  /** When it finished, epoch ms — what a "2 minutes ago" label wants. */
  at: number;
  /** The request as submitted, not the draft as it stands now. */
  request: RunRequestDraft;
  result: RunResult;
  /** Every frame, in arrival order, for anything that wants the raw log. */
  frames: StreamFrame[];
  progress: RunProgress;
  startedAt: number;
  finishedAt: number;
  /** Wall clock, this app's measurement. `progress.elapsedS` is the engine's. */
  elapsedS: number;
}

export interface SilkscreenRun {
  /** Where the engine lives, and the poll behind the status dot. */
  baseUrl: string;
  setBaseUrl: (url: string) => void;
  engine: EngineHealth;

  /** The editable request. */
  request: RunRequestDraft;
  updateRequest: (patch: Partial<RunRequestDraft>) => void;
  setDatasheet: (part: string, url: string) => void;
  removeDatasheet: (part: string) => void;
  /** Copy a past run's request back into the draft so it can be re-run. */
  restoreRequest: (id: string) => void;
  canStart: boolean;

  status: RunStatus;
  /** True while this app is showing a past run rather than the current one. */
  viewingHistory: boolean;
  /** The request that produced what is on screen, or null before the first run. */
  submitted: RunRequestDraft | null;

  /** Everything the frames have said so far. Never guesses ahead of them. */
  progress: RunProgress;
  /** `progress.stages`, lifted because most callers want only this. */
  stages: StageState[];
  /** `progress.feed`, the describer's sentences in arrival order. */
  lines: FeedLine[];
  frames: StreamFrame[];

  /** Wall-clock seconds, ticking from a real clock so a stall reads as a stall. */
  elapsedS: number;
  /** The same clock in milliseconds, for callers that format their own. */
  elapsedMs: number;
  startedAt: number | null;
  result: RunResult | null;
  error: SilkscreenError | null;

  /**
   * Run the draft, or the request passed in — for callers that keep the form
   * in their own state. Returns void so nothing can await, retry, or chain it.
   */
  start: (request?: Partial<GenerateRequest>) => void;
  cancel: () => void;
  reset: () => void;

  history: RunHistoryEntry[];
  historyLimit: number;
  viewingId: string | null;
  selectRun: (id: string | null) => void;
  clearHistory: () => void;
}

export interface UseSilkscreenRunOptions {
  baseUrl?: string;
  /** How many finished runs stay in memory. Boards are large; keep it small. */
  historyLimit?: number;
  healthIntervalMs?: number;
}

export const DEFAULT_HISTORY_LIMIT = 8;
export const DEFAULT_TIME_LIMIT_S = 20;
/** A stream is a few hundred frames; this only guards against a runaway one. */
const FRAME_LOG_LIMIT = 2000;

export const EMPTY_REQUEST: RunRequestDraft = {
  intent: "",
  datasheets: {},
  time_limit_s: DEFAULT_TIME_LIMIT_S,
  review: true,
  ground: false,
  debug: false,
};

let seq = 0;
function newId(prefix: string): string {
  seq += 1;
  return `${prefix}-${Date.now().toString(36)}-${seq}`;
}

/** Fold an explicit request over the draft, so a caller may pass all or none. */
function toDraft(
  base: RunRequestDraft,
  request?: Partial<GenerateRequest>
): RunRequestDraft {
  return {
    intent: request?.intent ?? base.intent,
    datasheets: { ...(request?.datasheets ?? base.datasheets) },
    time_limit_s: request?.time_limit_s ?? base.time_limit_s,
    review: request?.review ?? base.review,
    ground: request?.ground ?? base.ground,
    debug: request?.debug ?? base.debug,
  };
}

function hasDatasheet(draft: RunRequestDraft): boolean {
  return Object.entries(draft.datasheets).some(
    ([part, url]) => part.trim() && url.trim()
  );
}

/**
 * Which stages this request can emit at all.
 *
 * `output: false` is not a guess — nothing over HTTP passes an output path, so
 * `schematic_stage` provably does no work and its row must not sit pending
 * forever. `route` is the engine's own default and the service does not
 * override it.
 */
function planFor(draft: RunRequestDraft): RunPlan {
  return {
    datasheets: hasDatasheet(draft),
    review: draft.review,
    route: true,
    output: false,
  };
}

/**
 * Anything that is not already a `SilkscreenError` still has to reach the UI
 * with a `kind`, because that is the only thing the UI switches on.
 */
function toSilkscreenError(error: unknown): SilkscreenError {
  if (error instanceof SilkscreenError) return error;
  const err = error as Error | undefined;
  if (err?.name === "TimeoutError") {
    return new SilkscreenError("timeout", "The engine did not answer in time.", {
      detail: err?.message ?? "",
    });
  }
  return new SilkscreenError(
    "server",
    err?.message || "The run failed for an unknown reason."
  );
}

/**
 * The whole state machine for one board run.
 *
 * Call this ONCE, from `RunProvider`. Everything else reads it through
 * `useSilkscreenRun()` in `@/contexts/run.context`.
 *
 * The money rule shapes most of what follows: every `start()` is at most one
 * request to the engine, which spends real Gemini credit. There is no retry
 * anywhere in here — not on error, not on a dead stream, not on a failed health
 * check — and the client's single fallback (a 404, meaning the stream route
 * does not exist and so no run was ever started) is left exactly as it is.
 */
export function useSilkscreenRunState(
  options: UseSilkscreenRunOptions = {}
): SilkscreenRun {
  const historyLimit = options.historyLimit ?? DEFAULT_HISTORY_LIMIT;

  const [baseUrl, setBaseUrl] = useState(options.baseUrl ?? DEFAULT_BASE_URL);
  const engine = useEngineHealth(baseUrl, options.healthIntervalMs);

  const [request, setRequest] = useState<RunRequestDraft>(EMPTY_REQUEST);
  const [submitted, setSubmitted] = useState<RunRequestDraft | null>(null);

  const [status, setStatus] = useState<RunStatus>("idle");
  const [progress, setProgress] = useState<RunProgress>(() =>
    initialRunProgress()
  );
  const [frames, setFrames] = useState<StreamFrame[]>([]);
  const [result, setResult] = useState<RunResult | null>(null);
  const [error, setError] = useState<SilkscreenError | null>(null);

  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [elapsedMs, setElapsedMs] = useState(0);

  const [history, setHistory] = useState<RunHistoryEntry[]>([]);
  const [viewingId, setViewingId] = useState<string | null>(null);

  const mountedRef = useRef(true);
  const abortRef = useRef<AbortController | null>(null);
  // Set synchronously, unlike `status`, so a double-click cannot start two runs.
  const inFlightRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      // A dropped provider must not leave a paid run streaming into nothing.
      abortRef.current?.abort();
      abortRef.current = null;
      inFlightRef.current = false;
    };
  }, []);

  // The clock is independent of the event stream on purpose: if the engine goes
  // quiet for ninety seconds, the user should see ninety seconds pass, not a
  // frozen number that reads as "finished".
  useEffect(() => {
    if ((status !== "running" && status !== "starting") || startedAt === null)
      return;
    setElapsedMs(Date.now() - startedAt);
    const timer = window.setInterval(() => {
      setElapsedMs(Date.now() - startedAt);
    }, 200);
    return () => window.clearInterval(timer);
  }, [status, startedAt]);

  const updateRequest = useCallback((patch: Partial<RunRequestDraft>) => {
    setRequest((previous) => {
      const next = { ...previous, ...patch };
      if (patch.time_limit_s !== undefined) {
        const n = Number(patch.time_limit_s);
        next.time_limit_s = Number.isFinite(n)
          ? Math.min(MAX_TIME_LIMIT_S, Math.max(MIN_TIME_LIMIT_S, Math.round(n)))
          : previous.time_limit_s;
      }
      return next;
    });
  }, []);

  const setDatasheet = useCallback((part: string, url: string) => {
    setRequest((previous) => ({
      ...previous,
      datasheets: { ...previous.datasheets, [part]: url },
    }));
  }, []);

  const removeDatasheet = useCallback((part: string) => {
    setRequest((previous) => {
      const datasheets = { ...previous.datasheets };
      delete datasheets[part];
      // Grounding with nothing to ground on says nothing; drop the flag too.
      const stillHasOne = Object.values(datasheets).some((u) => u.trim());
      return {
        ...previous,
        datasheets,
        ground: stillHasOne ? previous.ground : false,
      };
    });
  }, []);

  const restoreRequest = useCallback(
    (id: string) => {
      const entry = history.find((h) => h.id === id);
      if (entry) {
        setRequest({ ...entry.request, datasheets: { ...entry.request.datasheets } });
      }
    },
    [history]
  );

  const start = useCallback(
    (override?: Partial<GenerateRequest>) => {
    if (inFlightRef.current) return;

    const draft = toDraft(request, override);
    if (!draft.intent.trim()) {
      setError(
        new SilkscreenError(
          "request",
          "Describe the board you want before starting a run."
        )
      );
      setStatus("error");
      return;
    }

    inFlightRef.current = true;
    const controller = new AbortController();
    abortRef.current = controller;

    const began = Date.now();
    // Accumulate into values owned by this run rather than reading state back
    // out: the history entry needs the finished log, and a state updater is not
    // a place to do work.
    let collectedFrames: StreamFrame[] = [];
    let collectedProgress = initialRunProgress(planFor(draft));

    setSubmitted(draft);
    setViewingId(null);
    // Not "running" yet: nothing has come back, and saying otherwise would be
    // the UI asserting something the engine has not confirmed.
    setStatus("starting");
    setFrames(collectedFrames);
    setProgress(collectedProgress);
    setResult(null);
    setError(null);
    setStartedAt(began);
    setElapsedMs(0);

    const onFrame = (frame: StreamFrame) => {
      if (!mountedRef.current || controller.signal.aborted) return;
      collectedFrames =
        collectedFrames.length >= FRAME_LOG_LIMIT
          ? [...collectedFrames.slice(1), frame]
          : [...collectedFrames, frame];
      // `reduceFrame` is documented never to throw and to count, not crash on,
      // an event name this build has never heard of.
      collectedProgress = reduceFrame(collectedProgress, frame);
      // Mirror into the debug console. `logServer` is the sanctioned path: it
      // scrubs credentials and reduces `result.kicad_pcb` to a length marker.
      logServer(frame.event, describeFrame(frame) ?? "", frame);
      setFrames(collectedFrames);
      setProgress(collectedProgress);
      // The first frame is the engine acknowledging the run.
      setStatus((previous) => (previous === "starting" ? "running" : previous));
    };

    // Deliberately fire-and-forget: `start()` returns void so no caller can
    // await it, retry it, or chain a second request onto it.
    void (async () => {
      try {
        const runResult = await generateStream(
          baseUrl,
          {
            intent: draft.intent,
            datasheets: draft.datasheets,
            time_limit_s: draft.time_limit_s,
            review: draft.review,
            ...(draft.ground ? { ground: true } : {}),
            ...(draft.debug ? { debug: true } : {}),
          },
          onFrame,
          controller.signal
        );
        if (!mountedRef.current) return;
        if (controller.signal.aborted) return; // cancel() already set the state

        const finished = Date.now();
        setResult(runResult);
        setStatus("done");
        setElapsedMs(finished - began);

        // The entry holds the same object references the live view is showing,
        // so keeping a run in history costs nothing beyond the array slot.
        const entry: RunHistoryEntry = {
          id: newId("run"),
          intent: draft.intent,
          at: finished,
          request: draft,
          result: runResult,
          frames: collectedFrames,
          progress: collectedProgress,
          startedAt: began,
          finishedAt: finished,
          elapsedS: (finished - began) / 1000,
        };
        setHistory((previous) => [entry, ...previous].slice(0, historyLimit));
        logEvent("run.finished", `Run finished in ${entry.elapsedS.toFixed(1)} s.`);
      } catch (caught) {
        if (!mountedRef.current) return;
        // A stale run — one that was cancelled and then superseded by a new
        // start() — must not write anything: its late rejection would clobber
        // the live run's status. Only the run that still owns abortRef may
        // report a terminal state.
        if (abortRef.current !== controller && controller.signal.aborted) {
          return;
        }
        // Check the signal first: an abort during the opening fetch surfaces
        // from the client as an `offline` SilkscreenError, which would
        // otherwise read as "the engine is down" when it plainly is not.
        if (controller.signal.aborted) {
          setStatus("cancelled");
          setElapsedMs(Date.now() - began);
          return;
        }
        const failure = toSilkscreenError(caught);
        logError("run.failed", failure.message, {
          kind: failure.kind,
          status: failure.status,
          errorId: failure.errorId,
        });
        setError(failure);
        setStatus("error");
        setElapsedMs(Date.now() - began);
      } finally {
        // The in-flight guard, like the abort handle, belongs to the CURRENT
        // run: a stale run releasing it would let a new start() fire while
        // another run is still streaming — a second paid run for one action.
        if (abortRef.current === controller) {
          abortRef.current = null;
          inFlightRef.current = false;
        }
      }
    })();
    },
    [baseUrl, historyLimit, request]
  );

  const cancel = useCallback(() => {
    const controller = abortRef.current;
    if (!controller) return;
    controller.abort();
    // Set it here as well as in the catch: the reader may take a moment to
    // notice, and the button must stop saying "running" the instant it is hit.
    setStatus("cancelled");
  }, []);

  const reset = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    inFlightRef.current = false;
    setStatus("idle");
    setSubmitted(null);
    setFrames([]);
    setProgress(initialRunProgress());
    setResult(null);
    setError(null);
    setStartedAt(null);
    setElapsedMs(0);
    setViewingId(null);
  }, []);

  const selectRun = useCallback(
    (id: string | null) => {
      // Flipping the view away from a live run would hide the thing the user is
      // paying for; make them cancel first.
      if (status === "running" || status === "starting") return;
      setViewingId(id);
    },
    [status]
  );

  const clearHistory = useCallback(() => {
    setHistory([]);
    setViewingId(null);
  }, []);

  const viewed = useMemo(
    () => (viewingId ? history.find((h) => h.id === viewingId) ?? null : null),
    [history, viewingId]
  );

  const shownProgress = viewed ? viewed.progress : progress;
  const busy = status === "running" || status === "starting";
  const canStart = !busy && request.intent.trim().length > 0;

  return {
    baseUrl,
    setBaseUrl,
    engine,

    request,
    updateRequest,
    setDatasheet,
    removeDatasheet,
    restoreRequest,
    canStart,

    status: viewed ? "done" : status,
    viewingHistory: viewed !== null,
    submitted: viewed ? viewed.request : submitted,

    progress: shownProgress,
    stages: shownProgress.stages,
    lines: shownProgress.feed,
    frames: viewed ? viewed.frames : frames,

    elapsedS: viewed ? viewed.elapsedS : elapsedMs / 1000,
    elapsedMs: viewed ? viewed.finishedAt - viewed.startedAt : elapsedMs,
    startedAt: viewed ? viewed.startedAt : startedAt,
    result: viewed ? viewed.result : result,
    error: viewed ? null : error,

    start,
    cancel,
    reset,

    history,
    historyLimit,
    viewingId,
    selectRun,
    clearHistory,
  };
}
