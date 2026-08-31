// Config-level guards for the two claims this app makes about itself: that
// the webview can only reach a silkscreen engine on loopback, and that it is
// this project rather than the upstream it was forked from.
//
// These read the shipped files rather than a copy, because the defect they
// guard against is someone loosening the real config, not the test's idea of
// it. `csp: null` — the state this replaces — meant the webview had no network
// restriction at all beyond the capability allowlist, which the frontend was
// bypassing anyway.

import { existsSync, readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const readJson = (relative: string) =>
  JSON.parse(readFileSync(new URL(relative, import.meta.url), "utf8"));

const tauriConfig = readJson("../../src-tauri/tauri.conf.json");
const packageJson = readJson("../../package.json");

/** "a 'self' b; c 'none'" -> { a: ["'self'", "b"], c: ["'none'"] } */
const parseCsp = (policy: string): Record<string, string[]> =>
  Object.fromEntries(
    policy
      .split(";")
      .map((directive) => directive.trim())
      .filter(Boolean)
      .map((directive) => {
        const [name, ...sources] = directive.split(/\s+/);
        return [name, sources];
      })
  );

// Everything the policies are allowed to name. Loopback and Tauri's own IPC
// scheme, and nothing that resolves off the machine.
const ALLOWED_SOURCES = new Set([
  "'self'",
  "'none'",
  "'unsafe-inline'",
  "data:",
  "blob:",
  "ipc:",
  "http://ipc.localhost",
  "http://127.0.0.1:*",
  "http://localhost:*",
  "http://[::1]:*",
  "ws://127.0.0.1:*",
  "ws://localhost:*",
]);

const { csp, devCsp } = tauriConfig.app.security;

describe("webview content security policy", () => {
  it("is set at all", () => {
    expect(typeof csp).toBe("string");
    expect(typeof devCsp).toBe("string");
  });

  it("defaults to same-origin and forbids plugins and base-tag rewriting", () => {
    const directives = parseCsp(csp);

    expect(directives["default-src"]).toEqual(["'self'"]);
    expect(directives["object-src"]).toEqual(["'none'"]);
    expect(directives["base-uri"]).toEqual(["'self'"]);
    expect(directives["frame-src"]).toEqual(["'none'"]);
  });

  it("names no origin that leaves the machine", () => {
    for (const policy of [csp, devCsp]) {
      for (const [directive, sources] of Object.entries(parseCsp(policy))) {
        for (const source of sources) {
          expect(
            ALLOWED_SOURCES.has(source),
            `${directive} names ${source}`
          ).toBe(true);
        }
      }
    }
  });

  it("allows connections only to loopback and Tauri's IPC", () => {
    // The same three origins as `http:default` in `src-tauri/capabilities`;
    // `ipc:`/`http://ipc.localhost` is how `invoke` itself reaches Rust.
    expect(parseCsp(csp)["connect-src"]).toEqual([
      "'self'",
      "ipc:",
      "http://ipc.localhost",
      "http://127.0.0.1:*",
      "http://localhost:*",
      "http://[::1]:*",
    ]);
  });

  it("keeps inline script out of the shipped policy", () => {
    // Dev needs it for Vite's React-refresh preamble. A release build does not,
    // and that is the build a stranger downloads.
    expect(parseCsp(csp)["script-src"]).toEqual(["'self'"]);
    expect(csp).not.toContain("unsafe-eval");
    expect(devCsp).not.toContain("unsafe-eval");
  });
});

describe("package metadata", () => {
  it("points issue and source links at this fork", () => {
    const links = [
      packageJson.homepage,
      packageJson.repository.url,
      packageJson.bugs.url,
    ];

    for (const link of links) {
      expect(link).toContain("machmoon/Kaleo");
      expect(link).not.toContain("pluely");
    }
  });

  it("describes a PCB client, not a covert meeting assistant", () => {
    expect(packageJson.description).toMatch(/silkscreen/i);
    expect(packageJson.description).not.toMatch(
      /cluely|meeting|interview|without anyone knowing/i
    );
  });

  it("keeps upstream's authorship credit", () => {
    // GPL-3.0 attribution: the fork is added alongside, never in place of.
    expect(packageJson.author.name).toBe("Srikanth Nani");
    expect(packageJson.license).toBe("GPL-3.0");
    expect(packageJson.contributors).toContainEqual(
      expect.objectContaining({ name: "machmoon" })
    );
  });

  it("does not solicit sponsorship for upstream", () => {
    expect(existsSync(new URL("../../.github/FUNDING.yml", import.meta.url))).toBe(
      false
    );
  });
});

// A URL pattern Tauri cannot tokenize does not merely fail to match -- it makes
// the whole scope fail to deserialize, so *every* request the app issues is
// rejected before it reaches the network. An IPv6 literal does exactly that,
// because `:` opens a named group in the pattern syntax and `[::1]` leaves one
// unnamed. That shipped once and presented as "could not reach the engine",
// with no request in the engine's log and no error in the app's, so it is
// pinned here rather than left to the next person to rediagnose.
describe("http capability scope", () => {
  const capabilities = ["default", "cross-platform"].map((name) => ({
    name,
    json: readJson(`../../src-tauri/capabilities/${name}.json`),
  }));

  const httpScopeUrls = (json: {
    permissions: (string | { identifier: string; allow?: { url: string }[] })[];
  }): string[] =>
    json.permissions
      .filter(
        (p): p is { identifier: string; allow?: { url: string }[] } =>
          typeof p === "object" && p.identifier === "http:default"
      )
      .flatMap((p) => (p.allow ?? []).map((entry) => entry.url));

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
});

// Upstream excluded its windows from screen capture so they stayed hidden
// during a call. This app is demoed and screen-shared, and a protected window
// records as a blank rectangle -- the board, schematic and review would be
// missing from exactly the video that is meant to show them.
describe("screen capture", () => {
  const windowRs = readFileSync(
    new URL("../../src-tauri/src/window.rs", import.meta.url),
    "utf8"
  );

  it("leaves the dashboard capturable", () => {
    expect(windowRs).not.toMatch(/\.content_protected\(true\)/);
  });

  it("does not protect the overlay either", () => {
    expect(tauriConfig.app.windows[0].contentProtected).toBe(false);
  });
});
