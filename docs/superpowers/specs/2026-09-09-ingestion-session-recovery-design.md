# 摄取会话崩溃恢复设计

状态：设计待用户审查；不代表功能已实现。

## 1. 目标与现状

在摄取进程退出、重启或与数据库短暂断连后，自动恢复仍被用户要求运行的会话。
PostgreSQL 是所有权事实源；Redis Worker 心跳仍只表示推理能力，不参与摄取所有权。

现有实现只认领 START_REQUESTED，且以可复用的 process_id 保护心跳。
崩溃后 RUNNING 无人接管；源对象的序号从 0 开始，重建后会重复；
reserve 校验会话而 complete_upload 与 reconciliation 可能在所有权变更后完成旧上传。
因此不能仅增加“心跳超时后改回 START_REQUESTED”的扫描。

验收目标：租约过期后至多一个新所有者；旧所有者不能新增有效帧任务；
停止请求不会被恢复路径重启；历史证据与工单不被改写。

## 2. 方案选择

采用同一会话上的数据库租约 + 单调递增 generation + 序号高水位。
不使用 Redis 分布式锁，也不在恢复时创建新的用户会话。
新建会话会分裂既有停止接口、幂等键和业务历史；仅更换 process_id 则不能隔离
同名进程重启和上传期间发生的所有权变更。

## 3. 数据与不可变约束

inspection_sessions 新增：

- owner_instance_id：每次进程启动生成的 UUID，与运维 process_id 分开。
- ingestion_generation：非负 BIGINT；每次初次认领或接管原子加一。
- lease_expires_at：数据库租约截止时间，未认领为空。
- last_reserved_sequence：非负 BIGINT，记录曾成功持久化的最高帧序号，禁止倒退。

frame_artifacts 新增 ingestion_generation，记录上传属于哪次认领。
保留现有 (organization_id, camera_id, stream_session_id, frame_sequence) 唯一约束。
认领返回不可变 claim：组织、相机、会话、实例 ID、generation、序号起点及源配置。
process_id 只用于诊断，不能独自授权写入。

## 4. 时间、认领与恢复

默认每 5 秒续约，租约 30 秒，轮询 1 秒；配置要求租约至少为续约间隔的三倍。
生产时间由 PostgreSQL clock_timestamp() 提供，在获得所需行锁之后取值，
避免事务启动时间或锁等待前的时间被用于授权。测试可注入时钟。

claim 在一个短事务中认领 START_REQUESTED 或租约已到期的 RUNNING，
使用 FOR UPDATE SKIP LOCKED，受本实例剩余容量限制。
空租约的旧 RUNNING 按迁移后的失效所有权处理。候选按更新时间、会话 ID 排序。
只查询候选不授予权限：取得锁后重新核验状态和截止时间，再推进 generation、
设置实例 ID、租约和心跳。正常续约不能改变 generation。

续约必须匹配组织、会话、实例、generation、RUNNING 和未到期租约；
在截止时刻及之后拒绝续约。旧进程即使恢复连通也不能自行复活。
竞争判定以行锁内的状态和数据库时间为准，而不是客户端观察时间。

## 5. 锁顺序与写入隔离

新增跨资源事务遵循 camera anchor → session → artifact → task/outbox 的顺序。
认领、续约和用户停止操作仅锁 session，不得随后获取 camera 锁。
涉及清理 reservation 的恢复逻辑必须另开遵循统一锁顺序的事务，不能在
已锁 session 的认领事务中追加反向 camera 锁。

SelectedFrame 与 AdmissionRequest 携带 claim。reserve 在 camera/session 锁内验证
租户、相机、会话、实例、generation、RUNNING、未到期租约，再持久化 PENDING。
失权返回明确的 INGESTION_LEASE_LOST，不上传对象。

complete_upload 必须重新验证调用者 claim、artifact generation 和当前会话租约；
检查与创建 READY/outbox 在同一事务内完成。预检成功不能代替提交时授权。
所有权限字段由内部 claim 传递，不能由 HTTP 任意指定。

补偿进程不伪装成旧所有者：它依据数据库 artifact generation 与当前有效会话
核验是否仍可晋升。旧 generation、已停止或租约过期的 PENDING 不得生成任务；
以明确原因终结该 PENDING，仅清理仍指向该 artifact 的 reservation。
已上传对象进入现有可重试补偿清理流程，保留核对信息；不能误删 AVAILABLE
证据或清除新所有者的 reservation。fail_upload 同样不得影响新一代的对象/预约。

已在租约有效且停止请求提交前完成晋升的 READY/RUNNING 推理任务允许完成，
继续受现有 task fencing 保护；停止摄取不等于撤销已受理的业务事件。

## 6. 序号与源恢复语义

