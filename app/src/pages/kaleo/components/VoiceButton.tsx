import { CheckIcon, Loader2Icon, MicIcon, XIcon } from "lucide-react";
import { Button } from "@/components";
import { useVoiceInput } from "@/hooks/useVoiceInput";
import { MAX_RECORDING_MS } from "@/lib/silkscreen/voice";

const MAX_RECORDING_S = Math.floor(MAX_RECORDING_MS / 1000);

export interface VoiceButtonProps {
  baseUrl: string;
  token?: string;
  disabled?: boolean;
  /** Receives the transcript for the intent draft. Never submits anything. */
  onTranscript: (text: string) => void;
}

/**
 * Speak the intent instead of typing it.
 *
 * The transcript only ever lands in the draft for the user to read and edit —
 * transcription is cheap, generation is not, and nothing here can reach the
 * submit path. The states are honest by construction: the dot pulses only
 * while the recorder runs, the seconds come from a real clock, and the spinner
 * shows only while a real request is in flight.
 */
export const VoiceButton = ({
  baseUrl,
  token,
  disabled,
  onTranscript,
}: VoiceButtonProps) => {
  const voice = useVoiceInput({ baseUrl, token, onTranscript });

  if (voice.status === "recording") {
    return (
      <div
        className="flex shrink-0 items-center gap-1 rounded-md border border-input/50 py-0.5 pl-2"
        data-testid="voice-recording"
      >
        <span className="size-2 shrink-0 animate-pulse rounded-full bg-destructive" />
        <span
          className="font-mono text-xs tabular-nums text-muted-foreground"
          data-testid="voice-elapsed"
        >
          {voice.elapsedS}s/{MAX_RECORDING_S}s
        </span>
        <Button
          variant="ghost"
          size="icon"
          className="size-6"
          onClick={voice.stop}
          title="Stop and transcribe"
          data-testid="voice-stop"
        >
          <CheckIcon className="size-3.5" />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          className="size-6"
          onClick={voice.cancel}
          title="Discard recording"
          data-testid="voice-cancel"
        >
          <XIcon className="size-3.5" />
        </Button>
      </div>
    );
  }

  if (voice.status === "transcribing") {
    return (
      <div className="flex shrink-0 items-center gap-1" data-testid="voice-transcribing">
        <Loader2Icon className="size-4 animate-spin text-muted-foreground" />
        <Button
          variant="ghost"
          size="icon"
          className="size-6"
          onClick={voice.cancel}
          title="Cancel transcription"
          data-testid="voice-cancel"
        >
          <XIcon className="size-3.5" />
        </Button>
      </div>
    );
  }

  // Idle and error share the mic control; error adds the real message beside it.
  return (
    <div className="flex min-w-0 shrink-0 items-center gap-1">
      {voice.status === "error" && (
        <span
          className="max-w-40 truncate text-xs text-destructive"
          title={voice.error}
          data-testid="voice-error"
        >
          {voice.error}
        </span>
      )}
      <Button
        variant="ghost"
        size="icon"
        disabled={disabled}
        onClick={() => void voice.start()}
        title="Dictate the prompt — transcribed text lands in the field for review"
        data-testid="voice-start"
      >
        <MicIcon className="size-4" />
      </Button>
    </div>
  );
};
