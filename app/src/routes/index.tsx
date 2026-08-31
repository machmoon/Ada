import { useEffect } from "react";
import {
  BrowserRouter as Router,
  Routes,
  Route,
  useNavigate,
} from "react-router-dom";
import {
  Dashboard,
  Kaleo,
  Workbench,
  Engine,
  Console,
  Settings,
  Shortcuts,
  Audio,
} from "@/pages";
import { DashboardLayout } from "@/layouts";
import { RunProvider, useSilkscreenRun } from "@/contexts";
import { useNavigationRequests } from "@/hooks/useRunBridge";
import {
  loadEngineBaseUrl,
  loadEngineToken,
} from "@/pages/engine/components/EngineConnection";
import { KALEO_STORAGE_KEYS } from "@/config/kaleo.constants";

/**
 * Keep this window's engine address and token in step with the other window.
 *
 * Each webview seeds its provider from storage once, at creation — so before
 * this component existed, saving a new address on the dashboard's Engine page
 * left the overlay POSTing at the old one forever. The `storage` event fires
 * in every window except the writer, and the loaders re-validate on read, so
 * a hand-edited key still cannot smuggle in a non-loopback address.
 */
function EngineSettingsSync() {
  const run = useSilkscreenRun();
  useEffect(() => {
    const handler = (event: StorageEvent) => {
      if (event.key === KALEO_STORAGE_KEYS.ENGINE_BASE_URL) {
        run.setBaseUrl(loadEngineBaseUrl());
      }
      if (event.key === KALEO_STORAGE_KEYS.ENGINE_TOKEN) {
        run.setToken(loadEngineToken());
      }
    };
    window.addEventListener("storage", handler);
    return () => window.removeEventListener("storage", handler);
  }, [run]);
  return null;
}

/**
 * Lets the overlay say "show the review" to the dashboard window.
 *
 * The dashboard is created once at startup and thereafter hidden and shown,
 * never destroyed, so the URL it was built with only applies the first time.
 * `open_dashboard` in `window.rs` emits the route it wants; this puts it there.
 * Must sit inside `<Router>` — `useNavigate` needs the router context.
 */
const NavigationBridge = () => {
  const navigate = useNavigate();
  useNavigationRequests(navigate);
  return null;
};

export default function AppRoutes() {
  return (
    <Router>
      <NavigationBridge />
      {/* RunProvider sits above both window trees: the overlay (/) starts
          runs, and the dashboard's workbench reads them. The two windows are
          separate webviews, so each gets its own provider instance — the
          overlay streams its live run over the Tauri event bridge (see
          hooks/useRunBridge.ts) and additionally announces each FINISHED run
          over the storage bridge (lib/silkscreen/bridge.ts), which the
          dashboard adopts; engine settings cross windows via
          EngineSettingsSync below. */}
      {/* Seed from the persisted (and re-validated on read) engine address,
          so runs target what the Engine page says they target. */}
      <RunProvider baseUrl={loadEngineBaseUrl()} token={loadEngineToken()}>
      <EngineSettingsSync />
      <Routes>
        <Route path="/" element={<Kaleo />} />
        <Route element={<DashboardLayout />}>
          <Route path="/workbench" element={<Workbench />} />
          <Route path="/engine" element={<Engine />} />
          <Route path="/console" element={<Console />} />
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/shortcuts" element={<Shortcuts />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/audio" element={<Audio />} />
        </Route>
      </Routes>
      </RunProvider>
    </Router>
  );
}
