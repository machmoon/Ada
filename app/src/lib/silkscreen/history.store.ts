// Where finished runs live between restarts.
//
// The run hook already keeps a session's runs in memory. This is the half that
// survives quitting the app: one table in the SQLite database Tauri already
// opens for this app (`sqlite:kaleo.db`, registered in `src-tauri/src/lib.rs`).
//
// Three rules shaped it, and every one of them is a rule about not lying:
//
// 1. **Only real rows come back.** `load` returns what the database actually
//    holds. A row whose JSON will not parse, or whose stored progress is not
//    the shape the UI reads, is DROPPED rather than patched up — a history
//    entry rebuilt from `initialRunProgress()` would show a finished run as a
//    list of pending stages that never ticked, which is a picture of something
//    that did not happen. An unreadable row is not a run we can show.
//
// 2. **Restored runs say so.** `frames` is not persisted (a run is up to 2000
//    stream frames, nothing on the review surface reads them, and the
//    cross-window bridge already drops them for the same reason). So a
//    rehydrated entry carries `frames: []`, which is the truth — this process
//    received no frames — and `restored: true` so the UI can label the row
//    instead of implying the raw log is still there.
//
// 3. **Absence is not an error.** With no Tauri host, no `sql` capability, or
//    no database, every method degrades to a no-op and the session simply has
//    no persisted history. Nothing here may throw into a run: a failed write
//    must not turn a board the engine successfully produced into an error.

import type { RunProgress } from "./describe";
import { logError } from "./log";
import type { RunResult } from "./types";
import type { RunHistoryEntry, RunRequestDraft } from "@/hooks/useSilkscreenRun";

/** The database Tauri registers migrations for; see `src-tauri/src/lib.rs`. */
export const HISTORY_DB = "sqlite:kaleo.db";

export const HISTORY_TABLE = "kaleo_runs";

/**
 * The table, created on first use rather than by a Rust migration.
 *
 * `CREATE TABLE IF NOT EXISTS` is idempotent and needs only the `sql:execute`
 * permission the app already grants, so a build of the Rust side is not a
 * prerequisite for the feature working. The columns the UI sorts and prunes on
 * are real columns; everything with structure is JSON in a TEXT column,
 * because the shapes it holds (`RunResult`, `RunProgress`) are the engine's to
 * change and re-modelling them in SQL would make every engine addition a
 * migration here.
 */
export const SCHEMA_STATEMENTS: string[] = [
  `CREATE TABLE IF NOT EXISTS ${HISTORY_TABLE} (
     id TEXT PRIMARY KEY,
     intent TEXT NOT NULL,
     at_ms INTEGER NOT NULL,
     started_at INTEGER NOT NULL,
     finished_at INTEGER NOT NULL,
     elapsed_s REAL NOT NULL,
     request TEXT NOT NULL,
     result TEXT NOT NULL,
     progress TEXT NOT NULL
   )`,
  `CREATE INDEX IF NOT EXISTS ${HISTORY_TABLE}_finished_at
     ON ${HISTORY_TABLE} (finished_at DESC)`,
];

const INSERT = `INSERT OR REPLACE INTO ${HISTORY_TABLE}
  (id, intent, at_ms, started_at, finished_at, elapsed_s, request, result, progress)
  VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)`;

/**
 * Keep the newest `n` rows and drop the rest.
 *
 * Runs carry a whole `.kicad_pcb` each, so an unbounded table would grow
 * without limit for a feature whose UI only ever shows a handful.
 */
const PRUNE = `DELETE FROM ${HISTORY_TABLE} WHERE id NOT IN (
  SELECT id FROM ${HISTORY_TABLE} ORDER BY finished_at DESC LIMIT $1
)`;

const SELECT = `SELECT id, intent, at_ms, started_at, finished_at, elapsed_s,
  request, result, progress
  FROM ${HISTORY_TABLE} ORDER BY finished_at DESC LIMIT $1`;

const CLEAR = `DELETE FROM ${HISTORY_TABLE}`;

