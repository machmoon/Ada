// @vitest-environment jsdom
//
// The bridge across two windows, driven through a fake event bus that behaves
// like Tauri's: `emit` reaches every listener including the sender's own
// window, and every payload is JSON round-tripped on the way, because the real
// IPC serializes it. Anything that would not survive the wire fails here.
//
// The case that matters most is the last one a fire-and-forget broadcast would
// get wrong: a workbench opened *after* the run already finished.

import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));

import {
  RUN_HISTORY_EVENT,
  RUN_REQUEST_EVENT,
  RUN_STATE_EVENT,
  NAVIGATE_EVENT,
  type BridgeTransport,
} from "@/lib/silkscreen/bridge";
import {
  initialRunProgress,
  reduceFrame,
  type RunProgress,
} from "@/lib/silkscreen/describe";
import type { RunResult } from "@/lib/silkscreen/types";
import { EMPTY_REQUEST, type RunHistoryEntry, type SilkscreenRun } from "./useSilkscreenRun";
import { useNavigationRequests, useRunBridge } from "./useRunBridge";

const RESULT: RunResult = {
  intent: "an stm32 dev board",
  kicad_pcb: "(kicad_pcb (version 20240108))",
  board_mm: [40, 30],
  status: "feasible",
  findings: [{ severity: "blocker", title: "no decoupling on U1" }],
  nets: ["VCC", "GND"],
};

interface LogLine {
  op: "emit" | "listen";
  event: string;
  from: string;
  payload?: unknown;
}

/**
 * Tauri's event bus, as far as this bridge can tell it apart from the real
 * one: a broadcast `emit` that also reaches the emitting window, and a
 * serializing hop.
 */
interface Registration {
  handler: (payload: unknown) => void;
  /** The window this listener asked to be addressed as, if any. */
  target?: string;
}

class Bus {
  listeners = new Map<string, Registration[]>();
  log: LogLine[] = [];

  private deliver(event: string, payload: unknown, to?: string) {
    const delivered = JSON.parse(JSON.stringify(payload ?? null));
    for (const entry of [...(this.listeners.get(event) ?? [])]) {
      // Tauri's rule, reproduced: an addressed emit reaches a listener whose
      // target matches — and also any listener that registered no target at
      // all, because `Any` short-circuits the address filter. A broadcast
      // reaches everything.
      if (to !== undefined && entry.target !== undefined && entry.target !== to) {
        continue;
      }
      entry.handler(delivered);
    }
  }

  /** What Rust's `app.emit_to(label, ...)` does. */
  async emitTo(to: string, event: string, payload: unknown) {
    this.log.push({ op: "emit", event, from: `rust->${to}`, payload });
    this.deliver(event, payload, to);
  }

  transportFor(label: string): BridgeTransport {
    return {
      label,
      emit: async (event, payload) => {
        this.log.push({ op: "emit", event, from: label, payload });
        // The IPC serializes; a payload holding a class instance or a function
        // would arrive mangled, so make the test suffer the same trip.
        this.deliver(event, payload);
      },
      listen: async (event, handler, options) => {
        this.log.push({ op: "listen", event, from: label });
        const handlers = this.listeners.get(event) ?? [];
        const entry: Registration = { handler, target: options?.target };
        handlers.push(entry);
        this.listeners.set(event, handlers);
        return () => {
          const current = this.listeners.get(event) ?? [];
          const at = current.indexOf(entry);
          if (at >= 0) current.splice(at, 1);
        };
      },
    };
  }

  /** Index of the first matching log line, or -1. */
  indexOf(op: LogLine["op"], event: string, from: string): number {
    return this.log.findIndex(
      (line) => line.op === op && line.event === event && line.from === from
    );
  }

  payloadsOn(event: string): unknown[] {
    return this.log
      .filter((line) => line.op === "emit" && line.event === event)
      .map((line) => line.payload);
  }
}

const noop = () => {};

function midRunProgress(): RunProgress {
  let progress = initialRunProgress({ review: true, route: true });
  progress = reduceFrame(progress, { event: "run.accepted", t_s: 0 });
  progress = reduceFrame(progress, { event: "stage.start", stage: "place", t_s: 1 });
  return progress;
}

function doneProgress(): RunProgress {
  let progress = midRunProgress();
  progress = reduceFrame(progress, { event: "stage.done", stage: "place", t_s: 3 });
  progress = reduceFrame(progress, { event: "run.done", t_s: 4, result: RESULT });
  return progress;
}

function fakeRun(overrides: Partial<SilkscreenRun> = {}): SilkscreenRun {
  const progress = overrides.progress ?? initialRunProgress();
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
    progress,
  };
}

