import { useEffect, useRef, useState } from "react";
import { getEvidenceUrl, getInferenceTask, replayInferenceTask } from "../../api/client";
import type { InferenceTask } from "../../api/types";
import { operationError, statusLabels } from "./operations";

export function TaskDiagnostics({ taskId, canManage, onChange }: {
  taskId: string; canManage: boolean; onChange: () => void;
}) {
  const [task, setTask] = useState<InferenceTask>();
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [evidence, setEvidence] = useState<{ url: string; seconds: number }>();
  const [replayed, setReplayed] = useState(false);
  const inFlight = useRef(false);
  const active = useRef(true);
  const controller = useRef(new AbortController());
  useEffect(() => {
    active.current = true;
    const request = new AbortController(); controller.current = request;
    void getInferenceTask(taskId, request.signal).then(setTask).catch(cause => {
      if (!request.signal.aborted) setError(operationError(cause));
    });
    return () => { active.current = false; request.abort(); };
  }, [taskId]);
  useEffect(() => {
    if (!evidence) return;
    const timer = window.setTimeout(() => setEvidence(undefined), evidence.seconds * 1000);
    return () => window.clearTimeout(timer);
  }, [evidence]);

  async function act(kind: "replay" | "evidence") {
    if (!task || inFlight.current) return;
    inFlight.current = true; setBusy(true); setError("");
    try {
      if (kind === "replay") {
        const result = await replayInferenceTask(task.task_id);
        if (!active.current) return;
        setReplayed(true); setMessage(`已创建重放任务 ${result.task_id}`); onChange();
      } else {
        const result = await getEvidenceUrl(task.artifact_id, controller.current.signal);
        if (!active.current) return;
        const url = new URL(result.url);
        if (!["https:", "http:"].includes(url.protocol) || !Number.isFinite(result.expires_in) || result.expires_in <= 0) throw new Error("Invalid evidence link");
        setEvidence({ url: url.href, seconds: Math.min(result.expires_in, 60) });
      }
    } catch (cause) { if (active.current) setError(operationError(cause)); }
    finally { inFlight.current = false; if (active.current) setBusy(false); }
  }

  return <section aria-label="任务详情" className="operations-card">
    <h3>任务详情 · {taskId.slice(0, 8)}</h3>
    {error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    {!task && !error && <p>正在加载任务详情…</p>}
    {task && <>
      <p>{statusLabels[task.status] ?? task.status} · 尝试 {task.attempt_count} 次</p>
      {task.error_code && <p>错误代码：{task.error_code}</p>}
      {task.error_detail && <p>{task.error_detail}</p>}
      <div className="operations-actions">
        {canManage && task.status === "DEAD_LETTER" && <button disabled={busy || replayed} onClick={() => void act("replay")}>重放死信任务</button>}
        <button disabled={busy} onClick={() => void act("evidence")}>{evidence ? "刷新证据链接" : "查看证据"}</button>
        {evidence && <a href={evidence.url} target="_blank" rel="noopener noreferrer">打开证据（{evidence.seconds} 秒有效）</a>}
      </div>
      <p className="operations-hint">仅已归档为证据的帧可查看。链接到期后可重新获取。</p>
      <h4>执行尝试</h4>
      {!task.attempts.length && <p>尚无执行记录</p>}
      <ol>{task.attempts.map(attempt => <li key={attempt.attempt_id}>
        第 {attempt.attempt_no} 次 · {attempt.worker_id} · {attempt.outcome ?? "执行中"}
        <p>{new Date(attempt.started_at).toLocaleString()} · {attempt.duration_ms === null ? "耗时待记录" : `${attempt.duration_ms} ms`}{attempt.error_code ? ` · ${attempt.error_code}` : ""}</p>
      </li>)}</ol>
      <h4>分发记录</h4>
      {!task.dispatches.length && <p>尚无分发记录</p>}
      <ol>{task.dispatches.map(dispatch => <li key={dispatch.outbox_id}>
        分发 {dispatch.dispatch_seq ?? "—"} · {dispatch.published_at ? "已发送" : "待发送"} · 发送尝试 {dispatch.publish_attempts} 次
        {dispatch.last_error && <p>{dispatch.last_error}</p>}
      </li>)}</ol>
    </>}
  </section>;
}
