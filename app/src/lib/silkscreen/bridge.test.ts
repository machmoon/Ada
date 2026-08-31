// The wire format and the merge, with no React and no Tauri involved.
//
// The cross-window behaviour these functions are wired into lives in
// `hooks/useRunBridge.test.tsx`; this file pins the parts that must be true
// regardless of who is calling them.

import { describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));

import { SilkscreenError } from "./client";
import { initialRunProgress, reduceFrame, type RunProgress } from "./describe";
import type { RunResult } from "./types";
import type { RunHistoryEntry, SilkscreenRun } from "@/hooks/useSilkscreenRun";
import { EMPTY_REQUEST } from "@/hooks/useSilkscreenRun";
import {
  acceptsSnapshot,
  fromBridgedError,
  isRunHistorySnapshot,
  isRunRequest,
  isRunStateSnapshot,
  mirrorRun,
  roleFor,
  tauriTransport,
  toBridgedError,
  toHistorySnapshot,
  toStateSnapshot,
  type RunStateSnapshot,
} from "./bridge";

const RESULT: RunResult = {
  intent: "an stm32 dev board",
  kicad_pcb: "(kicad_pcb (version 20240108))",
  board_mm: [40, 30],
  status: "feasible",
  findings: [{ severity: "blocker", title: "no decoupling on U1" }],
  parts: [{ ref: "U1", footprint: "LQFP-48" }],
  nets: ["VCC", "GND"],
};

/** A real mid-run progress, built the only way the app ever builds one. */
function progressAfterFrames(): RunProgress {
  let progress = initialRunProgress({ review: true, route: true });
  progress = reduceFrame(progress, { event: "run.accepted", t_s: 0 });
  progress = reduceFrame(progress, { event: "stage.start", stage: "place", t_s: 1 });
  return progress;
}

function fakeRun(overrides: Partial<SilkscreenRun> = {}): SilkscreenRun {
  const noop = () => {};
  const progress = initialRunProgress();
  return {
    baseUrl: "http://127.0.0.1:8081",
    setBaseUrl: noop,
    token: "",
    setToken: noop,
    engine: {
      baseUrl: "http://127.0.0.1:8081",
      ok: true,
      detail: "",
      checking: false,
      lastCheckedAt: null,
      recheck: noop,
    } as SilkscreenRun["engine"],
    request: EMPTY_REQUEST,
    updateRequest: noop,
    setDatasheet: noop,
    removeDatasheet: noop,
    restoreRequest: noop,
    canStart: true,
    status: "idle",
    viewingHistory: false,
    submitted: null,
    progress,
    stages: progress.stages,
    lines: progress.feed,
    frames: [],
    elapsedS: 0,
    elapsedMs: 0,
    startedAt: null,
    result: null,
    error: null,
    start: noop,
    cancel: noop,
    reset: noop,
    history: [],
    historyLimit: 8,
    historyReady: true,
    viewingId: null,
    selectRun: noop,
    clearHistory: noop,
    ...overrides,
  };
}

function historyEntry(id: string, intent: string): RunHistoryEntry {
  return {
    id,
    intent,
    at: 1_700_000_000_000,
    request: { ...EMPTY_REQUEST, intent },
    result: { ...RESULT, intent },
    frames: [{ event: "run.accepted", t_s: 0 }],
    progress: progressAfterFrames(),
    startedAt: 1_700_000_000_000,
    finishedAt: 1_700_000_012_000,
    elapsedS: 12,
  };
}

function stateSnapshot(
  overrides: Partial<RunStateSnapshot> = {}
): RunStateSnapshot {
  return {
    v: 1,
    from: "main",
    status: "done",
    submitted: { ...EMPTY_REQUEST, intent: "an stm32 dev board", review: true },
    progress: progressAfterFrames(),
    result: RESULT,
    error: null,
    startedAt: 1_700_000_000_000,
    elapsedMs: 12_000,
    ...overrides,
  };
}

describe("roleFor", () => {
  it("makes the overlay the publisher", () => {
    expect(roleFor("main")).toBe("publisher");
    expect(roleFor("kaleo")).toBe("publisher");
  });

  it("makes the dashboard a mirror", () => {
    expect(roleFor("dashboard")).toBe("mirror");
  });

  it("turns the bridge off where there is no run surface or no host", () => {
    expect(roleFor("capture-overlay-0")).toBe("off");
    expect(roleFor(null)).toBe("off");
    expect(roleFor(undefined)).toBe("off");
    expect(roleFor("")).toBe("off");
  });
});

