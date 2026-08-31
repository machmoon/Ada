// @vitest-environment jsdom
//
// Smoke tests for the review surface: the components render what the response
// actually carried, and the honesty rules hold — an empty findings list never
// reads as a clean board, an absent metric never reads as zero, and the
// suggested-fix control is inert.

import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { ArtifactPanel } from "@/components/artifacts/ArtifactPanel";
import { BoardView } from "@/components/board/BoardView";
import { FindingCard, findingKey } from "@/components/review/FindingCard";
import { OrderPanel } from "@/components/artifacts/OrderPanel";
import { ReviewPanel } from "@/components/review/ReviewPanel";
import { RunSummary } from "@/components/artifacts/RunSummary";
import type { Finding, Placements, RunResult } from "@/lib/silkscreen/types";

beforeAll(() => {
  // Radix ScrollArea measures itself; jsdom has no ResizeObserver.
  if (!("ResizeObserver" in globalThis)) {
    class ResizeObserverStub {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    vi.stubGlobal("ResizeObserver", ResizeObserverStub);
  }
});

afterEach(() => cleanup());

const PLACEMENTS: Placements = {
  board_mm: [20, 10],
  frame: "solver-y-up",
  parts: [
    {
      ref: "U1",
      footprint: "Package_TO_SOT_SMD:SOT-223",
      value: "AMS1117-3.3",
      layer: "F.Cu",
      rotated: false,
      x_mm: 2,
      y_mm: 2,
      courtyard_mm: [2, 2, 7, 7],
      pads: [
        { number: "1", net: "GND", rect_mm: [3, 3, 1, 2] },
        { number: "2", net: "VOUT", rect_mm: [5, 3, 1, 2] },
      ],
    },
    {
      ref: "C1",
      footprint: "Capacitor_SMD:C_0805",
      value: "10uF",
      layer: "F.Cu",
      rotated: false,
      x_mm: 12,
      y_mm: 4,
      courtyard_mm: [12, 4, 2, 3],
      pads: [],
    },
  ],
};

/** The shape `_finding_dict` in service/app.py actually sends. */
const WIRE_FINDING: Finding = {
  severity: "blocker",
  title: "C2 exceeds the LDO's maximum output capacitance",
  detail: "The AMS1117 datasheet limits output capacitance to 22 uF.",
  parts: ["output_cap"],
  refs: ["C2"],
  citation: "AMS1117 datasheet, p. 3",
  suggested_fix: "Replace C2 with a 22 uF part.",
};

describe("BoardView", () => {
  it("renders every part with its ref as an identity attribute", () => {
    const { container } = render(<BoardView placements={PLACEMENTS} />);
    const parts = container.querySelectorAll('[data-testid="board-part"]');
    expect(parts).toHaveLength(2);
    const refs = Array.from(parts).map((el) => el.getAttribute("data-ref"));
    expect(refs).toEqual(["U1", "C1"]);
    // Labels are drawn outside the flipped group, one per part.
    const labels = container.querySelectorAll('[data-testid="board-part-label"]');
    expect(Array.from(labels).map((el) => el.textContent)).toEqual(["U1", "C1"]);
    // The header counts derive from the placements, not a placeholder.
    expect(screen.getByTestId("board-size").textContent).toBe("20.0 × 10.0 mm");
    expect(screen.getByTestId("board-part-count").textContent).toBe("2 parts");
  });

  it("is honest about having no board at all", () => {
    render(<BoardView placements={null} />);
    const empty = screen.getByTestId("board-empty");
    expect(empty.textContent).toContain("No board yet");
    expect(empty.textContent).toContain("Generate a board");
  });

  it("distinguishes 'no run yet' from 'placements arrived empty'", () => {
    render(
      <BoardView
        placements={{ board_mm: [10, 10], frame: "solver-y-up", parts: [] }}
      />
    );
    expect(screen.getByTestId("board-empty").textContent).toContain(
      "placements with no parts"
    );
  });

  it("marks selected parts by ref", () => {
    const { container } = render(
      <BoardView placements={PLACEMENTS} selected={["C1"]} />
    );
    const c1 = container.querySelector('[data-testid="board-part"][data-ref="C1"]');
    const u1 = container.querySelector('[data-testid="board-part"][data-ref="U1"]');
    expect(c1?.getAttribute("data-selected")).toBe("true");
    expect(u1?.hasAttribute("data-selected")).toBe(false);
  });
});

describe("ReviewPanel", () => {
  it("renders a wire-shaped finding with a visible title, refs, citation and inert fix", () => {
    render(<ReviewPanel findings={[WIRE_FINDING]} reviewRequested />);
    const card = screen.getByTestId("finding-card");
    expect(card.getAttribute("data-sev")).toBe("blocker");
    // No origin on the wire finding: it must NOT present as proven.
    expect(card.getAttribute("data-origin")).toBe("unattributed");
    expect(
      within(card).getByTestId("finding-card-head").textContent
    ).toContain("C2 exceeds the LDO's maximum output capacitance");
    expect(
      within(card).getByTestId("finding-card-detail").textContent
    ).toContain("22 uF");
    expect(
      within(card).getByTestId("finding-card-citation").textContent
    ).toContain("AMS1117 datasheet, p. 3");
    const refChips = within(card).getAllByTestId("finding-card-ref");
    expect(refChips.map((el) => el.getAttribute("data-ref"))).toEqual(["C2"]);
    const fix = within(card).getByTestId("finding-card-fix");
    expect((fix as HTMLButtonElement).disabled).toBe(true);
    expect(
      within(card).getByTestId("finding-card-fix-note").textContent
    ).toContain("Nothing here changes the board");
  });

  it("never renders an empty findings list as a clean board", () => {
    render(<ReviewPanel findings={[]} reviewRequested />);
    // The coverage statement is always there and says what did NOT run.
    const counts = screen.getByTestId("what-was-checked-counts");
    expect(counts.textContent).not.toMatch(/clean/i);
    const panel = screen.getByTestId("review-panel");
    expect(panel.textContent).toContain("What was checked");
    expect(panel.textContent).toMatch(/No deterministic geometry/);
  });

  it("says so plainly when the response carried no review at all", () => {
    render(<ReviewPanel />);
    expect(screen.getByTestId("review-no-review").textContent).toContain(
      "no review at all"
    );
    // The coverage statement still renders.
    expect(screen.getByTestId("review-panel").textContent).toContain(
      "What was checked"
    );
  });

  it("shows legacy blockers only when structured findings are absent", () => {
    render(<ReviewPanel blockers={["C2 too large"]} />);
    expect(screen.getByTestId("review-legacy-blockers")).toBeTruthy();

    cleanup();
    // With findings present the same blockers would double-report; they hide.
    render(<ReviewPanel findings={[WIRE_FINDING]} blockers={["C2 too large"]} />);
    expect(screen.queryByTestId("review-legacy-blockers")).toBeNull();
  });

  it("counts on the filter tabs derive from the findings that arrived", () => {
    const findings: Finding[] = [
      WIRE_FINDING,
      { severity: "note", title: "Consider a bulk cap" },
      { severity: "someday-new-severity", title: "From the future" },
    ];
    render(<ReviewPanel findings={findings} />);
    const tabs = screen.getAllByTestId("review-filter-severity");
    const byValue = Object.fromEntries(
      tabs.map((el) => [el.getAttribute("data-value"), el.textContent])
    );
    expect(byValue["all"]).toBe("All 3");
    expect(byValue["blocker"]).toBe("Blockers 1");
    expect(byValue["note"]).toBe("Notes 1");
    expect(byValue["other"]).toBe("Other 1");
    // An unknown severity keeps its own label on the card, not a relabel.
    const cards = screen.getAllByTestId("finding-card");
    expect(
      cards.some(
        (c) => c.getAttribute("data-severity-raw") === "someday-new-severity"
      )
    ).toBe(true);
  });
});

// The three states `findingCount` alone cannot tell apart. The middle one is
// the point: "the review ran and raised nothing" and "no review came back" are
// different facts, and only the first is a result.
describe("WhatWasChecked, through ReviewPanel", () => {
  const counts = () => screen.getByTestId("what-was-checked-counts");
  const ranLines = () =>
    screen
      .queryAllByTestId("what-was-checked-item")
      .filter((el) => el.getAttribute("data-kind") === "ran")
      .map((el) => el.textContent ?? "");
  const notRunLines = () =>
    screen
      .queryAllByTestId("what-was-checked-item")
      .filter((el) => el.getAttribute("data-kind") === "not-run")
      .map((el) => el.textContent ?? "");

  it("review ran with findings: the pass is claimed and the counts are the findings", () => {
    render(<ReviewPanel findings={[WIRE_FINDING]} />);
    expect(counts().getAttribute("data-review-ran")).toBe("true");
    expect(counts().textContent).toContain("1 finding(s)");
    expect(ranLines().some((l) => /adversarial model pass/.test(l))).toBe(true);
  });

  it("review ran with zero findings: the pass is claimed, and it raised nothing", () => {
    render(<ReviewPanel findings={[]} />);
    expect(counts().getAttribute("data-review-ran")).toBe("true");
    expect(counts().textContent).toContain("This review raised nothing");
    expect(ranLines().some((l) => /adversarial model pass/.test(l))).toBe(true);
    // A review that ran is not the response that carried none.
    expect(screen.queryByTestId("review-no-review")).toBeNull();
  });

  it("no review in the response: absent reads as absent, never as a clean pass", () => {
    render(<ReviewPanel />);
    // The defect: an undefined `findings` used to tick the adversarial pass
    // and report "This review raised nothing" beside "carried no review at all".
    expect(counts().getAttribute("data-review-ran")).toBe("false");
    expect(counts().textContent).not.toMatch(/This review raised nothing/);
    expect(counts().textContent).toMatch(/absence, not a clean result/);
    expect(ranLines().some((l) => /adversarial model pass/.test(l))).toBe(false);
    expect(notRunLines().some((l) => /no review block/.test(l))).toBe(true);
  });

  it("an explicitly skipped review still says it was turned off, not merely absent", () => {
    render(<ReviewPanel reviewRequested={false} />);
    expect(counts().getAttribute("data-review-ran")).toBe("false");
    expect(ranLines().some((l) => /adversarial model pass/.test(l))).toBe(false);
    expect(notRunLines().some((l) => /nothing checked it/.test(l))).toBe(true);
  });
});

describe("OrderPanel routing report", () => {
  it("names unrouted nets and their reasons with no order block in the response", () => {
    // The app never asks for an `order`, so gating this on one hid it always.
    render(
      <ArtifactPanel
        result={
          {
            kicad_pcb: "(kicad_pcb)",
            routing: { unrouted: { VBUS: "no path on 2 layers" } },
          } as unknown as RunResult
        }
      />
    );
    expect(screen.getByTestId("order-panel").getAttribute("data-state")).toBe("absent");
    const routing = screen.getByTestId("order-routing");
    expect(routing.getAttribute("data-present")).toBe("yes");
    const net = screen.getByTestId("unrouted-net");
    expect(net.getAttribute("data-ref")).toBe("VBUS");
    expect(net.textContent).toContain("VBUS");
    expect(net.textContent).toContain("no path on 2 layers");
  });

  it("distinguishes a routing report with nothing open from no report at all", () => {
    render(<OrderPanel unroutedNets={{}} />);
    const present = screen.getByTestId("order-routing");
    expect(present.getAttribute("data-present")).toBe("yes");
    expect(present.textContent).toContain("no unfinished nets");

    cleanup();
    render(<OrderPanel />);
    const absent = screen.getByTestId("order-routing");
    expect(absent.getAttribute("data-present")).toBe("no");
    expect(absent.textContent).toContain("carried no");
    expect(absent.textContent).not.toContain("no unfinished nets");
  });
});

describe("FindingCard origins", () => {
  it("a proven finding shows its measurement; marked-proven-without-one is called out", () => {
    render(
      <FindingCard
        finding={{
          severity: "error",
          title: "Courtyards overlap",
          origin: "proven",
          evidence: "overlap 0.3 mm at (12.1, 4.0)",
        }}
        findingId={findingKey({ severity: "error" }, 0)}
      />
    );
    expect(screen.getByTestId("finding-card-evidence").textContent).toContain(
      "overlap 0.3 mm"
    );
    expect(screen.getByText("proven by measurement")).toBeTruthy();

    cleanup();
    render(
      <FindingCard
        finding={{ severity: "error", title: "Claims proof", origin: "proven" }}
        findingId="f0"
      />
    );
    expect(screen.getByTestId("finding-card-evidence").textContent).toContain(
      "marked proven but carried no measurement"
    );
  });

  it("suggested and absent origins both render as not-proven, visually distinct from proven", () => {
    render(
      <FindingCard
        finding={{ severity: "note", title: "Model thought of this", origin: "suggested" }}
        findingId="f1"
      />
    );
    expect(screen.getByText("suggested by a model")).toBeTruthy();
    expect(screen.queryByText("proven by measurement")).toBeNull();

    cleanup();
    render(
      <FindingCard finding={{ severity: "note", title: "No origin" }} findingId="f2" />
    );
    expect(screen.getByText("unattributed")).toBeTruthy();
    expect(screen.queryByText("proven by measurement")).toBeNull();
  });

  it("reads the audit CLI's message/fix vocabulary too", () => {
    render(
      <FindingCard
        finding={
          {
            severity: "warning",
            message: "Silkscreen over pad",
            fix: "Move the label",
          } as Finding
        }
        findingId="f3"
      />
    );
    expect(screen.getByTestId("finding-card-head").textContent).toContain(
      "Silkscreen over pad"
    );
    expect(screen.getByTestId("finding-card-fix").textContent).toContain(
      "Move the label"
    );
  });
});

describe("RunSummary", () => {
  it("renders 'not reported' for absent fields and never a confident 0", () => {
    // A minimal-but-real response: nothing measured, nothing timed.
    render(<RunSummary result={{} as RunResult} />);
    const metrics = screen.getAllByTestId("run-summary-metric");
    expect(metrics.length).toBeGreaterThanOrEqual(6);
    for (const metric of metrics) {
      expect(metric.getAttribute("data-present")).toBe("no");
      expect(metric.textContent).toContain("not reported");
      // The load-bearing half: an omitted metric must never read as zero.
      expect(metric.textContent).not.toMatch(/\b0(\.\d+)?\s*(mm|s)?\b/);
    }
    expect(screen.getByTestId("run-summary-served-by").textContent).toContain(
      "provider not reported"
    );
    expect(screen.getByTestId("run-summary-cache").getAttribute("data-present")).toBe(
      "no"
    );
  });

  it("an explicit null wirelength is 'not reported', not 0.00 mm", () => {
    render(<RunSummary result={{ wirelength_mm: null } as RunResult} />);
    const wirelength = screen
      .getAllByTestId("run-summary-metric")
      .find((el) => el.getAttribute("data-metric") === "wirelength")!;
    expect(wirelength.getAttribute("data-present")).toBe("no");
    expect(wirelength.textContent).not.toContain("0.00");
  });

  it("a genuine zero is shown as zero — measured nothing and measured zero differ", () => {
    render(<RunSummary result={{ nets: [], duration_s: 0 } as RunResult} />);
    const byKey = Object.fromEntries(
      screen
        .getAllByTestId("run-summary-metric")
        .map((el) => [el.getAttribute("data-metric"), el])
    );
    expect(byKey["nets"].getAttribute("data-present")).toBe("yes");
    expect(byKey["nets"].textContent).toContain("0");
    expect(byKey["duration"].getAttribute("data-present")).toBe("yes");
    expect(byKey["duration"].textContent).toContain("0.00 s");
  });

  it("with no result at all it measures nothing", () => {
    render(<RunSummary result={null} />);
    expect(screen.getByTestId("run-summary").getAttribute("data-state")).toBe(
      "empty"
    );
    expect(screen.queryAllByTestId("run-summary-metric")).toHaveLength(0);
  });

  it("reports the cache groups it got, and absence for the ones it did not", () => {
    render(
      <RunSummary
        result={{ cache: { hit: ["AMS1117"], unusable: [] } } as RunResult}
      />
    );
    expect(screen.getByTestId("cache-hit").getAttribute("data-count")).toBe("1");
    expect(screen.getByTestId("cache-read").getAttribute("data-present")).toBe("no");
    expect(screen.getByTestId("cache-unusable").getAttribute("data-count")).toBe("0");
  });
});
