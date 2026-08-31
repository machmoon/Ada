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
import { RunProvider } from "@/contexts";
import { useNavigationRequests } from "@/hooks/useRunBridge";
import {
  loadEngineBaseUrl,
  loadEngineToken,
} from "@/pages/engine/components/EngineConnection";

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
          overlay publishes its run over the Tauri event bridge and the
          dashboard's provider mirrors it (see hooks/useRunBridge.ts). */}
      {/* Seed from the persisted (and re-validated on read) engine address,
          so runs target what the Engine page says they target. */}
      <RunProvider baseUrl={loadEngineBaseUrl()} token={loadEngineToken()}>
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
