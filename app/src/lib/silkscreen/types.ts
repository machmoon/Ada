// The shapes `service/app.py` actually answers with.
//
// These mirror the documented response contract, which is *additive-only*: the
// service may add fields, never change or remove one. So every field beyond
// the handful the run genuinely cannot complete without is optional here, and
// the UI is written to render what arrived rather than to assume a shape.

/** A rectangle as the service sends it: `[x, y, w, h]`, min-corner first, mm. */
export type RectMm = [number, number, number, number];

export interface Pad {
  number: string;
  net: string | null;
  rect_mm: RectMm;
}

export interface PlacedPart {
  ref: string;
  footprint: string;
  value: string | null;
  layer: string;
  rotated: boolean;
  x_mm: number;
  y_mm: number;
  courtyard_mm: RectMm;
  pads: Pad[];
}

export interface Placements {
  /** `[width, height]` in millimetres. */
  board_mm: [number, number];
  /**
   * Always `"solver-y-up"`. The service resolves rotation before it sends
   * this, so the single Y flip in `board.ts` is the only coordinate work left.
   */
  frame: string;
  parts: PlacedPart[];
}

/**
 * `/generate` findings come from the adversarial reviewer, whose vocabulary is
 * `blocker`/`marginal`/`note` (engine `agents/review.py`). The audit CLI's
 * separate rules engine speaks `error`/`warning`/`info` with a proven/suggested
 * origin; that surface never flows through `/generate` today, but the union is
 * kept open so a future response carrying it renders instead of crashing.
 */
export type Severity =
  | "blocker"
  | "marginal"
  | "note"
  | "error"
  | "warning"
  | "info"
  | (string & {});
export type Origin = "proven" | "suggested" | (string & {});

/**
 * One review finding, as `_finding_dict` in `service/app.py` sends it.
 *
 * `parts` keeps the spec's own part names — the vocabulary `title` and
 * `detail` are written in — while `refs` carries the same parts as they are
 * labelled on the board, which is the identity the board well shares.
 * The audit-CLI fields (`origin`, `evidence`, `rule`) stay optional for the
 * reason above; nothing from `/generate` carries them today.
 */
export interface Finding {
  id?: string;
  severity: Severity;
  title?: string;
  detail?: string;
  parts?: string[];
  refs?: string[];
  citation?: string | null;
  suggested_fix?: string | null;
  origin?: Origin;
  evidence?: string | null;
  rule?: string | null;
}

/**
 * One end of a net: a PIN, not a part.
 *
 * The engine's circuit IR requires pin-level terminals (`C1.1`, never `C1`)
 * because "one leg of this capacitor to this pin" has to be expressible, and
 * `_schematic_dict` in `service/app.py` preserves that: `pin` is the logical
 * name the spec was written in (`"AVDD"`, or `"1"` for a passive) and `number`
 * is the physical pin it resolves to. A UI that collapses these to `part_id`
 * throws away the only thing the IR exists for.
 *
 * `ref` is the board reference designator, and is null until refs are
 * assigned; `part_id` is the spec name and is always present.
 */
export interface SchematicPin {
  part_id: string;
  ref?: string | null;
  pin: string;
  number?: string | null;
}

export interface SchematicNet {
  name: string;
  endpoints: SchematicPin[];
}

/**
 * The `schematic` block: electrical truth with no geometry in it, versioned so
 * a later renderer can tell which shape it received.
 */
export interface Schematic {
  version?: number;
  parts?: {
    /** Spec name — the vocabulary endpoints and findings are written in. */
    id: string;
    ref?: string | null;
    /** `"device"`, or a passive type: `"resistor"`, `"capacitor"`, … */
    kind?: string;
    value?: string | null;
    symbol?: string | null;
    pins?: { name: string; number?: string | null }[];
  }[];
  nets?: SchematicNet[];
}

export interface RunResult {
  /** The emitted board file, as text. Present on every successful run. */
  kicad_pcb?: string;
  /** The intent string the run was asked for, echoed back. */
  intent?: string;
  /** `[width, height]` in millimetres — same numbers as `placements.board_mm`. */
  board_mm?: [number, number];
  /** CP-SAT solver status, lowercase on the wire: optimal / feasible / fallback. */
  status?: string;
  /** How many repair rounds the propose loop needed. */
  repair_rounds?: number;
  /** Flattened blocker strings. The compatibility surface; prefer `findings`. */
  blockers?: string[];
  findings?: Finding[];
  warnings?: string[];
  parts?: { ref: string; footprint: string }[];
  nets?: string[];
  datasheets?: unknown[];
  placements?: Placements;
  schematic?: Schematic;
  order?: Record<string, unknown>;
  /**
   * The router's own report, sent on every run: counts, the nets it finished,
   * and — the honesty contract — every net it could not, by name, with the
   * reason. `completion` is the routed fraction (0..1).
   */
  routing?: {
    tracks?: number;
    vias?: number;
    routed?: string[];
    unrouted?: Record<string, string>;
    warnings?: string[];
    completion?: number;
  };
  grounding?: Record<string, unknown>;
  duration_s?: number;
  wirelength_mm?: number | null;
  served_by?: string | null;
  cache?: { hit?: string[]; read?: string[]; unusable?: string[] };
}

/** The error body both POST routes share. */
export interface RunError {
  error: string;
  detail?: string;
  error_id?: string;
  /** Only the `run.error` stream frame carries this; error bodies do not. */
  status?: number;
}

/**
 * One NDJSON frame off `/generate/stream`.
 *
 * The envelope is `{event, t_s, ...}`; everything else depends on the event
 * name, and unknown events are expected — the engine may emit stages this
 * build has never heard of, and dropping them silently is the contract.
 */
export interface StreamFrame {
  event: string;
  t_s?: number;
  stage?: string;
  status?: number;
  result?: RunResult;
  [key: string]: unknown;
}

export interface GenerateRequest {
  intent: string;
  datasheets?: Record<string, string>;
  time_limit_s?: number;
  review?: boolean;
  ground?: boolean;
  debug?: boolean;
}
