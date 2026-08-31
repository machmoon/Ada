// @vitest-environment jsdom
//
// The assistant panel, driven through the real overlay.
//
// Nothing between the control and the socket is stubbed: this renders the
// actual `Kaleo` page inside the actual `RunProvider`, and the only thing
// mocked is `@tauri-apps/plugin-http`'s `fetch` — the scoped client that is
// the app's single exit to the network. So every assertion below is about
// bytes that would really have left the process.
//
// That is the point of the file. The one rule this feature is built under is
// that an affordance must do something real against the engine, and the only
// way to hold a control to that is to look at the request it produces:
//
//   * a datasheet chip has to appear in `datasheets` in the `/generate/stream`
//     body, keyed by the part number the user typed;
//   * a history row has to replay a run the store really held, WITHOUT any new
//     request going out;
//   * the mic has to POST audio to `/transcribe` and put the transcript in the
//     box rather than into a run.

import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tauri-apps/plugin-http", () => ({ fetch: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn(async () => undefined) }));
vi.mock("@tauri-apps/api/event", () => ({
  listen: vi.fn(async () => () => {}),
  emit: vi.fn(async () => {}),
}));
vi.mock("@tauri-apps/api/window", () => ({
  getCurrentWindow: () => ({ label: "main" }),
}));

// Pluely's app-shell hook: shortcut registration, a SQLite migration and a
// window-visibility listener, none of which this panel is about.
vi.mock("@/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/hooks")>();
  return {
    ...actual,
    useApp: () => ({
      isHidden: false,
      setIsHidden: () => {},
      handleSelectConversation: () => {},
      handleNewConversation: () => {},
      systemAudio: {},
    }),
  };
});

// The spoken digest is on by default and jsdom has no speech synthesiser.
vi.mock("@/lib/speech", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/speech")>();
  return {
    ...actual,
    speaker: { speak: vi.fn(async () => {}), stop: vi.fn(), isSpeaking: () => false },
  };
});

import { fetch as tauriFetch } from "@tauri-apps/plugin-http";
import { RunProvider } from "@/contexts";
import { initialRunProgress } from "@/lib/silkscreen/describe";
import type { RunHistoryStore } from "@/lib/silkscreen/history.store";
import type { RunHistoryEntry } from "@/hooks/useSilkscreenRun";
import { agoLabel, verdictLabel } from "./components/RunHistoryPanel";
import Kaleo from "./index";

const mockFetch = vi.mocked(tauriFetch);
const BASE = "http://127.0.0.1:8081";
const DATASHEET = "https://example.com/AMS1117.pdf";

beforeAll(() => {
  // Radix measures its own popovers and scroll areas.
  if (!("ResizeObserver" in globalThis)) {
    class ResizeObserverStub {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    vi.stubGlobal("ResizeObserver", ResizeObserverStub);
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = () => {};
  }
  if (!Element.prototype.hasPointerCapture) {
    Element.prototype.hasPointerCapture = () => false;
  }
});

// jest-dom is not installed in this project, so the DOM assertions read the
// element directly. Same checks, no extra dependency.
const disabled = (el: HTMLElement): boolean =>
  (el as HTMLButtonElement).disabled === true;
const value = (el: HTMLElement): string => (el as HTMLInputElement).value;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** A finished NDJSON run, delivered in one chunk. */
function streamResponse(result: Record<string, unknown>): Response {
  const encoder = new TextEncoder();
  const lines = [
    JSON.stringify({ event: "run.accepted", t_s: 0 }),
    JSON.stringify({ event: "run.done", t_s: 1, result }),
  ]
    .map((line) => `${line}\n`)
    .join("");
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(lines));
        controller.close();
      },
    }),
    { status: 200 }
  );
}

const RESULT = {
  status: "feasible",
  board_mm: [20, 10],
  nets: ["VCC", "GND"],
  findings: [],
  duration_s: 1.5,
  placements: { board_mm: [20, 10], frame: "solver-y-up", parts: [] },
};

