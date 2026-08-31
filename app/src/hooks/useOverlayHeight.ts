// The overlay window is created 600×54 and non-resizable, so the *window*
// never grows on its own — only the content does, and anything below 54px is
// clipped by the webview. Upstream Pluely solved this with a Rust command,
// `set_window_height`, which survived the cleanup (registered in lib.rs) but
// lost its last frontend caller. This hook is the caller: it watches the
// overlay Card's rendered height and keeps the window exactly that tall.
//
// The run that "felt like nothing happened" was this: the state machine,
// progress checklist, result card and error card all worked, all rendered —
// from y=54 down, invisible.

import { useEffect, useRef } from "react";
import { invoke } from "@tauri-apps/api/core";

/** The bar's collapsed height; must match `app.height` in tauri.conf.json. */
export const OVERLAY_COLLAPSED_HEIGHT = 54;
/** Generous ceiling so a long activity feed scrolls instead of eating the screen. */
export const OVERLAY_MAX_HEIGHT = 600;

/** Clamp a measured content height to the window's allowed band. */
export function overlayHeightFor(contentHeight: number): number {
  if (!Number.isFinite(contentHeight) || contentHeight <= 0) {
    return OVERLAY_COLLAPSED_HEIGHT;
  }
  return Math.max(
    OVERLAY_COLLAPSED_HEIGHT,
    Math.min(OVERLAY_MAX_HEIGHT, Math.ceil(contentHeight))
  );
}

/**
 * Attach the returned ref to the element whose height the window should track
 * (the overlay Card). One resize invoke per animation frame, and only when the
 * clamped height actually changed. A failed invoke is warned, not thrown — a
 * clipped-but-working overlay beats a crashed one.
 */
export function useOverlayHeight<T extends HTMLElement>() {
  const ref = useRef<T | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;

    let last = -1;
    let frame = 0;
    const apply = () => {
      frame = 0;
      const height = overlayHeightFor(el.getBoundingClientRect().height);
      if (height === last) return;
      last = height;
      invoke("set_window_height", { height }).catch((error) => {
        console.warn("[kaleo overlay] set_window_height failed:", error);
      });
    };

    const observer = new ResizeObserver(() => {
      if (frame) return;
      frame = requestAnimationFrame(apply);
    });
    observer.observe(el);
    apply();

    return () => {
      observer.disconnect();
      if (frame) cancelAnimationFrame(frame);
    };
  }, []);

  return ref;
}
