import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { RealtimeWorkbench } from "./features/workbench/RealtimeWorkbench";

createRoot(document.getElementById("root")!).render(
  <StrictMode><RealtimeWorkbench since={new Date(0).toISOString()} /></StrictMode>,
);
