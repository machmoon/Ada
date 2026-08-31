import { useCallback } from "react";
import { invoke } from "@tauri-apps/api/core";
import { ErrorBoundary } from "react-error-boundary";
import { LayoutDashboardIcon } from "lucide-react";
import { Button, Card, DragButton, Updater } from "@/components";
import { ErrorLayout } from "@/layouts";
import { useApp } from "@/hooks";
import { useSilkscreenRun } from "@/contexts";
import {
  ActivityFeed,
  PromptBar,
  RunFailure,
  RunProgress,
  RunSummary,
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

  const busy = !SETTLED.includes(run.status);

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
            <Updater />
            <DragButton />
          </div>

          {busy ? (
            <div className="flex flex-col gap-2 border-t border-input/40 pt-2">
              <RunProgress
                stages={run.stages}
                elapsedS={run.elapsedS}
                onCancel={() => run.cancel()}
              />
              <ActivityFeed lines={run.lines} className="max-h-40" />
            </div>
          ) : null}

          {run.status === "done" && run.result ? (
            <div className="border-t border-input/40 pt-2">
              <RunSummary
                result={run.result}
                // What this run was actually asked for, not what the form says
                // now — the form is editable while the result is on screen.
                reviewRequested={run.submitted?.review ?? true}
                elapsedS={run.elapsedS}
                onOpenReview={openDashboard}
                onNewRun={() => run.reset()}
              />
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
    </ErrorBoundary>
  );
};

export default Kaleo;
