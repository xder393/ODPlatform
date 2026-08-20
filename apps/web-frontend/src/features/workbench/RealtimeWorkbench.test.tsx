import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RealtimeWorkbench } from "./RealtimeWorkbench";

class TestWebSocket {
  static instances: TestWebSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;

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
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => [alert] }));
  });

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
});
