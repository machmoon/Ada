// Config-level guards for two properties this app cannot assert from its own
// code: that the webview is allowed to reach the engine at all, and that its
// windows can be recorded.
//
// These read the shipped files rather than a copy, because the defect they
// guard against is someone loosening or mistyping the real config.

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const readJson = (relative: string) =>
  JSON.parse(readFileSync(new URL(relative, import.meta.url), "utf8"));

// A URL pattern Tauri cannot tokenize does not merely fail to match -- it makes
// the whole scope fail to deserialize, so *every* request the app issues is
// rejected before it reaches the network. An IPv6 literal does exactly that,
// because `:` opens a named group in the pattern syntax and `[::1]` leaves one
// unnamed. This has shipped twice now, once as a `https://**` glob and once as
// `http://[::1]:*`, and it presents as "could not reach the silkscreen engine"
// with no request in the engine's log and no error in the app's -- health()
// swallows its errors behind a status dot, and the plugin rejects with a bare
// string, so `error.message` reads `undefined`.
describe("http capability scope", () => {
  const capabilities = ["default", "cross-platform"].map((name) => ({
    name,
    json: readJson(`../../src-tauri/capabilities/${name}.json`),
  }));

  const httpScopeUrls = (json: {
    permissions: (string | { identifier: string; allow?: { url?: string }[] })[];
  }): string[] =>
    json.permissions
      .filter(
        (p): p is { identifier: string; allow?: { url?: string }[] } =>
          typeof p === "object" && p.identifier === "http:default"
      )
      .flatMap((p) => (p.allow ?? []).map((e) => e.url ?? ""));

  it.each(capabilities)("$name declares an http scope", ({ json }) => {
    expect(httpScopeUrls(json).length).toBeGreaterThan(0);
  });

  it.each(capabilities)("$name has no IPv6 literal", ({ json }) => {
    for (const url of httpScopeUrls(json)) {
      expect(url).not.toMatch(/\[|\]/);
    }
  });

  it.each(capabilities)("$name stays on loopback", ({ json }) => {
    for (const url of httpScopeUrls(json)) {
      expect(url).toMatch(/^http:\/\/(127\.0\.0\.1|localhost):\*/);
    }
  });

  it("both platform files declare the same scope", () => {
    const [a, b] = capabilities.map((c) => httpScopeUrls(c.json).sort());
    expect(a).toEqual(b);
  });
});

// Upstream excluded its windows from screen capture so they stayed hidden
// during a call. This app is demoed and screen-shared, and a protected window
// records as a blank rectangle -- the board, schematic and review would be
// missing from exactly the video meant to show them.
describe("screen capture", () => {
  it("leaves the dashboard capturable", () => {
    const windowRs = readFileSync(
      new URL("../../src-tauri/src/window.rs", import.meta.url),
      "utf8"
    );
    expect(windowRs).not.toMatch(/\.content_protected\(true\)/);
  });

  it("does not protect the overlay either", () => {
    const config = readJson("../../src-tauri/tauri.conf.json");
    expect(config.app.windows[0].contentProtected).toBe(false);
  });
});
