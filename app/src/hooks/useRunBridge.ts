/**
 * The React end of the run-state bridge. See `lib/silkscreen/bridge.ts` for the
 * wire format and the reasoning behind it.
 *
 * One hook serves both ends so the rules of hooks are never bent: the role is
 * decided once, and the effect belonging to the other role returns immediately.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import {
  RUN_HISTORY_EVENT,
  RUN_REQUEST_EVENT,
  RUN_STATE_EVENT,
  NAVIGATE_EVENT,
  acceptsSnapshot,
  isRunHistorySnapshot,
  isRunRequest,
  isRunStateSnapshot,
  mirrorRun,
  roleFor,
  tauriTransport,
  toHistorySnapshot,
  toStateSnapshot,
  type BridgeRole,
  type BridgeTransport,
  type RunHistorySnapshot,
  type RunStateSnapshot,
} from "@/lib/silkscreen/bridge";
import type { SilkscreenRun } from "./useSilkscreenRun";

/**
 * Snapshot version counter, per webview.
 *
 * Module scope, not React state: it must survive a re-render and it must never
 * be the thing that triggers one.
 */
let snapshotSeq = 0;
const nextVersion = () => (snapshotSeq += 1);

export interface RunBridgeOptions {
  /** Force the role. Omitted in the app; tests use it to pin an end. */
  role?: BridgeRole;
  /**
   * Override the transport. `null` means "there is no bridge" and is a
   * supported state, not a failure: the mirror then shows the local idle run,
   * which is the workbench's honest empty state.
   */
  transport?: BridgeTransport | null;
}

/**
 * Wrap a local run state machine with whichever end of the bridge this window
 * is. The publisher returns its own state untouched; the mirror returns
 * another window's run in the same shape.
 */