describe("tauriTransport", () => {
  it("returns null outside a Tauri webview instead of throwing", () => {
    // No `__TAURI_INTERNALS__` here, which is exactly the "bridge unavailable"
    // case: the caller must get a value it can branch on, not an exception.
    expect(tauriTransport()).toBeNull();
  });
});

describe("the wire format", () => {
  it("carries the engine's own RunResult, not a second result type", () => {
    const run = fakeRun({ status: "done", result: RESULT });
    const snapshot = toStateSnapshot(run, "main", 7);
    // Identity, not a copy: nothing reshapes the result on the way out.
    expect(snapshot.result).toBe(RESULT);
    expect(snapshot.v).toBe(7);
    expect(snapshot.from).toBe("main");
    expect(snapshot.status).toBe("done");
  });

  it("round-trips an error back into a SilkscreenError the UI can switch on", () => {
    const error = new SilkscreenError("setup", "The engine has no key.", {
      status: 502,
      errorId: "abc123",
      detail: "GOOGLE_API_KEY",
    });
    const rebuilt = fromBridgedError(toBridgedError(error));
    expect(rebuilt).toBeInstanceOf(SilkscreenError);
    expect(rebuilt?.kind).toBe("setup");
    expect(rebuilt?.message).toBe("The engine has no key.");
    expect(rebuilt?.status).toBe(502);
    expect(rebuilt?.errorId).toBe("abc123");
    expect(rebuilt?.detail).toBe("GOOGLE_API_KEY");
  });

  it("treats no error as no error at both ends", () => {
    expect(toBridgedError(null)).toBeNull();
    expect(fromBridgedError(null)).toBeNull();
  });

  it("drops raw stream frames from history but keeps everything read", () => {
    const snapshot = toHistorySnapshot(
      [historyEntry("run-1", "a buck converter")],
      "main",
      3
    );
    const entry = snapshot.entries[0];
    expect(entry).not.toHaveProperty("frames");
    expect(entry.id).toBe("run-1");
    expect(entry.request.intent).toBe("a buck converter");
    expect(entry.result.intent).toBe("a buck converter");
    expect(entry.elapsedS).toBe(12);
  });
});

describe("payload validation", () => {
  it("accepts a well-formed snapshot", () => {
    expect(isRunStateSnapshot(stateSnapshot())).toBe(true);
    expect(isRunHistorySnapshot(toHistorySnapshot([], "main", 1))).toBe(true);
    expect(isRunRequest({ from: "dashboard" })).toBe(true);
  });

  it("rejects anything that is not one", () => {
    for (const junk of [
      null,
      undefined,
      "run.done",
      42,
      {},
      { v: 1, from: "main" },
      { v: "1", from: "main", status: "done", progress: { stages: [], feed: [] } },
      { v: 1, from: 3, status: "done", progress: { stages: [], feed: [] } },
      { v: 1, from: "main", status: "done", progress: {} },
      { v: 1, from: "main", status: "done", progress: { stages: [] } },
    ]) {
      expect(isRunStateSnapshot(junk)).toBe(false);
    }
    expect(isRunHistorySnapshot({ v: 1, from: "main" })).toBe(false);
    expect(isRunRequest({})).toBe(false);
  });
});

describe("acceptsSnapshot", () => {
  it("takes the first one and every newer one", () => {
    expect(acceptsSnapshot(null, { v: 1, from: "main" })).toBe(true);
    expect(acceptsSnapshot({ v: 1, from: "main" }, { v: 2, from: "main" })).toBe(
      true
    );
  });

  it("cannot be rewound by a stale or duplicate delivery", () => {
    expect(acceptsSnapshot({ v: 5, from: "main" }, { v: 4, from: "main" })).toBe(
      false
    );
    expect(acceptsSnapshot({ v: 5, from: "main" }, { v: 5, from: "main" })).toBe(
      false
    );
  });

  it("yields to a different publisher", () => {
    expect(acceptsSnapshot({ v: 9, from: "main" }, { v: 1, from: "other" })).toBe(
      true
    );
  });
});

