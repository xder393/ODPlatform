import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { AdviceConfidence, AdviceResponse } from "../../api/types";
import { AdvicePanel } from "./AdvicePanel";

const citation = {
  chunk_id: "39f7d0a1-1f2e-4b3c-9d4e-5a6b7c8d9e0f",
  document_id: "6f2a5c8e-2b1d-4a3f-9c5d-0e1f2a3b4c5d",
  source_name: "划痕检验规程",
  document_version: 3,
  page_number: 12,
  paragraph_number: 4,
  snippet: "当置信度超过阈值时应立即停机复核。",
};

function makeAdvice(confidence: AdviceConfidence, answer = "建议立即停机复核。"): AdviceResponse {
  return { answer, confidence, citations: [citation] };
}

describe("AdvicePanel", () => {
  it("renders the HIGH badge with accessible text and semantic class", () => {
    render(<AdvicePanel advice={makeAdvice("HIGH")} />);
    expect(screen.getByText("可信度高")).toHaveClass("confidence-high");
    expect(screen.getByRole("button", { name: "模拟暂停产线" })).toBeEnabled();
  });

  it("renders the MEDIUM badge with accessible text and semantic class", () => {
    render(<AdvicePanel advice={makeAdvice("MEDIUM")} />);
    expect(screen.getByText("可信度中")).toHaveClass("confidence-medium");
    expect(screen.getByRole("button", { name: "模拟暂停产线" })).toBeEnabled();
  });

  it("renders the LOW badge and disables the pause button", () => {
    render(<AdvicePanel advice={makeAdvice("LOW", "仅存在跨产品证据，请人工复核后再处置。")} />);
    expect(screen.getByText("可信度低")).toHaveClass("confidence-low");
    expect(screen.getByRole("button", { name: "模拟暂停产线" })).toBeDisabled();
  });

  it("renders the UNAVAILABLE badge and disables the pause button", () => {
    render(<AdvicePanel advice={makeAdvice("UNAVAILABLE", "AI advice unavailable. Please use human review.")} />);
    expect(screen.getByText("AI 建议不可用，请人工判断")).toHaveClass("confidence-unavailable");
    expect(screen.getByRole("button", { name: "模拟暂停产线" })).toBeDisabled();
  });

  it("renders each citation with source name, document version and page/paragraph", () => {
    render(<AdvicePanel advice={makeAdvice("HIGH")} />);
    expect(screen.getByText("划痕检验规程 · 文档版本 3 · 第 12 页第 4 段")).toBeInTheDocument();
    expect(screen.getByText("当置信度超过阈值时应立即停机复核。")).toBeInTheDocument();
    expect(screen.getByText("建议立即停机复核。")).toBeInTheDocument();
  });

  it("invokes the caller-provided pause handler when the pause button is clicked", () => {
    const onRequestPause = vi.fn();
    render(<AdvicePanel advice={makeAdvice("HIGH")} onRequestPause={onRequestPause} />);
    fireEvent.click(screen.getByRole("button", { name: "模拟暂停产线" }));
    expect(onRequestPause).toHaveBeenCalledTimes(1);
  });

  it("honours the caller-provided pauseDisabled flag", () => {
    render(<AdvicePanel advice={makeAdvice("HIGH")} pauseDisabled />);
    expect(screen.getByRole("button", { name: "模拟暂停产线" })).toBeDisabled();
  });
});
