import "@testing-library/jest-dom/vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RealtimeWorkbench } from "./RealtimeWorkbench";

class TestWebSocket {
  static instances: TestWebSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onclose: (() => void) | null = null;
  close = vi.fn();

  constructor(readonly url: string) {
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

function ok(body: unknown) {
  return { ok: true, status: 200, json: async () => body };
}

describe("RealtimeWorkbench", () => {
  beforeEach(() => {
    TestWebSocket.instances = [];
    localStorage.setItem("odp_token", "test-token");
    vi.stubGlobal("WebSocket", TestWebSocket);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("renders reconciled alerts and opens a ticket-only socket", async () => {
    let capturedHeaders: Headers | undefined;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "/api/v1/cases") return ok([]);
      if (url === "/api/v1/inspection-events") {
        capturedHeaders = init?.headers as Headers | undefined;
        return ok({ items: [{ cursor: "12", alert }], next_cursor: "12" });
      }
      if (url === "/api/v1/auth/websocket-ticket") return ok({ ticket: "ticket-1", expires_in: 60 });
      throw new Error(`Unexpected request: ${url}`);
    }));
    render(<RealtimeWorkbench since="2026-08-19T08:00:00Z" />);

    expect(await screen.findByText("疑似表面划痕")).toBeInTheDocument();
    expect(screen.getByText("96.4%")).toBeInTheDocument();
    await waitFor(() => expect(TestWebSocket.instances).toHaveLength(1));
    expect(TestWebSocket.instances[0].url).toContain("ticket=ticket-1");
    expect(TestWebSocket.instances[0].url).toContain("cursor=12");
    expect(TestWebSocket.instances[0].url).not.toContain("test-token");
    expect(TestWebSocket.instances[0].url).not.toContain("token=");
    expect(capturedHeaders?.get("Authorization")).toBe("Bearer test-token");

    fireEvent.click(screen.getByRole("button", { name: "确认处置" }));
    expect(screen.getByText("已确认处置")).toBeInTheDocument();
  });

  it("does not open the alert feed when no token is stored", async () => {
    localStorage.removeItem("odp_token");
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/v1/cases") return ok([]);
      throw new Error(`Unexpected request: ${String(input)}`);
    }));
    render(<RealtimeWorkbench since="2026-08-19T08:00:00Z" />);

    expect(TestWebSocket.instances).toHaveLength(0);
    expect(vi.mocked(fetch).mock.calls.filter(([input]) => String(input).includes("inspection-events"))).toHaveLength(0);
  });

  it("renders a future envelope once on the already-open socket", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/cases") return ok([]);
      if (url === "/api/v1/inspection-events") return ok({ items: [], next_cursor: null });
      if (url === "/api/v1/auth/websocket-ticket") return ok({ ticket: "ticket-2", expires_in: 60 });
      throw new Error(`Unexpected request: ${url}`);
    }));
    render(<RealtimeWorkbench since="2026-08-19T08:00:00Z" />);
    await waitFor(() => expect(TestWebSocket.instances).toHaveLength(1));

    await act(async () => {
      TestWebSocket.instances[0].onmessage?.({ data: JSON.stringify({ cursor: "13", alert }) } as MessageEvent<string>);
      TestWebSocket.instances[0].onmessage?.({ data: JSON.stringify({ cursor: "14", alert }) } as MessageEvent<string>);
    });

    expect(await screen.findByText("疑似表面划痕")).toBeInTheDocument();
    expect(screen.getAllByText("疑似表面划痕")).toHaveLength(1);
    const cursorStorageKey = Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index))
      .find((key) => key?.startsWith("odp_alert_cursor:"));
    expect(cursorStorageKey).toBeDefined();
    expect(localStorage.getItem(cursorStorageKey!)).toBe("13");
  });
});
