// @vitest-environment jsdom
//
// The loopback-only claim lives or dies here.
//
// Both provider paths used to choose the webview's native `fetch` whenever the
// URL contained "http" — which is every URL — and the webview's fetch answers
// to nothing in `src-tauri/capabilities`. Only the Tauri client can be refused
// by the allowlist, so these tests assert the global is never touched, for a
// remote host and a loopback one alike. A regression here silently reopens an
// unscoped network channel, which is exactly the bug this replaces.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("@tauri-apps/api/event", () => ({ listen: vi.fn() }));

import { fetch as tauriFetch } from "@tauri-apps/plugin-http";
import { AI_PROVIDERS, SPEECH_TO_TEXT_PROVIDERS } from "@/config";
// Through the barrel, not the modules directly: `ai-response.function` reads
// `getResponseSettings` back out of `@/lib`, and importing the leaf first
// leaves that cycle half-initialised. The app always enters through `@/lib`.
import { fetchAIResponse, fetchSTT } from "@/lib";

const scopedFetch = vi.mocked(tauriFetch);
const nativeFetch = vi.fn();
let realFetch: typeof globalThis.fetch | undefined;

/** The OpenAI entry, forced non-streaming so the JSON branch is the one read. */
const aiProvider = { ...AI_PROVIDERS[0], streaming: false };
const sttProvider = SPEECH_TO_TEXT_PROVIDERS[0];

const withHost = (provider: { curl: string }, host: string) => ({
  ...provider,
  curl: provider.curl.replace(/https:\/\/[^/\s"]+/, host),
});

const drain = async (stream: AsyncIterable<string>): Promise<string[]> => {
  const chunks: string[] = [];
  for await (const chunk of stream) {
    chunks.push(chunk);
  }
  return chunks;
};

beforeEach(() => {
  localStorage.clear();
  scopedFetch.mockReset();
  nativeFetch.mockReset();
  realFetch = globalThis.fetch;
  globalThis.fetch = nativeFetch as unknown as typeof globalThis.fetch;
});

afterEach(() => {
  globalThis.fetch = realFetch as typeof globalThis.fetch;
});

describe("fetchAIResponse transport", () => {
  const run = (provider: { curl: string }) =>
    drain(
      fetchAIResponse({
        provider: { ...aiProvider, ...provider },
        selectedProvider: {
          provider: "openai",
          variables: { api_key: "k", model: "m" },
        },
        userMessage: "place a board",
      })
    );

  const okJson = () =>
    scopedFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ choices: [{ message: { content: "hello" } }] }),
    } as unknown as Response);

  it("sends a remote provider through the scoped client, not the webview", async () => {
    okJson();

    await expect(run(aiProvider)).resolves.toEqual(["hello"]);

    expect(nativeFetch).not.toHaveBeenCalled();
    expect(scopedFetch).toHaveBeenCalledTimes(1);
    expect(scopedFetch.mock.calls[0][0]).toBe(
      "https://api.openai.com/v1/chat/completions"
    );
  });

  it("sends a loopback provider through the scoped client too", async () => {
    okJson();

    await run(withHost(aiProvider, "http://127.0.0.1:8081"));

    expect(nativeFetch).not.toHaveBeenCalled();
    expect(scopedFetch.mock.calls[0][0]).toBe(
      "http://127.0.0.1:8081/v1/chat/completions"
    );
  });

  it("reports an allowlist refusal instead of falling back to the webview", async () => {
    scopedFetch.mockRejectedValueOnce(
      new Error("url not allowed on the configured scope: https://api.openai.com/")
    );

    const chunks = await run(aiProvider);

    expect(chunks.join("")).toContain("url not allowed on the configured scope");
    expect(nativeFetch).not.toHaveBeenCalled();
  });
});

describe("fetchSTT transport", () => {
  const audio = () => new Blob([new Uint8Array([1, 2, 3])], { type: "audio/wav" });

  const run = (provider: { curl: string }) =>
    fetchSTT({
      provider: { ...sttProvider, ...provider },
      selectedProvider: {
        provider: "openai-whisper",
        variables: { api_key: "k", model: "m" },
      },
      audio: audio(),
    });

  const okText = () =>
    scopedFetch.mockResolvedValueOnce({
      ok: true,
      text: async () => JSON.stringify({ text: "two resistors" }),
    } as unknown as Response);

  it("sends a remote provider through the scoped client, not the webview", async () => {
    okText();

    await expect(run(sttProvider)).resolves.toBe("two resistors");

    expect(nativeFetch).not.toHaveBeenCalled();
    expect(scopedFetch).toHaveBeenCalledTimes(1);
    expect(scopedFetch.mock.calls[0][0]).toBe(
      "https://api.openai.com/v1/audio/transcriptions"
    );
  });

  it("sends a loopback provider through the scoped client too", async () => {
    okText();

    await run(withHost(sttProvider, "http://localhost:8081"));

    expect(nativeFetch).not.toHaveBeenCalled();
    expect(scopedFetch.mock.calls[0][0]).toBe(
      "http://localhost:8081/v1/audio/transcriptions"
    );
  });

  it("surfaces an allowlist refusal rather than retrying unscoped", async () => {
    scopedFetch.mockRejectedValueOnce(
      new Error("url not allowed on the configured scope: https://api.openai.com/")
    );

    await expect(run(sttProvider)).rejects.toThrow(
      /url not allowed on the configured scope/
    );
    expect(nativeFetch).not.toHaveBeenCalled();
  });
});