export function useRunBridge(
  local: SilkscreenRun,
  options: RunBridgeOptions = {}
): SilkscreenRun {
  // Resolved once. The window label cannot change under a running webview, and
  // probing the Tauri host on every render would be pointless work.
  const [transport] = useState<BridgeTransport | null>(() =>
    options.transport !== undefined ? options.transport : tauriTransport()
  );
  const [role] = useState<BridgeRole>(
    () => options.role ?? roleFor(transport?.label)
  );

  const [state, setState] = useState<RunStateSnapshot | null>(null);
  const [history, setHistory] = useState<RunHistorySnapshot | null>(null);
  const [viewingId, setViewingId] = useState<string | null>(null);

  // The current run, readable from inside a listener without making every
  // listener re-register whenever the run ticks.
  const liveRef = useRef(local);
  useEffect(() => {
    liveRef.current = local;
  });

  const publishing = role === "publisher" && transport !== null;
  const mirroring = role === "mirror" && transport !== null;

  // --- publisher -----------------------------------------------------------

  // The live run. Fires on every frame, because `progress` is a new object per
  // frame — that is the live-progress path. `elapsedMs` is deliberately not a
  // dependency: it ticks five times a second and the mirror derives it from
  // `startedAt` instead.
  useEffect(() => {
    if (!publishing || !transport) return;
    void transport.emit(
      RUN_STATE_EVENT,
      toStateSnapshot(liveRef.current, transport.label, nextVersion())
    );
  }, [
    publishing,
    transport,
    local.status,
    local.submitted,
    local.progress,
    local.result,
    local.error,
    local.startedAt,
  ]);

  // Past runs, on their own event: this only changes when a run finishes, and
  // folding it into the state snapshot would re-send every stored board on
  // every progress frame.
  useEffect(() => {
    if (!publishing || !transport) return;
    void transport.emit(
      RUN_HISTORY_EVENT,
      toHistorySnapshot(local.history, transport.label, nextVersion())
    );
  }, [publishing, transport, local.history]);

  // The late-subscriber half of the handshake. A workbench opened after a run
  // finished asks; this answers with the state as it stands right now, not with
  // a replay of frames it would have to reconstruct.
  useEffect(() => {
    if (!publishing || !transport) return;
    let live = true;
    let unlisten = () => {};

    void transport
      .listen(RUN_REQUEST_EVENT, (payload) => {
        if (!isRunRequest(payload) || payload.from === transport.label) return;
        void transport.emit(
          RUN_STATE_EVENT,
          toStateSnapshot(liveRef.current, transport.label, nextVersion())
        );
        void transport.emit(
          RUN_HISTORY_EVENT,
          toHistorySnapshot(liveRef.current.history, transport.label, nextVersion())
        );
      })
      .then((fn) => {
        if (live) unlisten = fn;
        else fn();
      });

    return () => {
      live = false;
      unlisten();
    };
  }, [publishing, transport]);

  // --- mirror --------------------------------------------------------------

  useEffect(() => {
    if (!mirroring || !transport) return;
    let live = true;
    const unlisteners: Array<() => void> = [];
    const keep = (fn: () => void) => {
      if (live) unlisteners.push(fn);
      else fn();
    };

    void (async () => {
      keep(
        await transport.listen(RUN_STATE_EVENT, (payload) => {
          if (!isRunStateSnapshot(payload)) return;
          if (payload.from === transport.label) return;
          setState((previous) =>
            acceptsSnapshot(previous, payload) ? payload : previous
          );
        })
      );
      keep(
        await transport.listen(RUN_HISTORY_EVENT, (payload) => {
          if (!isRunHistorySnapshot(payload)) return;
          if (payload.from === transport.label) return;
          setHistory((previous) =>
            acceptsSnapshot(previous, payload) ? payload : previous
          );
        })
      );

      // Only now — asking before both listeners are registered would race the
      // reply. This is what saves a workbench opened after the run ended: the
      // broadcast it missed is re-sent on request. If nobody answers, nothing
      // arrives and the bench keeps its honest empty state.
      if (!live) return;
      await transport.emit(RUN_REQUEST_EVENT, { from: transport.label });
    })();

    return () => {
      live = false;
      for (const fn of unlisteners) fn();
      unlisteners.length = 0;
    };
  }, [mirroring, transport]);

  // A live run must not be swapped out from under the user mid-flight, the
  // same guard the run hook applies to its own history rail.
  const liveStatus = state?.status;
  const selectRun = useCallback(
    (id: string | null) => {
      if (liveStatus === "running" || liveStatus === "starting") return;
      setViewingId(id);
    },
    [liveStatus]
  );

  // The clock the mirror shows while a run is live. `startedAt` came from the
  // publisher; this only turns it into a ticking number, exactly as the run
  // hook does in the overlay.
  const [, setTick] = useState(0);
  const ticking =
    mirroring && (liveStatus === "running" || liveStatus === "starting");
  useEffect(() => {
    if (!ticking) return;
    const timer = setInterval(() => setTick((n) => n + 1), 200);
    return () => clearInterval(timer);
  }, [ticking]);

  if (role !== "mirror") return local;
  return mirrorRun(local, { state, history, viewingId, selectRun });
}

/**
 * Listen for the navigation requests Rust sends when the user asks for the
 * review from the overlay.
 *
 * The dashboard window is created once at startup and then hidden and shown
 * rather than destroyed, so its URL only ever applies once. Without this, "Open
 * the full review" lands wherever the user last left the dashboard.
 *
 * The overlay never listens. Its route *is* its whole UI, so navigating it
 * would destroy the window the user is typing into — and Tauri would otherwise
 * deliver the event there too, because a listener registered with the default
 * `Any` target receives addressed emits meant for other windows. The explicit
 * target below closes that hole; the role check makes it moot.
 */
export function useNavigationRequests(
  onNavigate: (path: string) => void,
  options: { transport?: BridgeTransport | null } = {}
): void {
  const [transport] = useState<BridgeTransport | null>(() =>
    options.transport !== undefined ? options.transport : tauriTransport()
  );

  const handlerRef = useRef(onNavigate);
  useEffect(() => {
    handlerRef.current = onNavigate;
  });

  useEffect(() => {
    if (!transport) return;
    if (roleFor(transport.label) !== "mirror") return;
    let live = true;
    let unlisten = () => {};

    void transport
      .listen(NAVIGATE_EVENT, (payload) => {
        // Only in-app absolute paths. Anything else is not a route this app
        // owns, and a router is not a place to follow an arbitrary string.
        if (typeof payload !== "string") return;
        if (!payload.startsWith("/") || payload.startsWith("//")) return;
        handlerRef.current(payload);
      }, { target: transport.label })
      .then((fn) => {
        if (live) unlisten = fn;
        else fn();
      });

    return () => {
      live = false;
      unlisten();
    };
  }, [transport]);
}
