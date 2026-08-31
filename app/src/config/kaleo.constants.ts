// Constants for Kaleo's own surfaces. Deliberately a separate file from
// `constants.ts`: that one is inherited Pluely configuration, and keeping the
// silkscreen keys out of it makes the fork's additions greppable.

/**
 * Storage keys, namespaced under `silkscreen_` so they cannot collide with the
 * Pluely keys sharing this localStorage.
 */
export const KALEO_STORAGE_KEYS = {
  ENGINE_BASE_URL: "silkscreen_engine_base_url",
  /**
   * The optional bearer token for an engine deployed behind a token gate.
   * localStorage on purpose: it is how this app already stores provider API
   * keys (no JS in this build touches the keychain plugin), and the engine is
   * loopback-only here anyway.
   */
  ENGINE_TOKEN: "silkscreen_engine_token",
  /**
   * The spoken-digest switch ("1"/"0", absent means on) and the optional
   * ElevenLabs credentials. The key is localStorage for the same reason the
   * engine token is; its value must never appear in a log or error string —
   * see `src/lib/speech/`.
   */
  VOICE_ENABLED: "silkscreen_voice_enabled",
  ELEVENLABS_KEY: "silkscreen_elevenlabs_key",
  ELEVENLABS_VOICE_ID: "silkscreen_elevenlabs_voice_id",
} as const;

/**
 * Hostnames the engine may live on.
 *
 * Loopback only, and the check is a hard refusal rather than a warning. The
 * service ships no authentication and no CORS headers by design, so a base URL
 * pointing anywhere else hands an unauthenticated `/generate` — which spends
 * the user's Gemini quota on every call — to whoever can reach that address.
 * `127.0.0.0/8` is matched by prefix because the whole block is loopback.
 */
export const LOOPBACK_HOSTS = ["localhost", "::1", "[::1]"] as const;

/**
 * How to start the engine, quoted from the repository's own docs rather than
 * paraphrased — a command that does not run is worse than no command.
 *
 * The `.env` line is the one that catches people: `service/app.py` reads the
 * process environment only and has no dotenv loader, so a service started
 * directly with a `.env` sitting next to it comes up keyless and answers
 * `/generate` with a 502 naming `GOOGLE_API_KEY`. `silkscreen serve` loads
 * `.env` itself, which is why it is listed first.
 */
export interface EngineStartStep {
  id: string;
  title: string;
  detail: string;
  command: string;
}

export const ENGINE_START_STEPS: EngineStartStep[] = [
  {
    id: "serve",
    title: "The easy way",
    detail:
      "`silkscreen serve` loads .env itself before starting the server, so the key is already in the environment. Run it from the repository checkout.",
    command: "silkscreen serve --port 8081",
  },
  {
    id: "module",
    title: "Running the service module directly",
    detail:
      "python -m service.app does NOT read .env — cli.py has the dotenv loader, service/app.py reads the process environment only. Export GOOGLE_API_KEY yourself or the engine comes up keyless and every run fails with a 502 naming that variable.",
    command: "set -a && . ./.env && set +a && PORT=8081 python -m service.app",
  },
  {
    id: "module-powershell",
    title: "The same thing on PowerShell",
    detail:
      "Windows equivalent of exporting the key before starting the module.",
    command: '$env:GOOGLE_API_KEY = "..."; $env:PORT = "8081"; python -m service.app',
  },
  {
    id: "install",
    title: "If nothing is installed yet",
    detail:
      "Create the venv and install the engine editable, from the repository root. The install is ~400 MB, almost all of it OR-Tools.",
    command:
      'python3 -m venv .venv && ./.venv/bin/pip install -e ".[dev,agents,cloud,adk]"',
  },
];
