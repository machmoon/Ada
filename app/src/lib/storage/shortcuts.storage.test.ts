// @vitest-environment jsdom
//
// A PCB tool must not own a global screenshot or system-audio hotkey the
// moment it is installed. Both actions survive — the settings screen still
// lists them and the Rust handlers still exist — they just ship disarmed, and
// the Rust side registers nothing whose binding is `enabled: false`.

import { beforeEach, describe, expect, it } from "vitest";

import {
  DEFAULT_SHORTCUT_ACTIONS,
  SHORTCUT_ACTIONS_DISABLED_BY_DEFAULT,
  STORAGE_KEYS,
} from "@/config";
import {
  getAllShortcutActions,
  getDefaultShortcutsConfig,
  getShortcutsConfig,
  resetShortcutsToDefaults,
  updateShortcutBinding,
} from "@/lib";

const enabledIds = (config: { bindings: Record<string, { enabled: boolean }> }) =>
  Object.entries(config.bindings)
    .filter(([, binding]) => binding.enabled)
    .map(([id]) => id)
    .sort();

beforeEach(() => {
  localStorage.clear();
});

describe("seeded shortcut defaults", () => {
  it("arms exactly the navigation and voice actions", () => {
    expect(enabledIds(getDefaultShortcutsConfig())).toEqual([
      "audio_recording",
      "focus_input",
      "move_window",
      "toggle_dashboard",
      "toggle_window",
    ]);
  });

  it("leaves screen capture and system audio disarmed", () => {
    const { bindings } = getDefaultShortcutsConfig();

    expect(bindings.screenshot.enabled).toBe(false);
    expect(bindings.system_audio.enabled).toBe(false);
    expect(SHORTCUT_ACTIONS_DISABLED_BY_DEFAULT).toEqual([
      "screenshot",
      "system_audio",
    ]);
  });

  it("still seeds a binding for the disarmed actions", () => {
    // The settings toggle bails out when a binding is missing, so removing the
    // binding outright would leave the user unable to switch these on at all.
    const { bindings } = getDefaultShortcutsConfig();

    for (const action of DEFAULT_SHORTCUT_ACTIONS) {
      expect(bindings[action.id]).toBeDefined();
      expect(bindings[action.id].key).not.toBe("");
    }
  });

  it("keeps both actions offerable in settings", () => {
    const ids = getAllShortcutActions().map((action) => action.id);

    expect(ids).toContain("screenshot");
    expect(ids).toContain("system_audio");
  });

  it("registers nothing extra on a first launch with empty storage", () => {
    expect(enabledIds(getShortcutsConfig())).toEqual(
      enabledIds(getDefaultShortcutsConfig())
    );
  });
});

describe("stored shortcut config", () => {
  it("honours an explicit opt-in over the disarmed default", () => {
    updateShortcutBinding("screenshot", "cmd+shift+s", true);

    expect(getShortcutsConfig().bindings.screenshot.enabled).toBe(true);
  });

  it("disarms them again on a reset to defaults", () => {
    updateShortcutBinding("screenshot", "cmd+shift+s", true);
    updateShortcutBinding("system_audio", "cmd+shift+m", true);

    const reset = resetShortcutsToDefaults();

    expect(reset.bindings.screenshot.enabled).toBe(false);
    expect(reset.bindings.system_audio.enabled).toBe(false);
    expect(getShortcutsConfig().bindings.screenshot.enabled).toBe(false);
  });

  it("does not re-arm them for an install that stored the old defaults", () => {
    // Only the ids the old build wrote out are reused; anything absent falls
    // back to the seed, which is now off.
    localStorage.setItem(
      STORAGE_KEYS.SHORTCUTS,
      JSON.stringify({
        bindings: {
          toggle_window: {
            action: "toggle_window",
            key: "cmd+backslash",
            enabled: true,
          },
        },
        customActions: [],
      })
    );

    const { bindings } = getShortcutsConfig();

    expect(bindings.screenshot.enabled).toBe(false);
    expect(bindings.system_audio.enabled).toBe(false);
  });
});
