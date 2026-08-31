import { createContext, useContext, type ReactNode } from "react";
import {
  useSilkscreenRunState,
  type SilkscreenRun,
  type UseSilkscreenRunOptions,
} from "@/hooks/useSilkscreenRun";
import { useRunBridge, type RunBridgeOptions } from "@/hooks/useRunBridge";

export type {
  RunHistoryEntry,
  RunRequestDraft,
  RunStatus,
  SilkscreenRun,
} from "@/hooks/useSilkscreenRun";
export {
  DEFAULT_HISTORY_LIMIT,
  DEFAULT_TIME_LIMIT_S,
  EMPTY_REQUEST,
} from "@/hooks/useSilkscreenRun";

const RunContext = createContext<SilkscreenRun | undefined>(undefined);

export interface RunProviderProps extends UseSilkscreenRunOptions {
  children: ReactNode;
  /**
   * Which end of the cross-window bridge this provider is, and how it talks.
   * Resolved from the window label when omitted; tests pin it.
   */
  bridge?: RunBridgeOptions;
}

/**
 * The single owner of "what is the engine doing right now".
 *
 * There is exactly one of these *per window*, mounted above everything that
 * renders a board. That is not tidiness — it is the money rule: the state
 * machine holds the only `AbortController` and the only in-flight guard, and a
 * second one would be a second way to bill the user for one prompt. It also
 * means the `/healthz` poll happens once for the whole window rather than once
 * per status dot.
 *
 * "Per window" is the load-bearing part. Each Tauri webview is its own JS
 * realm with its own React root (`main.tsx`), so the dashboard necessarily has
 * a second instance of this provider, and it is necessarily idle. `useRunBridge`
 * settles what that second instance is allowed to be: in the overlay it is the
 * owner and publishes what it sees; in the dashboard it is a read-only mirror
 * of the overlay's run, with run control inert so the "one owner" rule survives
 * two windows. With no bridge available the mirror stays idle and the workbench
 * shows its own empty state.
 */
export const RunProvider = ({
  children,
  bridge,
  ...options
}: RunProviderProps) => {
  const local = useSilkscreenRunState(options);
  const value = useRunBridge(local, bridge);
  return <RunContext.Provider value={value}>{children}</RunContext.Provider>;
};

export const useSilkscreenRun = (): SilkscreenRun => {
  const context = useContext(RunContext);

  if (!context) {
    throw new Error("useSilkscreenRun must be used within a RunProvider");
  }

  return context;
};

/** Short alias. Same hook, same single provider — nothing here is a second one. */
export const useRun = useSilkscreenRun;
