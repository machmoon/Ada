import { useCallback, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { ErrorBoundary } from "react-error-boundary";
import { ChevronDownIcon, ChevronRightIcon, LayoutDashboardIcon } from "lucide-react";
import { Button, Card, DragButton } from "@/components";
import { SaveBoardButton } from "@/components/artifacts/SaveBoardButton";
import { boardFilename } from "@/components/artifacts/ArtifactPanel";
import { ErrorLayout } from "@/layouts";
import { useApp } from "@/hooks";
import { useOverlayHeight } from "@/hooks/useOverlayHeight";
import { useRunVoice } from "@/hooks/useRunVoice";
import { useSilkscreenRun } from "@/contexts";
import { VoiceToggle } from "./components/VoiceToggle";
import {
  ActivityFeed,
  DatasheetChips,
  PromptBar,
  RunFailure,
  RunProgress,
  RunSummary,
  ViewingPastRun,
} from "./components";

/**
 * Statuses in which no run is in flight. Written as the complement so a status
 * the state machine adds later locks the submit control rather than freeing it.
 */
const SETTLED: string[] = ["idle", "done", "error", "cancelled"];

/**
 * The floating overlay: one prompt, one run, one result.
 *
 * The shell is Pluely's — a Card in a transparent, undecorated window, with the
 * drag handle and updater the app draws for itself because the OS draws no
 * titlebar. Only the domain inside it is silkscreen's.
 *
 * All run state lives in the provider, deliberately: it owns the only in-flight
 * guard and the only AbortController, and a run costs real money.
 */
const Kaleo = () => {
  const { isHidden } = useApp();
  const run = useSilkscreenRun();
  // Speaks the digest once when the live run completes; see useRunVoice.
  useRunVoice(run);

  const busy = !SETTLED.includes(run.status);

  // The past run currently on screen, if the history list put one there. Null
  // whenever the result block is showing the run that actually just happened.
  const viewed = run.viewingId
    ? run.history.find((entry) => entry.id === run.viewingId) ?? null
    : null;

  // The window is created 600×54 and cannot grow on its own; this keeps it as
  // tall as the Card actually is, which is the whole fix for runs that used to
  // render invisibly below the fold.
  const overlayRef = useOverlayHeight<HTMLDivElement>();

  const [feedOpen, setFeedOpen] = useState(false);

  // `start` takes an optional override, so it must never be handed straight to
  // onClick — React would pass the click event as the override.
  const submit = useCallback(() => run.start(), [run]);

  const openDashboard = useCallback(async () => {
    try {
      await invoke("open_dashboard");
    } catch (error) {
      console.error("Failed to open the review window:", error);
    }
  }, []);

  // Open the dashboard once per completed LIVE run: the bridge has already
  // handed it the result, so it opens populated, on the board — the moment
  // worth surfacing. History browsing and failures never trigger this.
  const openedRunRef = useRef<string | null>(null);
  const latestRunId = run.history[0]?.id ?? null;
  useEffect(() => {
    if (run.status !== "done" || !run.result || run.viewingHistory) return;
    if (!latestRunId || openedRunRef.current === latestRunId) return;
    openedRunRef.current = latestRunId;
    void openDashboard();
  }, [run.status, run.result, run.viewingHistory, latestRunId, openDashboard]);

  const engineDown = !busy && run.engine.lastCheckedAt !== null && !run.engine.ok;

  return (
    <ErrorBoundary
      fallbackRender={() => <ErrorLayout isCompact />}
      resetKeys={["kaleo-error"]}
    >
      <div
        className={`w-screen h-screen flex overflow-hidden justify-center items-start ${
          isHidden ? "hidden pointer-events-none" : ""
        }`}
      >
        <div ref={overlayRef} className="w-full">
        <Card className="w-full flex flex-col gap-2 p-2">
          <div className="flex w-full flex-row items-center gap-1.5">
            <PromptBar
              request={run.request}
              onRequestChange={run.updateRequest}
              onSubmit={submit}
              onCancel={() => run.cancel()}
              canStart={run.canStart}
              busy={busy}
              hidden={isHidden}
              engine={run.engine}
              baseUrl={run.baseUrl}
            />
            <Button
              size="icon"
              variant="ghost"
              title="Open the review window"
              onClick={openDashboard}
              data-testid="open-dashboard"
            >
              <LayoutDashboardIcon className="size-4" />
            </Button>
            <DragButton />
          </div>

          {/* What is attached to the next run, under the box it will be sent
              with. Renders nothing at all when nothing is attached. */}
          <DatasheetChips disabled={busy} />

          {engineDown ? (
            <p
              className="text-[10px] leading-tight text-destructive/90"
              data-testid="engine-down-reason"
            >
              Engine unreachable at <span className="font-mono">{run.baseUrl}</span>
              {" — "}click the dot to re-check, or start it:{" "}
              <span className="font-mono">PORT=8081 python -m service.app</span>
            </p>
          ) : null}

          {busy ? (
            <div className="flex flex-col gap-2 border-t border-input/40 pt-2">
              <RunProgress
                stages={run.stages}
                elapsedS={run.elapsedS}
                onCancel={() => run.cancel()}
              />
              {/* The checklist and the clock narrate the run; the raw feed is
                  detail on demand, so the busy overlay stays compact. */}
              <button
                type="button"
                className="flex items-center gap-1 self-start text-[10px] text-muted-foreground"
                onClick={() => setFeedOpen((open) => !open)}
                data-testid="activity-disclosure"
              >
                {feedOpen ? (
                  <ChevronDownIcon className="size-3" />
                ) : (
                  <ChevronRightIcon className="size-3" />
                )}
                activity · {run.lines.length} lines
              </button>
              {feedOpen ? (
                <ActivityFeed lines={run.lines} className="max-h-40" />
              ) : null}
            </div>
          ) : null}

          {run.status === "done" && run.result ? (
            <div className="flex flex-col gap-2 border-t border-input/40 pt-2">
              {/* A past run and a fresh one render the same summary, so the
                  only thing telling them apart has to be said out loud. */}
              {viewed ? (
                <ViewingPastRun
                  entry={viewed}
                  onBack={() => run.selectRun(null)}
                />
              ) : null}
              <div className="relative">
                {/* The mute control lives with the result it would narrate. */}
                <VoiceToggle className="absolute right-0 top-0" />
                <RunSummary
                  result={run.result}
                  // What this run was actually asked for, not what the form
                  // says now — the form is editable while the result is up.
                  reviewRequested={run.submitted?.review ?? true}
                  elapsedS={run.elapsedS}
                  onOpenReview={openDashboard}
                  onNewRun={() => run.reset()}
                />
                <SaveBoardButton
                  className="mt-2"
                  content={run.result.kicad_pcb}
                  filename={boardFilename(run.submitted?.intent)}
                  label="Save .kicad_pcb"
                  variant="outline"
                  size="sm"
                />
              </div>
            </div>
          ) : null}

          {run.status === "error" && run.error ? (
            <div className="border-t border-input/40 pt-2">
              <RunFailure
                error={run.error}
                baseUrl={run.baseUrl}
                onRetry={submit}
                onDismiss={() => run.reset()}
              />
            </div>
          ) : null}

          {run.status === "cancelled" ? (
            <div className="flex items-center justify-between gap-2 border-t border-input/40 pt-2">
              <span className="text-[11px] text-muted-foreground">
                Run cancelled. The engine may have finished the work it had
                already started, but nothing came back.
              </span>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => run.reset()}
                data-testid="cancelled-dismiss"
              >
                Dismiss
              </Button>
            </div>
          ) : null}
        </Card>
        </div>
      </div>
    </ErrorBoundary>
  );
};

export default Kaleo;
