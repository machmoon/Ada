// Past runs, in the overlay.
//
// Pluely kept past chats; this keeps past *runs*, and the difference is not
// cosmetic. `/generate` is one shot — there is no endpoint that continues a
// conversation — so nothing here offers a follow-up turn or a reply box. A row
// does exactly two things the engine can back: it puts a finished run back on
// screen (`selectRun`, reading state the run already holds), and it copies that
// run's request back into the draft so it can be sent again (`restoreRequest`,
// which fills the form and does not start anything).
//
// Rows come from two places and say which: a run this session watched, or one
// read back out of the database at startup. A restored row kept its result,
// its request and the engine's own stage record, but not the raw stream frames
// — so it says so rather than letting the debug console look mysteriously
// empty.

import { useState } from "react";
import { HistoryIcon, RotateCcwIcon, Trash2Icon } from "lucide-react";
import {
  Button,
  Popover,
  PopoverContent,
  PopoverTrigger,
  ScrollArea,
} from "@/components";
import { useSilkscreenRun } from "@/contexts";
import type { RunHistoryEntry } from "@/contexts";
import { cn } from "@/lib/utils";

/** "just now" / "4m ago" / "2h ago" / "3d ago". Coarse on purpose. */
export function agoLabel(at: number, now: number = Date.now()): string {
  const seconds = Math.floor((now - at) / 1000);
  if (!Number.isFinite(seconds) || seconds < 45) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${Math.max(1, minutes)}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

/**
 * The one-line verdict for a row.
 *
 * Counted from the findings the response actually carried, and never collapsed
 * to "clean". The three ways a row can have no findings are three different
 * facts and are labelled as three different things: the review was switched
 * off, the response carried no review at all, or the review ran and reported
 * nothing. Only the last of those is evidence about the board.
 */
export function verdictLabel(entry: RunHistoryEntry): string {
  const findings = entry.result?.findings;
  const flattened = entry.result?.blockers ?? [];

  if (findings && findings.length > 0) {
    const blockers = findings.filter((f) => f.severity === "blocker").length;
    if (blockers > 0) return `${blockers} blocker${blockers === 1 ? "" : "s"}`;
    return `${findings.length} finding${findings.length === 1 ? "" : "s"}`;
  }
  // The older flattened surface, used when `findings` is absent.
  if (flattened.length > 0) {
    return `${flattened.length} blocker${flattened.length === 1 ? "" : "s"}`;
  }
  if (entry.request?.review === false) return "review was off";
  if (findings === undefined) return "no review in the response";
  return "review reported nothing";
}

export interface RunHistoryPanelProps {
  /** A run is in flight; swapping the view would hide what is being paid for. */
  busy?: boolean;
}

export const RunHistoryPanel = ({ busy }: RunHistoryPanelProps) => {
  const {
    history,
    historyReady,
    viewingId,
    selectRun,
    restoreRequest,
    clearHistory,
  } = useSilkscreenRun();
  const [open, setOpen] = useState(false);

  const empty = history.length === 0;

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          disabled={empty}
          title={
            empty
              ? historyReady
                ? "No finished runs yet"
                : "Reading stored runs…"
              : `Past runs — ${history.length} stored`
          }
          aria-label="Past runs"
          data-testid="history-trigger"
          data-count={history.length}
        >
          <HistoryIcon className="size-4" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        side="bottom"
        sideOffset={8}
        className="w-96 border border-input/50 p-2"
      >
        <div className="flex flex-col gap-1" data-testid="history-panel">
          <div className="flex items-center justify-between gap-2 px-1 pb-1">
            <span className="text-[11px] font-medium">Past runs</span>
            <Button
              variant="ghost"
              size="sm"
              className="h-6 px-1.5 text-[10px]"
              title="Forget every stored run, here and on disk"
              data-testid="history-clear"
              onClick={() => {
                clearHistory();
                setOpen(false);
              }}
            >
              <Trash2Icon className="size-3" />
              Clear
            </Button>
          </div>

          {busy ? (
            <p className="px-1 pb-1 text-[10px] text-muted-foreground">
              A run is in flight. Cancel it first to look at a past one.
            </p>
          ) : null}

          <ScrollArea className="max-h-72">
            <div className="flex flex-col gap-1 pr-2">
              {history.map((entry) => {
                const active = entry.id === viewingId;
                return (
                  <div
                    key={entry.id}
                    className={cn(
                      "flex items-center gap-1 rounded-lg border px-2 py-1.5",
                      active
                        ? "border-primary bg-accent/60"
                        : "border-input/50 bg-card"
                    )}
                    data-testid="history-entry"
                    data-run-id={entry.id}
                    data-restored={entry.restored ? "1" : "0"}
                  >
                    <button
                      type="button"
                      className="flex min-w-0 flex-1 flex-col items-start gap-0.5 text-left disabled:opacity-60"
                      disabled={busy}
                      title={entry.intent}
                      data-testid="history-entry-open"
                      onClick={() => {
                        selectRun(entry.id);
                        setOpen(false);
                      }}
                    >
                      <span className="w-full truncate text-[11px]">
                        {entry.intent.trim() || "(no prompt recorded)"}
                      </span>
                      <span className="flex flex-wrap items-center gap-1.5 text-[10px] text-muted-foreground">
                        <span>{agoLabel(entry.at)}</span>
                        <span aria-hidden>·</span>
                        <span className="tabular-nums">
                          {entry.elapsedS.toFixed(1)}s
                        </span>
                        <span aria-hidden>·</span>
                        <span>{verdictLabel(entry)}</span>
                        {entry.restored ? (
                          <>
                            <span aria-hidden>·</span>
                            <span
                              data-testid="history-entry-restored"
                              title="Read back from disk at startup. The board, the request and the engine's stage record were kept; the raw frame log was not."
                            >
                              earlier session
                            </span>
                          </>
                        ) : null}
                      </span>
                    </button>
                    <Button
                      variant="ghost"
                      size="icon"
                      className="size-7 shrink-0"
                      title="Put this prompt and its datasheets back in the box"
                      aria-label="Reuse this prompt"
                      data-testid="history-entry-reuse"
                      onClick={() => {
                        restoreRequest(entry.id);
                        setOpen(false);
                      }}
                    >
                      <RotateCcwIcon className="size-3.5" />
                    </Button>
                  </div>
                );
              })}
            </div>
          </ScrollArea>
        </div>
      </PopoverContent>
    </Popover>
  );
};

export interface ViewingPastRunProps {
  entry: RunHistoryEntry;
  onBack: () => void;
}

/**
 * The banner over a past run.
 *
 * Without it the overlay's result block is indistinguishable from a run that
 * just finished — same summary, same buttons — and the user would have no way
 * to tell that the engine did not just do this work.
 */
export const ViewingPastRun = ({ entry, onBack }: ViewingPastRunProps) => (
  <div
    className="flex items-center justify-between gap-2"
    data-testid="viewing-past-run"
    data-run-id={entry.id}
  >
    <span className="min-w-0 truncate text-[11px] text-muted-foreground">
      Showing a past run from {agoLabel(entry.at)}
      {entry.restored ? " (an earlier session)" : ""} — nothing was re-run.
    </span>
    <Button
      size="sm"
      variant="ghost"
      className="shrink-0"
      onClick={onBack}
      data-testid="viewing-past-run-back"
    >
      Back
    </Button>
  </div>
);
