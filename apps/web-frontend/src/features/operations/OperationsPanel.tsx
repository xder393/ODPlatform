import { useCallback, useEffect, useState } from "react";
import { getCurrentActor, listInferenceTasks, listInspectionSessions } from "../../api/client";
import type { CurrentActor, InferenceTask, InspectionSession } from "../../api/types";
import { InspectionOperations } from "./InspectionOperations";
import { TaskDiagnostics } from "./TaskDiagnostics";
import { operationError, statusLabels } from "./operations";
import "./operations.css";

export function OperationsPanel() {
  const [actor, setActor] = useState<CurrentActor>();
  const [sessions, setSessions] = useState<InspectionSession[]>([]);
  const [tasks, setTasks] = useState<InferenceTask[]>([]);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [selected, setSelected] = useState<string>();
  const [revision, setRevision] = useState(0);
  const [updated, setUpdated] = useState<string>();
  const refresh = useCallback(() => setRevision(value => value + 1), []);

  useEffect(() => {
    const request = new AbortController();
    void getCurrentActor(request.signal).then(profile => {
      if (!request.signal.aborted && Array.isArray(profile.line_ids)) setActor(profile);
    }).catch(cause => { if (!request.signal.aborted) setError(operationError(cause)); });
    return () => request.abort();
  }, [revision]);

  useEffect(() => {
    if (!actor) return;
    let stopped = false;
    let timer: number | undefined;
    let request: AbortController | undefined;
    async function load() {
      if (stopped || document.hidden) return;
      request = new AbortController();
      const current = request;
      try {
        const [sessionResult, taskResult] = await Promise.all([
          listInspectionSessions(current.signal), listInferenceTasks(status, current.signal),
        ]);
        if (stopped || current.signal.aborted) return;
        setSessions(sessionResult.items); setTasks(taskResult.items);
        setUpdated(new Date().toLocaleTimeString()); setError("");
      } catch (cause) { if (!stopped && !current.signal.aborted) setError(operationError(cause)); }
      finally { if (!stopped && !current.signal.aborted && !document.hidden) timer = window.setTimeout(() => void load(), 10_000); }
    }
    function visibilityChanged() {
      window.clearTimeout(timer); request?.abort();
      if (!document.hidden) void load();
    }
    document.addEventListener("visibilitychange", visibilityChanged);
    void load();
    return () => { stopped = true; window.clearTimeout(timer); request?.abort(); document.removeEventListener("visibilitychange", visibilityChanged); };
  }, [actor, status, revision]);

  return <section className="operations-panel" aria-label="检测运行管理">
    <div className="operations-heading"><h2>检测运行管理</h2><button onClick={refresh}>刷新运行状态</button></div>
    {error && <p role="alert">运行状态加载失败：{error}</p>}
    {!actor && !error && <p>正在读取操作权限…</p>}
    {actor && <>
      <p className="operations-hint">组织 {actor.organization_id} · {({ INSPECTOR: "质检员（只读运行信息）", SUPERVISOR: "班组长", ADMINISTRATOR: "管理员" })[actor.role]}{updated && ` · 更新于 ${updated}`}</p>
      <InspectionOperations actor={actor} sessions={sessions} onChange={refresh} />
      <section aria-label="推理任务" className="operations-card">
        <div className="operations-heading"><h3>推理任务</h3><label>任务状态<select value={status} onChange={e => { setStatus(e.target.value); setSelected(undefined); }}>
          <option value="">全部</option>{["READY", "RUNNING", "RETRY_WAIT", "DEAD_LETTER", "BLOCKED_COMPATIBILITY", "SUCCEEDED", "SKIPPED_STALE", "SKIPPED_BACKPRESSURE"].map(value => <option key={value} value={value}>{statusLabels[value]}</option>)}
        </select></label></div>
        <p className="operations-hint">最多显示最近 100 条任务，每 10 秒刷新；后台页面暂停刷新。</p>
        {!tasks.length && <p>暂无符合条件的任务</p>}
        <ul className="operations-list">{tasks.map(task => <li key={task.task_id}>
          <button aria-label={`查看任务 ${task.task_id}`} aria-pressed={selected === task.task_id} onClick={() => setSelected(task.task_id)}>{task.task_id.slice(0, 8)}</button>
          <span className="operations-status">{statusLabels[task.status] ?? task.status}</span><span>尝试 {task.attempt_count} 次 · 相机 {task.camera_id.slice(0, 8)}</span>
        </li>)}</ul>
      </section>
      {selected && <TaskDiagnostics key={selected} taskId={selected} canManage={actor.role === "SUPERVISOR" || actor.role === "ADMINISTRATOR"} onChange={refresh} />}
    </>}
  </section>;
}
