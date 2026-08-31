import { BrowserRouter as Router, Routes, Route } from "react-router-dom";
import {
  Dashboard,
  App,
  Kaleo,
  Workbench,
  Engine,
  Console,
  SystemPrompts,
  ViewChat,
  Settings,
  DevSpace,
  Shortcuts,
  Audio,
  Screenshot,
  Chats,
  Responses,
} from "@/pages";
import { DashboardLayout } from "@/layouts";
import { RunProvider } from "@/contexts";

export default function AppRoutes() {
  return (
    <Router>
      {/* RunProvider sits above both window trees: the overlay (/) starts
          runs, and the dashboard's workbench reads them. The two windows are
          separate webviews, so each gets its own provider instance — the
          overlay's live run does not appear in the dashboard's tree yet
          (known gap, see the PR notes). */}
      <RunProvider>
      <Routes>
        <Route path="/" element={<Kaleo />} />
        {/* Pluely's chat bar, kept until the removal pass: */}
        <Route path="/pluely" element={<App />} />
        <Route element={<DashboardLayout />}>
          <Route path="/workbench" element={<Workbench />} />
          <Route path="/engine" element={<Engine />} />
          <Route path="/console" element={<Console />} />
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/chats" element={<Chats />} />
          <Route path="/system-prompts" element={<SystemPrompts />} />
          <Route path="/chats/view/:conversationId" element={<ViewChat />} />
          <Route path="/shortcuts" element={<Shortcuts />} />
          <Route path="/screenshot" element={<Screenshot />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/audio" element={<Audio />} />
          <Route path="/responses" element={<Responses />} />
          <Route path="/dev-space" element={<DevSpace />} />
        </Route>
      </Routes>
      </RunProvider>
    </Router>
  );
}
