// "Attach a datasheet for a part" — the assistant panel's attach affordance.
//
// This is Pluely's paperclip, pointed at the one thing this engine can
// genuinely do with an attachment. `POST /generate` takes
// `datasheets: {partNumber: url}`; the service hands each URL to the datasheet
// reader, and with `ground` on, the reviewer's findings are checked back
// against those pages. So the affordance is a *part number and a URL*, not a
// file picker: nothing in the engine accepts uploaded bytes, and a control
// that took a local PDF would have to either silently drop it or pretend it
// had been read. The chips say plainly that the engine fetches these itself.
//
// The pairs live in the run draft (`request.datasheets`), the same field the
// options popover edits and the same field `normalizeRequest` serialises, so a
// chip on screen and a key in the request body are the same fact.

import { useState } from "react";
import { FileTextIcon, PaperclipIcon, XIcon } from "lucide-react";
import {
  Button,
  Input,
  Label,
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components";
import { useSilkscreenRun } from "@/contexts";
import { cn } from "@/lib/utils";

/**
 * The engine dereferences these URLs itself, so only a scheme it can fetch is
 * worth accepting. The service enforces the same rule on a grounded run
 * (`'datasheet URL is not an http(s) URL'`); refusing it here means the user
 * finds out before a run is paid for rather than after.
 */
export function datasheetUrlProblem(raw: string): string | null {
  const url = raw.trim();
  if (!url) return "A datasheet URL is required.";
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return "That is not a URL the engine can fetch.";
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return "Only http and https URLs — the engine fetches the PDF itself.";
  }
  if (!parsed.hostname) return "That URL has no host to fetch from.";
  return null;
}

/** Only pairs that survive `normalizeRequest`; a half-filled one is not attached. */
export function attachedPairs(
  datasheets: Record<string, string>
): [string, string][] {
  return Object.entries(datasheets).filter(
    ([part, url]) => part.trim().length > 0 && url.trim().length > 0
  );
}

export interface AttachDatasheetButtonProps {
  /** A run is in flight: the engine already has the datasheets it was given. */
  disabled?: boolean;
}

/**
 * The paperclip. Opens a two-field form and writes one pair into the draft.
 *
 * It cannot start a run and it cannot upload anything; the only thing it does
 * is add a key to `request.datasheets`.
 */
export const AttachDatasheetButton = ({
  disabled,
}: AttachDatasheetButtonProps) => {
  const { request, setDatasheet } = useSilkscreenRun();
  const [open, setOpen] = useState(false);
  const [part, setPart] = useState("");
  const [url, setUrl] = useState("");
  const [touched, setTouched] = useState(false);

  const trimmedPart = part.trim();
  const urlProblem = datasheetUrlProblem(url);
  const partProblem = trimmedPart ? null : "A part number is required.";
  const problem = partProblem ?? urlProblem;
  const replacing = trimmedPart in request.datasheets;
  const count = attachedPairs(request.datasheets).length;

  const attach = () => {
    setTouched(true);
    if (problem) return;
    setDatasheet(trimmedPart, url.trim());
    setPart("");
    setUrl("");
    setTouched(false);
    setOpen(false);
  };

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) setTouched(false);
      }}
    >
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          disabled={disabled}
          title={
            count
              ? `Attach a datasheet — ${count} attached`
              : "Attach a datasheet for a part"
          }
          aria-label="Attach a datasheet for a part"
          data-testid="datasheet-attach-trigger"
          data-count={count}
        >
          <PaperclipIcon className="size-4" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        side="bottom"
        sideOffset={8}
        className="w-96 border border-input/50 p-4"
      >
        <div className="flex flex-col gap-3" data-testid="datasheet-attach-form">
          <div className="flex flex-col gap-1">
            <Label htmlFor="kaleo-attach-part" className="text-xs">
              Part number
            </Label>
            <Input
              id="kaleo-attach-part"
              placeholder="AMS1117-3.3"
              value={part}
              className="text-xs"
              data-testid="datasheet-part-input"
              onChange={(e) => setPart(e.target.value)}
            />
          </div>

          <div className="flex flex-col gap-1">
            <Label htmlFor="kaleo-attach-url" className="text-xs">
              Datasheet URL
            </Label>
            <Input
              id="kaleo-attach-url"
              placeholder="https://…/AMS1117.pdf"
              value={url}
              className="text-xs"
              data-testid="datasheet-url-input"
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  attach();
                }
              }}
              onChange={(e) => setUrl(e.target.value)}
            />
          </div>

          <p className="text-[10px] text-muted-foreground">
            The engine fetches this itself — nothing is uploaded from this
            machine. Attaching a datasheet lets the reader use it while the
            board is designed; turning on “Ground in the datasheets” in run
            options additionally checks the review’s findings back against
            these pages.
          </p>

          {touched && problem ? (
            <p className="text-[11px] text-destructive" data-testid="datasheet-attach-error">
              {problem}
            </p>
          ) : null}

          {replacing ? (
            <p className="text-[10px] text-muted-foreground">
              {trimmedPart} already has a datasheet attached; this replaces it.
            </p>
          ) : null}

          <Button
            size="sm"
            className="w-fit"
            onClick={attach}
            data-testid="datasheet-attach-submit"
          >
            <PaperclipIcon className="size-3.5" />
            Attach
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
};

export interface DatasheetChipsProps {
  className?: string;
  /** Removal freezes mid-run, like every other run option. */
  disabled?: boolean;
}

/**
 * What is attached, as chips.
 *
 * Renders nothing when nothing is attached — an empty tray would suggest the
 * run has a grounding source it does not have.
 */
export const DatasheetChips = ({ className, disabled }: DatasheetChipsProps) => {
  const { request, removeDatasheet } = useSilkscreenRun();
  const pairs = attachedPairs(request.datasheets);
  if (pairs.length === 0) return null;

  return (
    <div
      className={cn("flex flex-wrap items-center gap-1.5", className)}
      data-testid="datasheet-chips"
      data-count={pairs.length}
    >
      {pairs.map(([partNumber, url]) => (
        <span
          key={partNumber}
          className="flex max-w-full items-center gap-1 rounded-full border border-input/60 bg-muted/40 py-0.5 pl-2 pr-0.5 text-[11px]"
          data-testid="datasheet-chip"
          data-part={partNumber}
          // The URL is the whole payload; it belongs where it can be checked.
          title={`${partNumber} — ${url}`}
        >
          <FileTextIcon className="size-3 shrink-0 text-muted-foreground" />
          <span className="truncate">{partNumber}</span>
          <Button
            variant="ghost"
            size="icon"
            className="size-4 rounded-full"
            disabled={disabled}
            title={`Remove the ${partNumber} datasheet`}
            aria-label={`Remove the ${partNumber} datasheet`}
            data-testid="datasheet-chip-remove"
            onClick={() => removeDatasheet(partNumber)}
          >
            <XIcon className="size-3" />
          </Button>
        </span>
      ))}
      <span className="text-[10px] text-muted-foreground">
        {request.ground
          ? "sent with the run, and the review is checked against them"
          : "sent with the run"}
      </span>
    </div>
  );
};