/**
 * The two calls this module makes, narrowed from `@tauri-apps/plugin-sql`'s
 * `Database`. An interface rather than the class so the whole store is
 * testable with no Tauri host, and so "there is no database" is a value
 * (`null`) rather than an exception thrown from inside an effect.
 */
export interface SqlDatabase {
  execute(query: string, values?: unknown[]): Promise<unknown>;
  select<T>(query: string, values?: unknown[]): Promise<T>;
}

export interface RunHistoryStore {
  /** Newest first, at most `limit`. Returns `[]` when there is nothing stored. */
  load(limit: number): Promise<RunHistoryEntry[]>;
  /** Persist one finished run, then prune the table down to `keep` rows. */
  save(entry: RunHistoryEntry, keep: number): Promise<void>;
  clear(): Promise<void>;
}

/** One row as SQLite hands it back. Every field is checked before use. */
interface HistoryRow {
  id?: unknown;
  intent?: unknown;
  at_ms?: unknown;
  started_at?: unknown;
  finished_at?: unknown;
  elapsed_s?: unknown;
  request?: unknown;
  result?: unknown;
  progress?: unknown;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

function parseJson(value: unknown): unknown {
  if (typeof value !== "string") return undefined;
  try {
    return JSON.parse(value);
  } catch {
    return undefined;
  }
}

/**
 * Is this the `RunProgress` the review surface reads?
 *
 * Only the two fields the UI iterates are required. The check exists so a
 * corrupt or older blob is dropped by {@link rowToEntry} instead of reaching a
 * component that would render a finished run as a list of stages that never
 * started.
 */
function isProgress(value: unknown): value is RunProgress {
  return (
    isRecord(value) && Array.isArray(value.stages) && Array.isArray(value.feed)
  );
}

function isRequest(value: unknown): value is RunRequestDraft {
  return isRecord(value) && typeof value.intent === "string";
}

const num = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) ? value : null;

/**
 * One stored row as a history entry, or `null` if the row cannot be trusted.
 *
 * Returning `null` is the point of this function: the caller drops the row.
 * There is deliberately no path here that substitutes a default for something
 * the database did not actually contain.
 */
export function rowToEntry(row: HistoryRow): RunHistoryEntry | null {
  if (typeof row?.id !== "string" || !row.id) return null;

  const at = num(row.at_ms);
  const startedAt = num(row.started_at);
  const finishedAt = num(row.finished_at);
  const elapsedS = num(row.elapsed_s);
  if (at === null || startedAt === null || finishedAt === null) return null;
  if (elapsedS === null) return null;

  const request = parseJson(row.request);
  const result = parseJson(row.result);
  const progress = parseJson(row.progress);
  if (!isRequest(request)) return null;
  if (!isRecord(result)) return null;
  if (!isProgress(progress)) return null;

  return {
    id: row.id,
    intent: typeof row.intent === "string" ? row.intent : request.intent,
    at,
    request,
    result: result as RunResult,
    // Honest, not lossy-by-accident: this process received no stream frames
    // for a run it did not watch happen.
    frames: [],
    progress,
    startedAt,
    finishedAt,
    elapsedS,
    restored: true,
  };
}

export function entryToRow(entry: RunHistoryEntry): unknown[] {
  return [
    entry.id,
    entry.intent,
    entry.at,
    entry.startedAt,
    entry.finishedAt,
    entry.elapsedS,
    JSON.stringify(entry.request),
    JSON.stringify(entry.result),
    JSON.stringify(entry.progress),
  ];
}

/**
 * A store over an already-open database.
 *
 * Exported separately from {@link defaultRunHistoryStore} so tests drive the
 * real SQL text against a fake `SqlDatabase` — the statements below are the
 * thing worth testing, and they should not need a Tauri host to exercise.
 */