/** The overlay's state the moment a run has landed. */
function finishedRun(): SilkscreenRun {
  return fakeRun({
    status: "done",
    result: RESULT,
    progress: doneProgress(),
    submitted: { ...EMPTY_REQUEST, intent: "an stm32 dev board", review: true },
    startedAt: 1_700_000_000_000,
    elapsedMs: 12_000,
    elapsedS: 12,
  });
}

function historyEntry(id: string, intent: string): RunHistoryEntry {
  return {
    id,
    intent,
    at: 1_700_000_000_000,
    request: { ...EMPTY_REQUEST, intent },
    result: { ...RESULT, intent },
    frames: [{ event: "run.accepted", t_s: 0 }],
    progress: doneProgress(),
    startedAt: 1_700_000_000_000,
    finishedAt: 1_700_000_012_000,
    elapsedS: 12,
  };
}

function mountPublisher(bus: Bus, run: SilkscreenRun) {
  const transport = bus.transportFor("main");
  return renderHook(
    ({ current }: { current: SilkscreenRun }) =>
      useRunBridge(current, { role: "publisher", transport }),
    { initialProps: { current: run } }
  );
}

function mountMirror(bus: Bus, transport?: BridgeTransport | null) {
  const t = transport === undefined ? bus.transportFor("dashboard") : transport;
  return renderHook(() =>
    useRunBridge(fakeRun(), { role: "mirror", transport: t })
  );
}

let bus: Bus;
beforeEach(() => {
  bus = new Bus();
});

describe("the late subscriber", () => {
  it("gets the finished board when the workbench opens after the run ended", async () => {
    // The whole run happens with nobody listening.
    const publisher = mountPublisher(bus, fakeRun());
    await act(async () => {
      publisher.rerender({ current: finishedRun() });
    });

    // Only now does the user open the review window.
    const mirror = mountMirror(bus);

    await waitFor(() => {
      expect(mirror.result.current.result).toEqual(RESULT);
    });
    expect(mirror.result.current.status).toBe("done");
    expect(mirror.result.current.submitted?.intent).toBe("an stm32 dev board");
  });

  it("asks only after both listeners are registered, so the reply cannot race it", async () => {
    mountPublisher(bus, finishedRun());
    mountMirror(bus);

    await waitFor(() => {
      expect(bus.indexOf("emit", RUN_REQUEST_EVENT, "dashboard")).toBeGreaterThan(-1);
    });

    const asked = bus.indexOf("emit", RUN_REQUEST_EVENT, "dashboard");
    const listeningToState = bus.indexOf("listen", RUN_STATE_EVENT, "dashboard");
    const listeningToHistory = bus.indexOf("listen", RUN_HISTORY_EVENT, "dashboard");
    expect(listeningToState).toBeGreaterThan(-1);
    expect(listeningToHistory).toBeGreaterThan(-1);
    expect(asked).toBeGreaterThan(listeningToState);
    expect(asked).toBeGreaterThan(listeningToHistory);
  });

  it("gets the run history it never saw arrive", async () => {
    const publisher = mountPublisher(bus, fakeRun());
    await act(async () => {
      publisher.rerender({
        current: fakeRun({
          ...finishedRun(),
          history: [historyEntry("run-2", "a blinky"), historyEntry("run-1", "a buck")],
        }),
      });
    });

    const mirror = mountMirror(bus);
    await waitFor(() => {
      expect(mirror.result.current.history).toHaveLength(2);
    });
    expect(mirror.result.current.history.map((h) => h.id)).toEqual([
      "run-2",
      "run-1",
    ]);

    // And the rail can switch the bench to a past run.
    act(() => mirror.result.current.selectRun("run-1"));
    await waitFor(() => {
      expect(mirror.result.current.result?.intent).toBe("a buck");
    });
    expect(mirror.result.current.viewingHistory).toBe(true);
  });
});

