// @vitest-environment jsdom
//
// Autostart is the setting that decides whether a stranger's machine runs this
// app at login without being asked. The window is always-on-top, off the
// taskbar and on every workspace, so a default-on autostart is the covert-use
// profile this fork is shedding — hence a test, not just a changed literal.

import { beforeEach, describe, expect, it } from "vitest";

import { STORAGE_KEYS } from "@/config";
import {
  DEFAULT_CUSTOMIZABLE_STATE,
  getCustomizableState,
  updateAutostart,
} from "@/lib";

beforeEach(() => {
  localStorage.clear();
});

describe("DEFAULT_CUSTOMIZABLE_STATE", () => {
  it("does not add the app to login items on a fresh install", () => {
    expect(DEFAULT_CUSTOMIZABLE_STATE.autostart.isEnabled).toBe(false);
  });

  it("reads back autostart-off when nothing has been stored yet", () => {
    expect(getCustomizableState().autostart.isEnabled).toBe(false);
  });

  it("reads back autostart-off when the stored state predates the setting", () => {
    // An upgrade from a build without the key must not be read as consent.
    localStorage.setItem(
      STORAGE_KEYS.CUSTOMIZABLE,
      JSON.stringify({ appIcon: { isVisible: true } })
    );

    expect(getCustomizableState().autostart.isEnabled).toBe(false);
  });
});

describe("updateAutostart", () => {
  it("keeps the user's opt-in once they make it", () => {
    updateAutostart(true);

    expect(getCustomizableState().autostart.isEnabled).toBe(true);
  });

  it("lets the user opt back out", () => {
    updateAutostart(true);
    updateAutostart(false);

    expect(getCustomizableState().autostart.isEnabled).toBe(false);
  });
});
