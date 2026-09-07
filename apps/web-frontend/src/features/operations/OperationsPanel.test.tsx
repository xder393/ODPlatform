import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { OperationsPanel } from "./OperationsPanel";

const line = "20000000-0000-4000-8000-000000000001";
const camera = "30000000-0000-4000-8000-000000000001";
const task = { task_id: "task-1", organization_id: "org-1", camera_id: camera,
  line_id: line, artifact_id: "artifact-1", status: "DEAD_LETTER", dispatch_seq: 1,
  attempt_count: 3, error_code: "INVALID_INPUT", error_detail: "Invalid image",
  created_at: "2026-09-07T12:00:00Z", updated_at: "2026-09-07T12:00:00Z",
  attempts: [{ attempt_id: "attempt-1", attempt_no: 1, fence_token: 1, worker_id: "worker-1",
    started_at: "2026-09-07T12:00:00Z", finished_at: "2026-09-07T12:00:01Z",
    outcome: "FAILED", error_code: "INVALID_INPUT", duration_ms: 1000 }], dispatches: [] };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

function setup(role = "SUPERVISOR") {
  localStorage.setItem("odp_token", "test-token");
  const requests: { url: string; init?: RequestInit }[] = [];
  let session: Record<string, unknown> | undefined;
  let failStart = false;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    requests.push({ url, init });
    if (url === "/api/v1/auth/me") return json({ actor_id: "actor-1", role, organization_id: "org-1", line_ids: [line] });
    if (url === "/api/v1/inspection-sessions" && init?.method === "POST") {
      if (failStart) { failStart = false; throw new Error("network interrupted"); }
      const body = JSON.parse(String(init.body));
      session = { ...body, session_id: "session-1", organization_id: "org-1", sanitized_uri: body.source_ref,
        secret_reference: null, status: "START_REQUESTED", created_at: task.created_at, updated_at: task.updated_at };
      return json(session, 201);
    }
    if (url.endsWith("session-1:stop")) {
      session = { ...session, status: "STOP_REQUESTED" };
      return json(session);
    }
    if (url === "/api/v1/inspection-sessions") return json({ items: session ? [session] : [] });
    if (url.startsWith("/api/v1/inference-tasks?")) return json({ items: [task] });
    if (url.endsWith("task-1:replay")) return json({ task_id: "new-task", source_task_id: "task-1", status: "READY", dispatch_seq: 1 }, 201);
    if (url.endsWith("/inference-tasks/task-1")) return json(task);
    if (url.endsWith("/artifact-1/evidence-url")) return json({ url: "https://evidence.test/frame?signature=private", expires_in: 60 });
    throw new Error(`Unexpected request ${url}`);
  }));
  return { requests, failNextStart: () => { failStart = true; } };
}

afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear(); });

it("keeps inspector diagnostics read-only while allowing evidence access", async () => {
  setup("INSPECTOR");
  render(<OperationsPanel />);
  fireEvent.click(await screen.findByRole("button", { name: /查看任务 task-1/ }));
  expect(await screen.findByText("Invalid image")).toBeVisible();
  expect(screen.queryByRole("button", { name: "启动实时检测" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "重放死信任务" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "查看证据" }));
  const link = await screen.findByRole("link", { name: "打开证据（60 秒有效）" });
  expect(link).toHaveAttribute("href", "https://evidence.test/frame?signature=private");
  expect(Object.values(localStorage)).not.toContain("https://evidence.test/frame?signature=private");
});

it("reuses the session idempotency key after a network failure and stops the created session", async () => {
  const { requests, failNextStart } = setup();
  render(<OperationsPanel />);
  await screen.findByRole("button", { name: "启动实时检测" });
  fireEvent.change(screen.getByLabelText("相机 ID"), { target: { value: camera } });
  fireEvent.change(screen.getByLabelText("视频源"), { target: { value: "scratch-loop" } });
  failNextStart();
  fireEvent.click(screen.getByRole("button", { name: "启动实时检测" }));
  expect(await screen.findByText(/操作失败/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "启动实时检测" }));
  fireEvent.click(await screen.findByRole("button", { name: /停止检测/ }));
  expect(await screen.findByText("等待停止")).toBeVisible();
  const starts = requests.filter(r => r.url === "/api/v1/inspection-sessions" && r.init?.method === "POST");
  expect(starts).toHaveLength(2);
  expect(new Headers(starts[0].init?.headers).get("Idempotency-Key")).toBeTruthy();
  expect(new Headers(starts[0].init?.headers).get("Idempotency-Key"))
    .toBe(new Headers(starts[1].init?.headers).get("Idempotency-Key"));
});

