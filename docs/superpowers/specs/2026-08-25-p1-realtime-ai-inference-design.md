# P1 实时 AI 质检推理链路设计

## 1. 背景与目标

P0 已交付企业质检业务闭环、任务状态机骨架、模型可复现字段、RAG、审计、可观测性和端到端测试，但实时视觉任务仍主要依赖进程内仓储与确定性演示调用，尚未形成可跨进程恢复的生产级推理链路。

P1 将首个增量聚焦为“真实异步 AI 质检闭环”：视频帧接入、对象存储、可靠任务分发、独立 Worker 推理、实时告警和人工处置。目标是同时体现 AI 应用工程、分布式一致性、实时背压、故障恢复和可观测性能力。

本设计冻结以下总体定义：

> PostgreSQL 任务事实源 + Transactional Outbox + Redis Streams 分发平面 + DB fencing 的独立推理 Worker。

Redis Streams 不是任务状态机。系统提供 at-least-once delivery，通过数据库幂等约束与 fencing 实现 effectively-once business effect，不宣称 exactly-once。

## 2. 已确认范围与验收基线

### 2.1 输入与模型

- 主验收链路使用录制的工业缺陷视频循环模拟摄像头，保证可重复 E2E。
- 提供 RTSP 与本地摄像头 Source Adapter，作为现场接入扩展。
- CI 使用固定视频样本与确定性 Mock 推理，不依赖摄像头或 GPU。
- 生产演示接入 ONNX Runtime，优先支持 YOLO 导出的 ONNX 模型，并可在 CPU 运行。
- Mock 与 ONNX Adapter 遵循同一视觉推理端口和结果 Schema。
- CUDA/TensorRT 保留适配扩展点，但不作为 P1 验收依赖。
- 每次有效推理记录模型版本、模型摘要、阈值、预处理参数和输入帧哈希。

### 2.2 性能与可靠性

- 本地 Docker Compose、纯 CPU 稳定接入 4 路模拟摄像头。
- 每路输入 10 FPS、推理采样 2 FPS，总吞吐 8 次推理/秒。
- 正常负载下，采集到 WebSocket 告警到达的 P95 小于 2 秒。
- 推理任务成功率不低于 99%；普通帧允许按策略跳过，但积压不得持续增长。
- Worker 异常退出后 30 秒内重新认领任务。
- Redis 短暂中断恢复后 Outbox 自动补发，不产生重复工单。
- 同一缺陷事件最多产生一个业务工单。
- 架构记录 50 路摄像头水平扩展目标，但不把单机 50 路设为 P1 CI 门禁。

## 3. 方案选择

考虑过三种实现：

1. PostgreSQL 任务表轮询：组件最少，但实时延迟、轮询压力和消费恢复表达能力不足。
2. PostgreSQL + Outbox + Redis Streams + DB fencing：兼顾低延迟、持久恢复、消费组扩容和一致性，作为 P1 方案。
3. Kafka 事件流：吞吐和回放能力最强，但对当前规模与演示环境过重，保留为未来容量演进路线。

P1 不引入 Celery。Pending、Claim、ACK、背压、过期帧和消费组恢复直接使用 Redis Streams 原语表达，业务状态与执行权仍由 PostgreSQL 控制。

## 4. 组件与权威边界

```text
Recorded Video / RTSP / Camera
              │
              ▼
        Frame Ingestor
              │
              ├──────────────► MinIO
              │                processing frame / evidence
              ▼
     Task Application Service
              │ PostgreSQL transaction
              ▼
      FrameArtifact / Task
          + Task Outbox
              │
              ▼
     Generic Outbox Relay
              │
              ▼
     Redis Inference Stream
              │
              ▼
       Inference Worker
       ├─ XREADGROUP / XAUTOCLAIM
       ├─ DB lease + fencing
       ├─ freshness / integrity
       └─ ONNX / Mock
              │
              ▼
    Inspection Domain Service
              │ fenced PostgreSQL transaction
              ├─ InferenceAttempt
              ├─ PublishedResult
              ├─ InspectionEvent
              ├─ Case / Evidence
              └─ Alert Outbox
                    │
                    ▼
             Generic Outbox Relay
                    │
                    ▼
             Redis Alert Stream
                    │
                    ▼
             Realtime Gateway
                    │ WebSocket
                    ▼
               Inspector UI
                    │ reconnect / cursor recovery
                    ▼
                 REST API
```

### 4.1 组件职责

