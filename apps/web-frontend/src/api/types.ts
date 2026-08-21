/** 与后端 API 契约一一对应的类型定义。 */

export type CaseStatus =
  | "PENDING_CONFIRMATION"
  | "IN_REVIEW"
  | "RESOLVED"
  | "FALSE_POSITIVE";

/** 前端允许发起的人工状态流转目标（后端契约中不包含 PENDING_CONFIRMATION）。 */
export type CaseTransitionStatus = Exclude<CaseStatus, "PENDING_CONFIRMATION">;

export interface InspectionEventSummary {
  defect_class: string;
  confidence: number;
  model_release: string;
  preprocessing_parameters: Record<string, string>;
  threshold: number;
  input_frame_sha256: string;
}

export interface CaseSummary {
  case_id: string;
  status: CaseStatus;
  updated_at: string;
  inspection_events: InspectionEventSummary[];
}

export type AdviceConfidence = "HIGH" | "MEDIUM" | "LOW" | "UNAVAILABLE";

export interface Citation {
  chunk_id: string;
  document_id: string;
  source_name: string;
  document_version: number;
  page_number: number;
  paragraph_number: number;
  snippet: string;
}

export interface AdviceResponse {
  answer: string;
  confidence: AdviceConfidence;
  citations: Citation[];
}

export interface PauseResponse {
  status: string;
  message: string;
}

export interface ReauthenticateResponse {
  reauthenticated: boolean;
}
