# TODD：项目状态看板

本文件是 agent 交接和当前开发状态账本。功能状态变化时，必须同步 `docs/development-progress.md`。

## 当前角色

dev-lead-agent

## 当前阶段

已实现并通过 focused tests；待 architect-agent review

## 当前目标

把 Hermes 核心状态、skills、memory、pairing、auth 和 AIOps 本地状态统一落到 `/data` 下的 PVC 目录。

## 当前变更请求

CR-2026-05-11-001：将 Hermes 核心状态迁移到 PVC

## 已完成

- 2026-05-09：补齐容器/K8s Feishu 群消息策略配置，默认 `FEISHU_GROUP_POLICY=open`，避免群聊 @ 消息被 allowlist 空配置丢弃。
- 2026-05-11：将 Hermes 状态默认目录切到 `/data/hermes`，AIOps 状态默认目录切到 `/data/aiops`，覆盖 pairing、skills、memory 和本地 SQLite/JSON 状态。

## 进行中

- CR-2026-05-11-001 仍需 architect-agent 复核部署路径，并做真实集群滚动重启验证。

## 未开始

- 待补充。

## 阻塞项

- 无。

## 最近决策

| 日期 | 决策 | 负责人 | 来源 |
| --- | --- | --- | --- |
| 2026-05-09 | 容器默认 `FEISHU_GROUP_POLICY=open`，生产如需限制用户可改为 `allowlist` 并配置 `FEISHU_ALLOWED_USERS`。 | dev-lead-agent | CR-2026-05-09-001 |
| 2026-05-11 | 单个 PVC 挂载 `/data`，Hermes 状态落 `/data/hermes`，AIOps 状态落 `/data/aiops`。 | dev-lead-agent | CR-2026-05-11-001 |

## 下一步

1. 使用 `product-domain-agent` 补全 `docs/00-PDD.md`、`docs/01-BDD.md` 和 `docs/02-DDD.md`。
2. 使用 `architect-agent` 产出 `docs/03-SDD.md`。
3. 使用 `dev-lead-agent` 更新实施计划和 TDD 测试计划。
4. 当前任务：补 architect-agent review，并在真实集群验证 pod 重启后 pairing、skills、memory 保留。
5. `dev-lead-agent` 先读取 `using-superpowers`，自动规划子 agent 工作流。
6. 开发任务由 `implementation-agent`、`test-agent`、`review-agent` 分别实现、验证和审查。

## 验证记录

| 日期 | 命令 / 检查 | 结果 | 说明 |
| --- | --- | --- | --- |
| 2026-05-09 | `rtk python3 -m pytest tests/test_deploy_entrypoint.py tests/test_k8s_manifests.py -q` | 9 passed | 验证容器入口渲染 Feishu 群消息默认策略，K8s ConfigMap 暴露策略 env。 |
| 2026-05-11 | `pytest tests/test_data_dir_env.py tests/test_deploy_entrypoint.py tests/test_k8s_manifests.py tests/test_cost_guard.py tests/test_rejection_learner.py -q` | 21 passed | 验证 Hermes/AIOps 持久化路径切到 PVC 目录。 |
