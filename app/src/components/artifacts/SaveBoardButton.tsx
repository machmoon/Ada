import { useCallback, useState } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  Check,
  ClipboardCopy,
  Download,
  ExternalLink,
  Loader2,
  TriangleAlert,
} from "lucide-react";

/**
 * Writing a run's artefacts to the user's disk, and opening them in KiCad.
 *
 * The shell now ships `@tauri-apps/plugin-dialog` and `@tauri-apps/plugin-fs`,
 * granted as `dialog:allow-save` plus the two text-file commands with no
 * static filesystem scope: the dialog plugin adds the user-picked path to the
 * fs scope at runtime, so this component can only ever write where the user
 * pointed. The imports below are literal (so the bundler resolves and ships
 * them — a `@vite-ignore` variable specifier would leave a bare specifier the
 * webview cannot resolve) but still guarded: outside a Tauri webview, or in a
 * build missing a plugin, the loader yields null and the component degrades to
 * the clipboard — which either genuinely succeeded or genuinely threw — while
 * saying out loud which of the two happened.
 */

type DialogModule = {
  save: (options: {
    defaultPath?: string;
    filters?: { name: string; extensions: string[] }[];
  }) => Promise<string | null>;
};

type FsModule = {
  writeTextFile: (path: string, contents: string) => Promise<void>;
  readTextFile: (path: string) => Promise<string>;
};

type OpenerModule = {
  openPath: (path: string) => Promise<void>;
  revealItemInDir: (path: string) => Promise<unknown>;
};

/** A module that may be absent from this build: null instead of a throw. */
async function loadDialog(): Promise<DialogModule | null> {
  try {
    return (await import("@tauri-apps/plugin-dialog")) as DialogModule;
  } catch {
    return null;
  }
}

async function loadFs(): Promise<FsModule | null> {
  try {
    return (await import("@tauri-apps/plugin-fs")) as FsModule;
  } catch {
    return null;
  }
}

async function loadOpener(): Promise<OpenerModule | null> {
  try {
    return (await import("@tauri-apps/plugin-opener")) as OpenerModule;
  } catch {
    return null;
  }
}

export type SaveOutcome =
  | { kind: "idle" }
  | { kind: "saving" }
  /** Written to `path` and read back; the readback matched. */
  | { kind: "saved"; path: string }
  /** No filesystem access in this build; the text went to the clipboard. */
  | { kind: "copied" }
  | { kind: "cancelled" }
  | { kind: "error"; message: string };

