import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";

// since 取纪元起点：登录后通过 REST 补偿拉取全部历史告警，保证演示/E2E 确定性。
createRoot(document.getElementById("root")!).render(
  <StrictMode><App since={new Date(0).toISOString()} /></StrictMode>,
);
