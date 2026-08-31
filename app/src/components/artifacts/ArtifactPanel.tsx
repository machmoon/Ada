import { cn } from "@/lib/utils";
import type { RunResult } from "@/lib/silkscreen/types";
import { RunSummary } from "./RunSummary";
import { OrderPanel } from "./OrderPanel";
import { SaveBoardButton } from "./SaveBoardButton";

/**
 * The column where a finished run becomes files and numbers.
 *
 * Composition only — each child owns its own honesty rules. The panel's own
 * job is to not exist before there is a result: an empty scoreboard reads as
 * "we measured nothing and found nothing wrong", which is the claim this
 * project most wants to avoid making.
 */

/** A filename from the intent, so two runs do not overwrite each other. */
export function boardFilename(intent?: string | null): string {
  const slug = (intent ?? "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48)
    .replace(/-+$/g, "");
  return `${slug || "board"}.kicad_pcb`;
}

export interface ArtifactPanelProps {
  /** The finished run, or `null`/`undefined` while none has completed. */
  result?: RunResult | null;
  /** The prompt this run came from; used only to name the saved file. */
  intent?: string | null;
  /** See {@link OrderPanel}'s prop of the same name. */
  unroutedNets?: Record<string, string> | null;
  className?: string;
}

export function ArtifactPanel({
  result,
  intent,
  unroutedNets,
  className,
}: ArtifactPanelProps) {
  if (!result) {
    return (
      <aside
        className={cn("flex flex-col gap-2 p-3", className)}
        data-testid="artifact-panel"
        data-state="empty"
      >
        <h2 className="text-sm font-semibold">Artifacts</h2>
        <p className="text-sm text-muted-foreground">
          Nothing to show yet. Files and measurements appear here once a run
          finishes.
        </p>
      </aside>
    );
  }

  const filename = boardFilename(intent);
  const pcb = result.kicad_pcb;

  return (
    <aside
      className={cn("flex flex-col gap-4 p-3", className)}
      data-testid="artifact-panel"
      data-state="ready"
    >
      <div className="flex flex-col gap-2">
        <div className="flex items-baseline justify-between gap-2">
          <h2 className="text-sm font-semibold">Artifacts</h2>
          {typeof pcb === "string" && (
            <span
              className="text-[11px] text-muted-foreground tabular-nums"
              data-testid="artifact-board-size"
            >
              {pcb.length.toLocaleString()} characters
            </span>
          )}
        </div>
        <SaveBoardButton content={pcb} filename={filename} />
      </div>

      <RunSummary result={result} />

      <OrderPanel order={result.order} unroutedNets={unroutedNets} />
    </aside>
  );
}