- `Frame Ingestor`：适配录制视频、RTSP 和本地摄像头，完成解码、采样、时间戳、哈希、Artifact Saga 和 Task Application Service 调用；不执行推理，也不能绕过应用服务直接写 Task。
- `Task Application Service`：校验租户、摄像头、会话、帧策略、TTL 和幂等键，在事务中创建 Task 与 Outbox。
- `Generic Outbox Relay`：按稳定 `event_type` 和配置映射目的地，可靠发布推理请求、告警及后续集成事件；不修改业务状态。
- `Inference Worker`：消费或重新认领 Redis 消息，通过数据库 fencing 获得执行权，校验帧并调用视觉引擎；不负责 WebSocket、Redis 告警直发或用户身份逻辑。
- `VisionInferenceEngine`：由 `OnnxRuntimeVisionAdapter` 与 `DeterministicMockVisionAdapter` 实现。Vision Adapter 不创建工单。
- `Inspection Domain Service`：作为 `InferenceResult → Business Effect` 的唯一边界，判定有效缺陷、去重、创建事件、创建或关联工单及写入 Alert Outbox。
- `Realtime Gateway`：消费告警事件并推送本实例 WebSocket 连接；断线遗漏由 REST 游标查询补偿。
- `Worker Recovery Loop`：由每个 Worker 执行 `XREADGROUP`、`XAUTOCLAIM`、数据库认领和任务执行。
- `Recovery Scheduler`：只处理数据库控制面，包括过期 lease、到期重试、死信监管、卡住的 Outbox claim 和 Artifact 对账；不消费 inference Stream，也不执行推理。

### 4.2 权威数据

- PostgreSQL：任务状态、执行权、Inference Attempt、Published Result、Inspection Event、Case、Outbox 与审计事实。
- MinIO：Frame 与 Evidence 的二进制内容。
- Redis：低延迟任务分发和实时通知，只表示“谁应立即被唤醒”。

Inference Worker 不直接向 Redis Alert Stream 发布。推理结果、业务事件、工单与 Alert Outbox 在同一个 fenced 数据库事务中提交，Redis 恢复后由 Relay 补发告警。

## 5. Artifact Saga 与生命周期

MinIO 与 PostgreSQL 不存在跨系统原子事务，帧接入采用显式 Saga：

```text
decode / sample / hash
→ PostgreSQL transaction: FrameArtifact(PENDING)
→ upload MinIO
→ HEAD / size / hash metadata verify
→ PostgreSQL transaction:
     FrameArtifact AVAILABLE
     + InferenceTask READY
     + TaskOutbox
→ COMMIT
```

失败处理：

- MinIO 上传失败：Artifact 标记 `FAILED`，不创建可执行 Task。
- MinIO 成功但第二个数据库事务失败：对象成为 orphan candidate，由 Reconciler 恢复或清理。
- 进程在上传后崩溃：Reconciler 对长期 `PENDING` Artifact 执行 `HEAD`，决定恢复、失败或清理。

帧分为两个生命周期：

- `PROCESSING`：只为实时推理服务，使用短 TTL，不默认永久保存。
- `EVIDENCE`：有效质检事件形成后晋升，并与 Case/InspectionEvent 关联，默认保留 90 天。

MinIO 对象不可原地覆盖。Cleaner 只能删除保留期已过且未被有效 Case 引用的对象，删除动作写审计。

## 6. 状态机与数据模型

### 6.1 `frame_artifacts`

字段至少包括：`artifact_id`、`organization_id`、`camera_id`、`stream_session_id`、`frame_sequence`、`captured_at`、`object_key`、`sha256`、`content_length`、`state`、`lifecycle`、`retention_until` 与时间戳。

```text
PENDING ──► AVAILABLE ──► EXPIRED ──► DELETED
   │             │
   ▼             └── lifecycle: PROCESSING → EVIDENCE
 FAILED
```

`(organization_id, camera_id, stream_session_id, frame_sequence)` 唯一。Evidence 晋升与 Case 创建在同一数据库事务中完成。

### 6.2 `inference_tasks`

```text
READY ──► RUNNING ──► SUCCEEDED
  │          │
  │          ├──► RETRY_WAIT ──► READY
  │          ├──► DEAD_LETTER
  │          └──► SKIPPED_STALE
  └─────────────► SKIPPED_STALE / SKIPPED_BACKPRESSURE
```

字段至少包括：`task_id`、`organization_id`、`artifact_id`、`idempotency_key`、`status`、`attempt_count`、`next_attempt_at`、`lease_owner`、`fence_token BIGINT`、`lease_expires_at`、错误编码、错误详情与时间戳。

