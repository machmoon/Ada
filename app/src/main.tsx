import React from "react";
import ReactDOM from "react-dom/client";
import { AppProvider, ThemeProvider } from "./contexts";
import "./global.css";
import AppRoutes from "./routes";

// Only two windows exist now (the Kaleo bar and the dashboard); both render
// the same app and route by URL. The capture-overlay-* branch died with
// capture.rs -- nothing creates those windows anymore.
{
  ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
    <React.StrictMode>
      <ThemeProvider>
        <AppProvider>
          <AppRoutes />
        </AppProvider>
      </ThemeProvider>
    </React.StrictMode>
  );
}
