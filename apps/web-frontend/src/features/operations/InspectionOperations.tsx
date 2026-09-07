import { useRef, useState, type FormEvent } from "react";
import { startInspectionSession, stopInspectionSession } from "../../api/client";
import type { CurrentActor, InspectionSession, InspectionSessionInput } from "../../api/types";
import { operationError, statusLabels } from "./operations";

export function InspectionOperations({ actor, sessions, onChange }: {
  actor: CurrentActor; sessions: InspectionSession[]; onChange: () => void;
}) {
  const canManage = actor.role === "SUPERVISOR" || actor.role === "ADMINISTRATOR";
  const [camera, setCamera] = useState("");
  const [line, setLine] = useState(actor.line_ids[0] ?? "");
  const [source, setSource] = useState("");
  const [kind, setKind] = useState<InspectionSessionInput["source_type"]>("RECORDED");
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const submission = useRef<{ body: string; key: string } | null>(null);

  async function start(event: FormEvent) {
    event.preventDefault();
    if (inFlight.current) return;
    const body: InspectionSessionInput = {
      camera_id: camera.trim(), line_id: line, source_type: kind,
      source_ref: source.trim(), ...(secret.trim() ? { secret_ref: secret.trim() } : {}),
    };
    const serialized = JSON.stringify(body);
    if (submission.current?.body !== serialized) submission.current = { body: serialized, key: crypto.randomUUID() };
    inFlight.current = true; setBusy(true); setError(""); setMessage("");
    try {
      await startInspectionSession(body, submission.current.key);
      submission.current = null;
      setMessage("启动请求已受理，等待采集进程启动"); onChange();
    } catch (cause) { setError(operationError(cause)); }
    finally { inFlight.current = false; setBusy(false); }
  }

  async function stop(id: string) {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(true); setError(""); setMessage("");
    try { await stopInspectionSession(id); setMessage("停止请求已受理"); onChange(); }
    catch (cause) { setError(operationError(cause)); }
    finally { inFlight.current = false; setBusy(false); }
  }

  return <section aria-label="检测会话" className="operations-card">
    <h3>检测会话</h3>
    {canManage && <form onSubmit={event => void start(event)} className="session-form">
      <label>产线 ID{actor.role === "ADMINISTRATOR"
        ? <input required value={line} onChange={e => setLine(e.target.value)} />
        : <select value={line} onChange={e => setLine(e.target.value)}>{actor.line_ids.map(id => <option key={id}>{id}</option>)}</select>}</label>
      <label>相机 ID<input required value={camera} onChange={e => setCamera(e.target.value)} /></label>
      <label>来源类型<select value={kind} onChange={e => setKind(e.target.value as typeof kind)}>
        <option value="RECORDED">录制视频</option><option value="RTSP">RTSP</option><option value="LOCAL_CAMERA">本地相机</option>
      </select></label>
      <label>视频源<input required maxLength={2048} value={source} onChange={e => setSource(e.target.value)} placeholder="例如 scratch-loop" /></label>
      <label>凭据引用（可选）<input maxLength={255} value={secret} onChange={e => setSecret(e.target.value)} placeholder="使用凭据引用，不填写密码" /></label>
      <button disabled={busy || !line} type="submit">启动实时检测</button>
    </form>}
    {error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    {sessions.length === 0 ? <p>暂无检测会话</p> : <ul className="operations-list">{sessions.map(item => <li key={item.session_id}>
      <strong>相机 {item.camera_id}</strong> <span className="operations-status">{statusLabels[item.status] ?? item.status}</span>
      <p>{item.source_type} · 会话 {item.session_id.slice(0, 8)}</p>
      {canManage && ["START_REQUESTED", "RUNNING"].includes(item.status) && <button disabled={busy} onClick={() => void stop(item.session_id)}>停止检测 {item.session_id.slice(0, 8)}</button>}
    </li>)}</ul>}
  </section>;
}
