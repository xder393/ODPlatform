import { configDefaults, defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// globals: true 让 @testing-library/react 的自动 cleanup 生效
// （RTL 依赖全局 afterEach 注册清理钩子，vitest 默认不注入全局）。
// e2e/ 目录由 Playwright 运行，不纳入 vitest 收集。
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    exclude: [...configDefaults.exclude, "e2e/**"],
  },
});
