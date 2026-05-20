# TODD：历史交接快照

> 事实源迁移说明（2026-05-20）：本文件不再作为当前任务状态、阻塞项或验收结论的事实源。AIops MVP 的实时协作状态以 Multica issue 为准：父 issue `AIO-12` 承载总体目标和协作规则，`AIO-13` 至 `AIO-18` 承载剩余 MVP 工作项、阻塞和验收结论。
>
> 本文件保留为历史交接快照，用于理解 2026-05-19 之前的现场状态、验证记录和重要决策。后续开发完成、阻塞、验收结果和剩余风险必须回写对应 Multica issue；只有长期产品、架构、测试或部署知识变化时，才更新仓库文档。

## Multica issue 映射

| 历史状态项 | 当前事实源 |
| --- | --- |
| AIops MVP 总体目标、成功定义、协作规则 | `AIO-12` |
| 真实飞书原生审批中心、事件订阅、polling 端到端验收 | `AIO-13` |
| 真实 Kubernetes 审批后安全执行链路验收 | `AIO-14` |
| `rollback_required` 与确定性 rollback 闭环补齐 | `AIO-15` |
| K8s 部署、runtime config、PVC 持久化真实环境验收 | `AIO-16` |
| 本地状态文档迁移到 Multica issue 事实源 | `AIO-17` |
| 真实飞书群/线程与审批可见性验收 | `AIO-18` |

## 历史角色

dev-lead-agent

## 历史阶段

飞书原生审批接入已登记 CR，等待产品/架构评审后由子 agent 实现、测试和审查。

## 历史目标

把审批主路径从自定义飞书审批卡片切到飞书原生审批：创建本地 approval 后创建飞书审批实例，飞书审批事件或 polling 补偿同步本地状态，只有本地 `approved` 才能进入执行。
当前现场排查还发现：`hooks/alert_webhook.py` 和 `toolsets/approval_async.py` 需要优先读取 `HERMES_CONFIG` / `HERMES_HOME`，否则 Pod 内 runtime 配置会被误读成仓库根 `config.yaml`，导致 native approval 退回旧卡片流程。

## 历史变更请求

CR-2026-05-15-001：接入飞书原生审批
CR-2026-05-19-001：统一 Hermes 运行时配置文件

## 已完成

- 2026-05-09：补齐容器/K8s Feishu 群消息策略配置，默认 `FEISHU_GROUP_POLICY=open`，避免群聊 @ 消息被 allowlist 空配置丢弃。
- 2026-05-11：将 Hermes 状态默认目录切到 `/data/hermes`，AIOps 状态默认目录切到 `/data/aiops`，覆盖 pairing、skills、memory 和本地 SQLite/JSON 状态。
- 2026-05-11：CR-2026-05-11-002 完成审批卡片强制投递、`approval_message_id` 回写、旧 pending 补发、sent outbox 补回写和自动化验证。
- 2026-05-19：CR-2026-05-19-001 完成运行时配置单一来源修复，删除仓库根 `config.yaml`，相关 hooks/runtime/toolsets 统一读取 `HERMES_CONFIG -> HERMES_HOME/config.yaml`。

## 历史进行中（已迁移至 Multica issue）

- CR-2026-05-11-001 仍需 architect-agent 复核部署路径，并做真实集群滚动重启验证。
- CR-2026-05-11-002 已完成本地自动化闭环；剩余真实飞书群/线程端到端验收。
- CR-2026-05-15-001 已完成本地实现、验证和复审；剩余真实飞书审批中心、事件订阅和 polling 端到端验收。

以上状态已迁移到 `AIO-13`、`AIO-16`、`AIO-18`。后续以 issue 状态和评论中的验收结论为准。

## 历史未开始（已迁移至 Multica issue）

- 真实飞书审批中心端到端验收。
- 真实集群滚动重启后的 PVC 持久化验收。

以上状态已迁移到 `AIO-13` 和 `AIO-16`。

## 历史阻塞项

- 无。

## 最近决策

