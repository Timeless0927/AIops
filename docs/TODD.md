# TODD：项目状态看板

本文件是 agent 交接和当前开发状态账本。功能状态变化时，必须同步 `docs/development-progress.md`。

## 当前角色

dev-lead-agent

## 当前阶段

飞书原生审批接入已登记 CR，等待产品/架构评审后由子 agent 实现、测试和审查。

## 当前目标

把审批主路径从自定义飞书审批卡片切到飞书原生审批：创建本地 approval 后创建飞书审批实例，飞书审批事件或 polling 补偿同步本地状态，只有本地 `approved` 才能进入执行。

## 当前变更请求

CR-2026-05-15-001：接入飞书原生审批

## 已完成

- 2026-05-09：补齐容器/K8s Feishu 群消息策略配置，默认 `FEISHU_GROUP_POLICY=open`，避免群聊 @ 消息被 allowlist 空配置丢弃。
- 2026-05-11：将 Hermes 状态默认目录切到 `/data/hermes`，AIOps 状态默认目录切到 `/data/aiops`，覆盖 pairing、skills、memory 和本地 SQLite/JSON 状态。
- 2026-05-11：CR-2026-05-11-002 完成审批卡片强制投递、`approval_message_id` 回写、旧 pending 补发、sent outbox 补回写和自动化验证。

## 进行中

- CR-2026-05-11-001 仍需 architect-agent 复核部署路径，并做真实集群滚动重启验证。
- CR-2026-05-11-002 已完成本地自动化闭环；剩余真实飞书群/线程端到端验收。
- CR-2026-05-15-001 已完成本地实现、验证和复审；剩余真实飞书审批中心、事件订阅和 polling 端到端验收。

## 未开始

- 真实飞书审批中心端到端验收。
- 真实集群滚动重启后的 PVC 持久化验收。

## 阻塞项

- 无。

## 最近决策

| 日期 | 决策 | 负责人 | 来源 |
| --- | --- | --- | --- |
| 2026-05-09 | 容器默认 `FEISHU_GROUP_POLICY=open`，生产如需限制用户可改为 `allowlist` 并配置 `FEISHU_ALLOWED_USERS`。 | dev-lead-agent | CR-2026-05-09-001 |
| 2026-05-11 | 单个 PVC 挂载 `/data`，Hermes 状态落 `/data/hermes`，AIOps 状态落 `/data/aiops`。 | dev-lead-agent | CR-2026-05-11-001 |
| 2026-05-11 | 审批可见性必须以审批记录、飞书审批卡片发送成功、`approval_message_id` 回写完成为闭环；sent outbox 可用于补回写避免重复发卡。 | dev-lead-agent | CR-2026-05-11-002 |
| 2026-05-15 | 飞书原生审批将成为主审批入口；飞书只决定人类批准与否，AIOps 本地 approval 状态机和 execution worker 仍是执行权威。 | dev-lead-agent | CR-2026-05-15-001 |

## 下一步

1. 对 CR-2026-05-15-001 做真实飞书审批中心、事件订阅和 polling 端到端验收。
2. 对 CR-2026-05-11-002 做真实飞书群/线程端到端验收，确认审批卡片实际可见。
3. 复核真实环境验收结果，必要时补 product-domain-agent / architect-agent 评审。
4. 按真实环境结果更新 `docs/development-progress.md`、`docs/CHANGE-REQUESTS.md` 和 `docs/TODD.md`。
5. 对 CR-2026-05-11-001 补 architect-agent review，并在真实集群验证 pod 重启后 pairing、skills、memory 保留。

## 验证记录

| 日期 | 命令 / 检查 | 结果 | 说明 |
| --- | --- | --- | --- |
| 2026-05-09 | `rtk python3 -m pytest tests/test_deploy_entrypoint.py tests/test_k8s_manifests.py -q` | 9 passed | 验证容器入口渲染 Feishu 群消息默认策略，K8s ConfigMap 暴露策略 env。 |
| 2026-05-11 | `pytest tests/test_data_dir_env.py tests/test_deploy_entrypoint.py tests/test_k8s_manifests.py tests/test_cost_guard.py tests/test_rejection_learner.py -q` | 21 passed | 验证 Hermes/AIOps 持久化路径切到 PVC 目录。 |
| 2026-05-11 | `python3 -m pytest tests/test_approval_async.py tests/test_feishu_conversation.py tests/test_feishu_approval_overlay.py tests/test_alert_webhook.py tests/test_recovery.py tests/test_message_delivery.py -q` | 68 passed, 14 warnings | 验证审批卡片投递、回写、旧 pending 补发、Alertmanager 顺序、startup recovery 和 sent outbox 补回写。 |
| 2026-05-18 | `tests/test_alert_webhook_server.py tests/test_data_dir_env.py`; `tests/test_approval_async.py`; 主目标 focused tests；YAML 解析 | 3 passed; 27 passed; 95 passed, 15 warnings; YAML 通过 | 验证外部审批边界、`toolsets` import shadowing 修复、飞书原生审批 focused 流程和配置模板。 |
| 2026-05-19 | `deploy/entrypoint.sh` / `deploy/hermes-config.template.yaml` / `deploy/k8s/configmap.yaml` 配置检查 | 已更新，待镜像构建验证 | 容器启动时会渲染 `FEISHU_APPROVAL_CODE`、`FEISHU_APPROVAL_ENABLED` 和 `FEISHU_APPROVAL_POLLING_ENABLED` 到 `HERMES_CONFIG`。 |
