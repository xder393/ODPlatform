import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RealtimeWorkbench } from "./RealtimeWorkbench";

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

const alert = {
  event_id: "8a62f22a-0c04-45c5-bb5d-4f1f4ed53a29",
  organization_id: "7e73b0c4-1b45-48b4-874e-522c2d879592",
  camera_id: "f32250a4-a4b3-49c9-a7c3-a1bf08414731",
  occurred_at: "2026-08-19T08:30:00Z",
  defect_class: "scratch",
  confidence: 0.964,
};

describe("RealtimeWorkbench", () => {
  beforeEach(() => {
    TestWebSocket.instances = [];
    vi.stubGlobal("WebSocket", TestWebSocket);
    // 工作台挂载后还会通过 /api/v1/cases 拉取工单列表，这里按路径区分两类响应。
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).startsWith("/api/v1/cases")) {
        return { ok: true, json: async () => [] };
      }
      return { ok: true, json: async () => [alert] };
    }));
  });

  afterEach(() => vi.useRealTimers());

  it("renders the alert and reconciles unseen events when the socket reconnects", async () => {
    render(<RealtimeWorkbench since="2026-08-19T08:00:00Z" />);
    TestWebSocket.instances[0].onopen?.();

    expect(await screen.findByText("疑似表面划痕")).toBeInTheDocument();
    expect(screen.getByText("96.4%")).toBeInTheDocument();
    await waitFor(() =>
      expect(fetch).toHaveBeenCalledWith(
        "/api/v1/inspection-events?updated_after=2026-08-19T08%3A00%3A00Z",
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "确认处置" }));
    expect(screen.getByText("已确认处置")).toBeInTheDocument();
  });

  it("appends an alert queued before reconciliation completes", async () => {
    let resolveReconciliation: (value: { ok: boolean; json: () => Promise<Array<typeof alert>> }) => void;
    const reconciliation = new Promise<{ ok: boolean; json: () => Promise<Array<typeof alert>> }>((resolve) => {
      resolveReconciliation = resolve;
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(reconciliation));
    render(<RealtimeWorkbench since="2026-08-19T08:00:00Z" />);

    TestWebSocket.instances[0].onopen?.();
    TestWebSocket.instances[0].onmessage?.({ data: JSON.stringify(alert) } as MessageEvent<string>);
    resolveReconciliation!({ ok: true, json: async () => [] });

    expect(await screen.findByText("疑似表面划痕")).toBeInTheDocument();
  });

  it("reconnects and reconciles after a failed reconciliation closes the socket", async () => {
    vi.useFakeTimers();
    // 挂载后工作台会额外请求一次 /api/v1/cases，按路径路由并单独统计补偿请求次数。
    let reconciliationAttempts = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).startsWith("/api/v1/cases")) {
        return { ok: true, json: async () => [] };
      }
      reconciliationAttempts += 1;
      return reconciliationAttempts === 1
        ? { ok: false }
        : { ok: true, json: async () => [alert] };
    }));
    render(<RealtimeWorkbench since="2026-08-19T08:00:00Z" />);

    TestWebSocket.instances[0].onopen?.();
    await Promise.resolve();
    expect(TestWebSocket.instances[0].close).toHaveBeenCalled();

    TestWebSocket.instances[0].onclose?.();
    await vi.advanceTimersByTimeAsync(1000);
    expect(TestWebSocket.instances).toHaveLength(2);

    TestWebSocket.instances[1].onopen?.();
    await Promise.resolve();
    expect(reconciliationAttempts).toBe(2);
  });
});