| 日期 | 决策 | 负责人 | 来源 |
| --- | --- | --- | --- |
| 2026-05-09 | 容器默认 `FEISHU_GROUP_POLICY=open`，生产如需限制用户可改为 `allowlist` 并配置 `FEISHU_ALLOWED_USERS`。 | dev-lead-agent | CR-2026-05-09-001 |
| 2026-05-11 | 单个 PVC 挂载 `/data`，Hermes 状态落 `/data/hermes`，AIOps 状态落 `/data/aiops`。 | dev-lead-agent | CR-2026-05-11-001 |
| 2026-05-11 | 审批可见性必须以审批记录、飞书审批卡片发送成功、`approval_message_id` 回写完成为闭环；sent outbox 可用于补回写避免重复发卡。 | dev-lead-agent | CR-2026-05-11-002 |
| 2026-05-15 | 飞书原生审批将成为主审批入口；飞书只决定人类批准与否，AIOps 本地 approval 状态机和 execution worker 仍是执行权威。 | dev-lead-agent | CR-2026-05-15-001 |
| 2026-05-19 | 运行时配置必须单一来源：`HERMES_CONFIG` 优先，其次 `HERMES_HOME/config.yaml`；仓库根 `config.yaml` 不再作为运行时配置来源。 | dev-lead-agent | CR-2026-05-19-001 |

## 历史下一步（请以 Multica issue 为准）

1. 对 CR-2026-05-15-001 做真实飞书审批中心、事件订阅和 polling 端到端验收。
2. 对 CR-2026-05-11-002 做真实飞书群/线程端到端验收，确认审批卡片实际可见。
3. 复核真实环境验收结果，必要时补 product-domain-agent / architect-agent 评审。
4. 按真实环境结果回写对应 Multica issue；如形成长期产品、架构、测试或部署知识，再更新仓库文档。
5. 对 CR-2026-05-11-001 补 architect-agent review，并在真实集群验证 pod 重启后 pairing、skills、memory 保留。

这些历史下一步已拆分到 `AIO-13`、`AIO-16` 和 `AIO-18`；真实 Kubernetes 执行与 rollback 验收由 `AIO-14`、`AIO-15` 承接。

## 验证记录

| 日期 | 命令 / 检查 | 结果 | 说明 |
| --- | --- | --- | --- |
| 2026-05-09 | `rtk python3 -m pytest tests/test_deploy_entrypoint.py tests/test_k8s_manifests.py -q` | 9 passed | 验证容器入口渲染 Feishu 群消息默认策略，K8s ConfigMap 暴露策略 env。 |
| 2026-05-11 | `pytest tests/test_data_dir_env.py tests/test_deploy_entrypoint.py tests/test_k8s_manifests.py tests/test_cost_guard.py tests/test_rejection_learner.py -q` | 21 passed | 验证 Hermes/AIOps 持久化路径切到 PVC 目录。 |
| 2026-05-11 | `python3 -m pytest tests/test_approval_async.py tests/test_feishu_conversation.py tests/test_feishu_approval_overlay.py tests/test_alert_webhook.py tests/test_recovery.py tests/test_message_delivery.py -q` | 68 passed, 14 warnings | 验证审批卡片投递、回写、旧 pending 补发、Alertmanager 顺序、startup recovery 和 sent outbox 补回写。 |
| 2026-05-18 | `tests/test_alert_webhook_server.py tests/test_data_dir_env.py`; `tests/test_approval_async.py`; 主目标 focused tests；YAML 解析 | 3 passed; 27 passed; 95 passed, 15 warnings; YAML 通过 | 验证外部审批边界、`toolsets` import shadowing 修复、飞书原生审批 focused 流程和配置模板。 |
| 2026-05-19 | `deploy/entrypoint.sh` / `deploy/hermes-config.template.yaml` / `deploy/k8s/configmap.yaml` 配置检查 | 已更新，待镜像构建验证 | 容器启动时会渲染 `FEISHU_APPROVAL_CODE`、`FEISHU_APPROVAL_ENABLED` 和 `FEISHU_APPROVAL_POLLING_ENABLED` 到 `HERMES_CONFIG`。 |
| 2026-05-19 | 飞书原生审批真实触发排查 | 已补配置，待镜像构建验证 | `create_approval_instance(...)` 还需要 `requester_open_id`；部署模板现在会渲染 `FEISHU_APPROVAL_REQUESTER_OPEN_ID`。 |
| 2026-05-19 | `rtk python3 -m pytest tests/test_alert_webhook.py tests/test_approval_async.py tests/test_approval_reply.py tests/test_identity_extended.py tests/test_permission_guard.py tests/test_feishu_approval_config.py tests/test_hermes_entry.py tests/test_feishu_approval_overlay.py -q` | 115 passed, 14 warnings | 验证运行时配置统一为 `HERMES_CONFIG -> HERMES_HOME/config.yaml`，删除 repo-root `config.yaml` 后无回退依赖，native approval 分支和 overlay 授权配置回归通过。 |
| 2026-05-19 | 静态扫描旧配置入口 | 通过，运行时代码无 repo-root fallback | `config.yaml` 已删除；运行时代码未发现 `_project_root()/config.yaml`、`parents[1]/config.yaml` 或 `HERMES_CONFIG_PATH` 旧入口残留。 |
