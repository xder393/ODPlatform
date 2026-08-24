import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { AdviceResponse, InspectionEventSummary } from "../../api/types";
import { CaseTimeline, type CaseTransitionEntry } from "./CaseTimeline";

const event: InspectionEventSummary = {
  defect_class: "scratch",
  confidence: 0.964,
  model_release: "mock-yolo-1.0",
  preprocessing_parameters: { resize: "640x640" },
  threshold: 0.8,
  input_frame_sha256: "ab".repeat(32),
};

const advice: AdviceResponse = {
  answer: "建议立即停机复核。",
  confidence: "HIGH",
  citations: [],
};

const firstTransition: CaseTransitionEntry = {
  at: "2026-08-20T10:05:00Z",
  fromStatus: "PENDING_CONFIRMATION",
  toStatus: "IN_REVIEW",
  actorLabel: "质检员张三",
};

describe("CaseTimeline", () => {
  it("renders detection events with defect class, confidence and model release", () => {
    render(
      <CaseTimeline events={[event]} advice={null} transitions={[]} status="PENDING_CONFIRMATION" />,
    );
    expect(screen.getByText("疑似表面划痕")).toBeInTheDocument();
    expect(screen.getByText("96.4%")).toBeInTheDocument();
    expect(screen.getByText("mock-yolo-1.0")).toBeInTheDocument();
  });

  it("renders the advice entry with a confidence badge", () => {
    render(
      <CaseTimeline
        events={[]}
        advice={advice}
        adviceAt="2026-08-20T10:04:00Z"
        transitions={[]}
        status="PENDING_CONFIRMATION"
      />,
    );
    expect(screen.getByText("AI 建议")).toBeInTheDocument();
    expect(screen.getByText("可信度高")).toHaveClass("confidence-high");
  });

  it("renders transition entries in time order with status labels", () => {
    const laterTransition: CaseTransitionEntry = {
      at: "2026-08-20T10:06:00Z",
      fromStatus: "IN_REVIEW",
      toStatus: "RESOLVED",
      actorLabel: "质检员李四",
    };
    render(
      <CaseTimeline
        events={[]}
        advice={null}
        transitions={[laterTransition, firstTransition]}
        status="RESOLVED"
      />,
    );
    const titles = screen.getAllByText(/人工状态变更/).map((node) => node.textContent);
    expect(titles).toEqual([
      "人工状态变更（质检员张三）",
      "人工状态变更（质检员李四）",
    ]);
    expect(screen.getByText("待确认 → 复核中")).toBeInTheDocument();
    expect(screen.getByText("复核中 → 已处置")).toBeInTheDocument();
  });

  it("renders the current status at the end", () => {
    render(
      <CaseTimeline events={[]} advice={null} transitions={[]} status="FALSE_POSITIVE" />,
    );
    expect(screen.getByText("当前状态")).toBeInTheDocument();
    expect(screen.getByText("误报")).toBeInTheDocument();
  });
});
