// The one mouth: a controller that speaks at most one utterance at a time.
//
// Two rules shape everything here. One utterance at a time — starting a new
// one stops the old, because a voice that talks over itself reads as broken.
// And `speak` never throws to the caller: voice is garnish, not the meal, so
// every failure mode — no key, no speechSynthesis, a 401, a refused
// autoplay — degrades to silence with one console warning that names the
// backend and the status, never the key.

import {
  createElevenLabsBackend,
  createWebSpeechBackend,
  type SpeechBackend,
} from "./backends";
import { loadVoiceSettings, type VoiceSettings } from "./settings";

export type BackendName = SpeechBackend["name"];

/**
 * Key present means ElevenLabs; absent means the free built-in voice. This is
 * the whole selection policy, kept as its own function so a test can pin it.
 */
export function chooseBackendName(apiKey: string | null | undefined): BackendName {
  return (apiKey ?? "").trim() ? "elevenlabs" : "webspeech";
}

export interface SpeakerDeps {
  /** Read fresh per utterance so a settings change applies to the next digest. */
  getSettings: () => VoiceSettings;
  makeBackend: (settings: VoiceSettings) => SpeechBackend;
  warn: (message: string) => void;
}

export interface Speaker {
  /** Speak `text`, stopping anything already playing. Never rejects. */
  speak(text: string): Promise<void>;
  stop(): void;
  isSpeaking(): boolean;
}

function defaultMakeBackend(settings: VoiceSettings): SpeechBackend {
  return chooseBackendName(settings.elevenLabsKey) === "elevenlabs"
    ? createElevenLabsBackend({
        apiKey: settings.elevenLabsKey,
        voiceId: settings.voiceId,
      })
    : createWebSpeechBackend();
}

/** Factory rather than a bare singleton so tests can inject fake backends. */
export function createSpeaker(deps?: Partial<SpeakerDeps>): Speaker {
  const getSettings = deps?.getSettings ?? loadVoiceSettings;
  const makeBackend = deps?.makeBackend ?? defaultMakeBackend;
  const warn =
    deps?.warn ?? ((message: string) => console.warn(`[kaleo voice] ${message}`));

  let current: SpeechBackend | null = null;
  // Each utterance takes a ticket; only the holder may clear the flag, so a
  // slow old utterance settling late cannot mark a newer one as finished.
  let ticket = 0;
  let speaking = false;

  const stop = () => {
    ticket += 1;
    speaking = false;
    try {
      current?.stop();
    } catch {
      // A backend failing to stop must not stop the caller.
    }
    current = null;
  };

  return {
    async speak(text: string): Promise<void> {
      const trimmed = text.trim();
      if (!trimmed) return;
      stop();
      const mine = ticket;
      let backend: SpeechBackend;
      try {
        backend = makeBackend(getSettings());
      } catch (error) {
        warn(`voice unavailable: ${(error as Error)?.message ?? "unknown"}`);
        return;
      }
      current = backend;
      speaking = true;
      try {
        await backend.speak(trimmed);
      } catch (error) {
        // Degrade to silence. The message never carries the API key: the
        // backends are written to throw status codes and API names only.
        warn(
          `${backend.name} text-to-speech failed: ${
            (error as Error)?.message ?? "unknown"
          }`
        );
      } finally {
        if (ticket === mine) {
          speaking = false;
          current = null;
        }
      }
    },
    stop,
    isSpeaking(): boolean {
      return speaking;
    },
  };
}

/**
 * The app-wide instance. One per webview is exactly right: the point is that
 * the whole window shares a single voice that never talks over itself.
 */
export const speaker: Speaker = createSpeaker();
