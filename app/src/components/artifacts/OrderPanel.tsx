import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { RunResult } from "@/lib/silkscreen/types";
import { SaveBoardButton } from "./SaveBoardButton";
import {
  CircleAlert,
  FileText,
  Info,
  OctagonX,
  ShieldCheck,
  TriangleAlert,
} from "lucide-react";

/**
 * The `order` block: what a fab would be asked for, and the preflight that
 * stands in front of it.
 *
 * `engine/silkscreen/order.py` derives `orderable` from the blocker list
 * rather than storing it, so the verdict cannot disagree with its reasons and
 * there is deliberately no flag that waives one. This panel keeps that shape:
 * a blocked board leads with the refusal and the reasons, at full weight. The
 * refusal is the feature — a board with open nets fabricates into a beautiful
 * dead rectangle, and softening the message into a warning is how someone
 * spends money on one.
 *
 * Nothing here transacts. There is no order button because the engine does not
 * contact fabricators; the output is a package for a human to submit.
 */

interface WireOrderIssue {
  code?: string;
  severity?: string;
  title?: string;
  detail?: string;
  parts?: string[];
}

interface WireBoardSummary {
  width_mm?: number;
  height_mm?: number;
  area_mm2?: number;
  part_count?: number;
  parts_by_side?: Record<string, number>;
  net_count?: number;
  nets_with_two_or_more_pads?: number;
  solver_status?: string;
  panel_columns?: number;
  panel_rows?: number;
  boards_per_panel?: number;
  panel_width_mm_no_gaps_or_rails?: number;
  panel_height_mm_no_gaps_or_rails?: number;
}

interface WireManifest {
  board?: WireBoardSummary;
  options?: Record<string, unknown>;
  issues?: WireOrderIssue[];
  blocker_count?: number;
  orderable?: boolean;
  requires_human_approval?: boolean;
  disclaimer?: string;
}

/** `_order_block` in `service/app.py`. Every field optional on the wire. */
interface WireOrder {
  manifest?: WireManifest;
  issues?: WireOrderIssue[];
  orderable?: boolean;
  files?: { filename?: string; content?: string }[];
}

type Bucket = "blocker" | "warning" | "note" | "other";

/**
 * The engine's severities are blocker/warning/note. Anything else keeps its
 * own label and buckets as `other` — we never relabel a severity we do not
 * understand, and in particular never downgrade one.
 */
function bucketOf(severity: string | undefined): Bucket {
  switch (String(severity ?? "").toLowerCase()) {
    case "blocker":
      return "blocker";
    case "warning":
      return "warning";
    case "note":
      return "note";
    default:
      return "other";
  }
}

const BUCKET_ORDER: Bucket[] = ["blocker", "warning", "note", "other"];

const BUCKET_STYLE: Record<
  Bucket,
  { icon: typeof OctagonX; label: string; className: string }
> = {
  blocker: {
    icon: OctagonX,
    label: "Blocker",
    className: "border-destructive/60 bg-destructive/10",
  },
  warning: {
    icon: TriangleAlert,
    label: "Warning",
    className: "border-border/60 bg-muted/40",
  },
  note: { icon: Info, label: "Note", className: "border-border/60" },
  other: { icon: CircleAlert, label: "Unrecognised", className: "border-border/60" },
};