describe("mirrorRun", () => {
  const view = {
    state: null,
    history: null,
    viewingId: null,
    selectRun: () => {},
  };

  it("degrades to the local idle state when nothing has arrived", () => {
    const mirrored = mirrorRun(fakeRun(), view);
    // These four are exactly what `pages/workbench` reads to decide it has
    // nothing to show, i.e. the honest empty state rather than a spinner.
    expect(mirrored.result).toBeNull();
    expect(mirrored.status).toBe("idle");
    expect(mirrored.error).toBeNull();
    expect(mirrored.history).toEqual([]);
    expect(mirrored.elapsedMs).toBe(0);
  });

  it("puts a finished run on the bench", () => {
    const mirrored = mirrorRun(fakeRun(), { ...view, state: stateSnapshot() });
    expect(mirrored.result).toBe(RESULT);
    expect(mirrored.status).toBe("done");
    expect(mirrored.submitted?.intent).toBe("an stm32 dev board");
    expect(mirrored.elapsedMs).toBe(12_000);
    expect(mirrored.elapsedS).toBe(12);
  });

  it("shows exactly the progress the publisher reported and nothing else", () => {
    const progress = progressAfterFrames();
    const mirrored = mirrorRun(fakeRun(), {
      ...view,
      state: stateSnapshot({ status: "running", result: null, progress }),
    });

    expect(mirrored.progress).toBe(progress);
    expect(mirrored.stages).toBe(progress.stages);
    expect(mirrored.lines).toBe(progress.feed);
    // Only `place` started; nothing invented a tick for anything after it.
    expect(mirrored.stages.filter((s) => s.status === "running")).toHaveLength(1);
    expect(mirrored.stages.find((s) => s.id === "place")?.status).toBe("running");
    expect(mirrored.stages.find((s) => s.id === "review")?.status).toBe("pending");
    expect(mirrored.stages.some((s) => s.status === "done")).toBe(false);
    // No frames crossed, and the mirror says so rather than implying it saw them.
    expect(mirrored.frames).toEqual([]);
  });

  it("ticks a live run from the publisher's startedAt, not from a guess", () => {
    const mirrored = mirrorRun(fakeRun(), {
      ...view,
      state: stateSnapshot({
        status: "running",
        result: null,
        startedAt: 1_000,
        elapsedMs: 0,
      }),
      now: () => 4_500,
    });
    expect(mirrored.elapsedMs).toBe(3_500);
    expect(mirrored.startedAt).toBe(1_000);
  });

  it("rebuilds the error so the workbench can explain a failed run", () => {
    const mirrored = mirrorRun(fakeRun(), {
      ...view,
      state: stateSnapshot({
        status: "error",
        result: null,
        error: {
          kind: "offline",
          message: "Nothing is listening on that port.",
          status: 0,
          errorId: "",
          detail: "",
        },
      }),
    });
    expect(mirrored.status).toBe("error");
    expect(mirrored.result).toBeNull();
    expect(mirrored.error).toBeInstanceOf(SilkscreenError);
    expect(mirrored.error?.kind).toBe("offline");
  });

  it("keeps run control inert — the overlay owns the only in-flight guard", () => {
    const local = fakeRun({ canStart: true });
    const mirrored = mirrorRun(local, { ...view, state: stateSnapshot() });
    expect(mirrored.canStart).toBe(false);
    expect(mirrored.start).not.toBe(local.start);
    expect(() => mirrored.start()).not.toThrow();
    expect(() => mirrored.cancel()).not.toThrow();
    // Still the same run on the bench afterwards: nothing local happened.
    expect(mirrored.result).toBe(RESULT);
  });

  it("keeps the engine address and token local to this window", () => {
    const setBaseUrl = () => {};
    const local = fakeRun({ baseUrl: "http://localhost:9999", setBaseUrl });
    const mirrored = mirrorRun(local, { ...view, state: stateSnapshot() });
    expect(mirrored.baseUrl).toBe("http://localhost:9999");
    expect(mirrored.setBaseUrl).toBe(setBaseUrl);
    expect(mirrored.engine).toBe(local.engine);
  });

  it("serves the history rail, and rehydrates frames as the empty truth", () => {
    const history = toHistorySnapshot(
      [historyEntry("run-2", "a blinky"), historyEntry("run-1", "a buck converter")],
      "main",
      1
    );
    const mirrored = mirrorRun(fakeRun(), { ...view, state: stateSnapshot(), history });
    expect(mirrored.history.map((h) => h.id)).toEqual(["run-2", "run-1"]);
    expect(mirrored.history[0].frames).toEqual([]);
  });

  it("shows a selected past run instead of the live one", () => {
    const history = toHistorySnapshot(
      [historyEntry("run-2", "a blinky"), historyEntry("run-1", "a buck converter")],
      "main",
      1
    );
    const mirrored = mirrorRun(fakeRun(), {
      ...view,
      state: stateSnapshot(),
      history,
      viewingId: "run-1",
    });
    expect(mirrored.viewingHistory).toBe(true);
    expect(mirrored.status).toBe("done");
    expect(mirrored.result?.intent).toBe("a buck converter");
    expect(mirrored.submitted?.intent).toBe("a buck converter");
    expect(mirrored.elapsedMs).toBe(12_000);
    expect(mirrored.error).toBeNull();
  });
});
