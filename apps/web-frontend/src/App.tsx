import { useCallback, useState } from "react";

import { LoginForm } from "./features/auth/LoginForm";
import { RealtimeWorkbench } from "./features/workbench/RealtimeWorkbench";
import { inspectionAlertCursorStorageKey } from "./features/workbench/useInspectionFeed";
import "./app.css";

const TOKEN_KEY = "odp_token";

/**
 * 鉴权门：无 token 时渲染登录表单；有 token 时渲染实时工作台，
 * 并在顶部提供退出登录（清 token 回登录页）。
 * 登录/登出通过 key 强制重挂载工作台，保证 WebSocket 连接随之重建。
 */
export function App({ since }: { since: string }) {
  const [token, setToken] = useState<string | null>(() =>
    localStorage.getItem(TOKEN_KEY),
  );
  const [workbenchKey, setWorkbenchKey] = useState(0);

  const handleAuthenticated = useCallback((nextToken: string) => {
    setToken(nextToken);
    setWorkbenchKey((key) => key + 1);
  }, []);

  const handleLogout = useCallback(() => {
    if (token) localStorage.removeItem(inspectionAlertCursorStorageKey(token));
    localStorage.removeItem(TOKEN_KEY);
    // Remove the former global format if a user is upgrading from Task 5 pre-fix builds.
    localStorage.removeItem("odp_alert_cursor");
    setToken(null);
    setWorkbenchKey((key) => key + 1);
  }, [token]);

  if (!token) {
    return <LoginForm onAuthenticated={handleAuthenticated} />;
  }

  return (
    <>
      <header className="app-header">
        <h1 className="app-title">企业 AI 质检平台</h1>
        <button type="button" className="app-logout" onClick={handleLogout}>
          退出登录
        </button>
      </header>
      <RealtimeWorkbench key={workbenchKey} since={since} />
    </>
  );
}
