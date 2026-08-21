import { expect, test } from "@playwright/test";

/**
 * 确定性闭环：登录 → 实时告警 → 选中工单 → AI 处置建议（可信度 + 引用来源）
 * → 确认复检（时间线 待确认 → 复核中）→ 完成处置（状态已处置）。
 *
 * 依赖后端 seed 数据（模拟相机划痕告警 + 知识文档）与演示账户；
 * 服务由 CI 的 docker compose 统一拉起（E2E_BASE_URL 指向 nginx 前端入口）。
 */
test("质检员完成从告警到已处置的完整闭环", async ({ page }) => {
  // 1. 打开首页 → 登录演示账户
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "企业 AI 质检平台" })).toBeVisible();
  await page.getByLabel("邮箱").fill("inspector@example.test");
  await page.getByLabel("密码").fill("odp-inspector-dev");
  await page.getByRole("button", { name: "登录" }).click();

  // 2. 看到实时告警（疑似表面划痕）
  await expect(page.getByRole("heading", { name: "实时质检工作台" })).toBeVisible();
  await expect(page.getByText("疑似表面划痕").first()).toBeVisible();

  // 3. 工单列表选中一个待确认工单 → AI 处置建议：可信度高徽标 + 引用来源含文档版本/页码
  await page.getByRole("button", { name: /待确认/ }).first().click();
  const advicePanel = page.getByRole("region", { name: "AI 处置建议" });
  await expect(advicePanel.getByText("可信度高")).toBeVisible();
  await expect(advicePanel.getByText(/文档版本 \d+/)).toBeVisible();
  await expect(advicePanel.getByText(/第 \d+ 页第 \d+ 段/)).toBeVisible();

  // 4. 确认复检 → 时间线出现「待确认 → 复核中」
  await page.getByRole("button", { name: "确认复检" }).click();
  await expect(page.getByText("待确认 → 复核中")).toBeVisible();

  // 5. 完成处置 → 状态变已处置
  await page.getByRole("button", { name: "完成处置" }).click();
  await expect(page.getByRole("heading", { name: /已处置/ })).toBeVisible();
});
