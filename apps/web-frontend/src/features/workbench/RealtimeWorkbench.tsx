import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import {
  ApiError,
  listCases,
  reauthenticate,
  requestAdvice,
  simulatePause,
  transitionCase,
} from "../../api/client";
import type { AdviceResponse, CaseSummary, CaseTransitionStatus } from "../../api/types";
import { AdvicePanel } from "../advice/AdvicePanel";
import { caseStatusLabels, CaseTimeline, type CaseTransitionEntry } from "../cases/CaseTimeline";
import "./workbench.css";
import { InspectionAlert, useInspectionFeed } from "./useInspectionFeed";

const defectNames: Record<string, string> = { scratch: "疑似表面划痕" };

const transitionActionLabels: Record<CaseTransitionStatus, string> = {
  IN_REVIEW: "确认复检",
  RESOLVED: "完成处置",
  FALSE_POSITIVE: "标记误报",
};

function describeApiError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return "无权限执行该操作";
    if (error.status === 404) return "工单不存在或已被删除";
    if (error.status === 409) return "状态流转不合法，请按正确流程操作";
    return `操作失败（HTTP ${error.status ?? "未知"}）`;
  }
  return "操作失败，请稍后重试";
}

function AlertCard({ alert }: { alert: InspectionAlert }) {
  const [action, setAction] = useState<string>();
  return (
    <article aria-label={defectNames[alert.defect_class] ?? alert.defect_class}>
      <h2>{defectNames[alert.defect_class] ?? alert.defect_class}</h2>
      <p>置信度：<strong>{(alert.confidence * 100).toFixed(1)}%</strong></p>
      <p>相机：{alert.camera_id}</p>
      <div>
        <button onClick={() => setAction("已确认处置")}>确认处置</button>
        <button onClick={() => setAction("已标记为误报")}>标记误报</button>
      </div>
      {action && <p>{action}</p>}
    </article>
  );
}

