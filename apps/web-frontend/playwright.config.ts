import { defineConfig, devices } from "@playwright/test";

// 仅声明本配置用到的 process.env 形状（不引入 @types/node 依赖）。
declare const process: {
  env: Record<string, string | undefined>;
};

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  // CI 由 deploy/compose.yaml 统一拉起 api / nginx 静态服务，本配置不自动起 dev server
  // （本机无 docker 未验证；本地联调可解除下面的注释）。
  // webServer: {
  //   command: "npm run dev -- --port 8080",
  //   url: "http://localhost:8080",
  //   reuseExistingServer: true,
  // },
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:8080",
    trace: "retain-on-failure",
    ...(process.env.PLAYWRIGHT_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHANNEL } : {}),
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
