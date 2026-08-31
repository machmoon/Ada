# Fork notice and attribution

This directory is a fork of **Pluely** by Srikanth Nani.

- Upstream: https://github.com/iamsrikanthnani/pluely
- Forked at commit `62aa2d3d0390b832ac8a2b0cc9556fc096e58a98` (v1.0.0, 2026-07-14)
- Upstream licence: **GPL-3.0**, preserved verbatim in `app/LICENSE`
- Upstream's own README is kept unmodified as `app/UPSTREAM-README.md`

## Licensing, and why this directory is not MIT

The rest of this repository is MIT (see the root `LICENSE`). **This directory is
not.** Pluely is GPL-3.0, so everything in `app/` — and any binary built from it —
is GPL-3.0. Do not copy code out of `app/` into `engine/`, `service/`, or
`frontend/`: that would relicense the thing you pasted it into, which is the one
mistake in this arrangement that cannot be undone quietly.

The boundary that keeps the Python side MIT is **process separation**. The
desktop app talks to `service/app.py` over HTTP, exactly as the browser does.
They are two programs that communicate, not one program in two languages. The
engine is independently useful, independently installable (`pip install
silkscreen`), and has no build-time or link-time dependency on anything here.

Keeping that true is a design constraint, not a formality:

- Nothing in `app/` may import from, vendor, or embed the Python source.
- Communication is HTTP against the documented `/generate`, `/generate/stream`,
  and `/healthz` surface. If the app needs something the API does not expose, the
  fix is to extend the API, not to reach across the boundary.
- If the app ever bundles a Python runtime, it bundles it as a separate
  executable it launches, not as a library it links.

## Changes from upstream

GPL-3.0 section 5 requires modified files to carry prominent notices stating that
they were changed and the date. Record every change here as it lands.

