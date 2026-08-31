import { useNavigate } from "react-router-dom";
import { CircuitBoardIcon, CableIcon, TerminalIcon } from "lucide-react";
import { PageLayout } from "@/layouts";
import { Button } from "@/components";

/**
 * The dashboard window's landing route, and where the sidebar logo points.
 *
 * Upstream filled this with a licence panel, a "Pluely API" key form and a
 * token-usage chart fed by the vendor's `get_activity` endpoint. All three went
 * with the SaaS stack; nothing here replaces them, because Kaleo sells nothing
 * and meters nothing. What is left is a signpost to the surfaces that do work.
 */
const DESTINATIONS = [
  {
    icon: CircuitBoardIcon,
    label: "Workbench",
    href: "/workbench",
    blurb: "The finished board, its review findings and its artifacts.",
  },
  {
    icon: CableIcon,
    label: "Engine",
    href: "/engine",
    blurb: "Point the app at a silkscreen engine and check it answers.",
  },
  {
    icon: TerminalIcon,
    label: "Console",
    href: "/console",
    blurb: "The raw log of what the engine did on the last run.",
  },
];

const Dashboard = () => {
  const navigate = useNavigate();

  return (
    <PageLayout
      title="Ada"
      description="A desktop client for the silkscreen PCB engine. Describe a board in the overlay, and the run lands here."
    >
      <div className="grid gap-3 sm:grid-cols-3">
        {DESTINATIONS.map((destination) => (
          <button
            key={destination.href}
            onClick={() => navigate(destination.href)}
            className="flex flex-col items-start gap-2 rounded-xl border border-input/50 p-4 text-left transition-colors hover:bg-accent"
          >
            <destination.icon className="size-5 text-muted-foreground" />
            <span className="text-sm font-medium">{destination.label}</span>
            <span className="text-xs text-muted-foreground">
              {destination.blurb}
            </span>
          </button>
        ))}
      </div>

      <div className="rounded-md border border-input/50 p-4 text-xs text-muted-foreground space-y-2">
        <p>
          Runs start in the floating overlay, not here. Summon it with the
          toggle shortcut, describe the board you want, and open the review
          window when it finishes.
        </p>
        <p>
          The app talks to a silkscreen engine you run yourself, on loopback.
          Set its address on the{" "}
          <Button
            variant="link"
            size="sm"
            className="h-auto p-0 text-xs"
            onClick={() => navigate("/engine")}
          >
            Engine
          </Button>{" "}
          page.
        </p>
      </div>
    </PageLayout>
  );
};

export default Dashboard;
