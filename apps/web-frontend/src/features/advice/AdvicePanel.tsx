import type { AdviceConfidence, AdviceResponse } from "../../api/types";

/** 置信度徽标的语义类与可访问文本（时间线组件同样使用）。 */
export const confidenceMeta: Record<
  AdviceConfidence,
  { className: string; label: string }
> = {
  HIGH: { className: "confidence-high", label: "可信度高" },
  MEDIUM: { className: "confidence-medium", label: "可信度中" },
  LOW: { className: "confidence-low", label: "可信度低" },
  UNAVAILABLE: { className: "confidence-unavailable", label: "AI 建议不可用，请人工判断" },
};

export interface AdvicePanelProps {
  advice: AdviceResponse;
  /** 暂停产线动作，由父组件（工单处理区）提供。 */
  onRequestPause?: () => void;
  /** 由父组件决定是否禁用暂停按钮；未传时按建议可信度自动禁用。 */
  pauseDisabled?: boolean;
}

export function AdvicePanel({ advice, onRequestPause, pauseDisabled = false }: AdvicePanelProps) {
  const meta = confidenceMeta[advice.confidence];
  const lowConfidence = advice.confidence === "LOW" || advice.confidence === "UNAVAILABLE";
  const pauseButtonDisabled = pauseDisabled || lowConfidence;

  return (
    <section aria-label="AI 处置建议" className="advice-panel">
      <h3>AI 处置建议</h3>
      <p>
        <span className={`confidence-badge ${meta.className}`}>{meta.label}</span>
      </p>
      <p className="advice-answer">{advice.answer}</p>
      {advice.citations.length > 0 && (
        <>
          <h4>引用来源</h4>
          <ol className="citations">
            {advice.citations.map((citation) => (
              <li key={citation.chunk_id} className="citation">
                <p className="citation-meta">
                  {citation.source_name} · 文档版本 {citation.document_version} · 第{" "}
                  {citation.page_number} 页第 {citation.paragraph_number} 段
                </p>
                <blockquote className="citation-snippet">{citation.snippet}</blockquote>
              </li>
            ))}
          </ol>
        </>
      )}
      <button
        type="button"
        className="pause-button"
        disabled={pauseButtonDisabled}
        onClick={onRequestPause}
      >
        模拟暂停产线
      </button>
    </section>
  );
}
