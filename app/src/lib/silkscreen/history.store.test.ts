// The persisted-run store, driven against a fake database.
//
// The SQL text is the thing worth testing and it should not need a Tauri host
// to exercise, so `createRunHistoryStore` takes the two-method `SqlDatabase`
// interface and these tests hand it one. The assertions this file exists for
// are the honesty ones: a row that cannot be read back faithfully is dropped
// rather than patched up with defaults, and every failure path degrades to a
// no-op instead of throwing into a run.

import { describe, expect, it, vi } from "vitest";

import { initialRunProgress } from "./describe";
import {
  HISTORY_TABLE,
  NULL_HISTORY_STORE,
  SCHEMA_STATEMENTS,
  createRunHistoryStore,
  entryToRow,
  hasTauriHost,
  openHistoryDatabase,
  rowToEntry,
  sharedRunHistoryStore,
  type SqlDatabase,
} from "./history.store";
import type { RunHistoryEntry } from "@/hooks/useSilkscreenRun";

function entry(overrides: Partial<RunHistoryEntry> = {}): RunHistoryEntry {
  return {
    id: "run-1",
    intent: "a 3.3V LDO board",
    at: 1_700_000_100_000,
    request: {
      intent: "a 3.3V LDO board",
      datasheets: { AMS1117: "https://example.com/ams1117.pdf" },
      time_limit_s: 20,
      review: true,
      ground: true,
      debug: false,
    },
    result: { board_mm: [20, 10], status: "feasible", findings: [] },
    frames: [{ event: "run.done", t_s: 4 }],
    progress: initialRunProgress({ review: true, route: true }),
    startedAt: 1_700_000_096_000,
    finishedAt: 1_700_000_100_000,
    elapsedS: 4,
    ...overrides,
  };
}

/** A database that records every statement and answers `select` from a queue. */
function fakeDb(rows: unknown[] = []) {
  // Typed parameters, not `async () => …`: without them the mock's call tuple
  // infers as empty and every `call[0]` below is a type error.
  const execute = vi.fn(async (_query: string, _values?: unknown[]) => ({
    rowsAffected: 1,
  }));
  const select = vi.fn(async (_query: string, _values?: unknown[]) => rows);
  return {
    db: { execute, select } as unknown as SqlDatabase,
    execute,
    select,
    /** Every statement passed to `execute`, whitespace flattened. */
    statements: () =>
      execute.mock.calls.map((call) =>
        String(call[0]).replace(/\s+/g, " ").trim()
      ),
  };
}

/** The row shape SQLite hands back for a stored entry. */
function row(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  const source = entry();
  const values = entryToRow(source);
  return {
    id: values[0],
    intent: values[1],
    at_ms: values[2],
    started_at: values[3],
    finished_at: values[4],
    elapsed_s: values[5],
    request: values[6],
    result: values[7],
    progress: values[8],
    ...overrides,
  };
}

describe("rowToEntry", () => {
  it("round-trips an entry written by entryToRow", () => {
    const source = entry();
    const restored = rowToEntry(row());

    expect(restored).not.toBeNull();
    expect(restored?.id).toBe(source.id);
    expect(restored?.intent).toBe(source.intent);
    expect(restored?.at).toBe(source.at);
    expect(restored?.startedAt).toBe(source.startedAt);
    expect(restored?.finishedAt).toBe(source.finishedAt);
    expect(restored?.elapsedS).toBe(source.elapsedS);
    expect(restored?.request).toEqual(source.request);
    expect(restored?.result).toEqual(source.result);
    expect(restored?.progress.stages).toHaveLength(
      source.progress.stages.length
    );
  });

  it("marks the entry restored and reports no frames, because it has none", () => {
    const restored = rowToEntry(row());
    expect(restored?.restored).toBe(true);
    // Not "not loaded yet" — this process genuinely received no stream frames.
    expect(restored?.frames).toEqual([]);
  });

  it("drops a row whose progress blob is not the shape the UI reads", () => {
    // Rebuilding it from initialRunProgress() would draw a finished run as a
    // checklist of stages that never ticked, which is a picture of a run that
    // did not happen.
    expect(rowToEntry(row({ progress: JSON.stringify({ stages: 3 }) }))).toBeNull();
    expect(rowToEntry(row({ progress: "{not json" }))).toBeNull();
  });

  it("drops a row whose result or request cannot be parsed", () => {
    expect(rowToEntry(row({ result: "[]" }))).toBeNull();
    expect(rowToEntry(row({ result: "{oops" }))).toBeNull();
    expect(rowToEntry(row({ request: JSON.stringify({ nope: 1 }) }))).toBeNull();
  });

  it("drops a row with a missing id or a non-numeric clock", () => {
    expect(rowToEntry(row({ id: "" }))).toBeNull();
    expect(rowToEntry(row({ id: 7 }))).toBeNull();
    expect(rowToEntry(row({ finished_at: "yesterday" }))).toBeNull();
    expect(rowToEntry(row({ elapsed_s: null }))).toBeNull();
  });

  it("falls back to the stored request's intent when the column is absent", () => {
    const restored = rowToEntry(row({ intent: null }));
    expect(restored?.intent).toBe("a 3.3V LDO board");
  });
});

