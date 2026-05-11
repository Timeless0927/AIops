# 变更请求

本文件是三个角色之间的共享通信总线。

## 状态值

- `draft`
- `needs-product-review`
- `needs-architecture-review`
- `approved`
- `in-progress`
- `done`
- `rejected`
- `deferred`

## 入口规则

`dev-lead-agent` 必须在修改代码前，把所有会改变行为的用户反馈登记到这里。

## 模板

```markdown
## CR-YYYY-MM-DD-NNN：简短标题

状态：draft
负责人：dev-lead-agent
创建日期：YYYY-MM-DD

### 用户反馈

待补充。

### 初筛影响

- PDD：无 / 可能 / 必须
- BDD：无 / 可能 / 必须
- DDD：无 / 可能 / 必须
- SDD：无 / 可能 / 必须
- TDD：无 / 可能 / 必须
- TODD：必须

### 必要评审

- product-domain-agent：不需要 / 需要 / 已批准 / 已拒绝
- architect-agent：不需要 / 需要 / 已批准 / 已拒绝

### 决策

待补充。

### 影响文件

- 待补充。

### 验收标准

- [ ] 待补充。

### 验证

待补充。
```

## 待处理请求

## CR-2026-05-09-001：容器部署补齐 Feishu 群消息策略

状态：done
负责人：dev-lead-agent
创建日期：2026-05-09

### 用户反馈

本地启动 Hermes 后在飞书群里 @ 机器人可以响应；Docker 打包部署后不响应，怀疑配置没有传入容器。

### 初筛影响

- PDD：无
- BDD：无
- DDD：无
- SDD：无
- TDD：必须
- TODD：必须

### 必要评审

- product-domain-agent：不需要
- architect-agent：不需要

### 决策

这是部署配置传播缺口：Hermes Feishu adapter 默认 `FEISHU_GROUP_POLICY=allowlist` 且 `FEISHU_ALLOWED_USERS` 为空，会丢弃群消息。容器入口和 K8s ConfigMap 默认设置 `FEISHU_GROUP_POLICY=open`，保持与本地可 @ 响应行为一致；生产可改回 `allowlist` 并配置 `FEISHU_ALLOWED_USERS`。

### 影响文件

- `deploy/entrypoint.sh`
- `deploy/hermes-config.template.yaml`
- `deploy/k8s/configmap.yaml`
- `docs/user-guide.md`
- `docs/development-progress.md`
- `docs/TODD.md`
- `tests/test_deploy_entrypoint.py`
- `tests/test_k8s_manifests.py`

### 验收标准

- [x] 容器入口默认导出 `FEISHU_GROUP_POLICY=open`。
- [x] K8s ConfigMap 显式包含 `FEISHU_GROUP_POLICY` 和 `FEISHU_ALLOWED_USERS`。
- [x] 渲染后的 Hermes runtime config 明确记录群消息默认策略。
- [x] focused 部署测试通过。

### 验证

2026-05-09：`rtk python3 -m pytest tests/test_deploy_entrypoint.py tests/test_k8s_manifests.py -q`，9 passed。

## CR-2026-05-11-001：将 Hermes 核心状态迁移到 PVC

状态：in-progress
负责人：dev-lead-agent
创建日期：2026-05-11

### 用户反馈

镜像部署后 Feishu pairing、skills 安装、memory 和记忆状态在 pod 重启后丢失；希望把需要持久化的 Hermes 状态统一落到 PVC。

### 初筛影响

- PDD：无
- BDD：无
- DDD：无
- SDD：必须
- TDD：必须
- TODD：必须

### 必要评审

- product-domain-agent：不需要
- architect-agent：需要

### 决策

采用单个 PVC 挂载 `/data`，将 AIOps SQLite 统一放到 `/data/aiops`，将 Hermes 核心状态放到 `/data/hermes`，并把默认 `HERMES_HOME` / `HERMES_CONFIG` / `AIOPS_DATA_DIR` 切到这两个目录。这样 pairing、auth、session、skills、memory、AIOps 本地状态都会随 pod 重启保留。

### 影响文件

- `Dockerfile.aiops`
- `deploy/entrypoint.sh`
- `deploy/k8s/configmap.yaml`
- `deploy/k8s/README.md`
- `docs/user-guide.md`
- `docs/development-progress.md`
- `docs/TODD.md`
- `tests/test_data_dir_env.py`
- `tests/test_deploy_entrypoint.py`
- `tests/test_k8s_manifests.py`
- `toolsets/cost_guard.py`
- `toolsets/rejection_learner.py`

### 验收标准

- [x] Dockerfile 与 K8s ConfigMap 的默认路径都指向 `/data/hermes` 和 `/data/aiops`。
- [x] Hermes pairing、auth、session、skills、memory 路径随 `HERMES_HOME` 落到 PVC。
- [x] `cost_tracking.db` 和 `rejection_lessons.json` 也跟随 `AIOPS_DATA_DIR` 落到 PVC。
- [x] 相关部署测试和数据目录测试通过。

### 验证

2026-05-11：`pytest tests/test_data_dir_env.py tests/test_deploy_entrypoint.py tests/test_k8s_manifests.py tests/test_cost_guard.py tests/test_rejection_learner.py -q`，21 passed。
