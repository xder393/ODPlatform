/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// globals: true 让 @testing-library/react 的自动 cleanup 生效
// （RTL 依赖全局 afterEach 注册清理钩子，vitest 默认不注入全局）。
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
  },
});
