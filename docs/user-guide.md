# AIOps SRE Agent 用户手册

## 1. 项目简介

AIOps SRE Agent 是一个面向 Kubernetes 运维场景的飞书 SRE 助手。当前已支持 Alertmanager 告警接入、incident 记录、诊断摘要、飞书 Thread 上下文增强和文本审批状态变更。

当前版本的边界必须明确：审批通过只会更新 approval 状态和 incident timeline，不会自动执行 Kubernetes 写操作。真实 `k8s_write` / `k8s_exec` 自动执行、dry-run、健康检查和 rollback 属于后续阶段。

## 2. 环境准备

基础要求：

- Python 3.11。
- Docker/Kubernetes 部署时需要 `kubectl`。
- 飞书机器人应用凭证。
- 可用的模型 API endpoint。
- Alertmanager 可访问 Agent webhook。

容器部署会通过 `Dockerfile.aiops` 安装父项目和 `hermes-agent[messaging,feishu]`。生产启动入口是 `deploy/entrypoint.sh`。

## 3. 核心环境变量

必须配置：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_MAIN_CHAT_ID`
- `AIOPS_MODEL_BASE_URL`
- `AIOPS_MODEL_API_KEY`

常用可选配置：

- `FEISHU_VERIFICATION_TOKEN`
- `FEISHU_ENCRYPT_KEY`
- `FEISHU_GROUP_POLICY`，默认 `open`，群聊中被 @ 时响应任意用户；如需限制发送人，设为 `allowlist` 并配置 `FEISHU_ALLOWED_USERS`。
- `FEISHU_ALLOWED_USERS`，当 `FEISHU_GROUP_POLICY=allowlist` 时填写允许 @ 机器人的飞书 `open_id`，多个值用英文逗号分隔。
- `HERMES_HOME`，默认 `/data/hermes`，用于持久化 pairing、auth、session、skills 和 memory。
- `HERMES_CONFIG`，默认 `/data/hermes/config.yaml`。
- `AIOPS_MODEL_NAME`，默认 `gpt-5.4`。
- `AIOPS_MODEL_PROVIDER`，默认 `custom`。
- `AIOPS_AGENT_MAX_TURNS`，默认 `90`。
- `AIOPS_DATA_DIR`，默认 `/data/aiops`。
- `AIOPS_WEBHOOK_HOST`，默认 `0.0.0.0`。
- `AIOPS_WEBHOOK_PORT`，默认 `8765`。
- `AIOPS_WEBHOOK_ONLY=1`：只启动 Alertmanager webhook，不启动 Hermes gateway。

## 4. 启动方式

生产入口：

```bash
bash deploy/entrypoint.sh
```

启动流程：

```text
deploy/entrypoint.sh
  |- render ~/.hermes/config.yaml
  |- start hooks.alert_webhook_server
  `- start runtime.hermes_gateway
       |- install Feishu approval overlay
       `- run Hermes gateway
