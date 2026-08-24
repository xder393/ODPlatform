import "@testing-library/jest-dom/vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";

const ACTOR_A = "11111111-1111-4111-8111-111111111111";
const cursorKey = `odp_alert_cursor:${ACTOR_A}`;

function tokenFor(actorId: string): string {
  return `header.${btoa(JSON.stringify({ sub: actorId }))}.signature`;
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

describe("App 鉴权门", () => {
  beforeEach(() => {
    TestWebSocket.instances = [];
    vi.stubGlobal("WebSocket", TestWebSocket);
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/auth/login") return { ok: true, json: async () => ({ access_token: "jwt-abc" }) };
      return { ok: true, json: async () => [] };
    }));
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("shows the login form when no token is stored", () => {
    render(<App since="2026-08-19T08:00:00Z" />);

    expect(screen.getByRole("button", { name: "登录" })).toBeInTheDocument();
    expect(screen.queryByText("实时质检工作台")).not.toBeInTheDocument();
    expect(TestWebSocket.instances).toHaveLength(0);
  });

  it("shows the workbench when a token is stored", async () => {
    localStorage.setItem("odp_token", "test-token");
    render(<App since="2026-08-19T08:00:00Z" />);

    expect(
      await screen.findByRole("heading", { name: "实时质检工作台" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "退出登录" })).toBeInTheDocument();
  });

  it("logs in through the form and logs out to clear the token", async () => {
    render(<App since="2026-08-19T08:00:00Z" />);

    fireEvent.change(screen.getByLabelText("邮箱"), {
      target: { value: "inspector@example.test" },
    });
    fireEvent.change(screen.getByLabelText("密码"), {
      target: { value: "odp-inspector-dev" },
    });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    expect(
      await screen.findByRole("heading", { name: "实时质检工作台" }),
    ).toBeInTheDocument();
    expect(localStorage.getItem("odp_token")).toBe("jwt-abc");

    fireEvent.click(screen.getByRole("button", { name: "退出登录" }));

    expect(screen.getByRole("button", { name: "登录" })).toBeInTheDocument();
    expect(localStorage.getItem("odp_token")).toBeNull();
  });

  it("logout through App aborts the active request, closes the socket, clears only this actor cursor, and cancels retries", async () => {
    vi.useFakeTimers();
    localStorage.setItem("odp_token", tokenFor(ACTOR_A));
    localStorage.setItem(cursorKey, "44");
    let activeSignal: AbortSignal | undefined;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      activeSignal = init?.signal ?? undefined;
      if (String(input) === "/api/v1/cases") return { ok: true, json: async () => [] };
      if (String(input).startsWith("/api/v1/inspection-events")) return { ok: true, json: async () => ({ items: [], next_cursor: "44" }) };
      if (String(input) === "/api/v1/auth/websocket-ticket") return { ok: true, json: async () => ({ ticket: "logout-ticket", expires_in: 60 }) };
      throw new Error(`Unexpected request: ${String(input)}`);
    }));
    render(<App since="2026-08-19T08:00:00Z" />);
    await vi.advanceTimersByTimeAsync(0);
    expect(TestWebSocket.instances).toHaveLength(1);
    act(() => TestWebSocket.instances[0].onclose?.());

    fireEvent.click(screen.getByRole("button", { name: "退出登录" }));
    await vi.advanceTimersByTimeAsync(30_000);

    expect(activeSignal?.aborted).toBe(true);
    expect(TestWebSocket.instances[0].close).toHaveBeenCalled();
    expect(TestWebSocket.instances).toHaveLength(1);
    expect(localStorage.getItem("odp_token")).toBeNull();
    expect(localStorage.getItem(cursorKey)).toBeNull();
  });
});