describe("a mirror that was already open", () => {
  it("receives the run as it happens", async () => {
    const mirror = mountMirror(bus);
    const publisher = mountPublisher(bus, fakeRun());

    await act(async () => {
      publisher.rerender({
        current: fakeRun({ status: "running", progress: midRunProgress() }),
      });
    });
    await waitFor(() => expect(mirror.result.current.status).toBe("running"));
    expect(mirror.result.current.result).toBeNull();

    await act(async () => {
      publisher.rerender({ current: finishedRun() });
    });
    await waitFor(() => expect(mirror.result.current.result).toEqual(RESULT));
  });

  it("shows only the progress the engine actually reported", async () => {
    const publisher = mountPublisher(bus, fakeRun());
    await act(async () => {
      publisher.rerender({
        current: fakeRun({ status: "running", progress: midRunProgress() }),
      });
    });

    // Opened mid-run, having missed every frame so far.
    const mirror = mountMirror(bus);
    await waitFor(() => expect(mirror.result.current.status).toBe("running"));

    const stages = mirror.result.current.stages;
    expect(stages.find((s) => s.id === "place")?.status).toBe("running");
    // Nothing after the running stage is ticked, and nothing before it is
    // invented: `read` is skipped because no datasheet was supplied, and the
    // rest are still pending. The mirror reports the record, not a guess.
    expect(stages.find((s) => s.id === "review")?.status).toBe("pending");
    expect(stages.find((s) => s.id === "route")?.status).toBe("pending");
    expect(stages.some((s) => s.status === "done")).toBe(false);
    // No stream frames crossed the bridge, and the mirror does not pretend so.
    expect(mirror.result.current.frames).toEqual([]);
  });
});

describe("fail-safe", () => {
  it("degrades to the honest empty bench when there is no transport", async () => {
    const mirror = mountMirror(bus, null);
    // Give any effect that was going to fire the chance to.
    await act(async () => {});

    expect(mirror.result.current.result).toBeNull();
    expect(mirror.result.current.status).toBe("idle");
    expect(mirror.result.current.error).toBeNull();
    expect(mirror.result.current.history).toEqual([]);
    expect(bus.log).toHaveLength(0);
  });

  it("leaves the run untouched where there is no Tauri host at all", async () => {
    // `transport` omitted: the hook probes for a Tauri webview, finds none, and
    // resolves the role to "off". This is what a plain browser build does.
    const local = fakeRun();
    const { result } = renderHook(() => useRunBridge(local));
    await act(async () => {});
    expect(result.current).toBe(local);
  });

  it("keeps run control inert in the mirror", async () => {
    const publisher = mountPublisher(bus, fakeRun());
    await act(async () => {
      publisher.rerender({ current: finishedRun() });
    });
    const mirror = mountMirror(bus);
    await waitFor(() => expect(mirror.result.current.result).toEqual(RESULT));

    expect(mirror.result.current.canStart).toBe(false);
    act(() => mirror.result.current.start());
    act(() => mirror.result.current.cancel());
    // A second paid run for one prompt is the thing this prevents; nothing
    // moved, and the finished board is still on the bench.
    expect(mirror.result.current.result).toEqual(RESULT);
    expect(bus.payloadsOn(RUN_REQUEST_EVENT)).toHaveLength(1);
  });
});

describe("hostile and stale traffic", () => {
  it("ignores a snapshot that would rewind the bench", async () => {
    const publisher = mountPublisher(bus, fakeRun());
    await act(async () => {
      publisher.rerender({ current: finishedRun() });
    });
    const mirror = mountMirror(bus);
    await waitFor(() => expect(mirror.result.current.result).toEqual(RESULT));

    await act(async () => {
      await bus.transportFor("main").emit(RUN_STATE_EVENT, {
        v: 0,
        from: "main",
        status: "idle",
        submitted: null,
        progress: initialRunProgress(),
        result: null,
        error: null,
        startedAt: null,
        elapsedMs: 0,
      });
    });

    expect(mirror.result.current.result).toEqual(RESULT);
    expect(mirror.result.current.status).toBe("done");
  });

  it("ignores malformed payloads rather than half-applying them", async () => {
    const publisher = mountPublisher(bus, fakeRun());
    await act(async () => {
      publisher.rerender({ current: finishedRun() });
    });
    const mirror = mountMirror(bus);
    await waitFor(() => expect(mirror.result.current.result).toEqual(RESULT));

    const main = bus.transportFor("main");
    for (const junk of [null, "run.done", 42, {}, { v: 99, from: "main" }]) {
      await act(async () => {
        await main.emit(RUN_STATE_EVENT, junk);
      });
    }
    await act(async () => {
      await main.emit(RUN_HISTORY_EVENT, { v: 99, from: "main" });
    });

    expect(mirror.result.current.result).toEqual(RESULT);
    expect(mirror.result.current.history).toEqual([]);
  });

  it("ignores its own echo", async () => {
    const mirror = mountMirror(bus);
    await act(async () => {});

    await act(async () => {
      await bus.transportFor("dashboard").emit(RUN_STATE_EVENT, {
        v: 999,
        from: "dashboard",
        status: "done",
        submitted: null,
        progress: doneProgress(),
        result: RESULT,
        error: null,
        startedAt: 1,
        elapsedMs: 1,
      });
    });

    expect(mirror.result.current.result).toBeNull();
    expect(mirror.result.current.status).toBe("idle");
  });
});

