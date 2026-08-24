import { configDefaults, defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// globals: true 让 @testing-library/react 的自动 cleanup 生效
// （RTL 依赖全局 afterEach 注册清理钩子，vitest 默认不注入全局）。
// e2e/ 目录由 Playwright 运行，不纳入 vitest 收集。
//
// 本地开发（无 docker）时把 /api 与 /ws 代理到本机 FastAPI（:8000），
// 前端仍按同源路径调用，与 nginx 生产形态一致。
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    exclude: [...configDefaults.exclude, "e2e/**"],
  },
});
