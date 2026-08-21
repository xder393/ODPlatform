import { useState, type FormEvent } from "react";

import { ApiError, login } from "../../api/client";
import "./login.css";

export interface LoginFormProps {
  /** 登录成功回调；调用时 token 已写入 localStorage["odp_token"]。 */
  onAuthenticated: (token: string) => void;
}

export function LoginForm({ onAuthenticated }: LoginFormProps) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      const result = await login(email, password);
      localStorage.setItem("odp_token", result.access_token);
      onAuthenticated(result.access_token);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        setError("邮箱或密码错误");
      } else {
        setError("登录失败，请稍后重试");
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="login-page">
      <section className="login-card" aria-label="登录">
        <h1>企业 AI 质检平台</h1>
        <p className="login-subtitle">AI 质检工作台 · 请使用演示账户登录</p>
        <form className="login-form" onSubmit={submit}>
          <div className="login-field">
            <label htmlFor="login-email">邮箱</label>
            <input
              id="login-email"
              type="email"
              required
              autoComplete="username"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </div>
          <div className="login-field">
            <label htmlFor="login-password">密码</label>
            <input
              id="login-password"
              type="password"
              required
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </div>
          {error && <p className="login-error" role="alert">{error}</p>}
          <button type="submit" className="login-submit" disabled={busy}>
            登录
          </button>
        </form>
      </section>
    </main>
  );
}
