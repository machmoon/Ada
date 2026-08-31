// @vitest-environment jsdom
//
// The save path, held to its own promises.
//
// The component makes two claims worth pinning: it never reports "saved"
// without writing through the dialog-picked path AND reading the file back,
// and "Open in KiCad" hands that same path to `openPath`, falling back to
// reveal-in-folder rather than to silence. The three Tauri plugins are mocked
// at the module boundary — the exact seam the real webview crosses — so every
// assertion is about the calls that would really have reached Rust.

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SaveBoardButton, type SaveOutcome } from "./SaveBoardButton";

const plugins = vi.hoisted(() => ({
  save: vi.fn<(options: unknown) => Promise<string | null>>(),
  writeTextFile: vi.fn<(path: string, contents: string) => Promise<void>>(),
  readTextFile: vi.fn<(path: string) => Promise<string>>(),
  openPath: vi.fn<(path: string) => Promise<void>>(),
  revealItemInDir: vi.fn<(path: string) => Promise<void>>(),
}));

vi.mock("@tauri-apps/plugin-dialog", () => ({ save: plugins.save }));
vi.mock("@tauri-apps/plugin-fs", () => ({
  writeTextFile: plugins.writeTextFile,
  readTextFile: plugins.readTextFile,
}));
vi.mock("@tauri-apps/plugin-opener", () => ({
  openPath: plugins.openPath,
  revealItemInDir: plugins.revealItemInDir,
}));

const BOARD = "(kicad_pcb (version 20240108) (generator silkscreen))";
const PATH = "/Users/someone/boards/ldo.kicad_pcb";

/** Save successfully, leaving the component in the `saved` state. */
async function saveOnce(user: ReturnType<typeof userEvent.setup>) {
  plugins.save.mockResolvedValue(PATH);
  plugins.writeTextFile.mockResolvedValue(undefined);
  plugins.readTextFile.mockResolvedValue(BOARD);
  await user.click(screen.getByTestId("save-board-button"));
  await screen.findByTestId("open-in-kicad-button");
}

beforeEach(() => {
  for (const fn of Object.values(plugins)) fn.mockReset();
  // Reveal-on-save is a fire-and-forget courtesy; give it a resolution so an
  // unhandled rejection cannot leak into an unrelated assertion.
  plugins.revealItemInDir.mockResolvedValue(undefined);
});

afterEach(cleanup);

describe("saving", () => {
  it("writes the dialog-picked path, reads it back, and only then reports saved", async () => {
    const outcomes: SaveOutcome[] = [];
    const user = userEvent.setup();
    render(
      <SaveBoardButton
        content={BOARD}
        filename="ldo.kicad_pcb"
        onOutcome={(o) => outcomes.push(o)}
      />
    );

    await saveOnce(user);

    expect(plugins.writeTextFile).toHaveBeenCalledWith(PATH, BOARD);
    expect(plugins.readTextFile).toHaveBeenCalledWith(PATH);
    expect(screen.getByTestId("save-board-status").textContent).toContain(PATH);
    expect(outcomes).toEqual([{ kind: "saved", path: PATH }]);
  });

  it("suggests the artefact's own filename to the dialog", async () => {
    const user = userEvent.setup();
    render(<SaveBoardButton content={BOARD} filename="ldo.kicad_pcb" />);

    await saveOnce(user);

    expect(plugins.save).toHaveBeenCalledWith(
      expect.objectContaining({ defaultPath: "ldo.kicad_pcb" })
    );
  });

  it("does not trust a write whose readback differs", async () => {
    const user = userEvent.setup();
    render(<SaveBoardButton content={BOARD} />);
    plugins.save.mockResolvedValue(PATH);
    plugins.writeTextFile.mockResolvedValue(undefined);
    plugins.readTextFile.mockResolvedValue(BOARD.slice(0, 5));

    await user.click(screen.getByTestId("save-board-button"));

    const status = await screen.findByTestId("save-board-status");
    expect(status.textContent).toContain("Do not trust that file");
    expect(screen.queryByTestId("open-in-kicad-button")).toBeNull();
  });

  it("treats a dismissed dialog as a cancellation, not an error", async () => {
    const user = userEvent.setup();
    render(<SaveBoardButton content={BOARD} />);
    plugins.save.mockResolvedValue(null);

    await user.click(screen.getByTestId("save-board-button"));

    const status = await screen.findByTestId("save-board-status");
    expect(status.textContent).toContain("Save cancelled");
    expect(plugins.writeTextFile).not.toHaveBeenCalled();
  });

  it("disables itself when the run produced no board", () => {
    render(<SaveBoardButton content={undefined} />);

    const button = screen.getByTestId<HTMLButtonElement>("save-board-button");
    expect(button.disabled).toBe(true);
    expect(screen.getByTestId("save-board-status").textContent).toContain(
      "nothing to save"
    );
    expect(plugins.save).not.toHaveBeenCalled();
  });
});

describe("open in KiCad", () => {
  it("is offered only after a confirmed save, and hands over the saved path", async () => {
    const user = userEvent.setup();
    render(<SaveBoardButton content={BOARD} />);
    expect(screen.queryByTestId("open-in-kicad-button")).toBeNull();

    await saveOnce(user);
    plugins.openPath.mockResolvedValue(undefined);

    await user.click(screen.getByTestId("open-in-kicad-button"));

    await waitFor(() => expect(plugins.openPath).toHaveBeenCalledWith(PATH));
    expect(
      (await screen.findByTestId("open-in-kicad-status")).textContent
    ).toContain("Handed to the application");
  });

  it("falls back to revealing the file when no handler will launch", async () => {
    const user = userEvent.setup();
    render(<SaveBoardButton content={BOARD} />);
    await saveOnce(user);
    plugins.revealItemInDir.mockClear(); // drop the on-save courtesy call
    plugins.openPath.mockRejectedValue(new Error("no application for path"));

    await user.click(screen.getByTestId("open-in-kicad-button"));

    const status = await screen.findByTestId("open-in-kicad-status");
    expect(status.textContent).toContain("revealed in its folder");
    expect(plugins.revealItemInDir).toHaveBeenCalledWith(PATH);
  });

  it("says so when neither opening nor revealing worked", async () => {
    const user = userEvent.setup();
    render(<SaveBoardButton content={BOARD} />);
    await saveOnce(user);
    plugins.openPath.mockRejectedValue(new Error("no application for path"));
    plugins.revealItemInDir.mockRejectedValue(new Error("gone"));

    await user.click(screen.getByTestId("open-in-kicad-button"));

    const status = await screen.findByTestId("open-in-kicad-status");
    expect(status.textContent).toContain("Could not launch");
  });
});
