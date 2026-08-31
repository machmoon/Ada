// The store against a real SQLite database, across a simulated restart.
//
// `history.store.test.ts` proves the store calls the right statements with the
// right values. This file proves the statements themselves are valid SQLite
// and that what goes in comes back out of a *different* connection to the same
// file — which is the only thing "history survives a restart" can mean, short
// of quitting the app.
//
// The database is Node's own (`node:sqlite`), driven through the same
// two-method `SqlDatabase` interface the Tauri plugin is adapted to. One
// translation is needed and is the only difference from production: the plugin
// takes `$1`-style placeholders (its documented convention for the sqlite and
// postgres drivers) and binds them positionally through sqlx, while
// `node:sqlite` binds anonymous `?` positionally, so the adapter below rewrites
// `$N` to `?` in order. Everything else — the schema, the ordering, the prune,
// the JSON round trip — is exactly what ships.
//
// The suite skips itself if `node:sqlite` is unavailable (it needs Node
// >= 22.5). CI runs Node 22.

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { initialRunProgress } from "./describe";
import { createRunHistoryStore, type SqlDatabase } from "./history.store";
import type { RunHistoryEntry } from "@/hooks/useSilkscreenRun";

type NodeSqlite = typeof import("node:sqlite");

async function loadSqlite(): Promise<NodeSqlite | null> {
  try {
    return await import("node:sqlite");
  } catch {
    return null;
  }
}

/** The `SqlDatabase` the store expects, over a real SQLite connection. */
function adapt(db: InstanceType<NodeSqlite["DatabaseSync"]>): SqlDatabase {
  // `$1, $2, …` in order becomes `?, ?, …` in the same order.
  const positional = (query: string) => query.replace(/\$\d+/g, "?");
  return {
    async execute(query, values = []) {
      return db.prepare(positional(query)).run(...(values as never[]));
    },
    async select<T>(query: string, values: unknown[] = []) {
      return db.prepare(positional(query)).all(...(values as never[])) as T;
    },
  };
}

function entry(id: string, finishedAt: number): RunHistoryEntry {
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
    result: {
      status: "feasible",
      nets: ["VCC", "GND"],
      findings: [],
      // A real run carries the whole board file; make sure a long TEXT value
      // survives the round trip rather than testing only tiny payloads.
      kicad_pcb: `(kicad_pcb (version 20240108) ${"x".repeat(20_000)})`,
    },
    frames: [{ event: "run.done", t_s: 1 }],
    progress: initialRunProgress({ review: true, route: true }),
    startedAt: finishedAt - 4200,
    finishedAt,
    elapsedS: 4.2,
  };
}

let dir: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "kaleo-history-"));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

describe("run history against real SQLite", () => {
  it("survives a restart: a new connection reads back what the last one wrote", async (ctx) => {
    const sqlite = await loadSqlite();
    if (!sqlite) return ctx.skip();
    const file = join(dir, "kaleo.db");

    // --- session one: two runs finish -------------------------------------
    const first = new sqlite.DatabaseSync(file);
    const writing = createRunHistoryStore(adapt(first));
    await writing.save(entry("run-old", 1_000_000), 8);
    await writing.save(entry("run-new", 2_000_000), 8);
    first.close();

    // --- session two: a different connection to the same file -------------
    const second = new sqlite.DatabaseSync(file);
    const reading = createRunHistoryStore(adapt(second));
    const loaded = await reading.load(8);
    second.close();

    expect(loaded.map((e) => e.id)).toEqual(["run-new", "run-old"]);

    const newest = loaded[0];
    expect(newest.intent).toBe("board run-new");
    expect(newest.finishedAt).toBe(2_000_000);
    expect(newest.elapsedS).toBe(4.2);
    // The request, including its attached datasheet, is what was submitted.
    expect(newest.request.datasheets).toEqual({
      AMS1117: "https://example.com/ams1117.pdf",
    });
    expect(newest.request.time_limit_s).toBe(25);
    expect(newest.request.ground).toBe(true);
    // The board file came back whole.
    expect(newest.result.kicad_pcb).toHaveLength(
      `(kicad_pcb (version 20240108) ${"x".repeat(20_000)})`.length
    );
    expect(newest.result.nets).toEqual(["VCC", "GND"]);
    // The engine's own stage record, not a fresh one invented on load.
    expect(newest.progress.stages.length).toBeGreaterThan(0);
    // And the two honest markers of a rehydrated run.
    expect(newest.restored).toBe(true);
    expect(newest.frames).toEqual([]);
  });

  it("prunes to the cap, so the table cannot grow without bound", async (ctx) => {
    const sqlite = await loadSqlite();
    if (!sqlite) return ctx.skip();
    const file = join(dir, "kaleo.db");

    const db = new sqlite.DatabaseSync(file);
    const store = createRunHistoryStore(adapt(db));
    for (const [id, at] of [
      ["a", 1_000],
      ["b", 2_000],
      ["c", 3_000],
      ["d", 4_000],
    ] as const) {
      await store.save(entry(id, at), 2);
    }
    db.close();

    const reopened = new sqlite.DatabaseSync(file);
    const loaded = await createRunHistoryStore(adapt(reopened)).load(8);
    reopened.close();

    expect(loaded.map((e) => e.id)).toEqual(["d", "c"]);
  });

  it("re-saving the same run updates it rather than duplicating it", async (ctx) => {
    const sqlite = await loadSqlite();
    if (!sqlite) return ctx.skip();
    const db = new sqlite.DatabaseSync(join(dir, "kaleo.db"));
    const store = createRunHistoryStore(adapt(db));

    await store.save(entry("run-1", 1_000), 8);
    await store.save(
      { ...entry("run-1", 1_000), intent: "a corrected prompt" },
      8
    );
    const loaded = await store.load(8);
    db.close();

    expect(loaded).toHaveLength(1);
    expect(loaded[0].intent).toBe("a corrected prompt");
  });

  it("clearing really empties the table, so a restart resurrects nothing", async (ctx) => {
    const sqlite = await loadSqlite();
    if (!sqlite) return ctx.skip();
    const file = join(dir, "kaleo.db");

    const db = new sqlite.DatabaseSync(file);
    const store = createRunHistoryStore(adapt(db));
    await store.save(entry("run-1", 1_000), 8);
    await store.clear();
    db.close();

    const reopened = new sqlite.DatabaseSync(file);
    const loaded = await createRunHistoryStore(adapt(reopened)).load(8);
    reopened.close();

    expect(loaded).toEqual([]);
  });

  it("a corrupted row is skipped and the sound ones still load", async (ctx) => {
    const sqlite = await loadSqlite();
    if (!sqlite) return ctx.skip();
    const file = join(dir, "kaleo.db");

    const db = new sqlite.DatabaseSync(file);
    const store = createRunHistoryStore(adapt(db));
    await store.save(entry("good", 2_000), 8);
    await store.save(entry("bad", 1_000), 8);
    // Whatever wrote it, half a run is not a run we can show.
    db.prepare("UPDATE kaleo_runs SET progress = ? WHERE id = ?").run(
      "{truncated",
      "bad"
    );

    const loaded = await store.load(8);
    db.close();

    expect(loaded.map((e) => e.id)).toEqual(["good"]);
  });
});
