# 企业 AI 质检平台 P0 加固设计

## 1. 目标与边界

本轮只处理阻碍项目成为可信企业级演示的三个 P0 问题：核心业务状态不持久、告警链路不是真实时、认证凭证不安全。保留现有领域模型、API 主路径、React 工作台和 RAG 行为，不在本轮实现管理后台、真实设备控制、模型管理或完整 OpenTelemetry。

验收结果必须同时覆盖两种运行方式：

- Docker/企业模式使用 PostgreSQL 保存业务数据、Redis 保存二次认证标记并承载跨实例实时事件。
- 无 Docker 本机模式使用 SQLite 保存业务数据和事件，仍可执行登录、告警、处置、审计闭环。

## 2. 持久化架构

业务模块仅依赖仓储端口，不直接依赖 SQLAlchemy、PostgreSQL 或 SQLite。新增以下端口：

- `CaseRepositoryPort`：租户范围内查询、加载及带审计的状态流转。
- `AuditRepositoryPort`：锁定组织链头、追加审计记录、读取一致性快照。
- `ActorRepositoryPort`：按 ID/邮箱加载启用的用户及其组织、角色、产线范围。
- `PasswordCredentialPort`：读取密码哈希，不向业务层暴露明文密码。
- `ReauthenticationStorePort`：记录和读取五分钟内的二次认证标记。

SQLAlchemy 2 提供统一模型和事务边界，Alembic 管理迁移。PostgreSQL 与 SQLite 共用领域映射；数据库差异封装在适配器内部。首批表包括：

- `actors`、`actor_line_grants`、`password_credentials`
- `defect_cases`、`inspection_events`、`case_transitions`
- `audit_chain_heads`、`audit_logs`
- `inspection_alerts`、`websocket_tickets`

工单状态变化、工单历史追加、审计链追加必须处于同一数据库事务。PostgreSQL 使用 `SELECT ... FOR UPDATE` 锁定工单与组织审计链头；SQLite 使用 `BEGIN IMMEDIATE` 串行化写入。读取接口必须把 `organization_id` 作为必填条件，非管理员仍需执行产线范围授权。

审计表通过仓储接口只暴露追加和读取；迁移在 PostgreSQL 中撤销应用角色的 `UPDATE/DELETE` 权限。应用启动和每日校验读取持久化审计链，重启后校验状态不得丢失。

## 3. 身份与凭证安全

密码凭证使用 Argon2id 哈希，演示种子只在首次创建账户时把文档中的开发密码转换为哈希。日志、数据库和 API 响应均不得保存或输出明文密码。

JWT 继续使用 HS256，但验证必须满足：

- 算法固定为 HS256；
- `sub` 为已启用用户 UUID；
- `iat`、`exp` 必须存在并为整数时间戳；
- `exp` 必须晚于当前时间，`iat` 不得明显晚于服务器时间；
- 登录访问令牌有效期保持 12 小时。

浏览器 WebSocket 不再携带长期 JWT 查询参数。客户端先用 Bearer JWT 调用 `POST /api/v1/auth/websocket-ticket`，获得随机、单次使用、60 秒过期的票据；随后以该票据建立 WebSocket。Redis 模式使用原子 `GETDEL`/Lua 消费票据，本机模式使用数据库事务消费票据。票据仅映射 actor ID，不包含权限快照，连接建立时重新加载用户授权。

## 4. 实时告警与重连

`InspectionAlertFeedPort` 分成三个明确能力：持久发布、按游标补偿查询、订阅未来事件。发布先保存告警事实，再通知订阅者；通知失败不丢失已持久化事实。

WebSocket 建立后的流程为：

1. 原子消费短时票据并加载用户。
2. 接受连接并记录当前游标。
3. 查询并发送游标之后的授权告警。
4. 阻塞订阅未来事件，逐条执行组织和产线授权后推送。
5. 客户端断开或服务关闭时取消订阅，不产生一秒重连风暴。

Redis 模式使用带游标的阻塞式 `XREAD`，不再对整个 Stream 执行 `XRANGE - +`。Stream 设置近似最大长度，防止无限增长。本机 SQLite 模式保存递增事件 ID，并用进程内条件变量唤醒订阅者；重启后的补偿仍从数据库读取。

客户端维护最后成功处理的事件游标。首次登录可从空游标拉取当前可见告警；重连只补偿新事件。相同 `event_id` 继续去重，传输语义保持 at-most-once，数据库/事件表是最终事实来源。

## 5. 迁移与种子数据

Alembic 基线迁移创建 P0 表，并保留现有知识库 SQL 数据。迁移工具对 PostgreSQL 和 SQLite 均可执行。演示种子以稳定业务键执行 upsert：重复运行或 API 重启不得新增账户、工单、检测事件、凭证或审计记录，也不得重复提升知识文档版本。

现有内存适配器仅保留给小型单元测试，不再由 `create_app()` 的 Docker 或本机运行路径使用。测试可显式注入内存仓储。

## 6. 错误处理与可观测性

- 数据库不可用：健康检查返回非健康状态，业务写入返回 503，不回退到进程内数据。
- Redis 暂时不可用：告警事实仍持久化；WebSocket 关闭并由客户端退避重连，通过游标补偿恢复。
- 票据过期/重复使用：WebSocket 以策略违规码拒绝，不泄露失败细节。
- JWT 过期/格式错误：REST 返回统一 401。
- 审计追加失败：工单事务整体回滚。

访问日志不得记录 JWT 或 WebSocket 票据；nginx 对 WebSocket 路径使用不含查询字符串的日志格式。现有 Prometheus 指标增加活跃连接数、重连次数和补偿事件数。

## 7. 测试与验收

所有行为按测试先行实施。最低验收集：

- SQLite 和 PostgreSQL 仓储契约测试。
- API 重启后登录账户、工单状态、工单历史和审计链仍存在。
- 两个并发状态流转不会破坏工单状态或审计链。
- 已过期、缺少 `exp`、未来 `iat` 的 JWT 被拒绝。
- WebSocket 票据只能使用一次且 60 秒后失效。
- 连接建立后发布的新告警能实时到达，而非只收到历史快照。
- 断线重连只补偿游标之后的告警，不从纪元起点全量重放。
- 真实 Uvicorn + Chrome/Playwright 完成登录、告警、建议、处置和审计闭环。
- 后端、前端、平台全量测试、构建、Ruff 和 Compose 配置检查通过。

## 8. 实施裁决

采用渐进式适配器替换，不重写领域层，不引入事件溯源。P0 按“数据库基础与仓储 → 身份安全 → 实时传输 → 端到端迁移与回归”顺序实施。每个阶段独立提交并接受规格与代码质量审查；全部完成后再交付用户最终审查。
