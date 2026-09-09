import { expect, test } from "@playwright/test";

declare const process: { env: Record<string, string | undefined> };

test("真实采集：启动、推理证据、停止和新工单处置", async ({ page }) => {
  test.skip(!process.env.E2E_RECORDED_SOURCE, "requires a video inside the Compose ingestor");
  test.setTimeout(90_000);
  await page.goto("/");
  await page.getByLabel("邮箱").fill("leader@example.test");
  await page.getByLabel("密码").fill("odp-leader-dev");
  await page.getByRole("button", { name: "登录" }).click();
  const panel = page.getByRole("region", { name: "检测运行管理" });
  await expect(panel.getByRole("button", { name: "启动实时检测" })).toBeVisible();
  const camera = await page.evaluate(() => crypto.randomUUID());
  const before = await page.evaluate(async () => {
    const response = await fetch("/api/v1/cases", { headers: {
      Authorization: `Bearer ${localStorage.getItem("odp_token")}`,
    } });
    return (await response.json()).map((item: { case_id: string }) => item.case_id) as string[];
  });
  let sessionId: string | undefined;
  try {
    await panel.getByLabel("相机 ID").fill(camera);
    await panel.getByLabel("视频源", { exact: true }).fill(process.env.E2E_RECORDED_SOURCE!);
    await panel.getByLabel("产品类别（用于知识检索）").fill("外壳注塑件");
    const created = page.waitForResponse(response => response.url().endsWith("/inspection-sessions")
      && response.request().method() === "POST");
    await panel.getByRole("button", { name: "启动实时检测" }).click();
    const response = await created;
    expect(response.ok()).toBe(true);
    sessionId = (await response.json()).session_id;
    await panel.getByLabel("任务状态").selectOption("SUCCEEDED");
    const task = panel.getByRole("region", { name: "推理任务", exact: true }).getByRole("listitem")
      .filter({ hasText: `相机 ${camera.slice(0, 8)}` }).first();
    await expect(task.getByRole("button", { name: /查看任务/ })).toBeVisible({ timeout: 30_000 });
    await panel.getByRole("button", { name: `停止检测 ${sessionId!.slice(0, 8)}` }).click();
    await task.getByRole("button", { name: /查看任务/ }).click();
    await panel.getByRole("button", { name: "查看证据", exact: true }).click();
    const popupPromise = page.waitForEvent("popup");
    await panel.getByRole("link", { name: "打开证据（60 秒有效）" }).click();
    const popup = await popupPromise;
    await expect.poll(() => popup.locator("img").evaluateAll(images => images.some(
      image => (image as HTMLImageElement).naturalWidth > 0))).toBe(true);
    await popup.close();
    const newCase = await page.evaluate(async (previous) => {
      const response = await fetch("/api/v1/cases", { headers: {
        Authorization: `Bearer ${localStorage.getItem("odp_token")}`,
      } });
      return (await response.json()).find((item: { case_id: string }) => !previous.includes(item.case_id));
    }, before);
    expect(newCase).toBeTruthy();
    // New cases must become actionable without a reload after live inference.
    const caseButton = page.getByRole("button", { name: new RegExp(newCase.case_id.slice(0, 8)) });
    await expect(caseButton).toBeVisible({ timeout: 10_000 });
    await caseButton.click();
    const advice = page.getByRole("region", { name: "AI 处置建议" });
    await expect(advice.getByText("可信度高")).toBeVisible();
    await expect(advice.getByText(/文档版本 \d+/).first()).toBeVisible();
    await page.getByRole("button", { name: "确认复检" }).click();
    await expect(page.getByText("待确认 → 复核中")).toBeVisible();
    await page.getByRole("button", { name: "完成处置" }).click();
    await expect(page.getByRole("heading", { name: /已处置/ })).toBeVisible();
  } finally {
    if (sessionId && !page.isClosed()) await page.evaluate(async id => {
      await fetch(`/api/v1/inspection-sessions/${id}:stop`, { method: "POST", headers: {
        Authorization: `Bearer ${localStorage.getItem("odp_token")}`,
      } });
    }, sessionId);
  }
});
