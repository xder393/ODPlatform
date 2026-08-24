import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AdviceResponse, CaseSummary } from "../../api/types";
import { RealtimeWorkbench } from "./RealtimeWorkbench";

/** 建议面板与时间线都会渲染可信度徽标，面板断言必须限定在面板区域内。 */
async function panelBadge(label: string): Promise<HTMLElement> {
  const panel = await screen.findByRole("region", { name: "AI 处置建议" });
  return within(panel).getByText(label);
}

class TestWebSocket {
  static instances: TestWebSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onclose: (() => void) | null = null;
  close = vi.fn();

  constructor() {
    TestWebSocket.instances.push(this);
  }
}

const caseA: CaseSummary = {
  case_id: "a1b2c3d4-1111-4111-8111-111111111111",
  status: "PENDING_CONFIRMATION",
  updated_at: "2026-08-20T10:00:00Z",
  inspection_events: [
    {
      defect_class: "scratch",
      confidence: 0.964,
      model_release: "mock-yolo-1.0",
      preprocessing_parameters: { resize: "640x640" },
      threshold: 0.8,
      input_frame_sha256: "ab".repeat(32),
    },
  ],
};

const highAdvice: AdviceResponse = {
  answer: "建议立即停机复核。",
  confidence: "HIGH",
  citations: [
    {
      chunk_id: "39f7d0a1-1f2e-4b3c-9d4e-5a6b7c8d9e0f",
      document_id: "6f2a5c8e-2b1d-4a3f-9c5d-0e1f2a3b4c5d",
      source_name: "划痕检验规程",
      document_version: 1,
      page_number: 3,
      paragraph_number: 2,
      snippet: "置信度超过 0.9 时应立即停机复核。",
    },
  ],
};

const okJson = (body: unknown) => ({ ok: true, status: 200, json: async () => body });
const errJson = (status: number, detail: string) => ({
  ok: false,
  status,
  json: async () => ({ detail }),
});

describe("RealtimeWorkbench 工单处理", () => {
  beforeEach(() => {
    TestWebSocket.instances = [];
    vi.stubGlobal("WebSocket", TestWebSocket);
    localStorage.setItem("odp_token", "test-token");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("loads the case list on mount", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/v1/cases") return okJson([caseA]);
      throw new Error(`未预期的请求：${String(input)}`);
    }));
    render(<RealtimeWorkbench since="2026-08-19T08:00:00Z" />);

    expect(await screen.findByRole("button", { name: /待确认/ })).toBeInTheDocument();
    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith("/api/v1/cases", expect.anything()),
    );
  });

  it("selecting a case requests advice and shows the timeline", async () => {
    let capturedHeaders: Headers | undefined;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      capturedHeaders = init?.headers as Headers | undefined;
      const url = String(input);
      if (url === "/api/v1/cases") return okJson([caseA]);
      if (url.endsWith("/advice")) return okJson(highAdvice);
      throw new Error(`未预期的请求：${url}`);
    }));
    render(<RealtimeWorkbench since="2026-08-19T08:00:00Z" />);

    fireEvent.click(await screen.findByRole("button", { name: /待确认/ }));

    expect(await panelBadge("可信度高")).toBeInTheDocument();
    expect(screen.getByText("建议立即停机复核。")).toBeInTheDocument();
    expect(screen.getByText("疑似表面划痕")).toBeInTheDocument();
    expect(screen.getByText("96.4%")).toBeInTheDocument();
    expect(screen.getByText("mock-yolo-1.0")).toBeInTheDocument();
    await waitFor(() =>
      expect(capturedHeaders?.get("Authorization")).toBe("Bearer test-token"),
    );
  });

  it("shows the conflict copy when a transition is rejected with 409", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/cases") return okJson([caseA]);
      if (url.endsWith("/advice")) return okJson(highAdvice);
      if (url.endsWith("/transitions")) return errJson(409, "Invalid case transition");
      throw new Error(`未预期的请求：${url}`);
    }));
    render(<RealtimeWorkbench since="2026-08-19T08:00:00Z" />);

    fireEvent.click(await screen.findByRole("button", { name: /待确认/ }));
    await panelBadge("可信度高");
    fireEvent.click(screen.getByRole("button", { name: "完成处置" }));

    expect(await screen.findByText("状态流转不合法，请按正确流程操作")).toBeInTheDocument();
  });

  it("requires reauthentication before pausing and retries after it succeeds", async () => {
    let pauseCalls = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/cases") return okJson([caseA]);
      if (url.endsWith("/advice")) return okJson(highAdvice);
      if (url.endsWith("/pause")) {
        pauseCalls += 1;
        return pauseCalls === 1
          ? errJson(403, "Recent reauthentication is required.")
          : okJson({ status: "SIMULATED", message: "No production line was paused." });
      }
      if (url.endsWith("/reauthenticate")) return okJson({ reauthenticated: true });
      throw new Error(`未预期的请求：${url}`);
    }));
    render(<RealtimeWorkbench since="2026-08-19T08:00:00Z" />);

    fireEvent.click(await screen.findByRole("button", { name: /待确认/ }));
    await panelBadge("可信度高");
    fireEvent.click(screen.getByRole("button", { name: "模拟暂停产线" }));

    expect(
      await screen.findByText("该操作需要最近 5 分钟内的再次认证，请输入密码"),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/请输入密码/), { target: { value: "secret" } });
    fireEvent.click(screen.getByRole("button", { name: "确认认证" }));

    expect(await screen.findByText("No production line was paused.")).toBeInTheDocument();
    expect(pauseCalls).toBe(2);
    const reauthCall = vi.mocked(fetch).mock.calls.find(([input]) =>
      String(input).endsWith("/reauthenticate"),
    );
    expect(reauthCall).toBeDefined();
    expect(JSON.parse((reauthCall![1] as RequestInit).body as string)).toEqual({
      password: "secret",
    });
  });

  it("appends a transition entry to the timeline after a successful transition", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/cases") return okJson([caseA]);
      if (url.endsWith("/advice")) return okJson(highAdvice);
      if (url.endsWith("/transitions")) {
        return okJson({ ...caseA, status: "IN_REVIEW", updated_at: "2026-08-20T10:05:00Z" });
      }
      throw new Error(`未预期的请求：${url}`);
    }));
    render(<RealtimeWorkbench since="2026-08-19T08:00:00Z" />);

    fireEvent.click(await screen.findByRole("button", { name: /待确认/ }));
    await panelBadge("可信度高");
    fireEvent.click(screen.getByRole("button", { name: "确认复检" }));

    expect(await screen.findByText("待确认 → 复核中")).toBeInTheDocument();
    expect(screen.getByText(/确认复检成功/)).toBeInTheDocument();
    await waitFor(() => {
      const listCalls = vi.mocked(fetch).mock.calls.filter(
        ([input]) => String(input) === "/api/v1/cases",
      );
      expect(listCalls).toHaveLength(2);
    });
  });

  it("disables the pause button when advice confidence is LOW", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/cases") return okJson([caseA]);
      if (url.endsWith("/advice")) {
        return okJson({ ...highAdvice, confidence: "LOW" });
      }
      throw new Error(`未预期的请求：${url}`);
    }));
    render(<RealtimeWorkbench since="2026-08-19T08:00:00Z" />);

    fireEvent.click(await screen.findByRole("button", { name: /待确认/ }));
    await panelBadge("可信度低");

    expect(screen.getByRole("button", { name: "模拟暂停产线" })).toBeDisabled();
  });
});