function IssueRow({ issue }: { issue: WireOrderIssue }) {
  const bucket = bucketOf(issue.severity);
  const style = BUCKET_STYLE[bucket];
  const Icon = style.icon;
  return (
    <div
      className={cn("rounded-lg border px-3 py-2 flex gap-2", style.className)}
      data-testid="order-issue"
      data-severity={issue.severity ?? "unreported"}
      data-code={issue.code ?? ""}
    >
      <Icon
        className={cn(
          "size-4 mt-0.5 shrink-0",
          bucket === "blocker" ? "text-destructive" : "text-muted-foreground"
        )}
      />
      <div className="flex flex-col gap-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm font-medium">
            {issue.title || issue.code || "Unnamed issue"}
          </span>
          <Badge variant={bucket === "blocker" ? "destructive" : "outline"}>
            {bucket === "other" ? issue.severity ?? "unreported" : style.label}
          </Badge>
          {issue.code && (
            <span className="font-mono text-[11px] text-muted-foreground">
              {issue.code}
            </span>
          )}
        </div>
        {issue.detail && (
          <p className="text-sm leading-relaxed text-muted-foreground">
            {issue.detail}
          </p>
        )}
        {Array.isArray(issue.parts) && issue.parts.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {issue.parts.map((ref) => (
              <span
                key={ref}
                className="rounded-md border px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground"
                data-testid="order-issue-part"
                data-ref={ref}
              >
                {ref}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function summaryRows(board: WireBoardSummary): { label: string; value: string }[] {
  const rows: { label: string; value: string }[] = [];
  const push = (label: string, value: unknown, suffix = "") => {
    if (value === undefined || value === null) return;
    rows.push({ label, value: `${value}${suffix}` });
  };
  if (board.width_mm !== undefined && board.height_mm !== undefined) {
    rows.push({
      label: "Outline",
      value: `${board.width_mm} × ${board.height_mm} mm`,
    });
  }
  push("Area", board.area_mm2, " mm²");
  push("Parts", board.part_count);
  push("Nets", board.net_count);
  push("Nets needing copper", board.nets_with_two_or_more_pads);
  push("Placement status", board.solver_status);
  push("Boards per panel", board.boards_per_panel);
  if (
    board.panel_width_mm_no_gaps_or_rails !== undefined &&
    board.panel_height_mm_no_gaps_or_rails !== undefined
  ) {
    rows.push({
      label: "Panel (no gaps or rails)",
      value: `${board.panel_width_mm_no_gaps_or_rails} × ${board.panel_height_mm_no_gaps_or_rails} mm`,
    });
  }
  return rows;
}

function optionRows(options: Record<string, unknown>): {
  label: string;
  value: string;
}[] {
  return Object.entries(options).map(([key, value]) => ({
    label: key.replace(/_/g, " "),
    value: typeof value === "boolean" ? (value ? "yes" : "no") : String(value),
  }));
}

export interface OrderPanelProps {
  /** The response's `order` block, or `undefined` when none was requested. */
  order?: RunResult["order"] | null;
  /**
   * Nets the router could not finish, `{net: reason}`.
   *
   * The one-shot response carries no per-net routing report today — the only
   * place unrouted nets are named is the preflight's `unrouted-nets` detail,
   * which is prose. This prop is the seam for a caller that has the structured
   * form (from `board.unrouted_nets`, if the service ever sends it); when it
   * is absent the panel says the report is unavailable rather than implying
   * the board is fully routed.
   */
  unroutedNets?: Record<string, string> | null;
  className?: string;
}

export function OrderPanel({ order, unroutedNets, className }: OrderPanelProps) {
  const wire = (order ?? null) as WireOrder | null;

  if (!wire) {
    return (
      <section
        className={cn("flex flex-col gap-2", className)}
        data-testid="order-panel"
        data-state="absent"
      >
        <h3 className="text-sm font-semibold">Order preflight</h3>
        <p className="text-sm text-muted-foreground">
          This run did not ask for an order block, so no manufacturability
          check was run. That is not the same as passing one.
        </p>
      </section>
    );
  }

  const issues = wire.issues ?? wire.manifest?.issues ?? [];
  const hasIssueList = Array.isArray(wire.issues) || Array.isArray(wire.manifest?.issues);
  const blockers = issues.filter((i) => bucketOf(i.severity) === "blocker");
  // Read the verdict, do not infer it: the engine derives `orderable` from its
  // own blocker list, and a missing field is unknown rather than a pass.
  const orderable =
    typeof wire.orderable === "boolean"
      ? wire.orderable
      : typeof wire.manifest?.orderable === "boolean"
        ? wire.manifest.orderable
        : null;

  const rest = BUCKET_ORDER.filter((b) => b !== "blocker").flatMap((bucket) =>
    issues.filter((i) => bucketOf(i.severity) === bucket)
  );

  const board = wire.manifest?.board;
  const options = wire.manifest?.options;
  const files = Array.isArray(wire.files) ? wire.files : null;

  return (
    <section
      className={cn("flex flex-col gap-3", className)}
      data-testid="order-panel"
      data-state={orderable === null ? "unknown" : orderable ? "orderable" : "blocked"}
    >
      <h3 className="text-sm font-semibold">Order preflight</h3>

      <div
        className={cn(
          "rounded-lg border px-3 py-2.5 flex items-start gap-2",
          orderable === false
            ? "border-destructive/60 bg-destructive/10"
            : "border-border/60"
        )}
        data-testid="order-verdict"
      >
        {orderable === false ? (
          <OctagonX className="size-4 mt-0.5 shrink-0 text-destructive" />
        ) : orderable === true ? (
          <ShieldCheck className="size-4 mt-0.5 shrink-0 text-muted-foreground" />
        ) : (
          <CircleAlert className="size-4 mt-0.5 shrink-0 text-muted-foreground" />
        )}
        <div className="flex flex-col gap-1">
          <span
            className={cn(
              "text-sm font-semibold",
              orderable === false && "text-destructive"
            )}
          >
            {orderable === false
              ? `Not orderable — ${blockers.length} blocker${blockers.length === 1 ? "" : "s"}`
              : orderable === true
                ? "No blockers found"
                : "Verdict not reported"}
          </span>
          <span className="text-xs text-muted-foreground">
            {orderable === false
              ? "The preflight refuses this board. There is no override; fix the reasons below and re-run."
              : orderable === true
                ? "The checks that ran found nothing that blocks fabrication. That is a clean preflight, not a guarantee the circuit is correct — the preflight measures manufacturability, not intent."
                : "The order block arrived without an `orderable` field, so this build cannot say whether the board passed."}
          </span>
        </div>
      </div>

      {blockers.length > 0 && (
        <div className="flex flex-col gap-2" data-testid="order-blockers">
          {blockers.map((issue, index) => (
            <IssueRow key={`${issue.code ?? "blocker"}-${index}`} issue={issue} />
          ))}
        </div>
      )}

      {/* Routing honesty: the autorouter is partial by design and names what
          it could not finish. The response has no structured field for that,
          so we surface whatever we do have and never imply completeness. */}
      <div
        className="rounded-lg border border-border/60 px-3 py-2 flex flex-col gap-1"
        data-testid="order-routing"
        data-present={unroutedNets ? "yes" : "no"}
      >
        <span className="text-[11px] uppercase tracking-wide text-muted-foreground">
          Routing
        </span>
        {unroutedNets && Object.keys(unroutedNets).length > 0 ? (
          <ul className="flex flex-col gap-1">
            {Object.entries(unroutedNets).map(([net, reason]) => (
              <li
                key={net}
                className="text-sm"
                data-testid="unrouted-net"
                data-ref={net}
              >
                <span className="font-mono text-[12px]">{net}</span>
                <span className="text-muted-foreground"> — {reason}</span>
              </li>
            ))}
          </ul>
        ) : unroutedNets ? (
          <span className="text-sm text-muted-foreground">
            The router reported no unfinished nets.
          </span>
        ) : (
          <span className="text-sm text-muted-foreground">
            No per-net routing report came back with this run. Silkscreen's
            router is partial by design and names every net it cannot finish;
            if any were left open, the <span className="font-mono">unrouted-nets</span>{" "}
            blocker above spells them out. Do not read the absence of a report
            as a fully routed board.
          </span>
        )}
      </div>

      {rest.length > 0 && (
        <div className="flex flex-col gap-2" data-testid="order-notes">
          {rest.map((issue, index) => (
            <IssueRow key={`${issue.code ?? "issue"}-${index}`} issue={issue} />
          ))}
        </div>
      )}

      {hasIssueList && issues.length === 0 && (
        <p className="text-sm text-muted-foreground" data-testid="order-no-issues">
          The preflight ran and raised nothing — no blockers, warnings or notes.
        </p>
      )}

      {board && (
        <div className="flex flex-col gap-1" data-testid="order-board-summary">
          <span className="text-[11px] uppercase tracking-wide text-muted-foreground">
            Board, as the manifest describes it
          </span>
          <dl className="grid grid-cols-2 gap-x-3 gap-y-1">
            {summaryRows(board).map((row) => (
              <div
                key={row.label}
                className="flex items-baseline justify-between gap-2 border-b border-border/40 py-0.5"
                data-testid="order-summary-row"
                data-label={row.label}
              >
                <dt className="text-xs text-muted-foreground">{row.label}</dt>
                <dd className="text-xs font-medium tabular-nums">{row.value}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}

      {options && Object.keys(options).length > 0 && (
        <div className="flex flex-col gap-1" data-testid="order-options">
          <span className="text-[11px] uppercase tracking-wide text-muted-foreground">
            Options this order was preflighted against
          </span>
          <div className="flex flex-wrap gap-1">
            {optionRows(options).map((row) => (
              <span
                key={row.label}
                className="rounded-md border px-1.5 py-0.5 text-[11px] text-muted-foreground"
                data-testid="order-option"
                data-label={row.label}
              >
                {row.label}: <span className="font-medium">{row.value}</span>
              </span>
            ))}
          </div>
        </div>
      )}

      {files && (
        <div className="flex flex-col gap-1.5" data-testid="order-files">
          <span className="text-[11px] uppercase tracking-wide text-muted-foreground">
            Fabrication files ({files.length})
          </span>
          {files.length === 0 && (
            <span className="text-sm text-muted-foreground">
              The order block carried no fabrication files.
            </span>
          )}
          {files.map((file, index) => (
            <div
              key={file.filename ?? index}
              className="flex items-center justify-between gap-2 rounded-lg border border-border/60 px-2 py-1.5"
              data-testid="order-file"
              data-filename={file.filename ?? ""}
            >
              <span className="flex items-center gap-1.5 min-w-0">
                <FileText className="size-3.5 shrink-0 text-muted-foreground" />
                <span className="font-mono text-[12px] truncate">
                  {file.filename ?? "unnamed"}
                </span>
                <span className="text-[11px] text-muted-foreground shrink-0">
                  {typeof file.content === "string"
                    ? `${file.content.length} chars`
                    : "no content"}
                </span>
              </span>
              <SaveBoardButton
                content={file.content}
                filename={file.filename ?? "layer.gbr"}
                label="Save"
                variant="ghost"
                size="sm"
              />
            </div>
          ))}
        </div>
      )}

      <p className="text-[11px] text-muted-foreground">
        {wire.manifest?.disclaimer ??
          "Nothing has been purchased and no fabricator has been contacted. Silkscreen does not transact; this package is for a human to review and submit."}
      </p>
    </section>
  );
}
