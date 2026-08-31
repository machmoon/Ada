// The mute button for the spoken run digest.
//
// Its own component (and file) so the wire-up into the overlay stays a
// two-line diff — another session works in these pages concurrently.
// It owns nothing but the persisted on/off flag: what gets spoken and when
// lives in `useRunVoice`, and the backends never know this button exists.

import { useState } from "react";
import { Volume2Icon, VolumeXIcon } from "lucide-react";
import { Button } from "@/components";
import { isVoiceEnabled, saveVoiceEnabled, speaker } from "@/lib/speech";

export const VoiceToggle = ({ className }: { className?: string }) => {
  const [enabled, setEnabled] = useState(isVoiceEnabled);

  const toggle = () => {
    const next = !enabled;
    setEnabled(next);
    saveVoiceEnabled(next);
    // Muting means "stop talking", not "finish this sentence first".
    if (!next) speaker.stop();
  };

  return (
    <Button
      size="icon"
      variant="ghost"
      className={className}
      title={enabled ? "Voice on — mute the spoken digest" : "Voice off — unmute"}
      aria-label={enabled ? "Mute the spoken digest" : "Unmute the spoken digest"}
      aria-pressed={enabled}
      onClick={toggle}
      data-testid="voice-toggle"
      data-enabled={enabled ? "1" : "0"}
    >
      {enabled ? (
        <Volume2Icon className="size-4" />
      ) : (
        <VolumeXIcon className="size-4 text-muted-foreground" />
      )}
    </Button>
  );
};