Worker 通过单条条件更新认领任务，并使 `fence_token` 单调递增。所有终态提交必须同时匹配 `task_id`、`lease_owner`、当前 `fence_token` 且 `lease_expires_at > database_now`。数据库时间是 lease 判断依据。

### 6.3 `inference_attempts`

每次成功认领追加一条不可覆盖记录，包含 `attempt_id`、`task_id`、`attempt_no`、`worker_id`、`fence_token`、起止时间、结果、错误码和耗时。

`(task_id, attempt_no)` 与 `(task_id, fence_token)` 均唯一。

### 6.4 `published_inference_results`

只保存持有当前 fencing 权限的最终有效结果，包含 `result_id`、唯一 `task_id`、唯一 `attempt_id`、`artifact_id`、模型版本与摘要、运行时、预处理参数、阈值、输入帧哈希、检测输出、推理耗时和发布时间。

失败 Attempt 不产生 Published Result；迟到 Worker 的结果被数据库条件拒绝。

### 6.5 业务副作用

Inspection Domain Service 在 fenced 事务内写入：

```text
PublishedResult
+ InspectionEvent
+ Case create/link
+ Evidence promotion
+ AlertOutbox
+ Audit record
```

`inspection_events.source_result_id` 唯一。同一 Published Result 最多产生一个 Inspection Event。Case 使用稳定业务去重键，同一摄像头、缺陷类型和去重窗口内的连续检测关联同一 Case，避免告警风暴。

### 6.6 `outbox_events`

通用字段包括 `outbox_id`、聚合类型与 ID、`event_type`、`schema_version`、payload、`available_at`、claim owner 与 expiry、发布次数、`published_at`、最后错误与时间戳。

Relay 使用 `FOR UPDATE SKIP LOCKED` 批量领取。发布失败设置有上限的指数退避；发布成功但数据库更新失败允许重复发布。Outbox 记录不进入不可恢复的业务死信状态，已发布记录至少保留 7 天。

## 7. Transactional Outbox 与 Fencing

### 7.1 Outbox 事务边界

- 创建任务与 Task Outbox 在同一个 PostgreSQL 事务中完成。
- API、Frame Ingestor 和 Domain Service 不直接向 Redis 发布业务事件。
- 每条消息携带稳定 `outbox_id`、`task_id`、`event_type`、`schema_version` 和关联信息；Redis 消息 ID 不是业务主键。
- 任务重试新增 Outbox 事件，不修改或复用历史消息。
- Domain 保存稳定事件类型，Adapter 配置映射 Redis 目的地，例如：

```text
vision.inference.requested.v1 → inference.tasks
inspection.alert.created.v1  → inspection.alerts
```

### 7.2 DB fencing

Worker 收到消息后必须先原子认领数据库任务。Task 保存 `lease_owner`、`fence_token` 和 `lease_expires_at`。重新认领使 token 单调递增；只有当前 token 持有者能够写成功、失败或重试结果。

Redis ACK 只能发生在数据库事务提交之后。ACK 失败可能造成重复投递，但唯一约束与 DB fencing 防止重复业务结果。

`XAUTOCLAIM` 由实际执行推理的 Worker 调用，将长时间未 ACK 的 Pending 消息转移给自身。Redis ownership 只恢复投递，最终执行权仍由 PostgreSQL fencing 判定。

## 8. 背压与调度

- 每路摄像头最多允许 1 个 RUNNING 和 2 个 READY 任务。
- 采集与推理解耦，默认 10 FPS 输入、2 FPS 推理采样。
- 窗口满时使用 latest-frame-wins：最旧 READY 任务变为 `SKIPPED_BACKPRESSURE`，保留最新帧。
- Worker 执行前再次检查帧年龄；端到端年龄超过 2 秒变为 `SKIPPED_STALE`，不计入模型失败率。
- 已经 RUNNING 的普通任务不被新帧强制取消。
- 高危事件触发的证据帧和复核任务不按普通帧过期规则丢弃。
- 同一摄像头默认顺序处理，不同摄像头可并行。
- Redis 或 Worker 不可用时停止持续上传普通帧，只保留健康状态和有限证据，不形成无界积压。

Task Application Service 在创建新 Task 的事务中锁定摄像头调度状态并淘汰最旧 READY 任务。已被淘汰任务对应的旧 Stream 消息由 Worker 读取终态后直接 ACK。

