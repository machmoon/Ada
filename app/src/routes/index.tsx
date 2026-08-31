import { BrowserRouter as Router, Routes, Route } from "react-router-dom";
import { Kaleo, Workbench, Engine, Console, Settings, Shortcuts } from "@/pages";
import { DashboardLayout } from "@/layouts";
import { RunProvider } from "@/contexts";
import {
  loadEngineBaseUrl,
  loadEngineToken,
} from "@/pages/engine/components/EngineConnection";

export default function AppRoutes() {
  return (
    <Router>
      {/* RunProvider sits above both window trees: the overlay (/) starts
          runs, and the dashboard's workbench reads them. The two windows are
          separate webviews, so each gets its own provider instance — the
          overlay's live run does not appear in the dashboard's tree yet
          (known gap, see the PR notes). */}
      {/* Seed from the persisted (and re-validated on read) engine address,
          so runs target what the Engine page says they target. */}
      <RunProvider baseUrl={loadEngineBaseUrl()} token={loadEngineToken()}>
      <Routes>
        <Route path="/" element={<Kaleo />} />
        <Route element={<DashboardLayout />}>
          <Route path="/workbench" element={<Workbench />} />
          <Route path="/engine" element={<Engine />} />
          <Route path="/console" element={<Console />} />
          <Route path="/shortcuts" element={<Shortcuts />} />
          <Route path="/settings" element={<Settings />} />
        </Route>
      </Routes>
      </RunProvider>
    </Router>
  );
}