/** Route by path, the way the real service does. */
function routeFetch(result: Record<string, unknown> = RESULT) {
  mockFetch.mockImplementation(async (input: unknown) => {
    const url = String(input);
    if (url.endsWith("/healthz")) return jsonResponse({ ok: true });
    if (url.endsWith("/generate/stream")) return streamResponse(result);
    if (url.endsWith("/transcribe")) {
      return jsonResponse({ text: "a 3.3V LDO board", model: "test" });
    }
    return jsonResponse({ error: `unexpected ${url}` }, 404);
  });
}

/** Every request this test made to `path`, with its parsed JSON body. */
function requestsTo(path: string): Record<string, unknown>[] {
  return mockFetch.mock.calls
    .filter((call) => String(call[0]).endsWith(path))
    .map((call) => {
      const init = call[1] as { body?: string } | undefined;
      return init?.body ? JSON.parse(init.body) : {};
    });
}

function storeWith(entries: RunHistoryEntry[]): RunHistoryStore {
  return {
    load: async (limit) => entries.slice(0, limit),
    save: async () => {},
    clear: async () => {},
  };
}

function storedRun(overrides: Partial<RunHistoryEntry> = {}): RunHistoryEntry {
  return {
    id: "run-stored",
    intent: "an STM32 breakout with a 3.3V rail",
    at: Date.now() - 90_000,
    request: {
      intent: "an STM32 breakout with a 3.3V rail",
      datasheets: { STM32F103: "https://example.com/stm32.pdf" },
      time_limit_s: 30,
      review: true,
      ground: true,
      debug: false,
    },
    result: {
      status: "optimal",
      nets: ["VCC", "GND", "SWDIO"],
      findings: [],
      duration_s: 9.5,
      placements: { board_mm: [31.5, 17.25], frame: "solver-y-up", parts: [] },
    },
    frames: [],
    progress: initialRunProgress({ review: true, route: true }),
    startedAt: Date.now() - 99_500,
    finishedAt: Date.now() - 90_000,
    elapsedS: 9.5,
    restored: true,
    ...overrides,
  };
}

function mount(store: RunHistoryStore | null = null) {
  return render(
    <RunProvider
      baseUrl={BASE}
      historyStore={store}
      // No cross-window bridge in a test: this window is the whole app.
      bridge={{ role: "off", transport: null }}
    >
      <Kaleo />
    </RunProvider>
  );
}