认领读取 last_reserved_sequence，源对象以该值作为计数初值，第一帧使用初值 + 1。
reserve 与高水位更新同事务提交；回滚不推进高水位，已提交值不因 artifact 清理倒退。
重试已有 reservation 时仍检查 generation、内容哈希和现有幂等约束，不分配新序号。
新帧必须大于高水位；不同事务不能以同一序号插入不同内容。

记录视频恢复后从头播放，以新采集时间和不重复序号进入实时链路；不承诺视频
播放位置的 exactly-once。RTSP 重新连接到当前流，不补历史丢帧。
本地设备或容器内路径只在接管实例可访问时恢复；首版明确要求同质部署、共享
录制素材路径和可访问摄像头。源无法打开沿用 FAILED，不无限循环掩盖配置错误。
不宣称支持任意异构节点上的本地设备迁移。

## 7. 停止、异常与进程生命周期

用户请求停止后状态为 STOP_REQUESTED，立即禁止新 reserve/promotion/续约。
当前所有者取消摄取并安全关闭源；匹配 claim 后完成 STOPPED。
对于无人拥有或租约已过期的 STOP_REQUESTED，任何实例可在锁内终结为 STOPPED，
但绝不重新打开源。若 stop 与 takeover 竞争，先取得锁的转换完成后，后一方
必须重新读状态；已经提交的 stop 不能被后续 claim 覆盖。

STOPPED 表示数据库不再接受该会话新帧，不保证失联机器上的原生 read 已物理退出。
旧进程再次访问数据库会被拒绝；它对失权/停止的迟到 mark_failed 不得覆盖新状态。

心跳失败或异常必须终止该会话的摄取协程，不能只让心跳任务静默结束。
取消 asyncio.to_thread 不会终止底层上传，因此最终写入 fencing 是必要条件。
原生 VideoCapture 的 read/release 继续串行化；有界 RTSP read 是独立后续工作，
本项不通过并发 release 绕过阻塞。SIGKILL 由租约兜底；正常退出在源关闭后按 claim
条件释放租约，保留 RUNNING 供接管，不能把用户停止状态改回 RUNNING。

## 8. 迁移、权限与部署

新增迁移不修改历史业务事件；高水位由现存 artifact 最大序号回填，空集合为 0。
历史 artifact generation 为 0；新认领从 1 起，历史 PENDING 不授予新所有者权限。
旧 RUNNING 以过期租约等待新版进程接管；STOP_REQUESTED 只终结，不恢复。

此次升级要求先停止所有旧版摄取/补偿进程，再迁移并统一启动新版；
不支持新旧协议混跑，因为旧二进制不具备 generation 检查。
迁移后不直接降级旧版：需停止服务并评估恢复数据库备份或前向修复。
沿用最小数据库角色，仅赋予必要字段和操作，不新增跨租户 API 查询权限。

## 9. 可验证验收

单元/SQLite 测试验证协议分支；并发证明必须使用真实 PostgreSQL 独立连接/事务。

1. 初次认领、正常续约、截止边界、未过期拒绝接管。
2. 两实例并发抢过期会话：仅一个获得新 generation；同名 process_id 也被隔离。
3. 旧实例迟到 heartbeat、reserve、complete_upload、mark_failed 均不能影响新所有者。
4. 预约成功后发生接管，旧上传及补偿均不得生成 READY/outbox；新预约不被旧清理释放。
5. 停止与认领/完成上传分别竞争；停止提交后不得开启新有效写入。
6. 续约异常确实停止摄取；关闭阻塞不削弱数据库隔离。
7. 重启后序号增长、清理历史 artifact 后仍不回退，同租户多相机及跨租户均隔离。
8. 迁移验证历史高水位回填、旧 RUNNING 接管、旧 STOP_REQUESTED 终结和数据库角色。
9. 独立 Compose 演练：启动视频 → 获得证据 → SIGKILL 摄取进程 → 启动新版实例 →
   同会话 generation 增长并出现更高序号的新结果 → 请求停止且不再恢复。

CI 保留现有录制视频浏览器必跑验收；新增 PostgreSQL 并发和恢复演练不能以 skip
记为通过。演练使用独立测试项目和已提交的合成模型，不操作用户其他容器。
通过本项只证明恢复与隔离协议，不代表真实检测准确率、无丢帧、容量 SLO 或
原生 I/O 有界关闭已验收。

## 10. 实施边界

主要涉及 session schema/port/repository、摄取循环、source 计数初值、Artifact Saga、
task_control 的 admission/promotion/reconciliation，以及迁移、测试和运维文档。
不扩展产品目录、不重构 RAG、不更换 Redis 分发架构。
设计审查通过后另写实施计划，按“租约 → 写入 fencing → 生命周期 → 故障演练”交付。