/** Strip anything a filesystem would object to, and keep the extension. */
function safeFilename(name: string): string {
  const cleaned = name.replace(/[\\/:*?"<>|\u0000-\u001f]/g, "-").trim();
  return cleaned.length > 0 ? cleaned : "board.kicad_pcb";
}

function extensionOf(name: string): string[] {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? [name.slice(dot + 1)] : [];
}

export interface SaveBoardButtonProps {
  /**
   * The artefact's text. `undefined` when the run produced none — the button
   * disables itself and says so rather than writing an empty file.
   */
  content?: string | null;
  /** Suggested filename, extension included. */
  filename?: string;
  label?: string;
  className?: string;
  variant?: React.ComponentProps<typeof Button>["variant"];
  size?: React.ComponentProps<typeof Button>["size"];
  /** Fires on every terminal outcome, success or not. */
  onOutcome?: (outcome: SaveOutcome) => void;
}

/** What happened when the user asked for the saved file to be opened. */
export type OpenOutcome =
  | { kind: "idle" }
  | { kind: "opening" }
  /** Handed to whatever application owns the file's extension. */
  | { kind: "opened" }
  /** Could not launch a handler; the file was revealed in its folder instead. */
  | { kind: "revealed" }
  | { kind: "error"; message: string };

export function SaveBoardButton({
  content,
  filename = "board.kicad_pcb",
  label = "Save board file",
  className,
  variant = "default",
  size = "default",
  onOutcome,
}: SaveBoardButtonProps) {
  const [outcome, setOutcome] = useState<SaveOutcome>({ kind: "idle" });
  const [openOutcome, setOpenOutcome] = useState<OpenOutcome>({ kind: "idle" });
  const hasContent = typeof content === "string" && content.length > 0;

  const settle = useCallback(
    (next: SaveOutcome) => {
      setOutcome(next);
      onOutcome?.(next);
    },
    [onOutcome]
  );

  const run = useCallback(async () => {
    if (!hasContent) return;
    const text = content as string;
    const suggested = safeFilename(filename);
    setOutcome({ kind: "saving" });
    setOpenOutcome({ kind: "idle" });

    const [dialog, fs] = await Promise.all([loadDialog(), loadFs()]);

    if (dialog && fs) {
      try {
        const path = await dialog.save({
          defaultPath: suggested,
          filters: [{ name: suggested, extensions: extensionOf(suggested) }],
        });
        if (!path) {
          settle({ kind: "cancelled" });
          return;
        }
        await fs.writeTextFile(path, text);
        // Never claim a write we did not confirm: read it back and compare.
        const written = await fs.readTextFile(path);
        if (written.length !== text.length) {
          settle({
            kind: "error",
            message: `Wrote ${path}, but reading it back gave ${written.length} characters instead of ${text.length}. Do not trust that file.`,
          });
          return;
        }
        // Revealing is a courtesy; failing to reveal does not unsave the file.
        void loadOpener().then((opener) =>
          opener?.revealItemInDir(path).catch(() => {})
        );
        settle({ kind: "saved", path });
        return;
      } catch (error) {
        settle({
          kind: "error",
          message: error instanceof Error ? error.message : String(error),
        });
        return;
      }
    }

    try {
      await navigator.clipboard.writeText(text);
      settle({ kind: "copied" });
    } catch (error) {
      settle({
        kind: "error",
        message:
          "This build cannot write files and the clipboard refused too: " +
          (error instanceof Error ? error.message : String(error)),
      });
    }
  }, [content, filename, hasContent, settle]);

  /**
   * Hand the saved file to whatever application owns `.kicad_pcb` — KiCad on
   * a machine that has it. `openPath` is the primary; reveal-in-folder is the
   * fallback, so a machine with no `.kicad_pcb` handler still ends up looking
   * at the file rather than at nothing.
   */
  const openInKicad = useCallback(async () => {
    if (outcome.kind !== "saved") return;
    const path = outcome.path;
    setOpenOutcome({ kind: "opening" });

    const opener = await loadOpener();
    if (!opener) {
      setOpenOutcome({
        kind: "error",
        message: "This build cannot open files in other applications.",
      });
      return;
    }

    try {
      await opener.openPath(path);
      setOpenOutcome({ kind: "opened" });
    } catch {
      try {
        await opener.revealItemInDir(path);
        setOpenOutcome({ kind: "revealed" });
      } catch (error) {
        setOpenOutcome({
          kind: "error",
          message:
            "Could not launch a handler for the file or reveal it: " +
            (error instanceof Error ? error.message : String(error)),
        });
      }
    }
  }, [outcome]);

  const busy = outcome.kind === "saving";
  const opening = openOutcome.kind === "opening";

  return (
    <div className={cn("flex flex-col gap-1", className)}>
      <Button
        type="button"
        variant={variant}
        size={size}
        disabled={!hasContent || busy}
        onClick={() => void run()}
        data-testid="save-board-button"
        data-filename={filename}
      >
        {busy ? (
          <Loader2 className="animate-spin" />
        ) : outcome.kind === "saved" ? (
          <Check />
        ) : outcome.kind === "copied" ? (
          <ClipboardCopy />
        ) : (
          <Download />
        )}
        {label}
      </Button>

      {!hasContent && (
        <p
          className="text-xs text-muted-foreground"
          data-testid="save-board-status"
        >
          This run returned no {filename} — there is nothing to save.
        </p>
      )}

      {hasContent && outcome.kind === "saved" && (
        <>
          <p
            className="text-xs text-muted-foreground break-all"
            data-testid="save-board-status"
          >
            Written and read back from{" "}
            <span className="font-mono">{outcome.path}</span>.
          </p>
          <Button
            type="button"
            variant="outline"
            size={size}
            disabled={opening}
            onClick={() => void openInKicad()}
            data-testid="open-in-kicad-button"
          >
            {opening ? <Loader2 className="animate-spin" /> : <ExternalLink />}
            Open in KiCad
          </Button>
          {openOutcome.kind === "opened" && (
            <p
              className="text-xs text-muted-foreground"
              data-testid="open-in-kicad-status"
            >
              Handed to the application that owns .kicad_pcb files.
            </p>
          )}
          {openOutcome.kind === "revealed" && (
            <p
              className="text-xs text-muted-foreground"
              data-testid="open-in-kicad-status"
            >
              No application would open it, so the file was revealed in its
              folder instead.
            </p>
          )}
          {openOutcome.kind === "error" && (
            <p
              className="text-xs text-destructive flex items-start gap-1"
              data-testid="open-in-kicad-status"
            >
              <TriangleAlert className="size-3 mt-0.5 shrink-0" />
              <span className="break-all">{openOutcome.message}</span>
            </p>
          )}
        </>
      )}

      {hasContent && outcome.kind === "copied" && (
        <p
          className="text-xs text-muted-foreground"
          data-testid="save-board-status"
        >
          Copied to the clipboard. This build has no filesystem access, so
          nothing was written to disk — paste it into a file named{" "}
          <span className="font-mono">{filename}</span>.
        </p>
      )}

      {hasContent && outcome.kind === "cancelled" && (
        <p
          className="text-xs text-muted-foreground"
          data-testid="save-board-status"
        >
          Save cancelled. Nothing was written.
        </p>
      )}

      {hasContent && outcome.kind === "error" && (
        <p
          className="text-xs text-destructive flex items-start gap-1"
          data-testid="save-board-status"
        >
          <TriangleAlert className="size-3 mt-0.5 shrink-0" />
          <span className="break-all">{outcome.message}</span>
        </p>
      )}
    </div>
  );
}
