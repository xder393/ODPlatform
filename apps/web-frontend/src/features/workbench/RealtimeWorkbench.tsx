import { useState } from "react";

import { InspectionAlert, useInspectionFeed } from "./useInspectionFeed";

const defectNames: Record<string, string> = { scratch: "疑似表面划痕" };

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
    </main>
  );
}