export function RealtimeWorkbench({ since }: { since: string }) {
  const { alerts, reconnecting } = useInspectionFeed(since);
  const newest = alerts.at(-1);

  const [cases, setCases] = useState<CaseSummary[]>([]);
  const [casesError, setCasesError] = useState<string>();
  const [selectedId, setSelectedId] = useState<string>();
  const [caseDetail, setCaseDetail] = useState<CaseSummary>();
  const [advice, setAdvice] = useState<AdviceResponse | null>(null);
  const [adviceAt, setAdviceAt] = useState<string>();
  const [adviceError, setAdviceError] = useState<string>();
  const [transitions, setTransitions] = useState<CaseTransitionEntry[]>([]);
  const [actionMessage, setActionMessage] = useState<string>();
  const [actionError, setActionError] = useState<string>();
  const [needsReauth, setNeedsReauth] = useState(false);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const adviceRequest = useRef(0);

  const refreshCases = useCallback(async () => {
    try {
      const refreshed = await listCases();
      setCases(refreshed);
      setCasesError(undefined);
    } catch (error) {
      setCasesError(describeApiError(error));
    }
  }, []);

  useEffect(() => {
    void refreshCases();
  }, [refreshCases]);

  const loadAdvice = useCallback(async (caseId: string) => {
    const requestId = ++adviceRequest.current;
    setAdvice(null);
    setAdviceError(undefined);
    try {
      const result = await requestAdvice(caseId);
      if (adviceRequest.current === requestId) {
        setAdvice(result);
        setAdviceAt(new Date().toISOString());
      }
    } catch (error) {
      if (adviceRequest.current === requestId) setAdviceError(describeApiError(error));
    }
  }, []);

  const selectCase = (caseItem: CaseSummary) => {
    setSelectedId(caseItem.case_id);
    setCaseDetail(caseItem);
    setTransitions([]);
    setActionError(undefined);
    setActionMessage(undefined);
    setNeedsReauth(false);
    void loadAdvice(caseItem.case_id);
  };

  const applyTransition = async (toStatus: CaseTransitionStatus) => {
    if (!caseDetail) return;
    setBusy(true);
    setActionError(undefined);
    setActionMessage(undefined);
    try {
      const updated = await transitionCase(caseDetail.case_id, toStatus);
      setTransitions((current) => [
        ...current,
        {
          at: new Date().toISOString(),
          fromStatus: caseDetail.status,
          toStatus,
          actorLabel: "当前操作员",
        },
      ]);
      setCaseDetail(updated);
      setCases((current) =>
        current.map((item) => (item.case_id === updated.case_id ? updated : item)),
      );
      void refreshCases();
      setActionMessage(
        `${transitionActionLabels[toStatus]}成功，当前状态：${caseStatusLabels[updated.status]}`,
      );
    } catch (error) {
      setActionError(describeApiError(error));
    } finally {
      setBusy(false);
    }
  };

  const pauseProductionLine = async (caseId: string) => {
    const result = await simulatePause(caseId);
    setNeedsReauth(false);
    setActionMessage(result.message || "已模拟暂停产线");
  };

  const runPause = async () => {
    if (!caseDetail) return;
    setBusy(true);
    setActionError(undefined);
    setActionMessage(undefined);
    try {
      await pauseProductionLine(caseDetail.case_id);
    } catch (error) {
      if (
        error instanceof ApiError &&
        error.status === 403 &&
        error.message.includes("reauthentication")
      ) {
        setNeedsReauth(true);
        setActionError("该操作需要最近 5 分钟内的再次认证，请输入密码");
      } else {
        setActionError(describeApiError(error));
      }
    } finally {
      setBusy(false);
    }
  };

  const submitReauthentication = async (event: FormEvent) => {
    event.preventDefault();
    if (!caseDetail) return;
    setBusy(true);
    setActionError(undefined);
    try {
      await reauthenticate(password);
      setPassword("");
      await pauseProductionLine(caseDetail.case_id);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        setActionError("密码错误，请重新输入");
      } else {
        setActionError(describeApiError(error));
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <main>
      <h1>实时质检工作台</h1>
      <section aria-label="实时视频">
        <div role="img" aria-label="生产线实时视频占位符">视频流待接入</div>
      </section>
      {reconnecting && <p role="status">正在重新连接告警流…</p>}
      <section aria-label="告警详情">
        {alerts.map((alert) => <AlertCard key={alert.event_id} alert={alert} />)}
      </section>
      <div aria-live="assertive" role="alert">
        {newest ? `新告警：${defectNames[newest.defect_class] ?? newest.defect_class}` : "等待新的质检告警"}
      </div>
      <section aria-label="工单处理" className="case-handling">
        <h2>工单处理</h2>
        {casesError && (
          <p className="case-error" role="alert">工单列表加载失败：{casesError}</p>
        )}
        <div className="case-layout">
          <div className="case-list">
            <h3>工单列表</h3>
            {cases.length === 0 && !casesError && <p>暂无工单</p>}
            <ul>
              {cases.map((caseItem) => (
                <li key={caseItem.case_id}>
                  <button
                    type="button"
                    className={`case-list-item${caseItem.case_id === selectedId ? " case-list-item-selected" : ""}`}
                    aria-pressed={caseItem.case_id === selectedId}
                    onClick={() => selectCase(caseItem)}
                  >
                    <span>{caseItem.case_id.slice(0, 8)}</span>（{caseStatusLabels[caseItem.status]}）
                  </button>
                </li>
              ))}
            </ul>
          </div>
          <div className="case-detail">
            {caseDetail ? (
              <>
                <h3>工单 {caseDetail.case_id.slice(0, 8)} · {caseStatusLabels[caseDetail.status]}</h3>
                <div aria-live="polite" className="case-feedback">
                  {actionMessage && <p className="case-success" role="status">{actionMessage}</p>}
                  {actionError && <p className="case-error" role="alert">{actionError}</p>}
                </div>
                <div className="case-actions">
                  <button type="button" disabled={busy} onClick={() => void applyTransition("IN_REVIEW")}>
                    确认复检
                  </button>
                  <button type="button" disabled={busy} onClick={() => void applyTransition("RESOLVED")}>
                    完成处置
                  </button>
                  <button type="button" disabled={busy} onClick={() => void applyTransition("FALSE_POSITIVE")}>
                    标记误报
                  </button>
                </div>
                <CaseTimeline
                  events={caseDetail.inspection_events}
                  advice={advice}
                  adviceAt={adviceAt}
                  transitions={transitions}
                  status={caseDetail.status}
                />
                {adviceError ? (
                  <p className="case-error" role="alert">AI 建议获取失败：{adviceError}</p>
                ) : advice ? (
                  <AdvicePanel
                    advice={advice}
                    onRequestPause={() => void runPause()}
                    pauseDisabled={busy}
                  />
                ) : (
                  <p>正在生成 AI 处置建议…</p>
                )}
                {needsReauth && (
                  <form className="reauthenticate-form" onSubmit={submitReauthentication}>
                    <label htmlFor="reauth-password">请输入密码完成再次认证：</label>
                    <input
                      id="reauth-password"
                      type="password"
                      value={password}
                      onChange={(event) => setPassword(event.target.value)}
                    />
                    <button type="submit" disabled={busy}>确认认证</button>
                    <button type="button" onClick={() => setNeedsReauth(false)}>取消</button>
                  </form>
                )}
              </>
            ) : (
              <p>请从左侧选择一个工单查看详情</p>
            )}
          </div>
        </div>
      </section>
    </main>
  );
}