## 9. 执行与故障语义

### 9.1 正常流程

```text
XREADGROUP
→ PostgreSQL fenced claim
→ create InferenceAttempt
→ freshness check
→ load and verify Artifact
→ ONNX / Mock inference
→ Inspection Domain Service
→ fenced transaction COMMIT
→ XACK
```

无缺陷同样是 `SUCCEEDED`，但不创建 Case 或 Alert。

### 9.2 Worker 恢复

- 提交前崩溃：消息留在 PEL，lease 到期后由其他 Worker `XAUTOCLAIM` 并重新执行。
- 提交后、ACK 前崩溃：消息再次投递，Worker 看到任务已终态后 ACK，不创建第二个结果。
- 旧 Worker 恢复后提交：token 或 lease 校验失败。
- 建议 lease 20 秒、每 5 秒续租、推理硬超时 10 秒；PEL idle threshold 不小于 20 秒。

### 9.3 失败分类

- `STALE/BACKPRESSURE`：正常跳帧，不重试，不计模型错误。
- `RETRYABLE_INFRA`：MinIO 暂时不可用、运行时瞬时失败等，指数退避重试。
- `INVALID_INPUT`：哈希不匹配、格式损坏、Schema 非法，直接进入死信。
- `MODEL_CONFIGURATION`：模型摘要不匹配或版本缺失，停止对应消费并触发 P0 告警，避免重试风暴。
- `DATABASE_CONFLICT`：事务回滚且不 ACK，由 PEL 和 lease 恢复。
- 三次可重试失败后进入 `DEAD_LETTER`。

### 9.4 恢复职责

Worker Recovery Loop 负责 Redis 投递恢复。Recovery Scheduler 负责失效旧 lease、到期重试、死信统计、卡住的 Outbox claim 和 Artifact 对账。Scheduler 对任务执行 guarded DB transition 并创建新的 Outbox，但不成为 Redis inference consumer。

## 10. 实时告警与补偿

```text
AlertOutbox
→ Outbox Relay
→ inspection.alerts
→ Realtime Gateway
→ WebSocket
```

WebSocket 是 best-effort 实时提示；PostgreSQL Case/Alert 是事实来源。Gateway 推送本机连接后 ACK Redis 告警消息。每个 Gateway 实例使用独立、短生命周期的 Alert Stream consumer group，从创建时刻读取新告警，不与其他实例使用同一负载均衡组。

客户端保存数据库业务游标，重连后调用 `GET /cases?after=<cursor>` 恢复遗漏数据。重复通知允许出现，前端按 `alert_id` 去重。慢客户端被主动断开，并通过 REST 恢复。

## 11. API 与事件契约

### 11.1 REST 与 WebSocket

```text
POST /api/v1/inspection-sessions
POST /api/v1/inspection-sessions/{id}:stop
GET  /api/v1/inspection-sessions
GET  /api/v1/inspection-sessions/{id}

GET  /api/v1/inference-tasks
GET  /api/v1/inference-tasks/{id}
POST /api/v1/inference-tasks/{id}:replay

GET  /api/v1/cases?after=<cursor>&limit=<n>
GET  /api/v1/cases/{id}
GET  /api/v1/artifacts/{id}/evidence-url

WS   /api/v1/realtime/alerts
```

- 创建检测会话要求 `Idempotency-Key`。
- `source_uri` 不保存 RTSP 密码，只引用加密 `secret_ref`。
- Dead Letter replay 创建新任务和新 Outbox，不改写历史 Attempt。
- WebSocket 使用短时 ticket 或安全 Cookie，不在查询参数传长期 JWT。

### 11.2 事件信封

```json
{
  "event_id": "uuid",
  "event_type": "vision.inference.requested.v1",
  "schema_version": 1,
  "occurred_at": "RFC3339",
  "correlation_id": "uuid",
  "organization_id": "uuid",
  "aggregate_id": "uuid",
  "payload": {}
}
```

`event_id` 使用稳定 `outbox_id`。推理请求 payload 默认只包含 `task_id`，Worker 从 PostgreSQL 读取权威数据。消息不携带二进制、预签名 URL、凭据或完整租户配置。未支持的 Schema 版本不 ACK，并产生兼容性告警与隔离记录。

告警事件 `inspection.alert.created.v1` 携带 `alert_id`、Case/Event/Camera ID、缺陷类型、严重级别、置信度、发生时间与业务游标，不携带证据 URL。

## 12. 权限与安全

