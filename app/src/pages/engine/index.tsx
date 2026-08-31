import { useState } from "react";
import { CheckIcon, CopyIcon, TerminalIcon } from "lucide-react";
import { Button, Header } from "@/components";
import { PageLayout } from "@/layouts";
import { useCopyToClipboard, useEngineHealth } from "@/hooks";
import { useSilkscreenRun } from "@/contexts/run.context";
import { ENGINE_START_STEPS, type EngineStartStep } from "@/config/kaleo.constants";
import {
  EngineConnection,
  EngineStatus,
  VoiceSettings,
  loadEngineBaseUrl,
  loadEngineToken,
} from "./components";

/** One quoted command with a copy button. Its own component for the hook. */
const CommandBlock = ({ command }: { command: string }) => {
  const { isCopied, handleCopy } = useCopyToClipboard({ text: command });
  return (
    <div className="flex items-start gap-2 rounded-xl border border-input/50 bg-muted/40 p-2.5">
      <TerminalIcon className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
      <code
        data-testid="engine-start-command"
        className="flex-1 font-mono text-[11px] lg:text-xs leading-relaxed break-all select-text"
      >
        {command}
      </code>
      <Button
        size="icon"
        variant="ghost"
        className="size-6 shrink-0"
        title={isCopied ? "Copied" : "Copy command"}
        aria-label={isCopied ? "Copied" : "Copy command"}
        onClick={handleCopy}
      >
        {isCopied ? (
          <CheckIcon className="size-3" />
        ) : (
          <CopyIcon className="size-3" />
        )}
      </Button>
    </div>
  );
};

const StartStep = ({ step }: { step: EngineStartStep }) => (
  <div data-testid="engine-start-step" data-step={step.id} className="space-y-1.5">
    <p className="text-sm font-medium">{step.title}</p>
    <p className="text-xs leading-relaxed text-muted-foreground">
      {step.detail}
    </p>
    <CommandBlock command={step.command} />
  </div>
);

/**
 * The engine page: point Kaleo at a running service, and prove it is alive.
 *
 * This page exists because "connection refused" is not a diagnosis. Every
 * other surface in the app fails the same opaque way when the Python service
 * is not running, and the service is a separate process the user starts by
 * hand — so somewhere has to say which address is in use, whether anything is
 * answering there, and what to type if nothing is.
 */
const Engine = () => {
  // Read once: a rerender must not reach back into storage and stomp a value
  // the user just changed.
  const [baseUrl, setBaseUrl] = useState(loadEngineBaseUrl);
  const [token, setToken] = useState(loadEngineToken);
  // The poll restarts on its own when `baseUrl` changes — the hook keys on it.
  const health = useEngineHealth(baseUrl, undefined, token);
  // Saving here must reach the run layer too, or the address on this page and
  // the address runs actually target quietly diverge until the next launch.
  const run = useSilkscreenRun();
  const applyBaseUrl = (url: string) => {
    setBaseUrl(url);
    run.setBaseUrl(url);
  };
  const applyToken = (value: string) => {
    setToken(value);
    run.setToken(value);
  };

  return (
    <PageLayout
      title="Engine"
      description="The silkscreen service that generates boards"
      rightSlot={
        <div className="flex items-center gap-2">
          <EngineStatus health={health} showDetail />
          <Button
            size="sm"
            variant="outline"
            onClick={health.recheck}
            disabled={health.checking}
          >
            Recheck
          </Button>
        </div>
      }
    >
      <EngineConnection
        baseUrl={baseUrl}
        onBaseUrlChange={applyBaseUrl}
        token={token}
        onTokenChange={applyToken}
      />

      <VoiceSettings />

      <div id="engine-start" className="space-y-3">
        <Header
          title="Starting the engine"
          description="Kaleo does not start or bundle the service — it is a separate Python process you run from the silkscreen checkout"
          isMainTitle
        />
        <div className="space-y-4">
          {ENGINE_START_STEPS.map((step) => (
            <StartStep key={step.id} step={step} />
          ))}
        </div>
        <p className="text-xs leading-relaxed text-muted-foreground">
          The API key caveat is not a footnote: only the CLI reads{" "}
          <code className="font-mono">.env</code>.{" "}
          <code className="font-mono">cli.py</code> has a small dotenv loader
          and <code className="font-mono">service/app.py</code> does not — it
          reads the process environment only. A service started directly next to
          a filled-in <code className="font-mono">.env</code> therefore comes up
          with no key and answers every run with a 502 naming{" "}
          <code className="font-mono">GOOGLE_API_KEY</code>.
        </p>
      </div>

      <div id="engine-boundary" className="space-y-2">
        <Header
          title="Why loopback only"
          description="The constraint the address field enforces"
          isMainTitle
        />
        <p className="text-xs leading-relaxed text-muted-foreground">
          The service ships no authentication and sends no CORS headers, on
          purpose — it assumes it is reachable only from the machine it runs on.
          Every run it accepts spends your Gemini quota. So Kaleo refuses any
          address that is not <code className="font-mono">http://</code> on
          loopback: accepting one would put an open, billable endpoint on the
          network, and neither this app nor the service would notice.
        </p>
      </div>
    </PageLayout>
  );
};

export default Engine;
