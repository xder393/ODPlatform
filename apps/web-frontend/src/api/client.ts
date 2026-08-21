import type {
  AdviceResponse,
  CaseSummary,
  CaseTransitionStatus,
  PauseResponse,
  ReauthenticateResponse,
} from "./types";

/** 带 HTTP 状态码的 API 错误，调用方可用 status 区分 401/403/404/409。 */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, detail?: string) {
    super(detail || `请求失败（HTTP ${status}）`);
    this.name = "ApiError";
    this.status = status;
  }
}

async function readErrorDetail(response: Response): Promise<string | undefined> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    return typeof body.detail === "string" ? body.detail : undefined;
  } catch {
    return undefined;
  }
}

/**
 * 统一请求入口：自动附加 Bearer token（localStorage 的 "odp_token"），
 * 非 2xx 响应抛出携带 status 与后端 detail 的 ApiError。
 */
export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const token = localStorage.getItem("odp_token");
  const headers = new Headers(init?.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    throw new ApiError(response.status, await readErrorDetail(response));
  }
  return response;
}

export async function listCases(updatedAfter?: string): Promise<CaseSummary[]> {
  const query = updatedAfter
    ? `?updated_after=${encodeURIComponent(updatedAfter)}`
    : "";
  const response = await apiFetch(`/api/v1/cases${query}`);
  return (await response.json()) as CaseSummary[];
}

export async function transitionCase(
  caseId: string,
  status: CaseTransitionStatus,
): Promise<CaseSummary> {
  const response = await apiFetch(
    `/api/v1/cases/${encodeURIComponent(caseId)}/transitions`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    },
  );
  return (await response.json()) as CaseSummary;
}

export async function requestAdvice(caseId: string): Promise<AdviceResponse> {
  const response = await apiFetch(`/api/v1/cases/${encodeURIComponent(caseId)}/advice`, {
    method: "POST",
  });
  return (await response.json()) as AdviceResponse;
}

export async function simulatePause(caseId: string): Promise<PauseResponse> {
  const response = await apiFetch(`/api/v1/cases/${encodeURIComponent(caseId)}/pause`, {
    method: "POST",
  });
  return (await response.json()) as PauseResponse;
}

export async function reauthenticate(password: string): Promise<ReauthenticateResponse> {
  const response = await apiFetch("/api/v1/auth/reauthenticate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  return (await response.json()) as ReauthenticateResponse;
}

export interface LoginResponse {
  access_token: string;
}

/**
 * 登录：返回 access_token，不自动写入存储，由调用方（登录表单）决定。
 * 直接走原生 fetch 而不是 apiFetch，避免登录请求附带旧 token。
 */
export async function login(email: string, password: string): Promise<LoginResponse> {
  const response = await fetch("/api/v1/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!response.ok) {
    throw new ApiError(response.status, await readErrorDetail(response));
  }
  return (await response.json()) as LoginResponse;
}
