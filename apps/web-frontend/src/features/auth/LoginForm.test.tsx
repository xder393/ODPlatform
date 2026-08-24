import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LoginForm } from "./LoginForm";

const okJson = (body: unknown) => ({ ok: true, status: 200, json: async () => body });
const errJson = (status: number, detail: string) => ({
  ok: false,
  status,
  json: async () => ({ detail }),
});

describe("LoginForm", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("submits email and password, stores the token and notifies the caller", async () => {
    const onAuthenticated = vi.fn();
    vi.stubGlobal("fetch", vi.fn(async () => okJson({ access_token: "jwt-token-123" })));
    render(<LoginForm onAuthenticated={onAuthenticated} />);

    fireEvent.change(screen.getByLabelText("邮箱"), {
      target: { value: "inspector@example.test" },
    });
    fireEvent.change(screen.getByLabelText("密码"), {
      target: { value: "odp-inspector-dev" },
    });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    await waitFor(() => expect(onAuthenticated).toHaveBeenCalledWith("jwt-token-123"));
    expect(localStorage.getItem("odp_token")).toBe("jwt-token-123");
    const [url, init] = vi.mocked(fetch).mock.calls[0];
    expect(String(url)).toBe("/api/v1/auth/login");
    expect((init as RequestInit).method).toBe("POST");
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      email: "inspector@example.test",
      password: "odp-inspector-dev",
    });
  });

  it("shows the credential error copy on 401 and stores nothing", async () => {
    const onAuthenticated = vi.fn();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => errJson(401, "Invalid credentials")),
    );
    render(<LoginForm onAuthenticated={onAuthenticated} />);

    fireEvent.change(screen.getByLabelText("邮箱"), {
      target: { value: "inspector@example.test" },
    });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "wrong-password" } });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    expect(await screen.findByText("邮箱或密码错误")).toBeInTheDocument();
    expect(onAuthenticated).not.toHaveBeenCalled();
    expect(localStorage.getItem("odp_token")).toBeNull();
  });
});
