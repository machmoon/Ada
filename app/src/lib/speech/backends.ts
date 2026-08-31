// The two ways Kaleo can turn text into sound, behind one interface.
//
// `webspeech` is the default because it costs nothing and needs nothing: the
// webview's own `speechSynthesis`, which WKWebView (Tauri on macOS) and the
// Chromium webviews both ship. Its absence is checked at runtime and treated
// as "voice unavailable", never a crash — an embedded webview build without
// it must degrade to silence.
//
// `elevenlabs` is the paid upgrade, selected purely by the presence of an API
// key. The fetch goes through `@tauri-apps/plugin-http` exactly like the
// silkscreen client's — the app origin is `tauri://localhost`, so a webview
// fetch would be cross-origin, and the capability files already allow
// `https://**`. The API key travels in the `xi-api-key` header and NOWHERE
// else: not in a log, not in an error message, not in a thrown value.

import { fetch as tauriFetch } from "@tauri-apps/plugin-http";

export interface SpeechBackend {
  readonly name: "webspeech" | "elevenlabs";
  /**
   * Speak one utterance; resolves when playback ends, is stopped, or fails.
   * Rejections carry no secrets — the controller catches and logs them.
   */
  speak(text: string): Promise<void>;
  /** Cut playback now. Safe to call when nothing is playing. */
  stop(): void;
}

/** ElevenLabs' "Rachel", their long-standing default demo voice. */
export const DEFAULT_ELEVENLABS_VOICE_ID = "21m00Tcm4TlvDq8ikWAM";
export const ELEVENLABS_MODEL_ID = "eleven_multilingual_v2";
/** A digest is a few sentences; a minute is already generous. */
export const ELEVENLABS_TIMEOUT_MS = 30_000;

type SpeechWindow = {
  speechSynthesis?: SpeechSynthesis;
  SpeechSynthesisUtterance?: typeof SpeechSynthesisUtterance;
};

function speechGlobals(): Required<SpeechWindow> | null {
  const w = globalThis as SpeechWindow;
  // Verified defensively at runtime, not assumed from documentation: a
  // webview without the API must read as "no voice", never as a TypeError.
  if (!("speechSynthesis" in w) || !w.speechSynthesis) return null;
  if (typeof w.SpeechSynthesisUtterance !== "function") return null;
  return {
    speechSynthesis: w.speechSynthesis,
    SpeechSynthesisUtterance: w.SpeechSynthesisUtterance,
  };
}

export function webSpeechAvailable(): boolean {
  return speechGlobals() !== null;
}

export function createWebSpeechBackend(): SpeechBackend {
  return {
    name: "webspeech",
    speak(text: string): Promise<void> {
      const globals = speechGlobals();
      if (!globals) {
        return Promise.reject(
          new Error("speechSynthesis is not available in this webview")
        );
      }
      return new Promise<void>((resolve) => {
        const utterance = new globals.SpeechSynthesisUtterance(text);
        // `end` fires on natural completion AND after cancel(); `error` fires
        // on everything else. Either way the promise settles — a voice that
        // leaves a promise hanging leaves the speaking flag stuck on.
        utterance.onend = () => resolve();
        utterance.onerror = () => resolve();
        globals.speechSynthesis.cancel();
        globals.speechSynthesis.speak(utterance);
      });
    },
    stop(): void {
      speechGlobals()?.speechSynthesis.cancel();
    },
  };
}

/**
 * One text-to-speech call, returning the mp3 bytes.
 *
 * Split out from playback so a test can assert the request shape — URL, the
 * `xi-api-key` header, the JSON body — against a mocked fetch without ever
 * needing an Audio element or a real key.
 */
export async function fetchElevenLabsAudio(
  apiKey: string,
  text: string,
  voiceId: string = DEFAULT_ELEVENLABS_VOICE_ID
): Promise<Blob> {
  const id = voiceId.trim() || DEFAULT_ELEVENLABS_VOICE_ID;
  const response = await tauriFetch(
    `https://api.elevenlabs.io/v1/text-to-speech/${encodeURIComponent(id)}`,
    {
      method: "POST",
      headers: {
        "xi-api-key": apiKey,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ text, model_id: ELEVENLABS_MODEL_ID }),
      signal: AbortSignal.timeout(ELEVENLABS_TIMEOUT_MS),
    }
  );
  if (!response.ok) {
    // Status only. The response body could echo request details and the
    // message may end up in a console line, so neither the body nor the key
    // is allowed anywhere near this string.
    throw new Error(`ElevenLabs answered ${response.status}`);
  }
  return await response.blob();
}

export interface ElevenLabsBackendOptions {
  apiKey: string;
  voiceId?: string;
}

export function createElevenLabsBackend(
  options: ElevenLabsBackendOptions
): SpeechBackend {
  let audio: HTMLAudioElement | null = null;
  let objectUrl: string | null = null;

  const cleanup = () => {
    if (audio) {
      audio.onended = null;
      audio.onerror = null;
      audio.pause();
      audio = null;
    }
    if (objectUrl) {
      // Blob URLs pin their bytes until revoked; a digest per run would
      // otherwise leak an mp3 per board for the life of the window.
      URL.revokeObjectURL(objectUrl);
      objectUrl = null;
    }
  };

  return {
    name: "elevenlabs",
    async speak(text: string): Promise<void> {
      const blob = await fetchElevenLabsAudio(
        options.apiKey,
        text,
        options.voiceId
      );
      cleanup();
      await new Promise<void>((resolve, reject) => {
        objectUrl = URL.createObjectURL(blob);
        audio = new Audio(objectUrl);
        audio.onended = () => {
          cleanup();
          resolve();
        };
        audio.onerror = () => {
          cleanup();
          reject(new Error("audio playback failed"));
        };
        audio.play().catch((error: unknown) => {
          cleanup();
          reject(
            new Error((error as Error)?.message || "audio playback refused")
          );
        });
      });
    },
    stop(): void {
      cleanup();
    },
  };
}