it("reports the new replay identity only after a successful server response", async () => {
  const { requests } = setup();
  render(<OperationsPanel />);
  fireEvent.click(await screen.findByRole("button", { name: /查看任务 task-1/ }));
  fireEvent.click(await screen.findByRole("button", { name: "重放死信任务" }));
  expect(await screen.findByText(/已创建重放任务 new-task/)).toBeVisible();
  await waitFor(() => expect(requests.filter(r => r.url.endsWith("task-1:replay"))).toHaveLength(1));
});

it("clears expired evidence links and fetches a fresh link on demand", async () => {
  const { requests } = setup("INSPECTOR");
  render(<OperationsPanel />);
  fireEvent.click(await screen.findByRole("button", { name: /查看任务 task-1/ }));
  await screen.findByRole("button", { name: "查看证据" });
  vi.useFakeTimers();
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "查看证据" })));
  expect(screen.getByRole("link", { name: /打开证据/ })).toBeVisible();
  await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
  expect(screen.queryByRole("link", { name: /打开证据/ })).not.toBeInTheDocument();
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "查看证据" })));
  expect(screen.getByRole("link", { name: /打开证据/ })).toBeVisible();
  expect(requests.filter(r => r.url.endsWith("/evidence-url"))).toHaveLength(2);
});

it("suspends polling while hidden, resumes on visibility and cancels after unmount", async () => {
  const { requests } = setup();
  vi.useFakeTimers();
  const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  const view = render(<OperationsPanel />);
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  const count = () => requests.filter(r => r.url.startsWith("/api/v1/inference-tasks?")).length;
  expect(count()).toBe(1);
  hidden.mockReturnValue(true);
  act(() => document.dispatchEvent(new Event("visibilitychange")));
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(count()).toBe(1);
  hidden.mockReturnValue(false);
  await act(async () => document.dispatchEvent(new Event("visibilitychange")));
  expect(count()).toBe(2);
  view.unmount();
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(count()).toBe(2);
  const lastRead = requests.filter(r => r.url.startsWith("/api/v1/inference-tasks?")).at(-1);
  expect(lastRead?.init?.signal?.aborted).toBe(true);
});

it("does not claim replay success after a server conflict", async () => {
  setup();
  const fetchImplementation = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (input, init) => String(input).endsWith(":replay")
    ? json({ detail: "READY_WINDOW_FULL" }, 409) : fetchImplementation(input, init));
  render(<OperationsPanel />);
  fireEvent.click(await screen.findByRole("button", { name: /查看任务 task-1/ }));
  fireEvent.click(await screen.findByRole("button", { name: "重放死信任务" }));
  expect(await screen.findByText(/状态冲突或队列已满/)).toBeVisible();
  expect(screen.queryByText(/已创建重放任务/)).not.toBeInTheDocument();
});

it("retries a failed profile load when the user refreshes", async () => {
  setup();
  const original = vi.mocked(fetch).getMockImplementation()!;
  let firstProfile = true;
  vi.mocked(fetch).mockImplementation(async (input, init) => {
    if (String(input) === "/api/v1/auth/me" && firstProfile) {
      firstProfile = false; return json({ detail: "unavailable" }, 503);
    }
    return original(input, init);
  });
  render(<OperationsPanel />);
  expect(await screen.findByText(/运行状态加载失败/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "刷新运行状态" }));
  expect(await screen.findByRole("button", { name: "启动实时检测" })).toBeVisible();
});

it("never renders executable URLs returned as evidence", async () => {
  setup("INSPECTOR");
  const original = vi.mocked(fetch).getMockImplementation()!;
  vi.mocked(fetch).mockImplementation(async (input, init) => String(input).endsWith("/evidence-url")
    ? json({ url: "javascript:alert(1)", expires_in: 60 }) : original(input, init));
  render(<OperationsPanel />);
  fireEvent.click(await screen.findByRole("button", { name: /查看任务 task-1/ }));
  fireEvent.click(await screen.findByRole("button", { name: "查看证据" }));
  expect(await screen.findByText("操作失败，请稍后重试")).toBeVisible();
  expect(screen.queryByRole("link", { name: /打开证据/ })).not.toBeInTheDocument();
});
