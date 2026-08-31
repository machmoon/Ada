// Voice settings, stored the way the engine token is stored: localStorage
// through `safeLocalStorage`, keys namespaced in `KALEO_STORAGE_KEYS`.
//
// The ElevenLabs key lives here under the same reasoning as the engine
// token — this app already keeps provider API keys in localStorage and no JS
// in this build touches the keychain plugin. The one rule that is absolute:
// the key's VALUE never reaches a log line, an error message, or an export.
// Nothing in `src/lib/speech/` interpolates it into anything but the
// `xi-api-key` request header.

import { safeLocalStorage } from "@/lib/storage/helper";
import { KALEO_STORAGE_KEYS } from "@/config/kaleo.constants";

export interface VoiceSettings {
  /** The on/off switch for the spoken digest. */
  enabled: boolean;
  /** ElevenLabs API key; empty string means "use the browser's own voice". */
  elevenLabsKey: string;
  /** Optional ElevenLabs voice id; empty string means the default voice. */
  voiceId: string;
}

/**
 * Voice defaults ON: the webspeech backend is free, offline and needs no
 * account, so the demo moment works on a fresh install with nothing
 * configured. The stored value is only consulted to turn it OFF.
 */
export function isVoiceEnabled(): boolean {
  return safeLocalStorage.getItem(KALEO_STORAGE_KEYS.VOICE_ENABLED) !== "0";
}

export function saveVoiceEnabled(enabled: boolean): void {
  // "1"/"0" rather than remove-on-true so an explicit choice is
  // distinguishable from "never touched" if the default ever changes.
  safeLocalStorage.setItem(KALEO_STORAGE_KEYS.VOICE_ENABLED, enabled ? "1" : "0");
}

export function loadElevenLabsKey(): string {
  return (safeLocalStorage.getItem(KALEO_STORAGE_KEYS.ELEVENLABS_KEY) ?? "").trim();
}

export function saveElevenLabsKey(key: string): void {
  const trimmed = key.trim();
  if (trimmed) {
    safeLocalStorage.setItem(KALEO_STORAGE_KEYS.ELEVENLABS_KEY, trimmed);
  } else {
    // An empty field means "back to the free voice", not "store an empty key".
    safeLocalStorage.removeItem(KALEO_STORAGE_KEYS.ELEVENLABS_KEY);
  }
}

export function loadElevenLabsVoiceId(): string {
  return (
    safeLocalStorage.getItem(KALEO_STORAGE_KEYS.ELEVENLABS_VOICE_ID) ?? ""
  ).trim();
}

export function saveElevenLabsVoiceId(voiceId: string): void {
  const trimmed = voiceId.trim();
  if (trimmed) {
    safeLocalStorage.setItem(KALEO_STORAGE_KEYS.ELEVENLABS_VOICE_ID, trimmed);
  } else {
    safeLocalStorage.removeItem(KALEO_STORAGE_KEYS.ELEVENLABS_VOICE_ID);
  }
}

export function loadVoiceSettings(): VoiceSettings {
  return {
    enabled: isVoiceEnabled(),
    elevenLabsKey: loadElevenLabsKey(),
    voiceId: loadElevenLabsVoiceId(),
  };
}
