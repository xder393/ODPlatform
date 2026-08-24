import { expect, test, type WebSocket as PlaywrightWebSocket } from "@playwright/test";

declare global {
  interface Window {
    __odpInspectionSocketOpenUrls?: string[];
  }
}

/**
 * 确定性闭环：登录 → 实时告警 → 选中工单 → AI 处置建议（可信度 + 引用来源）
 * → 确认复检（时间线 待确认 → 复核中）→ 完成处置（状态已处置）。
 *
 * 依赖后端 seed 数据（模拟相机划痕告警 + 知识文档）与演示账户；
 * 服务由 CI 的 docker compose 统一拉起（E2E_BASE_URL 指向 nginx 前端入口）。
 */
test("质检员在同一实时连接中接收新告警并完成处置", async ({ page }) => {
  await page.addInitScript(() => {
    const NativeWebSocket = window.WebSocket;
    window.__odpInspectionSocketOpenUrls = [];
    class ObservedWebSocket extends NativeWebSocket {
      constructor(url: string | URL, protocols?: string | string[]) {
        super(url, protocols);
        this.addEventListener("open", () => {
          if (this.url.includes("/ws/inspection-events?ticket=")) {
            window.__odpInspectionSocketOpenUrls?.push(this.url);
          }
        }, { once: true });
      }
    }
    // `extends` preserves the native prototype chain and inherited static
    // constants (CONNECTING/OPEN/CLOSING/CLOSED); invalid construction still
    // follows the browser's native constructor validation through `super`.
    Object.defineProperty(ObservedWebSocket, "name", { value: "WebSocket" });
    window.WebSocket = ObservedWebSocket;
  });
  const receivedFrames: string[] = [];
  let selectedSocket: PlaywrightWebSocket | undefined;
  const inspectionSocket = new Promise<PlaywrightWebSocket>((resolve) => {
    page.on("websocket", (socket) => {
      if (selectedSocket || !socket.url().includes("/ws/inspection-events?ticket=")) return;
      selectedSocket = socket;
      socket.on("framereceived", (frame) => receivedFrames.push(String(frame.payload)));
      resolve(socket);
    });
  });
  let postLoginNavigations = 0;
  let postLoginLoads = 0;
  page.on("framenavigated", (frame) => {
    if (frame === page.mainFrame()) postLoginNavigations += 1;
  });
  page.on("load", () => { postLoginLoads += 1; });

  // 1. 打开首页 → 登录演示账户
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "企业 AI 质检平台" })).toBeVisible();
  await page.getByLabel("邮箱").fill("inspector@example.test");
  await page.getByLabel("密码").fill("odp-inspector-dev");
  await page.getByRole("button", { name: "登录" }).click();

  // 2. 看到实时告警（疑似表面划痕）
  await expect(page.getByRole("heading", { name: "实时质检工作台" })).toBeVisible();
  await expect(page.getByText("疑似表面划痕").first()).toBeVisible();
  const connectedSocket = await inspectionSocket;
  await page.waitForFunction(
    () => window.__odpInspectionSocketOpenUrls?.some((url) => url.includes("/ws/inspection-events?ticket=")),
  );
  postLoginNavigations = 0;
  postLoginLoads = 0;
  const navigationEntries = await page.evaluate(
    () => performance.getEntriesByType("navigation").length,
  );

  // 3. Socket 已打开后，通过仅 Docker/local 开发可用的受鉴权确定性
  // 触发器发布一条未来事件。断言卡片数量变化而页面未 reload。
  const alertCards = page.getByRole("heading", { name: "疑似表面划痕" });
  const beforeCount = await alertCards.count();
  const pageUrl = page.url();
  const trigger = await page.evaluate(async () => {
    const token = localStorage.getItem("odp_token");
    const response = await fetch("/api/v1/dev/inspection-events", {
      method: "POST",
      headers: { "Authorization": `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({
        event_id: "40000000-0000-4000-8000-000000000005",
        line_id: "20000000-0000-4000-8000-000000000001",
      }),
    });
    return { status: response.status, body: await response.json() };
  });
  expect(trigger.status).toBe(201);
  expect(trigger.body.alert.event_id).toBe("40000000-0000-4000-8000-000000000005");
  await expect.poll(() => receivedFrames.some((payload) => {
    try {
      return JSON.parse(payload).alert?.event_id === "40000000-0000-4000-8000-000000000005";
    } catch {
      return false;
    }
  })).toBe(true);
  expect(connectedSocket.url()).toContain("/ws/inspection-events?ticket=");
  await expect(alertCards).toHaveCount(beforeCount + 1);
  expect(page.url()).toBe(pageUrl);
  expect(postLoginNavigations).toBe(0);
  expect(postLoginLoads).toBe(0);
  expect(await page.evaluate(() => performance.getEntriesByType("navigation").length)).toBe(navigationEntries);

  // 4. 工单列表选中一个待确认工单 → AI 处置建议：可信度高徽标 + 引用来源含文档版本/页码
  await page.getByRole("button", { name: /待确认/ }).first().click();
  const advicePanel = page.getByRole("region", { name: "AI 处置建议" });
  await expect(advicePanel.getByText("可信度高")).toBeVisible();
  await expect(advicePanel.getByText(/文档版本 \d+/).first()).toBeVisible();
  await expect(advicePanel.getByText(/第 \d+ 页第 \d+ 段/).first()).toBeVisible();

  // 5. 确认复检 → 时间线出现「待确认 → 复核中」
  await page.getByRole("button", { name: "确认复检" }).click();
  await expect(page.getByText("待确认 → 复核中")).toBeVisible();

  // 6. 完成处置 → 状态变已处置
  await page.getByRole("button", { name: "完成处置" }).click();
  await expect(page.getByRole("heading", { name: /已处置/ })).toBeVisible();
});