beforeEach(() => {
  mockFetch.mockReset();
  routeFetch();
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

/* ------------------------------------------------------- datasheet attach */

describe("attaching a datasheet", () => {
  it("puts the pair in the /generate/stream body, keyed by the part number", async () => {
    const user = userEvent.setup();
    mount();

    await user.click(screen.getByTestId("datasheet-attach-trigger"));
    await user.type(screen.getByTestId("datasheet-part-input"), "AMS1117");
    await user.type(screen.getByTestId("datasheet-url-input"), DATASHEET);
    await user.click(screen.getByTestId("datasheet-attach-submit"));

    // The chip is the user-visible half of the same fact.
    const chip = await screen.findByTestId("datasheet-chip");
    expect(chip.getAttribute("data-part")).toBe("AMS1117");
    expect(chip.getAttribute("title")).toBe(`AMS1117 — ${DATASHEET}`);

    await user.type(screen.getByTestId("prompt-input"), "a 3.3V LDO board");
    await user.click(screen.getByTestId("prompt-submit"));

    await waitFor(() => expect(requestsTo("/generate/stream")).toHaveLength(1));
    const body = requestsTo("/generate/stream")[0];
    expect(body.intent).toBe("a 3.3V LDO board");
    // This is the assertion the whole affordance exists for.
    expect(body.datasheets).toEqual({ AMS1117: DATASHEET });
  });

  it("removing the chip removes the pair from the next run's body", async () => {
    const user = userEvent.setup();
    mount();

    await user.click(screen.getByTestId("datasheet-attach-trigger"));
    await user.type(screen.getByTestId("datasheet-part-input"), "AMS1117");
    await user.type(screen.getByTestId("datasheet-url-input"), DATASHEET);
    await user.click(screen.getByTestId("datasheet-attach-submit"));
    await screen.findByTestId("datasheet-chip");

    await user.click(screen.getByTestId("datasheet-chip-remove"));
    expect(screen.queryByTestId("datasheet-chip")).toBeNull();
    // With nothing attached, the tray is absent rather than empty.
    expect(screen.queryByTestId("datasheet-chips")).toBeNull();

    await user.type(screen.getByTestId("prompt-input"), "a bare board");
    await user.click(screen.getByTestId("prompt-submit"));

    await waitFor(() => expect(requestsTo("/generate/stream")).toHaveLength(1));
    expect(requestsTo("/generate/stream")[0].datasheets).toEqual({});
  });

  it("refuses a URL the engine could not fetch instead of attaching it", async () => {
    const user = userEvent.setup();
    mount();

    await user.click(screen.getByTestId("datasheet-attach-trigger"));
    await user.type(screen.getByTestId("datasheet-part-input"), "AMS1117");
    await user.type(
      screen.getByTestId("datasheet-url-input"),
      "/Users/me/Downloads/AMS1117.pdf"
    );
    await user.click(screen.getByTestId("datasheet-attach-submit"));

    // The engine dereferences the URL itself; a local path is not something it
    // can read, so nothing is attached and the reason is on screen.
    expect(
      (await screen.findByTestId("datasheet-attach-error")).textContent
    ).toMatch(/not a URL the engine can fetch/i);
    expect(screen.queryByTestId("datasheet-chip")).toBeNull();

    // A well-formed URL on a scheme the engine cannot fetch is refused too,
    // and says which schemes it can — the service enforces the same rule on a
    // grounded run, and finding out here costs nothing.
    await user.clear(screen.getByTestId("datasheet-url-input"));
    await user.type(
      screen.getByTestId("datasheet-url-input"),
      "file:///Users/me/Downloads/AMS1117.pdf"
    );
    await user.click(screen.getByTestId("datasheet-attach-submit"));
    expect(
      (await screen.findByTestId("datasheet-attach-error")).textContent
    ).toMatch(/http/i);
    expect(screen.queryByTestId("datasheet-chip")).toBeNull();
  });

  it("shows the count on the paperclip and keeps several parts apart", async () => {
    const user = userEvent.setup();
    mount();

    for (const [part, url] of [
      ["AMS1117", DATASHEET],
      ["STM32F103", "https://example.com/stm32.pdf"],
    ]) {
      await user.click(screen.getByTestId("datasheet-attach-trigger"));
      await user.type(screen.getByTestId("datasheet-part-input"), part);
      await user.type(screen.getByTestId("datasheet-url-input"), url);
      await user.click(screen.getByTestId("datasheet-attach-submit"));
    }

    await waitFor(() =>
      expect(screen.getAllByTestId("datasheet-chip")).toHaveLength(2)
    );
    expect(screen.getByTestId("datasheet-attach-trigger").getAttribute("data-count")).toBe("2");

    await user.type(screen.getByTestId("prompt-input"), "an STM32 board");
    await user.click(screen.getByTestId("prompt-submit"));

    await waitFor(() => expect(requestsTo("/generate/stream")).toHaveLength(1));
    expect(requestsTo("/generate/stream")[0].datasheets).toEqual({
      AMS1117: DATASHEET,
      STM32F103: "https://example.com/stm32.pdf",
    });
  });
});

describe("the attach control and the options form are one draft", () => {
  it("a chip attached from the paperclip shows up in the options rows", async () => {
    const user = userEvent.setup();
    mount();

    await user.click(screen.getByTestId("datasheet-attach-trigger"));
    await user.type(screen.getByTestId("datasheet-part-input"), "AMS1117");
    await user.type(screen.getByTestId("datasheet-url-input"), DATASHEET);
    await user.click(screen.getByTestId("datasheet-attach-submit"));
    await screen.findByTestId("datasheet-chip");

    await user.click(screen.getByTestId("prompt-options-trigger"));
    const rows = await screen.findAllByTestId("run-options-datasheet-row");
    expect(rows).toHaveLength(1);
    const inputs = within(rows[0]).getAllByRole("textbox");
    expect(value(inputs[0])).toBe("AMS1117");
    expect(value(inputs[1])).toBe(DATASHEET);
  });

  it("removing the last chip drops the grounding flag from the run", async () => {
    const user = userEvent.setup();
    mount();

    await user.click(screen.getByTestId("datasheet-attach-trigger"));
    await user.type(screen.getByTestId("datasheet-part-input"), "AMS1117");
    await user.type(screen.getByTestId("datasheet-url-input"), DATASHEET);
    await user.click(screen.getByTestId("datasheet-attach-submit"));
    await screen.findByTestId("datasheet-chip");

    await user.click(screen.getByTestId("prompt-options-trigger"));
    await user.click(await screen.findByTestId("run-options-ground"));
    await user.keyboard("{Escape}");

    // Grounding with nothing to ground on is a claim the run cannot back up —
    // and the service 400s it — so the flag comes off with the last source.
    await user.click(screen.getByTestId("datasheet-chip-remove"));
    await user.type(screen.getByTestId("prompt-input"), "a bare board");
    await user.click(screen.getByTestId("prompt-submit"));

    await waitFor(() => expect(requestsTo("/generate/stream")).toHaveLength(1));
    const body = requestsTo("/generate/stream")[0];
    expect(body.datasheets).toEqual({});
    expect("ground" in body).toBe(false);
  });

  it("carries the grounding flag when a datasheet really is attached", async () => {
    const user = userEvent.setup();
    mount();

    await user.click(screen.getByTestId("datasheet-attach-trigger"));
    await user.type(screen.getByTestId("datasheet-part-input"), "AMS1117");
    await user.type(screen.getByTestId("datasheet-url-input"), DATASHEET);
    await user.click(screen.getByTestId("datasheet-attach-submit"));
    await screen.findByTestId("datasheet-chip");

    await user.click(screen.getByTestId("prompt-options-trigger"));
    await user.click(await screen.findByTestId("run-options-ground"));
    await user.keyboard("{Escape}");

    await user.type(screen.getByTestId("prompt-input"), "a 3.3V LDO board");
    await user.click(screen.getByTestId("prompt-submit"));

    await waitFor(() => expect(requestsTo("/generate/stream")).toHaveLength(1));
    const body = requestsTo("/generate/stream")[0];
    expect(body.ground).toBe(true);
    expect(body.datasheets).toEqual({ AMS1117: DATASHEET });
  });
});

/* ------------------------------------------------------------- run history */

describe("run history in the overlay", () => {
  it("offers nothing to open when nothing has been stored", async () => {
    mount(storeWith([]));
    await waitFor(() =>
      expect(disabled(screen.getByTestId("history-trigger"))).toBe(true)
    );
    expect(screen.getByTestId("history-trigger").getAttribute("data-count")).toBe("0");
  });

  it("replays a stored run without going back to the engine", async () => {
    const user = userEvent.setup();
    const entry = storedRun();
    mount(storeWith([entry]));

    const trigger = await screen.findByTestId("history-trigger");
    await waitFor(() => expect(disabled(trigger)).toBe(false));
    await user.click(trigger);

    const row = await screen.findByTestId("history-entry");
    expect(row.getAttribute("data-run-id")).toBe("run-stored");
    // A run read back off disk says so rather than posing as this session's.
    expect(row.getAttribute("data-restored")).toBe("1");
    expect(within(row).getByTestId("history-entry-restored")).not.toBeNull();

    await user.click(within(row).getByTestId("history-entry-open"));

    // The stored run's own numbers are on screen.
    const summary = await screen.findByTestId("run-summary");
    expect(within(summary).getByTestId("summary-board").textContent).toContain("31.5 × 17.3 mm");
    expect(within(summary).getByTestId("summary-nets").textContent).toContain("3");
    // And it is labelled as a past run, not as something that just happened.
    expect(screen.getByTestId("viewing-past-run").getAttribute("data-run-id")).toBe("run-stored");
    // Replaying is reading, not running: nothing was sent, nothing was billed.
    expect(requestsTo("/generate/stream")).toHaveLength(0);
  });

  it("goes back to the live state from a replayed run", async () => {
    const user = userEvent.setup();
    mount(storeWith([storedRun()]));

    const trigger = await screen.findByTestId("history-trigger");
    await waitFor(() => expect(disabled(trigger)).toBe(false));
    await user.click(trigger);
    await user.click(
      within(await screen.findByTestId("history-entry")).getByTestId(
        "history-entry-open"
      )
    );
    await screen.findByTestId("viewing-past-run");

    await user.click(screen.getByTestId("viewing-past-run-back"));

    expect(screen.queryByTestId("viewing-past-run")).toBeNull();
    expect(screen.queryByTestId("run-summary")).toBeNull();
  });

  it("reuses a stored prompt and its datasheets without starting a run", async () => {
    const user = userEvent.setup();
    mount(storeWith([storedRun()]));

    const trigger = await screen.findByTestId("history-trigger");
    await waitFor(() => expect(disabled(trigger)).toBe(false));
    await user.click(trigger);
    await user.click(
      within(await screen.findByTestId("history-entry")).getByTestId(
        "history-entry-reuse"
      )
    );

    expect(value(screen.getByTestId("prompt-input"))).toBe("an STM32 breakout with a 3.3V rail");
    const chip = await screen.findByTestId("datasheet-chip");
    expect(chip.getAttribute("data-part")).toBe("STM32F103");
    expect(requestsTo("/generate/stream")).toHaveLength(0);
  });

  it("a run finished in this window joins the list and is not marked restored", async () => {
    const user = userEvent.setup();
    mount(storeWith([]));

    await user.type(screen.getByTestId("prompt-input"), "a 3.3V LDO board");
    await user.click(screen.getByTestId("prompt-submit"));
    await screen.findByTestId("run-summary");

    const trigger = screen.getByTestId("history-trigger");
    await waitFor(() => expect(trigger.getAttribute("data-count")).toBe("1"));
    await user.click(trigger);

    const row = await screen.findByTestId("history-entry");
    expect(row.getAttribute("data-restored")).toBe("0");
    expect(within(row).queryByTestId("history-entry-restored")).toBeNull();
  });
});

describe("the labels a history row carries", () => {
  it("keeps the three ways of having no findings apart", () => {
    const base = storedRun();
    // Review off is not a statement about the board.
    expect(
      verdictLabel({
        ...base,
        request: { ...base.request, review: false },
        result: { ...base.result, findings: undefined },
      })
    ).toBe("review was off");
    // A response with no review block at all.
    expect(
      verdictLabel({ ...base, result: { ...base.result, findings: undefined } })
    ).toBe("no review in the response");
    // The review ran and said nothing — still not "clean".
    expect(verdictLabel(base)).toBe("review reported nothing");
    expect(verdictLabel(base)).not.toMatch(/clean|ok|pass/i);
  });

  it("counts blockers ahead of other findings, from both response shapes", () => {
    const base = storedRun();
    expect(
      verdictLabel({
        ...base,
        result: {
          ...base.result,
          findings: [
            { severity: "blocker" },
            { severity: "note" },
            { severity: "blocker" },
          ],
        },
      })
    ).toBe("2 blockers");
    expect(
      verdictLabel({
        ...base,
        result: { ...base.result, findings: [{ severity: "note" }] },
      })
    ).toBe("1 finding");
    expect(
      verdictLabel({
        ...base,
        result: { ...base.result, findings: undefined, blockers: ["C2 is too big"] },
      })
    ).toBe("1 blocker");
  });

  it("ages a row from a real clock", () => {
    const now = 1_700_000_000_000;
    expect(agoLabel(now - 1_000, now)).toBe("just now");
    expect(agoLabel(now - 4 * 60_000, now)).toBe("4m ago");
    expect(agoLabel(now - 3 * 3_600_000, now)).toBe("3h ago");
    expect(agoLabel(now - 2 * 86_400_000, now)).toBe("2d ago");
  });
});

/* ------------------------------------------------------------------ voice */

describe("voice in the panel", () => {
  class FakeMediaRecorder {
    static isTypeSupported = () => true;
    static last: FakeMediaRecorder | null = null;

    state: "inactive" | "recording" = "inactive";
    mimeType: string;
    ondataavailable: ((event: { data: Blob }) => void) | null = null;
    onstop: (() => void) | null = null;

    constructor(_stream: MediaStream, options?: { mimeType?: string }) {
      this.mimeType = options?.mimeType ?? "audio/webm";
      FakeMediaRecorder.last = this;
    }

    start() {
      this.state = "recording";
    }

    stop() {
      this.state = "inactive";
      this.ondataavailable?.({ data: new Blob(["audio"], { type: this.mimeType }) });
      this.onstop?.();
    }
  }

  beforeEach(() => {
    FakeMediaRecorder.last = null;
    vi.stubGlobal("MediaRecorder", FakeMediaRecorder);
    Object.defineProperty(navigator, "mediaDevices", {
      value: {
        getUserMedia: vi.fn(async () => ({
          getTracks: () => [{ stop: vi.fn(), enabled: true }],
        })),
      },
      configurable: true,
    });
  });

  afterEach(() => vi.unstubAllGlobals());

  it("posts the recording to /transcribe and puts the text in the box", async () => {
    const user = userEvent.setup();
    mount();

    await user.click(screen.getByTestId("voice-start"));
    await screen.findByTestId("voice-recording");

    await act(async () => {
      FakeMediaRecorder.last?.stop();
    });

    await waitFor(() => expect(requestsTo("/transcribe")).toHaveLength(1));
    const body = requestsTo("/transcribe")[0];
    expect(typeof body.audio_b64).toBe("string");
    expect((body.audio_b64 as string).length).toBeGreaterThan(0);
    // Whatever container the recorder produced is what is declared, so the
    // service can hand Gemini a type it actually received.
    expect(body.mime_type).toBe("audio/ogg;codecs=opus");

    // The transcript lands in the draft for review. It never starts a run:
    // transcription is cheap and generation is not.
    await waitFor(() =>
      expect(value(screen.getByTestId("prompt-input"))).toBe("a 3.3V LDO board")
    );
    expect(requestsTo("/generate/stream")).toHaveLength(0);
  });

  it("the dictated prompt runs like a typed one, datasheets included", async () => {
    const user = userEvent.setup();
    mount();

    await user.click(screen.getByTestId("datasheet-attach-trigger"));
    await user.type(screen.getByTestId("datasheet-part-input"), "AMS1117");
    await user.type(screen.getByTestId("datasheet-url-input"), DATASHEET);
    await user.click(screen.getByTestId("datasheet-attach-submit"));

    await user.click(screen.getByTestId("voice-start"));
    await screen.findByTestId("voice-recording");
    await act(async () => {
      FakeMediaRecorder.last?.stop();
    });
    await waitFor(() =>
      expect(value(screen.getByTestId("prompt-input"))).toBe("a 3.3V LDO board")
    );

    await user.click(screen.getByTestId("prompt-submit"));

    await waitFor(() => expect(requestsTo("/generate/stream")).toHaveLength(1));
    const body = requestsTo("/generate/stream")[0];
    expect(body.intent).toBe("a 3.3V LDO board");
    expect(body.datasheets).toEqual({ AMS1117: DATASHEET });
  });
});