- 质检员查看授权产线告警、工单和证据。
- 班组长管理检测会话、查看任务诊断并执行死信 replay。
- 管理员管理授权范围内的模型配置和审计查询。
- 仓储层强制注入 `organization_id` 与资源范围。
- Replay、会话启停和模型切换写哈希链审计。
- 暂停产线仍为模拟操作，并沿用 5 分钟二次认证策略。
- Redis 使用 ACL；Worker 只消费 inference Stream，Gateway 只消费 alert Stream。
- API、Worker、Relay 和 Scheduler 使用不同 PostgreSQL 角色。
- MinIO 使用最小权限服务账号；证据预签名 URL 默认有效 60 秒。
- 生产连接使用 TLS；日志脱敏凭据、查询串和图像内容。

## 13. 可观测性

Frame、Artifact、Task、Outbox、Attempt、Result、Event、Case、Alert 和 WebSocket 使用同一 `correlation_id`。结构化日志与 Trace 贯穿 HTTP、数据库、Redis、MinIO 和 Worker。

核心指标包括：

- 输入/采样 FPS、帧年龄、过期与背压跳帧率。
- Artifact 状态、孤儿数量、上传与校验耗时。
- Outbox 待发布数量、最老记录年龄、失败与重试。
- Stream 长度、PEL 数量和最大 Pending idle。
- Worker 数量、lease 过期、fencing 拒绝、推理吞吐与延迟。
- 按模型版本统计成功率、错误率和置信度分布。
- 告警端到端延迟、缺陷率与重复工单抑制数量。
- WebSocket 连接、断连和慢客户端数量。

Docker Compose 集成 Prometheus、Grafana 和 Alertmanager，提供版本化仪表盘。死信大于 0、模型配置错误、Outbox 最老记录超过 30 秒、Worker/Pending 异常、Artifact 长期 Pending 和告警 P95 超标均触发告警。

## 14. 测试与验收

### 14.1 自动化测试

- 单元测试：状态转换、fencing、幂等、错误分类和帧淘汰。
- 属性测试：任意重复投递与执行顺序下，同一 Result 最多生成一个 Event。
- 集成测试：真实 PostgreSQL、Redis Streams、MinIO 和 Outbox Relay。
- 模型契约测试：Mock 与 ONNX Adapter 输出同一 Schema。
- 前端 E2E：固定视频到告警、工单、证据和人工处置。
- 安全测试：跨租户 Task、Case、Artifact 和 WebSocket 访问全部拒绝。

故障注入至少覆盖：提交前崩溃、提交后 ACK 前崩溃、Relay 发布后更新失败、Redis 中断、MinIO/DB Saga 中断、重复消息、旧 Worker 迟到提交、积压跳帧和服务重启恢复。

### 14.2 门禁

PR 门禁包括 Mock 完整 E2E、ONNX Smoke、4 路短时确定性负载、30 秒恢复和告警 P95 小于 2 秒。

Release/作品集验收使用真实 ONNX、纯 CPU、4 路视频连续运行 10 分钟，并输出吞吐、延迟、跳帧、资源利用率和恢复时间报告；不得出现持续增长积压或重复工单。

## 15. 部署形态

Docker Compose 包含 API/Realtime Gateway、Frame Ingestor、Outbox Relay、两个 Inference Worker、Recovery Scheduler、PostgreSQL、Redis、MinIO、Prometheus、Grafana 和 Alertmanager。

Worker 只有在模型加载、摘要校验和依赖检查通过后才 Ready。优雅关闭先停止领取新消息；未完成任务不错误 ACK，由 lease 与 PEL 机制恢复。

代码继续保持端口/适配器和模块化单体仓库结构，但 Ingestor、Relay、Worker 与 Scheduler 以独立进程运行。P1 不拆成多个代码仓库。

## 16. 非目标

P1 不包含：

- Kafka 或 Celery。
- GPU/TensorRT 生产优化。
- 单机 50 路摄像头承诺。
- 真实产线控制或真实停线。
- 模型训练平台和自动标注闭环。
- 视频全量长期归档。
- 完整企业管理后台，只提供检测链路所需运维诊断页面。
- WebSocket exactly-once 或 Redis 业务事实源。

## 17. 实施原则

以垂直切片推进：先交付固定视频、Mock 推理、持久任务、告警和工单的可恢复闭环，再接入 ONNX、RTSP、完整故障注入、指标与负载门禁。每个切片必须保持 Docker Compose 可运行并提供确定性测试证据。
