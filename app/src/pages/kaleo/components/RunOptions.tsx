import { useEffect, useMemo, useRef, useState } from "react";
import { MinusIcon, PlusIcon } from "lucide-react";
import { Button, Input, Label, Switch } from "@/components";
import { MAX_TIME_LIMIT_S, MIN_TIME_LIMIT_S } from "@/lib/silkscreen/client";
import type { RunRequestDraft } from "@/contexts";
import { cn } from "@/lib/utils";

/**
 * One datasheet row as the form holds it.
 *
 * The draft stores datasheets as `part -> url`, which cannot represent a row
 * mid-type (an empty part key, a renamed part). So the rows live here and only
 * complete pairs are pushed back into the draft; the client drops half-filled
 * rows anyway, and this keeps them visible while they are being typed.
 */
interface DatasheetRow {
  part: string;
  url: string;
}

const BLANK_ROW: DatasheetRow = { part: "", url: "" };

function rowsToRecord(rows: DatasheetRow[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const row of rows) {
    const part = row.part.trim();
    const url = row.url.trim();
    if (part && url) out[part] = url;
  }
  return out;
}

function recordToRows(record: Record<string, string>): DatasheetRow[] {
  const rows = Object.entries(record).map(([part, url]) => ({ part, url }));
  return rows.length ? rows : [BLANK_ROW];
}

function sameRecord(a: Record<string, string>, b: Record<string, string>): boolean {
  const ka = Object.keys(a);
  const kb = Object.keys(b);
  return ka.length === kb.length && ka.every((k) => a[k] === b[k]);
}

/** True when there is a URL for retrieval to actually ground on. */
function canGround(rows: DatasheetRow[]): boolean {
  return rows.some((row) => row.url.trim().length > 0);
}

function clampTimeLimit(raw: string): number {
  const n = Number.parseInt(raw, 10);
  if (!Number.isFinite(n)) return MIN_TIME_LIMIT_S;
  return Math.min(MAX_TIME_LIMIT_S, Math.max(MIN_TIME_LIMIT_S, n));
}

export interface RunOptionsProps {
  request: RunRequestDraft;
  onChange: (patch: Partial<RunRequestDraft>) => void;
  /** Options freeze mid-run: the engine already has the ones it was given. */
  disabled?: boolean;
}

export const RunOptions = ({ request, onChange, disabled }: RunOptionsProps) => {
  const [rows, setRows] = useState<DatasheetRow[]>(() =>
    recordToRows(request.datasheets)
  );
  const rowsRef = useRef(rows);
  rowsRef.current = rows;

  // Re-seed when the draft's datasheets change from somewhere else — restoring
  // a past run's request, say. Rows this form itself pushed match already.
  useEffect(() => {
    if (!sameRecord(request.datasheets, rowsToRecord(rowsRef.current))) {
      setRows(recordToRows(request.datasheets));
    }
  }, [request.datasheets]);

  const groundable = useMemo(() => canGround(rows), [rows]);

  const commitRows = (next: DatasheetRow[]) => {
    setRows(next);
    const datasheets = rowsToRecord(next);
    // Grounding with no source is a claim the run cannot back up, so it comes
    // off the moment the last URL does.
    const ground = request.ground && canGround(next);
    onChange({ datasheets, ground });
  };

  return (
    <div className="flex flex-col gap-4" data-testid="run-options">
      <div className="flex flex-col gap-1.5">
        <div className="flex items-center justify-between gap-2">
          <Label htmlFor="kaleo-time-limit" className="text-xs">
            Placer budget
          </Label>
          <span className="text-xs text-muted-foreground tabular-nums">
            {request.time_limit_s}s
          </span>
        </div>
        <Input
          id="kaleo-time-limit"
          type="number"
          inputMode="numeric"
          min={MIN_TIME_LIMIT_S}
          max={MAX_TIME_LIMIT_S}
          value={request.time_limit_s}
          disabled={disabled}
          data-testid="run-options-time-limit"
          onChange={(e) => onChange({ time_limit_s: clampTimeLimit(e.target.value) })}
          onBlur={(e) => onChange({ time_limit_s: clampTimeLimit(e.target.value) })}
        />
        <p className="text-[10px] text-muted-foreground">
          How long CP-SAT may search, {MIN_TIME_LIMIT_S}–{MAX_TIME_LIMIT_S}s. A
          short budget does not fail the run, it settles for a worse layout.
        </p>
      </div>

      <div className="flex items-start justify-between gap-3">
        <div className="flex flex-col">
          <Label htmlFor="kaleo-review" className="text-xs">
            Review the board
          </Label>
          <p className="text-[10px] text-muted-foreground">
            Runs the critic pass. Off means nothing was checked, not that the
            board is clean.
          </p>
        </div>
        <Switch
          id="kaleo-review"
          checked={request.review}
          disabled={disabled}
          data-testid="run-options-review"
          onCheckedChange={(checked) => onChange({ review: checked })}
        />
      </div>

      <div className="flex items-start justify-between gap-3">
        <div className="flex flex-col">
          <Label
            htmlFor="kaleo-ground"
            className={cn("text-xs", !groundable && "text-muted-foreground")}
          >
            Ground in the datasheets
          </Label>
          <p className="text-[10px] text-muted-foreground">
            {groundable
              ? "Retrieve pin facts from the PDFs below before proposing."
              : "Needs a datasheet URL — retrieval has nothing to ground on."}
          </p>
        </div>
        <Switch
          id="kaleo-ground"
          checked={request.ground && groundable}
          disabled={disabled || !groundable}
          data-testid="run-options-ground"
          onCheckedChange={(checked) => onChange({ ground: checked })}
        />
      </div>

      <div className="flex flex-col gap-2">
        <Label className="text-xs">Datasheets</Label>
        {rows.map((row, index) => (
          <div
            key={index}
            className="flex items-center gap-1.5"
            data-testid="run-options-datasheet-row"
            data-index={index}
          >
            <Input
              placeholder="Part"
              value={row.part}
              disabled={disabled}
              className="w-24 text-xs"
              onChange={(e) =>
                commitRows(
                  rows.map((r, i) => (i === index ? { ...r, part: e.target.value } : r))
                )
              }
            />
            <Input
              placeholder="https://…/datasheet.pdf"
              value={row.url}
              disabled={disabled}
              className="flex-1 text-xs"
              onChange={(e) =>
                commitRows(
                  rows.map((r, i) => (i === index ? { ...r, url: e.target.value } : r))
                )
              }
            />
            <Button
              variant="ghost"
              size="icon"
              className="size-8 shrink-0"
              title="Remove this datasheet"
              disabled={disabled || rows.length === 1}
              onClick={() => commitRows(rows.filter((_, i) => i !== index))}
            >
              <MinusIcon className="size-3.5" />
            </Button>
          </div>
        ))}
        <Button
          variant="outline"
          size="sm"
          className="w-fit"
          disabled={disabled}
          data-testid="run-options-add-datasheet"
          onClick={() => setRows([...rows, { ...BLANK_ROW }])}
        >
          <PlusIcon className="size-3.5" />
          Add a datasheet
        </Button>
      </div>
    </div>
  );
};
