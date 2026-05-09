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
