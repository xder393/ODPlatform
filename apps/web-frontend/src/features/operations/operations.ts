import { ApiError } from "../../api/client";

export const statusLabels: Record<string, string> = {
  START_REQUESTED: "等待启动", RUNNING: "运行中", STOP_REQUESTED: "等待停止",
  STOPPED: "已停止", FAILED: "失败", READY: "等待推理", RETRY_WAIT: "等待重试",
  SUCCEEDED: "已完成", DEAD_LETTER: "死信待处理", BLOCKED_COMPATIBILITY: "兼容性阻塞",
  SKIPPED_STALE: "帧已过期", SKIPPED_BACKPRESSURE: "背压跳过",
};

export function operationError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return "登录已失效，请重新登录";
    if (error.status === 403) return "无权限执行该操作";
    if (error.status === 404) return "记录不可访问，或尚无可用证据";
    if (error.status === 409) return "状态冲突或队列已满，请刷新后重试";
    if (error.status === 422) return "参数不符合要求，请检查输入";
    if (error.status === 503) return "服务暂不可用，请稍后重试";
  }
  return "操作失败，请稍后重试";
}
