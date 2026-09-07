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

/** 浏览器建立 WebSocket 前用 Bearer JWT 换取的一次性短期票据。 */
export interface WebSocketTicketResponse {
  ticket: string;
  expires_in: 60;
}

/** 后端的持久化告警事实；游标随传输包络而非业务事实本身返回。 */
export interface InspectionAlert {
  event_id: string;
  organization_id: string;
  camera_id: string;
  occurred_at: string;
  defect_class: string;
  confidence: number;
}

export interface InspectionAlertEnvelope {
  cursor: string;
  alert: InspectionAlert;
}

export interface InspectionAlertReconciliation {
  items: InspectionAlertEnvelope[];
  next_cursor: string | null;
}

export interface CurrentActor {
  actor_id: string;
  role: "INSPECTOR" | "SUPERVISOR" | "ADMINISTRATOR";
  organization_id: string;
  line_ids: string[];
}

export interface InspectionSessionInput {
  camera_id: string;
  line_id: string;
  source_type: "RECORDED" | "RTSP" | "LOCAL_CAMERA";
  source_ref: string;
  secret_ref?: string;
}

export interface InspectionSession {
  session_id: string;
  organization_id: string;
  camera_id: string;
  line_id: string;
  source_type: string;
  sanitized_uri: string;
  secret_reference: string | null;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface InferenceTask {
  task_id: string;
  organization_id: string;
  camera_id: string;
  line_id: string | null;
  artifact_id: string;
  status: string;
  dispatch_seq: number;
  attempt_count: number;
  error_code: string | null;
  error_detail: string | null;
  created_at: string;
  updated_at: string;
  attempts: {
    attempt_id: string; attempt_no: number; fence_token: number; worker_id: string;
    started_at: string; finished_at: string | null; outcome: string | null;
    error_code: string | null; duration_ms: number | null;
  }[];
  dispatches: {
    outbox_id: string; event_type: string; schema_version: number; dispatch_seq: number | null;
    available_at: string; published_at: string | null; publish_attempts: number;
    last_error: string | null;
  }[];
}

export interface TaskReplayResponse {
  task_id: string;
  source_task_id: string;
  status: string;
  dispatch_seq: number;
}

export interface EvidenceUrlResponse { url: string; expires_in: number }
