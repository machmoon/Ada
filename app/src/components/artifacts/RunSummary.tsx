import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { RunResult } from "@/lib/silkscreen/types";

/**
 * The scoreboard for a finished run.
 *
 * Every number here is read off the response. The response is additive-only
 * and most of its fields are optional, so the component distinguishes three
 * states that are easy to collapse and must not be: a value that arrived, a
 * value that arrived empty (zero parts really is zero parts), and a field the
 * response never sent. The last renders as "not reported" — never as 0, and
 * never as a reassuring blank. "We measured nothing" and "we measured zero"
 * are different claims about a board somebody may pay to fabricate.
 */

/** `null` means the response omitted the field; a string is a real value. */
type Value = string | null;

function fmt(value: number | null | undefined, digits: number, unit: string): Value {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  return `${value.toFixed(digits)} ${unit}`;
}

function count(list: unknown[] | undefined): Value {
  return Array.isArray(list) ? String(list.length) : null;
}

interface Metric {
  key: string;
  label: string;
  value: Value;
  /** Shown under the value when the field is present. */
  note?: string;
  /** Why the field can legitimately be missing, shown when it is. */
  absent?: string;
}

function MetricCell({ metric }: { metric: Metric }) {
  const present = metric.value !== null;
  return (
    <div
      className="flex flex-col gap-0.5 rounded-lg border border-border/60 px-3 py-2"
      data-testid="run-summary-metric"
      data-metric={metric.key}
      data-present={present ? "yes" : "no"}
    >
      <span className="text-[11px] uppercase tracking-wide text-muted-foreground">
        {metric.label}
      </span>
      <span
        className={cn(
          "text-sm font-medium tabular-nums",
          !present && "text-muted-foreground font-normal italic"
        )}
      >
        {present ? metric.value : "not reported"}
      </span>
      {present && metric.note && (
        <span className="text-[11px] text-muted-foreground">{metric.note}</span>
      )}
      {!present && metric.absent && (
        <span className="text-[11px] text-muted-foreground">{metric.absent}</span>
      )}
    </div>
  );
}

export interface RunSummaryProps {
  /** `null` while no run has finished — the summary then measures nothing. */
  result: RunResult | null | undefined;
  className?: string;
}

export function RunSummary({ result, className }: RunSummaryProps) {
  if (!result) {
    return (
      <section
        className={cn("flex flex-col gap-1", className)}
        data-testid="run-summary"
        data-state="empty"
      >
        <h3 className="text-sm font-semibold">Run summary</h3>
        <p className="text-sm text-muted-foreground">
          No finished run to measure yet.
        </p>
      </section>
    );
  }

  const board = result.placements?.board_mm;
  const boardValue =
    Array.isArray(board) && board.length === 2
      ? `${board[0].toFixed(2)} × ${board[1].toFixed(2)} mm`
      : null;

  const metrics: Metric[] = [
    {
      key: "board",
      label: "Board",
      value: boardValue,
      absent: "no placements block in the response",
    },
    {
      key: "parts",
      label: "Parts placed",
      value: count(result.parts),
      absent: "the response carried no parts list",
    },
    {
      key: "nets",
      label: "Nets",
      value: count(result.nets),
      absent: "the response carried no net list",
    },
    {
      key: "wirelength",
      label: "Wirelength",
      // Explicitly nullable on the wire: the placer reports none when it did
      // not compute one, and that is not "0 mm of wire".
      value: fmt(result.wirelength_mm, 2, "mm"),
      note: "half-perimeter estimate",
      absent: "the placer did not report a wirelength",
    },
    {
      key: "duration",
      label: "Duration",
      value: fmt(result.duration_s, 2, "s"),
      absent: "the service did not time this run",
    },
    {
      key: "datasheets",
      label: "Datasheets used",
      value: count(result.datasheets),
      absent: "no datasheet block in the response",
    },
  ];

  const cache = result.cache;
  const hit = cache?.hit;
  const read = cache?.read;
  const unusable = cache?.unusable;

  return (
    <section
      className={cn("flex flex-col gap-3", className)}
      data-testid="run-summary"
      data-state="ready"
    >
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">Run summary</h3>
        {/* `served_by` is the provider that actually answered — which is not
            always the one configured, because FallbackModel may have failed
            over mid-run. Absent means the service did not say. */}
        {/* The testid sits on an intrinsic span rather than on <Badge>, per
            the house rule. */}
        <span
          data-testid="run-summary-served-by"
          data-present={result.served_by ? "yes" : "no"}
        >
          {result.served_by ? (
            <Badge variant="secondary">served by {result.served_by}</Badge>
          ) : (
            <Badge variant="outline" className="text-muted-foreground italic">
              provider not reported
            </Badge>
          )}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        {metrics.map((metric) => (
          <MetricCell key={metric.key} metric={metric} />
        ))}
      </div>

      <div
        className="rounded-lg border border-border/60 px-3 py-2 flex flex-col gap-1"
        data-testid="run-summary-cache"
        data-present={cache ? "yes" : "no"}
      >
        <span className="text-[11px] uppercase tracking-wide text-muted-foreground">
          Datasheet fact cache
        </span>
        {!cache ? (
          <span className="text-sm text-muted-foreground italic">
            not reported — this run did not say whether the cache was consulted
          </span>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            <CacheGroup label="hit" parts={hit} testid="cache-hit" />
            <CacheGroup label="re-read" parts={read} testid="cache-read" />
            <CacheGroup
              label="unusable"
              parts={unusable}
              testid="cache-unusable"
              destructive
            />
          </div>
        )}
        {cache && (
          <span className="text-[11px] text-muted-foreground">
            An entry that was present but unreadable counts as a miss and a
            re-read, not a hit.
          </span>
        )}
      </div>
    </section>
  );
}

function CacheGroup({
  label,
  parts,
  testid,
  destructive,
}: {
  label: string;
  parts: string[] | undefined;
  testid: string;
  destructive?: boolean;
}) {
  if (!Array.isArray(parts)) {
    return (
      <span data-testid={testid} data-present="no">
        <Badge variant="outline" className="text-muted-foreground italic">
          {label}: not reported
        </Badge>
      </span>
    );
  }
  return (
    <span
      data-testid={testid}
      data-present="yes"
      data-count={parts.length}
      title={parts.length > 0 ? parts.join(", ") : undefined}
    >
      <Badge variant={destructive && parts.length > 0 ? "destructive" : "outline"}>
        {label}: {parts.length}
      </Badge>
    </span>
  );
}