| Date | What changed | Why |
|------|--------------|-----|
| 2026-08-31 | Forked at `62aa2d3`; upstream `README.md` renamed to `UPSTREAM-README.md`; this notice added. | Separate our documentation from upstream's without deleting theirs. |
| 2026-08-31 | `src-tauri/tauri.conf.json`: window title `Pluely - AI Assistant` → `Silkscreen`, then → `Kaleo`; `productName` and the crate name (`Cargo.toml`) likewise renamed to `kaleo`. | It is our app, and the team settled on the name Kaleo. |
| 2026-08-31 | `src-tauri/tauri.conf.json`: `alwaysOnTop` false → **true**. | Upstream summons itself with a hotkey and hides again. An ambient copilot that falls behind the window you are working in is not ambient. |
| 2026-08-31 | `src-tauri/tauri.conf.json`: `contentProtected` true → **false**. | This is upstream's screen-share invisibility, the load-bearing feature of an interview-cheating tool and the wrong default for ours. We *want* to be visible in a shared design review; hiding an engineering assistant from the people reviewing the engineering is the opposite of the pitch. |
| 2026-08-31 | `src-tauri/Cargo.toml`: dropped the macOS-only `cidre` dependency; `src-tauri/src/speaker/macos.rs`: replaced the cidre-backed CoreAudio system-audio tap with a stub that returns an explicit error, and reimplemented input/output device enumeration on `cpal`. | `cidre`'s build script runs `xcodebuild`, so `cargo build` fails outright on a machine with only the Command Line Tools — it demanded a ~10 GB Xcode install from every contributor. The module and all eleven Tauri commands stay registered so the React UI keeps finding them; system-audio capture now fails loudly instead of silently producing nothing. Windows and Linux implementations are untouched. |
| 2026-08-31 | `README.md` added for the fork (upstream's remains as `UPSTREAM-README.md`). | The app is Kaleo, and its documentation should say so without overwriting upstream's. |
| 2026-08-31 | `package.json`: `name` `pluely` → `kaleo`. | Fork identity. |
| 2026-08-31 | `src-tauri/tauri.conf.json`: `productName` `Pluely` → `Kaleo`; window title `Silkscreen` → `Kaleo`. | Fork identity; the window title briefly carried the project name before the app's own name was settled. |
| 2026-08-31 | `src-tauri/tauri.conf.json`: `identifier` `com.srikanthnani.pluely` → `com.silkscreen.kaleo`. | A new bundle identifier makes this a *distinct app install* on every platform — it will not overwrite, be overwritten by, or share OS-level state with an installed Pluely. That separation is exactly what a hard fork wants. |
| 2026-08-31 | `src-tauri/tauri.conf.json`: updater plugin config removed (its `endpoints` pointed at `https://pluely.com/api/update` with upstream's signing pubkey); `createUpdaterArtifacts` → `false`. | A fork must not phone upstream's update server — it would offer Pluely builds signed by upstream as "updates" to Kaleo, which is both broken and rude. No Kaleo update endpoint exists yet; the updater UI in `src/components/updater/` will error harmlessly until the plugin and component are removed or repointed. |
| 2026-08-31 | `src-tauri/Cargo.toml`: package `name` `pluely` → `kaleo`, `description` rewritten, `authors` now leads with the fork author (upstream credited). `[lib] name` stays `pluely_lib` because `src-tauri/src/main.rs` calls `pluely_lib::run()`; renaming it is part of the Rust-side rebrand pass, not this one. | Fork identity. |
| 2026-08-31 | `index.html`: `<title>` `Tauri + React + Typescript` → `Kaleo`. | Fork identity. |
| 2026-08-31 | `src/config/kaleo-identity.ts` added: exported `APP_NAME`/`APP_TAGLINE` plus the naming rationale. | One importable source of truth for the app's name so UI strings stop hardcoding it. |
| 2026-08-31 | The fork was briefly named Patrick during development, then settled as **Kaleo** the same day; every identity surface above (package name, product name, identifier, titles, identity module) carries Kaleo. | The naming intent is unchanged — a human-feeling name over a product name — only the name itself moved. |
| 2026-08-31 | `src/contexts/app.context.tsx`: the unconditional `trackAppStart` PostHog capture on launch was removed. | Upstream phoned PostHog a stable per-install id on every start with no consent surface. Kaleo sends no telemetry; the analytics module itself goes in the Pluely-removal pass. |
| 2026-08-31 | `src-tauri/capabilities/{default,cross-platform}.json`: the `posthog:*` permissions were dropped and `http:default` narrowed from `http://**`+`https://**` to loopback (`127.0.0.1`, `localhost`, `[::1]`) only. | The engine is plain HTTP on loopback and is the app's only legitimate fetch destination; the wide grant turned any JS-level bug into a network exfiltration channel. |
| 2026-08-31 | `src-tauri/src/lib.rs`: removed `.plugin(tauri_plugin_updater::Builder::new().build())`; `src/pages/{app,kaleo}/index.tsx`: removed the `<Updater />` render sites. | The updater config was already gone, but the plugin was still registered, so on the **first ever run of the binary** it read a `plugins.updater` block that no longer existed and panicked before a window appeared: `PluginInitialization("updater", "invalid type: null, expected struct Config")`. The note above predicted a harmless UI error; the reality was that the app could not start at all. A fork that ships no updater must not register one. |
| 2026-08-31 | `src-tauri/Cargo.toml` `[lib] name` `pluely_lib` → `kaleo_lib`, and `src-tauri/src/main.rs` now calls `kaleo_lib::run()`. | The Rust-side rebrand pass the entry above deferred. |
| 2026-08-31 | `src-tauri/Cargo.toml`: `repository`/`homepage`/`documentation` repointed from upstream's GitHub and wiki to this repository. | They described upstream's project, not this fork; crates metadata should not send people to a codebase that does not contain this code. |
| 2026-08-31 | `src-tauri/tauri.conf.json` and `src-tauri/src/lib.rs`: SQLite database `sqlite:pluely.db` → `sqlite:kaleo.db`. | Same reason as the bundle identifier: a distinct app should not read or migrate an installed Pluely's database. |
| 2026-08-31 | `src-tauri/pluely.desktop` renamed to `kaleo.desktop`, with `Name`, `Exec`, `Icon`, `StartupWMClass`, `Categories`, `Keywords` and `Comment` rewritten for this app. | It still advertised itself to Linux desktops as an AI meeting assistant, under upstream's binary name. |
| 2026-08-31 | `src-tauri/src/window.rs`: fallback window lookup `get_webview_window("pluely")` → `"kaleo"`. | A stale label that matched nothing; the real window is `main`. |

| 2026-08-31 | Optional engine access token: `src/lib/silkscreen/client.ts` gained `authHeaders()` and a `token` parameter on `health`/`generate`/`generateStream` (the 404 fallback re-sends it; 401 maps to a new `ErrorKind` `"auth"`), `src/lib/silkscreen/client.test.ts` covers it; `src/config/kaleo.constants.ts` adds `ENGINE_TOKEN`; `src/pages/engine/components/EngineConnection.tsx` adds the token field plus `loadEngineToken`/`saveEngineToken` and tests `/healthz` with it; `src/pages/engine/index.tsx`, `src/hooks/useEngineHealth.ts`, `src/hooks/useSilkscreenRun.ts`, and `src/routes/index.tsx` thread the token to every request; `src/pages/kaleo/components/RunProgress.tsx` explains the 401. Also fixed `client.ts`'s stale header comment that still claimed the http capability allows `http://**`. | A deployed engine can sit behind a bearer-token gate (`Authorization: Bearer`, 401 `{"error":"unauthorized"}`); locally with no token there is no gate and no header is sent. Stored in localStorage because that is how this app already stores provider API keys — no JS here touches the keychain plugin. |
| 2026-08-31 | `src/pages/engine/components/EngineConnection.tsx`: `validateEngineBaseUrl` now accepts `https://` origins for any host; plain `http://` stays loopback-only. `src-tauri/capabilities/{default,cross-platform}.json`: `http:default` allow list gains `https://**`. | The engine grew a bearer-token gate (service PR #21), so a deployed HTTPS engine is now a legitimate target; the old refusal reason ("no authentication") no longer holds. Plain-http remote stays refused — it would carry the token unencrypted. |
| 2026-08-31 | Spoken run digests: new `src/lib/speech/` (`summarize.ts` composes an ear-first digest of a finished run; `backends.ts` holds two `SpeechBackend`s — the webview's own `speechSynthesis`, probed at runtime because a webview without it must read as "no voice" rather than a TypeError, and ElevenLabs over `@tauri-apps/plugin-http` with the key confined to the `xi-api-key` header; `speaker.ts` enforces one utterance at a time and never throws to a caller — a failed TTS call degrades to silence with one warning, because a voice that crashes a run report is worse than no voice; `settings.ts` persists the toggle and key the way the engine token is stored). New `src/hooks/useRunVoice.ts` (speaks once per completed live run, keyed to the history entry id so re-renders and history browsing stay silent, and stops stale speech when a new run starts), `src/pages/kaleo/components/VoiceToggle.tsx`, `src/pages/engine/components/VoiceSettings.tsx`, plus tests (`summarize.test.ts`, `speaker.test.ts` — ElevenLabs fully mocked; a unit test must never spend anyone's quota). | The Jarvis moment: the run finishes and the app says what the review found while the findings render. Backend choice is "key present → ElevenLabs, absent → built-in voice" so the feature works with no account at all. |
| 2026-08-31 | `src/config/kaleo.constants.ts`: `KALEO_STORAGE_KEYS` gains `VOICE_ENABLED`, `ELEVENLABS_KEY`, `ELEVENLABS_VOICE_ID`. `src/pages/kaleo/index.tsx`: calls `useRunVoice(run)` and renders `VoiceToggle` beside the run summary. `src/pages/engine/index.tsx` and `src/pages/engine/components/index.ts`: render and export `VoiceSettings`. | The wire-up half of the voice feature above, kept to the smallest possible touches on files another session is working in; everything with logic lives in the new files. |
| 2026-08-31 | `src-tauri/icons/*` regenerated from the new Kaleo mark (two stepped traces, black over copper; vector source kept in the session scratchpad and worth committing to `images/`). Version 0.2.0 → 0.2.1 across the three manifests. | New brand identity replacing upstream Pluely's icon; the bump carries the icon and the spoken-findings feature into one distributable. |
