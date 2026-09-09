import { expect, test } from "@playwright/test";

// Browser UI contract. HTTP is controlled here; the video-pipeline E2E is separate.
for (const role of ["SUPERVISOR", "INSPECTOR"] as const) {
  test(`${role}: session controls, diagnostics and evidence`, async ({ page }) => {
    const line = "20000000-0000-4000-8000-000000000001";
    const camera = "30000000-0000-4000-8000-000000000001";
    const now = "2026-09-07T12:00:00Z";
    let sessionStatus: string | undefined;
    let replayCount = 0;
    const task = { task_id: "task-1", organization_id: "org-1", camera_id: camera,
      line_id: line, artifact_id: "artifact-1", status: "DEAD_LETTER", dispatch_seq: 1,
      attempt_count: 3, error_code: "INVALID_INPUT", error_detail: "Image decode failed",
      created_at: now, updated_at: now, attempts: [], dispatches: [] };
    await page.addInitScript(() => localStorage.setItem("odp_token", "browser-test-token"));
    await page.route("**/api/v1/**", async route => {
      const path = new URL(route.request().url()).pathname;
      let body: unknown = {};
      if (path === "/api/v1/auth/me") body = { actor_id: "actor-1", role, organization_id: "org-1", line_ids: [line] };
      else if (path === "/api/v1/inspection-sessions" || path.endsWith("session-1:stop")) {
        if (route.request().method() === "POST") {
          sessionStatus = path.endsWith(":stop") ? "STOP_REQUESTED" : "START_REQUESTED";
          if (!path.endsWith(":stop")) expect(route.request().headers()["idempotency-key"]).toBeTruthy();
        }
        const session = { session_id: "session-1", organization_id: "org-1", camera_id: camera,
          line_id: line, status: sessionStatus, source_type: "RECORDED", sanitized_uri: "scratch-loop",
          secret_reference: null, created_at: now, updated_at: now };
        body = route.request().method() === "POST" ? session : { items: sessionStatus ? [session] : [] };
      } else if (path.endsWith("task-1:replay")) {
        replayCount++; body = { task_id: "replay-1", source_task_id: "task-1", status: "READY", dispatch_seq: 1 };
      } else if (path.endsWith("/inference-tasks/task-1")) body = task;
      else if (path === "/api/v1/inference-tasks") body = { items: [task] };
      else if (path.endsWith("/evidence-url")) body = { url: new URL("/browser-evidence", page.url()).href, expires_in: 60 };
      else if (path === "/api/v1/cases") body = [];
      else if (path === "/api/v1/inspection-events") body = { items: [], next_cursor: null };
      else if (path === "/api/v1/auth/websocket-ticket") return route.fulfill({ status: 503, json: { detail: "not part of UI contract" } });
      else throw new Error(`Unexpected request ${path}`);
      await route.fulfill({ json: body });
    });
    await page.context().route("**/browser-evidence", route => route.fulfill({
      contentType: "text/html", body: "<h1>Evidence fixture</h1>",
    }));
    await page.goto("/");
    const panel = page.getByRole("region", { name: "检测运行管理" });
    await expect(panel).toBeVisible();
    if (role === "SUPERVISOR") {
      await panel.getByLabel("相机 ID").fill(camera);
      await panel.getByLabel("视频源", { exact: true }).fill("scratch-loop");
      await panel.getByRole("button", { name: "启动实时检测" }).click();
      await expect(panel.getByText("等待启动", { exact: true })).toBeVisible();
      await panel.getByRole("button", { name: /停止检测/ }).click();
      await expect(panel.getByText("等待停止", { exact: true })).toBeVisible();
    } else await expect(panel.getByRole("button", { name: "启动实时检测" })).toHaveCount(0);
    await panel.getByRole("button", { name: "查看任务 task-1" }).click();
    await expect(panel.getByText("Image decode failed")).toBeVisible();
    if (role === "SUPERVISOR") {
      await panel.getByRole("button", { name: "重放死信任务" }).click();
      await expect(panel.getByText("已创建重放任务 replay-1")).toBeVisible();
      expect(replayCount).toBe(1);
    } else await expect(panel.getByRole("button", { name: "重放死信任务" })).toHaveCount(0);
    await panel.getByRole("button", { name: "查看证据" }).click();
    const popupPromise = page.waitForEvent("popup");
    await panel.getByRole("link", { name: "打开证据（60 秒有效）" }).click();
    const popup = await popupPromise;
    await expect(popup.getByRole("heading", { name: "Evidence fixture" })).toBeVisible();
    await popup.close();
    await page.setViewportSize({ width: 390, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}
