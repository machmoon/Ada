// The clamp is the part of the window-height sync a unit test can pin: the
// hook itself needs a webview (ResizeObserver + a Tauri invoke) and is
// exercised by driving the built app.

import { describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));

import {
  OVERLAY_COLLAPSED_HEIGHT,
  OVERLAY_MAX_HEIGHT,
  overlayHeightFor,
} from "./useOverlayHeight";

describe("overlayHeightFor", () => {
  it("never goes below the collapsed bar height", () => {
    expect(overlayHeightFor(0)).toBe(OVERLAY_COLLAPSED_HEIGHT);
    expect(overlayHeightFor(-10)).toBe(OVERLAY_COLLAPSED_HEIGHT);
    expect(overlayHeightFor(30)).toBe(OVERLAY_COLLAPSED_HEIGHT);
    expect(overlayHeightFor(OVERLAY_COLLAPSED_HEIGHT)).toBe(
      OVERLAY_COLLAPSED_HEIGHT
    );
  });

  it("tracks content, rounded up to a whole pixel", () => {
    expect(overlayHeightFor(300)).toBe(300);
    expect(overlayHeightFor(299.2)).toBe(300);
  });

  it("caps at the ceiling and collapses on non-finite input", () => {
    expect(overlayHeightFor(10_000)).toBe(OVERLAY_MAX_HEIGHT);
    expect(overlayHeightFor(Number.POSITIVE_INFINITY)).toBe(
      OVERLAY_COLLAPSED_HEIGHT
    );
    expect(overlayHeightFor(Number.NaN)).toBe(OVERLAY_COLLAPSED_HEIGHT);
  });
});
