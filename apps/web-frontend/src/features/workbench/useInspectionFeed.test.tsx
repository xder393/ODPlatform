import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useInspectionFeed } from "./useInspectionFeed";

const ACTOR_A = "11111111-1111-4111-8111-111111111111";
const ACTOR_B = "22222222-2222-4222-8222-222222222222";

const cursorKey = (actorId: string) => `odp_alert_cursor:${actorId}`;

function tokenFor(actorId: string): string {
  return `header.${btoa(JSON.stringify({ sub: actorId }))}.signature`;
}

function envelope(cursor: string, eventId = `event-${cursor}`) {
  return {
    cursor,
    alert: {
      event_id: eventId,
      organization_id: "10000000-0000-4000-8000-000000000001",
      camera_id: "30000000-0000-4000-8000-000000000001",
      occurred_at: "2026-08-24T10:00:00Z",
      defect_class: "scratch",
      confidence: 0.99,
    },
  };
}

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  readonly close = vi.fn();
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onclose: (() => void) | null = null;

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }
}

describe("useInspectionFeed", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    localStorage.clear();
    vi.stubGlobal("WebSocket", FakeWebSocket);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("uses a fresh one-time ticket and commits a reconciled cursor only after accepting its envelope", async () => {
    const token = tokenFor(ACTOR_A);
    localStorage.setItem("odp_token", token);
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/auth/websocket-ticket") {
        return { ok: true, json: async () => ({ ticket: "ticket-1", expires_in: 60 }) };
      }
      if (url === "/api/v1/inspection-events") {
        return { ok: true, json: async () => ({ items: [envelope("12")], next_cursor: "12" }) };
      }
      throw new Error(`Unexpected request: ${url}`);
    }));

    const { result } = renderHook(() => useInspectionFeed("2026-01-01T00:00:00Z"));

    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    expect(FakeWebSocket.instances[0].url).toContain("ticket=ticket-1");
    expect(FakeWebSocket.instances[0].url).not.toContain(token);
    expect(FakeWebSocket.instances[0].url).not.toContain("token=");
    expect(result.current.alerts.map((alert) => alert.event_id)).toEqual(["event-12"]);
    expect(localStorage.getItem(cursorKey(ACTOR_A))).toBe("12");
  });

  it("deduplicates live events and never regresses the stored cursor", async () => {
    localStorage.setItem("odp_token", tokenFor(ACTOR_A));
    localStorage.setItem(cursorKey(ACTOR_A), "44");
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/v1/auth/websocket-ticket") {
        return { ok: true, json: async () => ({ ticket: "ticket-2", expires_in: 60 }) };
      }
      return { ok: true, json: async () => ({ items: [], next_cursor: "44" }) };
    }));

    const { result } = renderHook(() => useInspectionFeed("2026-01-01T00:00:00Z"));
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    expect(vi.mocked(fetch)).toHaveBeenCalledWith(
      "/api/v1/inspection-events?after_cursor=44",
      expect.anything(),
    );

    act(() => {
      FakeWebSocket.instances[0].onmessage?.({ data: JSON.stringify(envelope("45", "duplicate")) } as MessageEvent<string>);
      FakeWebSocket.instances[0].onmessage?.({ data: JSON.stringify(envelope("43", "duplicate")) } as MessageEvent<string>);
    });

    await waitFor(() => expect(result.current.alerts.map((alert) => alert.event_id)).toEqual(["duplicate"]));
    expect(localStorage.getItem(cursorKey(ACTOR_A))).toBe("45");
  });

  it("ignores another actor's cursor and cancels the pending socket and backoff timer on unmount", async () => {
    vi.useFakeTimers();
    localStorage.setItem("odp_token", tokenFor(ACTOR_B));
    localStorage.setItem(cursorKey(ACTOR_A), "44");
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/v1/auth/websocket-ticket") {
        return { ok: true, json: async () => ({ ticket: "ticket-3", expires_in: 60 }) };
      }
      return { ok: true, json: async () => ({ items: [], next_cursor: null }) };
    }));

    const { unmount } = renderHook(() => useInspectionFeed("2026-01-01T00:00:00Z"));
    await vi.runAllTicks();
    expect(vi.mocked(fetch)).toHaveBeenCalledWith("/api/v1/inspection-events", expect.anything());
    await vi.runAllTimersAsync();
    expect(FakeWebSocket.instances).toHaveLength(1);
    FakeWebSocket.instances[0].onclose?.();
    unmount();
    await vi.advanceTimersByTimeAsync(30_000);

    expect(FakeWebSocket.instances[0].close).toHaveBeenCalled();
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(localStorage.getItem(cursorKey(ACTOR_A))).toBe("44");
    expect(localStorage.getItem(cursorKey(ACTOR_B))).toBeNull();
  });

  it("aborts an in-flight reconciliation request when the authenticated view unmounts", async () => {
    localStorage.setItem("odp_token", tokenFor(ACTOR_A));
    let requestSignal: AbortSignal | undefined;
    vi.stubGlobal("fetch", vi.fn((_: RequestInfo | URL, init?: RequestInit) => {
      requestSignal = init?.signal ?? undefined;
      return new Promise<Response>(() => undefined);
    }));

    const { unmount } = renderHook(() => useInspectionFeed("2026-01-01T00:00:00Z"));
    await waitFor(() => expect(requestSignal).toBeDefined());
    unmount();

    expect(requestSignal?.aborted).toBe(true);
  });

  it("rejects malformed REST and WebSocket envelopes without mutating UI, dedupe state, or cursor", async () => {
    localStorage.setItem("odp_token", tokenFor(ACTOR_A));
    const invalidRest = {
      cursor: "broken-rest",
      alert: { ...envelope("ignored").alert, confidence: Number.NaN },
    };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/v1/inspection-events") {
        return { ok: true, json: async () => ({ items: [invalidRest], next_cursor: "broken-rest" }) };
      }
      return { ok: true, json: async () => ({ ticket: "ticket-validation", expires_in: 60 }) };
    }));

    const { result } = renderHook(() => useInspectionFeed("2026-01-01T00:00:00Z"));
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    expect(result.current.alerts).toEqual([]);
    expect(localStorage.getItem(cursorKey(ACTOR_A))).toBeNull();

    act(() => {
      FakeWebSocket.instances[0].onmessage?.({
        data: JSON.stringify({ cursor: "broken-ws", alert: { ...envelope("ignored").alert, event_id: "", confidence: 1.2 } }),
      } as MessageEvent<string>);
      FakeWebSocket.instances[0].onmessage?.({ data: JSON.stringify(envelope("valid", "same-id")) } as MessageEvent<string>);
    });

    await waitFor(() => expect(result.current.alerts.map((alert) => alert.event_id)).toEqual(["same-id"]));
    expect(localStorage.getItem(cursorKey(ACTOR_A))).toBe("valid");
  });

  it("uses a fresh ticket for every reconnect, bounds backoff, and resets only after stability", async () => {
    vi.useFakeTimers();
    localStorage.setItem("odp_token", tokenFor(ACTOR_A));
    let tickets = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/v1/inspection-events") return { ok: true, json: async () => ({ items: [], next_cursor: null }) };
      tickets += 1;
      return { ok: true, json: async () => ({ ticket: `ticket-${tickets}`, expires_in: 60 }) };
    }));
    renderHook(() => useInspectionFeed("2026-01-01T00:00:00Z"));
    await vi.advanceTimersByTimeAsync(0);
    expect(FakeWebSocket.instances).toHaveLength(1);

    for (const delay of [1_000, 2_000, 4_000, 8_000, 16_000, 30_000, 30_000]) {
      act(() => FakeWebSocket.instances.at(-1)?.onclose?.());
      await vi.advanceTimersByTimeAsync(delay);
      expect(FakeWebSocket.instances).toHaveLength(tickets);
      expect(FakeWebSocket.instances.at(-1)?.url).toContain(`ticket=ticket-${tickets}`);
    }

    act(() => FakeWebSocket.instances.at(-1)?.onopen?.());
    await vi.advanceTimersByTimeAsync(5_000);
    act(() => FakeWebSocket.instances.at(-1)?.onclose?.());
    await vi.advanceTimersByTimeAsync(1_000);
    expect(FakeWebSocket.instances.at(-1)?.url).toContain(`ticket=ticket-${tickets}`);
  });

  it("backs off ticket failures without opening a socket", async () => {
    vi.useFakeTimers();
    localStorage.setItem("odp_token", tokenFor(ACTOR_A));
    let ticketAttempts = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/v1/inspection-events") return { ok: true, json: async () => ({ items: [], next_cursor: null }) };
      ticketAttempts += 1;
      throw new Error("ticket unavailable");
    }));
    renderHook(() => useInspectionFeed("2026-01-01T00:00:00Z"));

    for (const delay of [1_000, 2_000, 4_000, 8_000, 16_000, 30_000, 30_000]) {
      await vi.advanceTimersByTimeAsync(0);
      expect(FakeWebSocket.instances).toHaveLength(0);
      await vi.advanceTimersByTimeAsync(delay);
    }
    expect(ticketAttempts).toBe(8);
    expect(FakeWebSocket.instances).toHaveLength(0);
  });
});