```

本项目不建议在生产中直接运行裸 `hermes gateway`，因为那会绕过父项目 runtime overlay，飞书审批文本不会在 LLM 前被拦截。

只启动 webhook：

```bash
AIOPS_WEBHOOK_ONLY=1 bash deploy/entrypoint.sh
```

## 5. 日常使用

### 5.1 普通文字消息

在飞书中向机器人发送普通文本，例如：

- `看下 default 命名空间的 pod 状态`
- `继续排查这个告警`
- `总结一下当前 incident`

普通文本会进入 Hermes 原始消息流程，由 Agent 结合当前上下文处理。

### 5.2 Thread 上下文

Alertmanager 创建 incident 后，系统会把 Feishu chat/thread/message 标识绑定到 incident。后续在同一 Thread 中继续提问时，`voice_context` 会优先注入绑定 incident 的状态、分析摘要和最近 timeline，而不是依赖完整聊天记录。

### 5.3 文本审批

当前支持两条精确命令：

```text
批准 <approval_id>
拒绝 <approval_id> <reason>
```

示例：

```text
批准 ap-1
拒绝 ap-2 风险过高
```

处理结果：

- 成功批准：`审批已批准：ap-1`
- 成功拒绝：`审批已拒绝：ap-2`
- 未知审批：`审批处理失败：审批记录不存在`
- 无法识别审批人：`审批处理失败：无法识别审批人身份`

注意：当前审批通过不会自动执行修复命令。它只更新 approval 状态，并写入 incident timeline。

## 6. 告警自动处理流程

1. Alertmanager 调用 `POST /webhooks/alertmanager`。
2. webhook 校验签名并提取告警字段。
3. 系统按 dedup key 创建或复用 incident。
4. 系统采集只读证据并持久化 analysis/evidence。
5. 系统从 `next_best_actions` 中生成一个 pending approval。
6. Feishu Thread 摘要中展示 incident 状态、诊断建议和 approval ID。
7. 运维人员通过文本命令批准或拒绝。
8. approval 状态和 incident timeline 更新。

当前不会执行第 9 步“自动修复”。自动修复需要后续单独实现 execution coordinator。

## 7. SRE 工具边界

当前工具按风险级别划分：

- `k8s_read`：只读查询，如 `get`、`describe`、`logs`。
- `prometheus_query`：PromQL 查询。
- `loki_query`：LogQL 查询。
- `k8s_write`：写操作工具，后续自动执行前必须经过审批、dry-run、锁和审计。
- `k8s_exec`：高风险工具，不进入当前自动执行范围。

Prompt 中的权限提示不是安全边界。真正的权限校验必须在工具层或审批授权层完成。

## 8. 审计与指标

当前系统会把关键状态写入 SQLite：

- incident 主状态。
- incident timeline。
- approval request/resolve/expire。
- analysis/evidence/case profile。

`sre_metrics` 已提供 pending approval 和 approval backlog 相关指标，便于观察审批积压。

## 9. 当前不支持的能力

以下能力是后续阶段，不应按当前已实现能力使用：

- 飞书审批卡片按钮。
- 审批人 RBAC 授权。
- 审批通过后自动执行 `k8s_write`。
- 自动执行 `k8s_exec`。
- server-side dry-run。
- operation lock 接入执行链路。
- 执行后健康检查。
- 自动 rollback。
- 多实例部署和 PostgreSQL 迁移。

## 10. 运维排查建议

### 审批文本没有被处理

检查：

- 生产入口是否是 `python3 -m runtime.hermes_gateway`。
- 是否绕过了 `deploy/entrypoint.sh` 直接启动 Hermes gateway。
- `tests/test_feishu_approval_overlay.py` 是否通过。
- 文本是否严格匹配 `批准 <id>` 或 `拒绝 <id> <reason>`。

### 普通消息没有进入 Agent

- 群聊 @ 不响应时，先检查容器是否传入 `FEISHU_GROUP_POLICY=open`，或在 `FEISHU_GROUP_POLICY=allowlist` 时传入了发送人的 `FEISHU_ALLOWED_USERS`。
- 检查 Hermes gateway 日志里是否出现 `Dropping group message that failed mention/policy gate`。
- 检查 overlay 是否错误拦截。普通文本应继续进入 Hermes 原始消息流。
- 如果 pairing、skills 或 memory 在 pod 重启后丢失，检查 `HERMES_HOME` 是否指向 PVC 内的 `/data/hermes`。

### Hermes 升级后审批失效

优先运行：

```bash
rtk pytest tests/test_feishu_approval_overlay.py tests/test_deploy_entrypoint.py -q
```

如果 `_process_inbound_message()` 签名变化，overlay 会 fail-fast。此时应回退 Hermes 指针或临时使用 `AIOPS_WEBHOOK_ONLY=1`，不要直接修改 `hermes-agent` 业务代码。

## 11. FAQ

**Q: 为什么不用飞书卡片按钮？**

当前先用文本审批打通真实 Feishu gateway 状态链路。卡片按钮是后续 UX 增强，必须复用同一个 approval resolve 入口。

**Q: 审批通过后为什么没有执行修复？**

这是当前设计边界。自动执行需要审批授权、dry-run、operation lock、审计、健康检查、rollback 和幂等保护，不能直接接在文本回复 handler 后面。

**Q: 可以直接改 `hermes-agent` 实现拦截吗？**

不建议。`hermes-agent` 是上游独立项目，父项目通过 runtime overlay 拦截真实 Feishu 入站路径，降低后续升级维护成本。
