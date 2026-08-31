// @vitest-environment jsdom
//
// Run history that survives a restart.
//
// The store is faked, but it is faked at the same interface the SQLite one
// implements (`RunHistoryStore`), so what these tests drive is the real
// hydrate/merge/save/clear wiring in the run hook. The assertions are the
// honesty ones: an empty store produces an empty list rather than a dressed-up
// placeholder, a restored row replays the run it actually stored, and
// `historyReady` distinguishes "nothing to show" from "not looked yet".

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));

vi.mock("@/hooks/useEngineHealth", () => ({
  useEngineHealth: () => ({
    baseUrl: "http://mock",
    ok: true,
    detail: "",
    checking: false,
    lastCheckedAt: null,
    recheck: () => {},
  }),
}));

vi.mock("@/lib/silkscreen/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/silkscreen/client")>();
  return { ...actual, generateStream: vi.fn() };
});

import { generateStream } from "@/lib/silkscreen/client";
import { initialRunProgress } from "@/lib/silkscreen/describe";
import type { RunHistoryStore } from "@/lib/silkscreen/history.store";
import type { RunResult } from "@/lib/silkscreen/types";
import {
  mergeHistory,
  useSilkscreenRunState,
  type RunHistoryEntry,
} from "./useSilkscreenRun";

const mockGenerateStream = vi.mocked(generateStream);

function stored(
  id: string,
  finishedAt: number,
  overrides: Partial<RunHistoryEntry> = {}
): RunHistoryEntry {
  return {
    id,
    intent: `board ${id}`,
    at: finishedAt,
    request: {
      intent: `board ${id}`,
      datasheets: { AMS1117: "https://example.com/ams1117.pdf" },
      time_limit_s: 25,
      review: true,
      ground: true,
      debug: false,
    },
    result: { status: "feasible", board_mm: [20, 10] },
    frames: [],
    progress: initialRunProgress({ review: true, route: true }),
    startedAt: finishedAt - 4000,
    finishedAt,
    elapsedS: 4,
    restored: true,
    ...overrides,
  };
}

/** A store that answers from an array and records every call. */
function fakeStore(rows: RunHistoryEntry[] = []) {
  let release: (() => void) | null = null;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let held = false;

  const store: RunHistoryStore & {
    saved: [RunHistoryEntry, number][];
    cleared: number;
  } = {
    saved: [],
    cleared: 0,
    load: async (limit) => {
      if (held) await gate;
      return rows.slice(0, limit);
    },
    save: async (entry, keep) => {
      store.saved.push([entry, keep]);
    },
    clear: async () => {
      store.cleared += 1;
    },
  };

  return {
    store,
    /** Make `load` hang until `release()` is called. */
    hold: () => {
      held = true;
    },
    release: () => release?.(),
  };
}

interface Driver {
  resolve: (result: RunResult) => void;
  request: () => { intent: string; datasheets?: Record<string, string> };
}

function arm(): Driver {
  let resolvePromise: ((r: RunResult) => void) | null = null;
  let captured: { intent: string; datasheets?: Record<string, string> } | null =
    null;
  mockGenerateStream.mockImplementationOnce(
    (_base, request) =>
      new Promise<RunResult>((res) => {
        captured = request;
        resolvePromise = res;
      })
  );
  return {
    resolve: (result) => resolvePromise?.(result),
    request: () => {
      if (!captured) throw new Error("generateStream was never called");
      return captured;
    },
  };
}

