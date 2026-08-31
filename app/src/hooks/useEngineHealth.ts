import { useCallback, useEffect, useRef, useState } from "react";
import { DEFAULT_BASE_URL, health } from "@/lib/silkscreen/client";

/** Long enough that a status dot is never the reason the engine is busy. */
export const HEALTH_POLL_INTERVAL_MS = 10_000;

export interface EngineHealth {
  /** The URL this result is about, so a caller need not track it separately. */
  baseUrl: string;
  /** True only when `/healthz` answered `ok: true`. Never optimistic. */
  ok: boolean;
  /** Why it is not ok, in the service's own words. Empty when ok. */
  detail: string;
  /** A probe is in flight right now. */
  checking: boolean;
  /** Epoch ms of the last completed probe, or null before the first one. */
  lastCheckedAt: number | null;
  /** Probe now. A no-op while a probe is already in flight. */
  recheck: () => void;
}

/**
 * Poll the engine's `/healthz` on a timer.
 *
 * The engine not running is an ORDINARY state for this app — Kaleo is a
 * desktop app and the Python service is a separate process the user starts —
 * so this never throws and never surfaces as an error. `client.health()`
 * already returns its reason instead of raising; this hook only adds the
 * timer, the in-flight guard and the unmount guard.
 *
 * Polling during a run is safe: the service is a `ThreadingHTTPServer`, so a
 * `/healthz` GET does not queue behind a streaming `/generate/stream`.
 */
export function useEngineHealth(
  baseUrl: string = DEFAULT_BASE_URL,
  intervalMs: number = HEALTH_POLL_INTERVAL_MS
): EngineHealth {
  const [state, setState] = useState<{
    ok: boolean;
    detail: string;
    lastCheckedAt: number | null;
  }>({ ok: false, detail: "", lastCheckedAt: null });
  const [checking, setChecking] = useState(false);

  const mountedRef = useRef(true);
  // Only one probe at a time, so a fast `recheck()` finger cannot stack them.
  const inFlightRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const probe = useCallback(async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    if (mountedRef.current) setChecking(true);
    try {
      const result = await health(baseUrl);
      if (!mountedRef.current) return;
      setState({
        ok: result.ok,
        detail: result.detail,
        lastCheckedAt: Date.now(),
      });
    } catch (error) {
      // `health()` is documented not to throw; if that ever changes, a status
      // dot is still not worth an unhandled rejection.
      if (!mountedRef.current) return;
      setState({
        ok: false,
        detail: (error as Error)?.message ?? "unreachable",
        lastCheckedAt: Date.now(),
      });
    } finally {
      inFlightRef.current = false;
      if (mountedRef.current) setChecking(false);
    }
  }, [baseUrl]);

  useEffect(() => {
    void probe();
    const timer = window.setInterval(() => void probe(), intervalMs);
    return () => window.clearInterval(timer);
  }, [probe, intervalMs]);

  const recheck = useCallback(() => {
    void probe();
  }, [probe]);

  return {
    baseUrl,
    ok: state.ok,
    detail: state.detail,
    checking,
    lastCheckedAt: state.lastCheckedAt,
    recheck,
  };
}
