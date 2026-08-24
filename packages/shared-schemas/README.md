# packages/shared-schemas — 共享数据契约

Web 后端与 Web 前端之间的版本化 Pydantic API/事件契约。任何跨端数据结构先在这里定义，再被 `apps/web-backend` 消费。

## 内容

- `src/odp_schemas/events.py` — `InspectionAlert`：实时质检告警事件
  （`event_id` / `organization_id` / `camera_id` / `occurred_at` /
  `defect_class` / `confidence`），WebSocket 推送与 REST 补偿共用。

## 约定

- 契约变更需保持向后兼容或显式升版本；前端对应的 TypeScript 类型以这里的 Pydantic 模型为准。
- 本包只放数据结构，不包含任何业务逻辑或基础设施依赖。