describe("what goes over the wire", () => {
  it("keeps stored boards off the live-progress event", async () => {
    const publisher = mountPublisher(bus, fakeRun());
    await act(async () => {
      publisher.rerender({
        current: fakeRun({
          status: "running",
          progress: midRunProgress(),
          history: [historyEntry("run-1", "a buck")],
        }),
      });
    });

    for (const payload of bus.payloadsOn(RUN_STATE_EVENT)) {
      expect(payload).not.toHaveProperty("history");
      expect(payload).not.toHaveProperty("entries");
    }
    // History travels on its own event, which only fires when it changes.
    expect(bus.payloadsOn(RUN_HISTORY_EVENT).length).toBeGreaterThan(0);
  });

  it("survives the IPC round trip with the engine's shapes intact", async () => {
    const publisher = mountPublisher(bus, fakeRun());
    await act(async () => {
      publisher.rerender({ current: finishedRun() });
    });
    const mirror = mountMirror(bus);
    await waitFor(() => expect(mirror.result.current.result).toEqual(RESULT));

    // Not just present — the same fields the review surface reads.
    const result = mirror.result.current.result;
    expect(result?.kicad_pcb).toBe(RESULT.kicad_pcb);
    expect(result?.board_mm).toEqual([40, 30]);
    expect(result?.findings?.[0].severity).toBe("blocker");
    expect(result?.nets).toEqual(["VCC", "GND"]);
    // And the progress arrived as a real RunProgress, not a husk.
    expect(mirror.result.current.stages.length).toBeGreaterThan(0);
    expect(mirror.result.current.lines.length).toBeGreaterThan(0);
  });
});

describe("useNavigationRequests", () => {
  it("routes the window where the overlay asked", async () => {
    const seen: string[] = [];
    const transport = bus.transportFor("dashboard");
    renderHook(() => useNavigationRequests((p) => seen.push(p), { transport }));
    await waitFor(() =>
      expect(bus.indexOf("listen", NAVIGATE_EVENT, "dashboard")).toBeGreaterThan(-1)
    );

    // What `open_dashboard` in window.rs does.
    await act(async () => {
      await bus.emitTo("dashboard", NAVIGATE_EVENT, "/workbench");
    });
    expect(seen).toEqual(["/workbench"]);
  });

  it("never navigates the overlay, whose route is its whole UI", async () => {
    const overlaySaw: string[] = [];
    const benchSaw: string[] = [];
    renderHook(() =>
      useNavigationRequests((p) => overlaySaw.push(p), {
        transport: bus.transportFor("main"),
      })
    );
    renderHook(() =>
      useNavigationRequests((p) => benchSaw.push(p), {
        transport: bus.transportFor("dashboard"),
      })
    );
    await waitFor(() =>
      expect(bus.indexOf("listen", NAVIGATE_EVENT, "dashboard")).toBeGreaterThan(-1)
    );

    // The publisher window does not even register: `emit_to` alone would not
    // save it, since a default-target listener receives addressed events too.
    expect(bus.indexOf("listen", NAVIGATE_EVENT, "main")).toBe(-1);

    await act(async () => {
      await bus.emitTo("dashboard", NAVIGATE_EVENT, "/workbench");
    });
    expect(benchSaw).toEqual(["/workbench"]);
    expect(overlaySaw).toEqual([]);
  });

  it("refuses anything that is not an in-app route", async () => {
    const seen: string[] = [];
    const transport = bus.transportFor("dashboard");
    renderHook(() => useNavigationRequests((p) => seen.push(p), { transport }));
    await waitFor(() =>
      expect(bus.indexOf("listen", NAVIGATE_EVENT, "dashboard")).toBeGreaterThan(-1)
    );

    const main = bus.transportFor("main");
    for (const junk of [
      null,
      42,
      { path: "/workbench" },
      "workbench",
      "https://example.com",
      "//example.com/workbench",
    ]) {
      await act(async () => {
        await main.emit(NAVIGATE_EVENT, junk);
      });
    }
    expect(seen).toEqual([]);
  });

  it("does nothing without a transport", async () => {
    const seen: string[] = [];
    renderHook(() =>
      useNavigationRequests((p) => seen.push(p), { transport: null })
    );
    await act(async () => {});
    expect(bus.log).toHaveLength(0);
    expect(seen).toEqual([]);
  });
});