beforeEach(() => {
  mockGenerateStream.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("run history persistence", () => {
  it("brings stored runs back on mount, flagged as restored", async () => {
    const { store } = fakeStore([stored("run-a", 2000), stored("run-b", 1000)]);
    const hook = renderHook(() =>
      useSilkscreenRunState({ historyStore: store })
    );

    await waitFor(() => expect(hook.result.current.history).toHaveLength(2));
    expect(hook.result.current.history.map((e) => e.id)).toEqual([
      "run-a",
      "run-b",
    ]);
    expect(hook.result.current.history[0].restored).toBe(true);
    expect(hook.result.current.history[0].frames).toEqual([]);
    expect(hook.result.current.historyReady).toBe(true);
  });

  it("an empty store leaves an empty list, and says it has finished looking", async () => {
    const { store } = fakeStore([]);
    const hook = renderHook(() =>
      useSilkscreenRunState({ historyStore: store })
    );

    await waitFor(() => expect(hook.result.current.historyReady).toBe(true));
    // No placeholder row, no fabricated "previous session".
    expect(hook.result.current.history).toEqual([]);
  });

  it("does not claim to be ready before the store has answered", async () => {
    const fake = fakeStore([stored("run-a", 2000)]);
    fake.hold();
    const hook = renderHook(() =>
      useSilkscreenRunState({ historyStore: fake.store })
    );

    expect(hook.result.current.historyReady).toBe(false);
    expect(hook.result.current.history).toEqual([]);

    await act(async () => {
      fake.release();
    });
    await waitFor(() => expect(hook.result.current.historyReady).toBe(true));
    expect(hook.result.current.history).toHaveLength(1);
  });

  it("with no store configured, the session is ready immediately and stores nothing", () => {
    const hook = renderHook(() =>
      useSilkscreenRunState({ historyStore: null })
    );
    expect(hook.result.current.historyReady).toBe(true);
    expect(hook.result.current.history).toEqual([]);
  });

  it("selecting a restored run replays exactly what was stored", async () => {
    const entry = stored("run-a", 2000, {
      result: { status: "optimal", board_mm: [31, 17], nets: ["VCC", "GND"] },
    });
    const { store } = fakeStore([entry]);
    const hook = renderHook(() =>
      useSilkscreenRunState({ historyStore: store })
    );
    await waitFor(() => expect(hook.result.current.history).toHaveLength(1));

    act(() => hook.result.current.selectRun("run-a"));

    expect(hook.result.current.status).toBe("done");
    expect(hook.result.current.viewingHistory).toBe(true);
    expect(hook.result.current.result).toEqual(entry.result);
    expect(hook.result.current.submitted).toEqual(entry.request);
    expect(hook.result.current.elapsedS).toBe(4);

    act(() => hook.result.current.selectRun(null));
    expect(hook.result.current.status).toBe("idle");
    expect(hook.result.current.result).toBeNull();
  });

  it("reusing a restored run puts its prompt and datasheets back in the draft", async () => {
    const { store } = fakeStore([stored("run-a", 2000)]);
    const hook = renderHook(() =>
      useSilkscreenRunState({ historyStore: store })
    );
    await waitFor(() => expect(hook.result.current.history).toHaveLength(1));

    act(() => hook.result.current.restoreRequest("run-a"));

    expect(hook.result.current.request.intent).toBe("board run-a");
    expect(hook.result.current.request.datasheets).toEqual({
      AMS1117: "https://example.com/ams1117.pdf",
    });
    expect(hook.result.current.request.time_limit_s).toBe(25);
    // Restoring fills the form. It does not spend anything.
    expect(mockGenerateStream).not.toHaveBeenCalled();
  });

  it("stores a finished run with the request it was actually sent", async () => {
    const { store } = fakeStore([]);
    const driver = arm();
    const hook = renderHook(() =>
      useSilkscreenRunState({ historyStore: store, historyLimit: 4 })
    );
    await waitFor(() => expect(hook.result.current.historyReady).toBe(true));

    act(() =>
      hook.result.current.updateRequest({
        intent: "an LDO board",
        datasheets: { AMS1117: "https://example.com/ams1117.pdf" },
      })
    );
    act(() => hook.result.current.start());
    act(() => driver.resolve({ status: "feasible" }));

    await waitFor(() => expect(store.saved).toHaveLength(1));
    const [entry, keep] = store.saved[0];
    expect(entry.intent).toBe("an LDO board");
    expect(entry.request.datasheets).toEqual({
      AMS1117: "https://example.com/ams1117.pdf",
    });
    expect(entry.result).toEqual({ status: "feasible" });
    expect(entry.restored).toBeUndefined();
    // Pruned to the same cap the in-memory list uses; boards are large.
    expect(keep).toBe(4);
  });

  it("a run that finishes during hydration keeps its place at the top", async () => {
    const fake = fakeStore([stored("old", 1_000)]);
    fake.hold();
    const driver = arm();
    const hook = renderHook(() =>
      useSilkscreenRunState({ historyStore: fake.store })
    );

    act(() => hook.result.current.start({ intent: "a fresh board" }));
    act(() => driver.resolve({ status: "feasible" }));
    await waitFor(() => expect(hook.result.current.status).toBe("done"));
    expect(hook.result.current.history).toHaveLength(1);

    await act(async () => {
      fake.release();
    });
    await waitFor(() => expect(hook.result.current.history).toHaveLength(2));

    // Newest first, and the live run kept the frames the stored one never had.
    expect(hook.result.current.history[0].intent).toBe("a fresh board");
    expect(hook.result.current.history[0].restored).toBeUndefined();
    expect(hook.result.current.history[1].id).toBe("old");
  });

  it("clearing history clears the store, so a restart does not resurrect it", async () => {
    const { store } = fakeStore([stored("run-a", 2000)]);
    const hook = renderHook(() =>
      useSilkscreenRunState({ historyStore: store })
    );
    await waitFor(() => expect(hook.result.current.history).toHaveLength(1));

    act(() => hook.result.current.clearHistory());

    expect(hook.result.current.history).toEqual([]);
    await waitFor(() => expect(store.cleared).toBe(1));
  });

  it("a store that throws never breaks the run that produced the board", async () => {
    const angry: RunHistoryStore = {
      load: async () => {
        throw new Error("database is locked");
      },
      save: async () => {
        throw new Error("database is locked");
      },
      clear: async () => {
        throw new Error("database is locked");
      },
    };
    const driver = arm();
    const hook = renderHook(() =>
      useSilkscreenRunState({ historyStore: angry })
    );

    act(() => hook.result.current.start({ intent: "a board" }));
    act(() => driver.resolve({ status: "feasible" }));

    await waitFor(() => expect(hook.result.current.status).toBe("done"));
    expect(hook.result.current.history).toHaveLength(1);
    expect(hook.result.current.error).toBeNull();

    // Every store call is caught at the call site, so none of the three
    // rejections above escapes as an unhandled promise.
    act(() => hook.result.current.clearHistory());
    expect(hook.result.current.history).toEqual([]);
  });
});

describe("mergeHistory", () => {
  it("keeps the in-memory copy of a run that is also on disk", () => {
    const live = stored("dup", 5_000, { restored: undefined, frames: [{ event: "run.done" }] });
    const merged = mergeHistory([live], [stored("dup", 5_000)], 8);
    expect(merged).toHaveLength(1);
    expect(merged[0].frames).toHaveLength(1);
  });

  it("orders by finish time and honours the cap", () => {
    const merged = mergeHistory(
      [stored("b", 2_000)],
      [stored("c", 3_000), stored("a", 1_000)],
      2
    );
    expect(merged.map((e) => e.id)).toEqual(["c", "b"]);
  });
});
