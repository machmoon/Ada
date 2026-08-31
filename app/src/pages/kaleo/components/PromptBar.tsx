import { useState } from "react";
import { CircuitBoardIcon, SettingsIcon, XIcon } from "lucide-react";
import {
  Button,
  Input,
  Popover,
  PopoverContent,
  PopoverTrigger,
  ScrollArea,
} from "@/components";
import type { EngineHealth } from "@/hooks";
import { useSilkscreenRun, type RunRequestDraft } from "@/contexts";
import { cn } from "@/lib/utils";
import { RunOptions } from "./RunOptions";
import { VoiceButton } from "./VoiceButton";
import { AttachDatasheetButton } from "./DatasheetBar";
import { RunHistoryPanel } from "./RunHistoryPanel";

/**
 * The engine's state as one dot.
 *
 * Three states, not two: before the first probe lands we do not know, and a
 * grey dot is the honest answer for that second — a green one would be a guess
 * and a red one would be an accusation.
 */
const EngineDot = ({
  engine,
  baseUrl,
}: {
  engine: EngineHealth;
  baseUrl: string;
}) => {
  const unknown = engine.lastCheckedAt === null;
  const label = unknown
    ? `Checking ${baseUrl}…`
    : engine.ok
      ? `Engine up at ${baseUrl}`
      : `Engine unreachable at ${baseUrl}${engine.detail ? ` — ${engine.detail}` : ""}`;

  return (
    <button
      type="button"
      className="flex size-4 shrink-0 items-center justify-center"
      title={`${label}. Click to re-check.`}
      aria-label={label}
      onClick={engine.recheck}
      data-testid="engine-status"
      data-online={unknown ? "unknown" : String(engine.ok)}
    >
      <span
        className={cn(
          "size-2 rounded-full",
          unknown && "bg-muted-foreground/40",
          engine.checking && "animate-pulse",
          !unknown && engine.ok && "bg-emerald-500",
          !unknown && !engine.ok && "bg-destructive"
        )}
      />
    </button>
  );
};

export interface PromptBarProps {
  request: RunRequestDraft;
  onRequestChange: (patch: Partial<RunRequestDraft>) => void;
  onSubmit: () => void;
  onCancel: () => void;
  /** The state machine's own guard: an in-flight run makes this false. */
  canStart: boolean;
  /** A run is in flight. */
  busy: boolean;
  /** The overlay is hidden by the global shortcut; keep focus out of it. */
  hidden: boolean;
  engine: EngineHealth;
  baseUrl: string;
}

export const PromptBar = ({
  request,
  onRequestChange,
  onSubmit,
  onCancel,
  canStart,
  busy,
  hidden,
  engine,
  baseUrl,
}: PromptBarProps) => {
  const [optionsOpen, setOptionsOpen] = useState(false);
  const submittable = canStart && !hidden;
  // Only for the bearer token — the run itself stays behind onSubmit.
  const { token } = useSilkscreenRun();

  return (
    <div className="flex flex-1 items-center gap-1.5">
      <EngineDot engine={engine} baseUrl={baseUrl} />

      <Input
        placeholder="What do you want on the board?"
        value={request.intent}
        disabled={busy || hidden}
        data-testid="prompt-input"
        className="flex-1"
        onChange={(e) => onRequestChange({ intent: e.target.value })}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && submittable) {
            e.preventDefault();
            onSubmit();
          }
        }}
      />

      <VoiceButton
        baseUrl={baseUrl}
        token={token}
        disabled={busy || hidden}
        // The transcript joins the draft through the same onChange path typing
        // uses, for the user to review and edit. It must never submit.
        onTranscript={(text) =>
          onRequestChange({
            intent: request.intent.trim()
              ? `${request.intent.trimEnd()} ${text}`
              : text,
          })
        }
      />

      {/* One control cluster, in the order the panel is used: say it, attach
          what backs it up, look at what was said before, tune the run. */}
      <AttachDatasheetButton disabled={busy || hidden} />

      <RunHistoryPanel busy={busy} />

      <Popover open={optionsOpen} onOpenChange={setOptionsOpen}>
        <PopoverTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            title="Run options"
            data-testid="prompt-options-trigger"
          >
            <SettingsIcon className="size-4" />
          </Button>
        </PopoverTrigger>
        <PopoverContent
          align="end"
          side="bottom"
          sideOffset={8}
          className="w-96 border border-input/50 p-4"
        >
          <ScrollArea className="max-h-[22rem]">
            <RunOptions request={request} onChange={onRequestChange} disabled={busy} />
          </ScrollArea>
        </PopoverContent>
      </Popover>

      {busy ? (
        <Button variant="outline" size="sm" onClick={onCancel} data-testid="prompt-cancel">
          <XIcon className="size-3.5" />
          Cancel
        </Button>
      ) : (
        <Button
          size="sm"
          onClick={onSubmit}
          disabled={!submittable}
          // This is the control that spends money. It says so, and it goes away
          // the moment a run starts, so one prompt cannot be fired twice.
          title={
            engine.ok
              ? "Generate a board — this calls the model and costs money"
              : "The engine is not answering; a run would fail immediately"
          }
          data-testid="prompt-submit"
        >
          <CircuitBoardIcon className="size-3.5" />
          Generate
        </Button>
      )}
    </div>
  );
};