export function createRunHistoryStore(db: SqlDatabase): RunHistoryStore {
  let schema: Promise<void> | null = null;

  const ensureSchema = (): Promise<void> => {
    if (!schema) {
      schema = (async () => {
        for (const statement of SCHEMA_STATEMENTS) await db.execute(statement);
      })();
      // A failed CREATE must not be cached as "done"; the next call retries.
      schema.catch(() => {
        schema = null;
      });
    }
    return schema;
  };

  return {
    async load(limit) {
      try {
        await ensureSchema();
        const rows = await db.select<HistoryRow[]>(SELECT, [
          Math.max(1, Math.floor(limit)),
        ]);
        if (!Array.isArray(rows)) return [];
        const entries: RunHistoryEntry[] = [];
        for (const row of rows) {
          const entry = rowToEntry(row);
          if (entry) entries.push(entry);
        }
        return entries;
      } catch (error) {
        logError("history.load", "Could not read stored runs.", {
          detail: (error as Error)?.message ?? "",
        });
        return [];
      }
    },

    async save(entry, keep) {
      try {
        await ensureSchema();
        await db.execute(INSERT, entryToRow(entry));
        await db.execute(PRUNE, [Math.max(1, Math.floor(keep))]);
      } catch (error) {
        // A board the engine really produced must not become an error because
        // a disk write failed. The run stays in memory for this session.
        logError("history.save", "Could not store the finished run.", {
          detail: (error as Error)?.message ?? "",
        });
      }
    },

    async clear() {
      try {
        await ensureSchema();
        await db.execute(CLEAR);
      } catch (error) {
        logError("history.clear", "Could not clear stored runs.", {
          detail: (error as Error)?.message ?? "",
        });
      }
    },
  };
}

/** Every method a no-op. What "there is no database here" looks like. */
export const NULL_HISTORY_STORE: RunHistoryStore = {
  load: async () => [],
  save: async () => {},
  clear: async () => {},
};

/**
 * Open `sqlite:kaleo.db` through the Tauri SQL plugin, or return null.
 *
 * The import is dynamic so this module stays importable in a plain Node test
 * run, where the plugin's `invoke` has no host to call.
 */
export async function openHistoryDatabase(): Promise<SqlDatabase | null> {
  try {
    const { default: Database } = await import("@tauri-apps/plugin-sql");
    return (await Database.load(HISTORY_DB)) as unknown as SqlDatabase;
  } catch {
    // No Tauri host, no capability, or no database. The session runs without
    // persistence, which is a state this app is allowed to be in.
    return null;
  }
}

/**
 * The store the app uses: one shared instance, opened on first use.
 *
 * Shared because both webviews mount a `RunProvider` and a second connection
 * would buy nothing. Lazy because opening a database is not something a render
 * should do.
 */
export function defaultRunHistoryStore(): RunHistoryStore {
  let backing: Promise<RunHistoryStore> | null = null;

  const resolve = (): Promise<RunHistoryStore> => {
    if (!backing) {
      backing = openHistoryDatabase().then((db) =>
        db ? createRunHistoryStore(db) : NULL_HISTORY_STORE
      );
    }
    return backing;
  };

  return {
    load: async (limit) => (await resolve()).load(limit),
    save: async (entry, keep) => (await resolve()).save(entry, keep),
    clear: async () => (await resolve()).clear(),
  };
}

/**
 * Is there a Tauri host to open a database through?
 *
 * Checked synchronously so a plain browser or a test run resolves to "no
 * persistence" without an async round trip that would settle after the caller
 * has already rendered — the difference between "we have not looked yet" and
 * "there is nothing to look at" is a state the history list shows the user.
 */
export function hasTauriHost(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

let shared: RunHistoryStore | null = null;

/**
 * The process-wide store, or `null` where nothing can be persisted.
 *
 * `useSilkscreenRunState` uses this by default, and treats `null` as "this
 * session keeps its runs in memory only".
 */
export function sharedRunHistoryStore(): RunHistoryStore | null {
  if (!hasTauriHost()) return null;
  if (!shared) shared = defaultRunHistoryStore();
  return shared;
}