describe("createRunHistoryStore", () => {
  it("creates the table on first use and not again", async () => {
    const fake = fakeDb();
    const store = createRunHistoryStore(fake.db);

    await store.save(entry(), 8);
    await store.save(entry({ id: "run-2" }), 8);

    const creates = fake
      .statements()
      .filter((sql) => sql.startsWith("CREATE"));
    expect(creates).toHaveLength(SCHEMA_STATEMENTS.length);
    expect(creates[0]).toContain(`CREATE TABLE IF NOT EXISTS ${HISTORY_TABLE}`);
  });

  it("writes the run's own request and result, then prunes to the cap", async () => {
    const fake = fakeDb();
    const store = createRunHistoryStore(fake.db);

    await store.save(entry(), 5);

    const insert = fake.execute.mock.calls.find((call) =>
      String(call[0]).includes("INSERT")
    );
    expect(insert).toBeDefined();
    const values = insert?.[1] as unknown[];
    expect(values[0]).toBe("run-1");
    // The attached datasheet is part of what a stored run remembers.
    expect(JSON.parse(String(values[6])).datasheets).toEqual({
      AMS1117: "https://example.com/ams1117.pdf",
    });
    expect(JSON.parse(String(values[7])).status).toBe("feasible");

    const prune = fake.execute.mock.calls.find((call) =>
      String(call[0]).includes("DELETE FROM")
    );
    expect(String(prune?.[0])).toContain("ORDER BY finished_at DESC LIMIT");
    expect(prune?.[1]).toEqual([5]);
  });

  it("loads newest first, capped, and skips rows it cannot trust", async () => {
    const fake = fakeDb([
      row(),
      row({ id: "run-2", progress: "{corrupt" }),
      row({ id: "run-3" }),
    ]);
    const store = createRunHistoryStore(fake.db);

    const loaded = await store.load(8);

    expect(loaded.map((e) => e.id)).toEqual(["run-1", "run-3"]);
    expect(String(fake.select.mock.calls[0][0])).toContain(
      "ORDER BY finished_at DESC"
    );
    expect(fake.select.mock.calls[0][1]).toEqual([8]);
  });

  it("returns an empty list rather than inventing rows when the table is empty", async () => {
    const store = createRunHistoryStore(fakeDb([]).db);
    expect(await store.load(8)).toEqual([]);
  });

  it("clear issues a delete over the whole table", async () => {
    const fake = fakeDb();
    await createRunHistoryStore(fake.db).clear();
    expect(fake.statements()).toContain(`DELETE FROM ${HISTORY_TABLE}`);
  });

  it("never throws: a broken database degrades to no history and no error", async () => {
    const broken: SqlDatabase = {
      execute: vi.fn(async () => {
        throw new Error("database is locked");
      }),
      select: vi.fn(async () => {
        throw new Error("database is locked");
      }),
    };
    const store = createRunHistoryStore(broken);

    await expect(store.load(8)).resolves.toEqual([]);
    // A board the engine really produced must not become an error here.
    await expect(store.save(entry(), 8)).resolves.toBeUndefined();
    await expect(store.clear()).resolves.toBeUndefined();
  });

  it("retries the schema after a failed create instead of caching the failure", async () => {
    let fail = true;
    const execute = vi.fn(async (_query: string, _values?: unknown[]) => {
      if (fail) throw new Error("no such database");
      return { rowsAffected: 1 };
    });
    const store = createRunHistoryStore({
      execute,
      select: vi.fn(async (_query: string, _values?: unknown[]) => []),
    } as unknown as SqlDatabase);

    await store.load(8);
    fail = false;
    await store.save(entry(), 8);

    expect(
      execute.mock.calls.filter((call) => String(call[0]).startsWith("CREATE"))
    ).not.toHaveLength(0);
    expect(
      execute.mock.calls.some((call) => String(call[0]).includes("INSERT"))
    ).toBe(true);
  });
});

describe("no Tauri host", () => {
  it("reports no host, opens no database, and offers no store", async () => {
    expect(hasTauriHost()).toBe(false);
    expect(await openHistoryDatabase()).toBeNull();
    // null, not a store that silently swallows writes: the run hook shows the
    // difference between "nothing stored" and "not looked yet".
    expect(sharedRunHistoryStore()).toBeNull();
  });

  it("the null store answers empty and accepts writes without effect", async () => {
    expect(await NULL_HISTORY_STORE.load(8)).toEqual([]);
    await expect(NULL_HISTORY_STORE.save(entry(), 8)).resolves.toBeUndefined();
    await expect(NULL_HISTORY_STORE.clear()).resolves.toBeUndefined();
  });
});
