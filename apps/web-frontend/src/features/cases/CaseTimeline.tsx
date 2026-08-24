import type {
  AdviceConfidence,
  AdviceResponse,
  CaseStatus,
  InspectionEventSummary,
} from "../../api/types";
import { confidenceMeta } from "../advice/AdvicePanel";

export const caseStatusLabels: Record<CaseStatus, string> = {
  PENDING_CONFIRMATION: "待确认",
  IN_REVIEW: "复核中",
  RESOLVED: "已处置",
  FALSE_POSITIVE: "误报",
};

const defectNames: Record<string, string> = { scratch: "疑似表面划痕" };

export interface CaseTransitionEntry {
  at: string;
  fromStatus?: CaseStatus;
  toStatus: CaseStatus;
  actorLabel?: string;
}

export interface CaseTimelineProps {
  events: InspectionEventSummary[];
  advice: AdviceResponse | null;
  /** AI 建议的获取时间（建议条目在时间线上的位置）；省略时排在检测事件之后。 */
  adviceAt?: string;
  transitions: CaseTransitionEntry[];
  status: CaseStatus;
}

type TimelineEntry =
  | { kind: "advice"; at: string; confidence: AdviceConfidence }
  | { kind: "transition"; at: string; transition: CaseTransitionEntry };

function formatTime(at: string): string {
  const date = new Date(at);
  return Number.isNaN(date.getTime()) ? at : date.toLocaleString("zh-CN", { hour12: false });
}

export function CaseTimeline({
  events,
  advice,
  adviceAt,
  transitions,
  status,
}: CaseTimelineProps) {
  const entries: TimelineEntry[] = [];
  if (advice) entries.push({ kind: "advice", at: adviceAt ?? "", confidence: advice.confidence });
  for (const transition of transitions) {
    entries.push({ kind: "transition", at: transition.at, transition });
  }
  entries.sort((a, b) => a.at.localeCompare(b.at));

  return (
    <section aria-label="工单时间线" className="case-timeline">
      <h3>工单时间线</h3>
      <ol className="timeline">
        {events.map((event, index) => (
          <li key={`event-${index}`} className="timeline-item timeline-event">
            <p className="timeline-title">检测事件</p>
            <p>缺陷类别：<strong>{defectNames[event.defect_class] ?? event.defect_class}</strong></p>
            <p>置信度：<strong>{(event.confidence * 100).toFixed(1)}%</strong></p>
            <p>模型版本：<strong>{event.model_release}</strong></p>
          </li>
        ))}
        {entries.map((entry) =>
          entry.kind === "advice" ? (
            <li key={`advice-${entry.at}`} className="timeline-item timeline-advice">
              <time className="timeline-time" dateTime={entry.at}>
                {formatTime(entry.at)}
              </time>
              <p className="timeline-title">AI 建议</p>
              <span className={`confidence-badge ${confidenceMeta[entry.confidence].className}`}>
                {confidenceMeta[entry.confidence].label}
              </span>
            </li>
          ) : (
            <li
              key={`transition-${entry.at}-${entry.transition.toStatus}`}
              className="timeline-item timeline-transition"
            >
              <time className="timeline-time" dateTime={entry.at}>
                {formatTime(entry.at)}
              </time>
              <p className="timeline-title">
                人工状态变更
                {entry.transition.actorLabel ? `（${entry.transition.actorLabel}）` : ""}
              </p>
              <p>
                {entry.transition.fromStatus
                  ? `${caseStatusLabels[entry.transition.fromStatus]} → `
                  : ""}
                {caseStatusLabels[entry.transition.toStatus]}
              </p>
            </li>
          ),
        )}
        <li className="timeline-item timeline-status">
          <p className="timeline-title">当前状态</p>
          <p>
            <strong>{caseStatusLabels[status]}</strong>
          </p>
        </li>
      </ol>
    </section>
  );
}
