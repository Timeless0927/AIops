# 基于 hermes-agent 重构 K8s AIOps SRE Agent 方案

## Context

当前项目已经从高层愿景进入分阶段落地。产品愿景仍然是：每个运维人员通过飞书/钉钉绑定自己的 agent 实例，语音或消息远程遥控，agent 主动发现问题并汇报修复方案，在人工审批、权限校验、dry-run、操作锁、健康检查和回滚能力齐备后再进入自动修复闭环。当前生产化边界是：告警可自动建 incident，分析可生成待审批动作，飞书文本审批可变更审批状态，但不会自动执行 Kubernetes 写操作。

hermes-agent（NousResearch，95K stars，MIT，v0.10.0）天然匹配这个场景：个人 agent 模型、飞书/钉钉 gateway、approval 系统、webhook 订阅、cron 定时任务、语音模式、skill 系统。缺的只是 K8s SRE 领域能力。

## 当前落地状态（2026-05）

当前仓库不维护 `hermes-agent` fork，也不把 AIOps 业务逻辑写入上游子模块。`hermes-agent` 作为独立上游依赖保留，父项目通过 runtime overlay 接入真实飞书消息路径。

已落地能力：

- `deploy/entrypoint.sh` 渲染 Hermes 配置，启动 Alertmanager webhook server，并通过 `python3 -m runtime.hermes_gateway` 启动 Hermes gateway。
- `runtime.hermes_gateway` 在进入 Hermes gateway runner 前安装父项目 overlay。
- `runtime.feishu_approval_overlay` patch Hermes `FeishuAdapter._process_inbound_message()`，在消息进入 LLM/session batching 前识别精确审批文本。
- `hooks/approval_reply.py` 是唯一审批状态变更入口，负责解析 `批准 <id>` / `拒绝 <id> <reason>` 并调用 `approval_async.resolve_approval()`。
- `hooks/voice_context.py` 只做 incident 上下文增强，不再承载审批路由。
- 普通飞书文本仍进入 Hermes 原始消息流。

当前明确未落地：

- 飞书交互卡片按钮。
- 审批人 RBAC 授权。
- 审批通过后的真实 `k8s_write` / `k8s_exec` 执行。
- server-side dry-run、operation lock、执行后健康检查和 rollback。

真实飞书审批路径：

```text
Feishu message
  |
  v
Hermes FeishuAdapter._process_inbound_message
  |
  v
runtime.feishu_approval_overlay
  |
  |- exact approval text?
  |    |
  |    |- require sender.open_id or sender.user_id
  |    |- hooks.approval_reply.handle_approval_reply()
  |    |- send result back to same chat/thread
  |    `- stop before LLM/session flow
  |
  `- normal text -> original Hermes message flow
```

这个边界是后续开发的事实源：审批回复先可靠变更状态，自动执行必须在审批授权、dry-run、锁、审计、健康检查和回滚设计完成后单独接入。

## 架构总览

```
┌─────────────────────────────────────────────────┐
│  运维人员                                         │
│  飞书 / 钉钉 / Slack / 语音                       │
└──────────────┬──────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────┐
│  hermes-agent gateway                            │
│  platforms/feishu.py | dingtalk.py                │
│  delivery.py (消息路由)                           │
│  hooks.py (生命周期事件)                          │
│  alert_dedup.py (告警去重/聚合) ← 新增            │
└──────────────┬──────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────┐
│  单 Agent（强模型：Opus / GPT-4o）                │
│  HermesAgentLoop (多轮 tool-calling)             │
│  单轮可并行调用多个工具 ← 关键                    │
│  approval.py (操作审批 + 超时升级) ← 扩展         │
│  tirith_security.py (命令扫描)                    │
│  credential_pool.py (密钥管理)                    │
│  redact.py (输出脱敏 + K8s Secret 专用) ← 扩展    │
│  cron/scheduler.py (定时巡检)                     │
│  webhook (接收 Alertmanager)                      │
│  incident_store.py (事件时间线) ← 新增            │
│  operation_lock.py (并发操作锁) ← 新增            │
└──────┬──────────┬──────────┬────────────────────┘
       │          │          │  单轮并行调用
┌──────▼───┐┌─────▼────┐┌───▼──────┐
│ k8s_read ││loki_query││prom_query│  ...更多工具
└──────┬───┘└─────┬────┘└───┬──────┘
       │          │          │
       ▼          ▼          ▼
   ┌──────────────────────────────────────────────┐
   │  langextract 结构化提取层（内置于每个工具）     │
   │  便宜模型：gemini-flash / ollama 本地          │
   │  自动分块 → 并行提取 → 多 pass → 精确溯源      │
   │  < 200 行跳过，>= 200 行自动触发               │
   └──────────────┬───────────────────────────────┘
                  │
┌─────────────────▼───────────────────────────────┐
│  K8s 集群 + 观测栈                                │
│  Kubernetes API / Prometheus / Loki / Alertmanager│
│  Helm / ArgoCD (可选扩展)                         │
└─────────────────────────────────────────────────┘
```

**为什么不需要多 Agent：**

hermes 的 tool-calling loop 支持单轮并行调用多个工具。Agent 一次请求同时发出
k8s_read + loki_query + prometheus_query，每个工具内部用 langextract（便宜模型）
处理大数据量，三个结果在同一轮返回。

对比多 Agent 方案：
- 更快：并行工具调用 < 并行 agent 调用（省去子 agent 的 LLM 理解+格式化开销）
- 更便宜：langextract 用 flash 级模型做提取，比子 agent 的完整 LLM 会话便宜
- 零上下文断裂：所有结构化结果直接回到强模型，不经过中间摘要
- 精确溯源：langextract 返回原文行号，agent 可以引用具体证据
- 更简单：不需要管理子 agent 生命周期、深度限制、memory 隔离

## 单 Agent 多用户架构

hermes 是单租户设计（SECURITY.md 明确标注 single-tenant trust model）。但我们需要多个运维人员各自有不同权限。解决方案：利用飞书/钉钉私聊天然隔离 session，在 gateway 层做身份绑定，工具层做硬编码权限校验。

```
运维 A（飞书私聊）──→ Session A（operator 权限）──┐
                                                  │
运维 B（飞书私聊）──→ Session B（admin 权限）────┤  共享状态层
                                                  │  ┌─────────────┐
运维群（通知专用）──→ 只推送，不接受命令 ─────────┤  │ SQLite      │
                                                  │  │ - incidents │
Alertmanager ────→ webhook → 自动创建 session ───┘  │ - audit_log │
                                                     │ - op_locks  │
                                                     │ - approvals │
                                                     └─────────────┘
```

**核心原则：每个私聊 = 独立 session，共享持久化状态，不改 hermes 核心。**

**第一层：Gateway 身份识别（`hooks/identity.py`）**

利用 hermes 的 `session:start` hook，在会话创建时自动绑定身份：

1. 从消息中提取 platform_user_id（飞书 sender.open_id / 钉钉 senderStaffId）
2. 在 config.yaml 的 `sre_permissions.operators` 中查找匹配
3. 找到 → 将 operator profile 注入 session context + system prompt
4. 找不到 → 拒绝服务，回复"你没有权限使用此 agent，请联系管理员"

身份信息注入到 session 的 system prompt：
```
当前操作者：张三（feishu:ou_xxx）
角色：operator
可用命名空间：default, staging
可用工具：k8s_read, k8s_write
```

**第二层：工具级硬编码权限校验**

LLM 的 system prompt 可以被 prompt injection 绕过。权限校验必须在工具的 Python 代码里做：

- 使用 `threading.local()` 存储当前 operator profile（hermes 用 ThreadPoolExecutor，thread-local 天然按 session 隔离）
- `session:start` hook 设置 `threading.local().operator = profile`
- 每个工具执行前硬检查：operator.allowed_tools、operator.namespaces
- 不满足 → 直接返回错误，不执行命令，不经过 LLM

即使 LLM 被诱导调用 k8s_exec，工具层也会硬拒绝。

**第三层：跨 Session 非阻塞审批**

审批不能阻塞 session（否则运维等 30 分钟审批期间什么都做不了）。当前 MVP 采用飞书文本审批，不使用卡片按钮，也不在审批通过后自动执行：

```text
1. Alertmanager 分析结果生成 next_best_action
2. alert_webhook 写入 approvals 表，incident timeline 记录 approval_requested
3. Admin 在同一飞书 thread 回复：批准 <approval_id> 或 拒绝 <approval_id> <reason>
4. runtime overlay 在 Hermes LLM 流程前拦截该文本
5. hooks/approval_reply.py resolve approval，并写 incident timeline
6. overlay 将结果回复到同一 chat/thread
```

当前审批回复只改变状态，不执行完整命令。后续如果要接自动执行，必须新增独立 execution coordinator，不能在 Feishu reply handler 内直接调用 `k8s_write` 或 `k8s_exec`。

飞书审批卡片属于后续增强：

- 只有在文本审批链路稳定后再引入 Interactive Card。
- 卡片 callback 仍应复用同一个 approval resolve 入口。
- 卡片按钮不能绕过审批人身份校验和权限规则。

**群聊定位：只读通知通道**

群聊不接受命令，只用于推送（告警诊断结果、巡检报告、每日摘要、审批超时升级）。
群聊收到命令时主动回复"请私聊我发送命令"，不静默忽略。
原因：群聊多人说话，agent 无法可靠区分"谁在下指令"。

**告警路由策略**

Alertmanager 告警进来时，如果目标运维已有活跃 session，注入到现有 session 而不是新建，避免运维同时管理多个对话窗口。

**共享状态（SQLite，WAL 模式）：**

| 表 | 为什么必须跨 session 共享 |
|---|---|
| incidents | 运维 A 创建的 incident，交接后运维 B 要能看到 |
| audit_log | 全局审计，不分 session |
| operation_locks | 防止不同运维同时操作同一资源 |
| approvals | 跨 session 审批流转 |
| hermes memory | SRE 经验对所有人有用，共享 |

不共享：hermes session history（每人独立对话上下文）。

**Phase 0 必须验证的假设：**

| 假设 | 验证方法 |
|------|---------|
| hermes 每个飞书私聊创建独立 session | 两个飞书账号同时私聊 agent，看对话是否隔离 |
| session:start hook 能拿到 sender 的 platform ID | 打印 hook event data，检查字段 |
| 工具执行时能访问 session 级状态 | 写测试工具，尝试读取 thread-local 或 agent context |

## 实施计划

### Phase 0: 环境搭建 + 跑通 hermes-agent

当前策略已经修订为：不 fork `hermes-agent`，不修改上游子模块代码。父项目把 Hermes 当作上游依赖，通过 toolsets、hooks、skills、配置和 runtime overlay 承载 AIOps 业务逻辑。

已验证的启动策略：

- Docker/entrypoint 安装并使用 `hermes-agent[messaging,feishu]`。
- 父项目 gateway wrapper：`python3 -m runtime.hermes_gateway`。
- wrapper 先把 `hermes-agent` 源码路径放入 `sys.path`，再安装 overlay，最后调用 Hermes gateway runner。
- overlay 只依赖一个 Hermes 私有入口：`FeishuAdapter._process_inbound_message()`。

维护策略：

| 接入方式 | 模块 | 是否改 hermes-agent | 维护要求 |
|---------|------|---------------------|----------|
| toolsets 注册 | K8s / SRE 工具集 | 否 | 跟随父项目测试 |
| hooks 目录 | 告警、上下文、审批状态 | 否 | 保持单一状态入口 |
| skills 目录 | SRE runbook | 否 | 纯文本迭代 |
| runtime overlay | Feishu 审批文本拦截 | 否 | Hermes 升级时跑兼容测试 |
| upstream PR | 通用能力增强 | 可选 | 只提交可泛化能力 |

Hermes 升级流程：

1. 更新 `hermes-agent` 指针或依赖版本。
2. 运行 overlay fail-fast 测试，确认 `_process_inbound_message()` 存在且签名兼容。
3. 运行 Feishu approval overlay focused tests。
4. 运行 entrypoint tests，确认生产启动仍经过 wrapper。
5. 如 overlay 不兼容，先回退 Hermes 指针或临时使用 webhook-only 模式，不在上游仓库内快速打业务补丁。

### Phase 1: K8s 工具集接入（核心价值）

从现有 k8s-aiops 迁移工具到 hermes tool 格式。三级安全分类：

| 工具 | 参数 | 审批级别 | 说明 |
|------|------|---------|------|
| `k8s_read` | `command: str, context?: str` | 无需审批 | 执行只读 kubectl 命令 |
| `k8s_write` | `command: str, context?: str` | 标准审批 | 执行写操作 kubectl 命令 |
| `k8s_exec` | `command: str, context?: str` | 高级审批（需 can_approve 角色） | exec/cp/port-forward/run |
| `prometheus_query` | `query: str, start?: str, end?: str` | 无需审批 | PromQL 查询 |
| `loki_query` | `query: str, start?: str, end?: str, limit?: int` | 无需审批 | LogQL 查询 |
| `k8sgpt_analyze` | `namespace?: str, filters?: str` | 无需审批 | AI 集群扫描 |

**1a. k8s_guard — 命令安全校验层**

所有 kubectl 命令先经过 `k8s_guard.py` 分类校验：

```
READ 白名单:  get, describe, logs, top, api-resources, explain, diff, auth can-i
WRITE 命令:   scale, patch, apply, create, edit, rollout, cordon, drain, taint, label, annotate
WRITE 高危:   delete pod/deployment/service/configmap（标准审批 + 二次确认）
EXEC 级别:    delete namespace/node/pv/crd、exec, cp, port-forward, run, attach, debug
禁止命令:     proxy（开放网络通道）
```

- LLM 调用 k8s_read 但命令实际是 write → 拒绝并提示用 k8s_write
- LLM 调用 k8s_read 但命令是 exec → 拒绝并提示用 k8s_exec
- 未知子命令 → 默认归类为 write，走审批

**1b. Secret 脱敏**

扩展 hermes `redact.py`，增加 K8s 专用规则：
- `kubectl get secret -o yaml/json` 输出中的 `data:` 字段 → 替换为 `[REDACTED]`
- base64 编码的 token/password 模式匹配
- 环境变量中的 `*_KEY`、`*_SECRET`、`*_TOKEN`、`*_PASSWORD`

**1c. 数据预处理层 — langextract 结构化提取**

工具输出的原始数据（日志、kubectl 输出、指标）通过 Google langextract（Apache-2.0，35K stars）做结构化提取，而不是简单的 delegate_task 摘要。

langextract 的核心能力：
- **自动分块**：超长文档按 token 切分成 chunk，不受 LLM context window 限制
- **并行处理**：max_workers=20 并行提取多个 chunk
- **多次 pass**：extraction_passes=3 提高召回率，不遗漏关键信息
- **精确溯源**：每条提取结果带 char_interval，指向原文精确位置
- **schema 强制**：输出结构一致，supervisor 无需解析自由文本
- **便宜模型**：用 gemini-2.5-flash 或本地 Ollama 模型，成本极低

按工具类型定义提取 schema：

**日志提取（loki_query 输出）：**
```python
examples = [
    lx.data.ExampleData(
        text="2024-01-15 03:22:11 [error] java.lang.OutOfMemoryError: Java heap space\n  at com.app.Service.process(Service.java:142)",
        extractions=[
            lx.data.Extraction(
                extraction_class="error",
                extraction_text="java.lang.OutOfMemoryError: Java heap space",
                attributes={"severity": "critical", "type": "OOM", "location": "Service.java:142"}
            ),
        ]
    )
]
# 提取类：error, warning, stack_trace, anomaly_pattern, timeout, connection_error
```

**K8s 资源提取（k8s_read 输出）：**
```python
# 提取类：unhealthy_pod, high_restart, pending_resource, failed_event, node_pressure
# 属性：name, namespace, status, reason, age, restart_count
```

**指标提取（prometheus_query 输出）：**
```python
# 提取类：threshold_breach, trend_anomaly, spike, correlation
# 属性：metric_name, value, threshold, direction, timestamp
```

处理流程：
```
工具原始输出
    │
    ├─ < 200 行 → 直接返回给 Supervisor（强模型直接处理）
    │
    └─ >= 200 行 → langextract 提取管道
         │
         ├─ 自动分块（max_char_buffer=1000）
         ├─ 并行提取（max_workers=10，便宜模型）
         ├─ 多 pass 合并去重
         └─ 返回结构化结果 + 原文行号引用
              │
              Supervisor 拿到的是：
              "第 847 行：OOMKilled（java.lang.OutOfMemoryError）
               第 1203 行：连续重启 15 次（CrashLoopBackOff）
               第 2001 行：内存使用 98.7%（超阈值）"
              而不是 2000 行原始数据
```

与 delegate_task 的关系：delegate_task 仍然保留，用于不需要结构化提取的场景（比如"帮我总结一下这个 incident 的处理过程"）。langextract 专门用于从大量原始运维数据中提取结构化关键信息。

实现为 `tools/sre_extractor.py`：
- 内置 SRE 领域的 extraction examples（日志/K8s/指标三套）
- 被 k8s_read / loki_query / prometheus_query 内部调用
- 模型配置：默认 gemini-2.5-flash，可切换为本地 Ollama（离线场景）

**1d. 查询安全护栏**

- `prometheus_query`：强制 time range，无 start/end 时默认最近 1 小时；query timeout 30s
- `loki_query`：强制 limit，无 limit 时默认 200；禁止 `{job=~".+"}` 这类全量匹配
- 两者都记录查询耗时，超过 10s 的查询写入慢查询日志

**1e. 多集群支持**

通过 `context` 参数指定 kubeconfig context。配置：
```yaml
k8s:
  default_context: "production"
  contexts:
    production:
      kubeconfig: "/path/to/prod.kubeconfig"
      prometheus: "http://prometheus.prod:9090"
      loki: "http://loki.prod:3100"
    staging:
      kubeconfig: "/path/to/staging.kubeconfig"
      prometheus: "http://prometheus.staging:9090"
      loki: "http://loki.staging:3100"
```

**1f. K8s API 限流保护**

agent 快速连续调 kubectl 可能触发 K8s API rate limit（尤其是托管集群 EKS/GKE/AKS）：
- 全局 QPS 限制：默认 10 qps，可按集群配置
- 指数退避重试：429/throttled 响应自动 backoff（1s → 2s → 4s，最大 30s）
- 批量查询优化：agent 连续调多个 `k8s_read` 时，合并为单次 `kubectl get` 多资源

**1g. 工具部分失败优雅降级**

agent 并行调用多个工具时，单个工具失败不应阻断整个诊断：
- 工具超时（默认 30s）→ 返回 `"[工具名] 超时，跳过此数据源"`
- 工具报错（如 Prometheus 不可用）→ 返回 `"[工具名] 不可用：{错误}，基于其他数据源继续分析"`
- agent 拿到部分结果后继续推理，在结论中标注"以下分析缺少 {数据源} 的数据"

**1h. 告警 → 集群自动路由**

Alertmanager 告警 payload 通常带 `cluster` label。webhook 层自动提取并设置当前会话的 k8s context：
- 告警带 `cluster: production` → 自动切换到 production context
- 告警不带 cluster label → 使用 default_context
- 运维手动指令"切到 staging 集群" → 切换 context
- 每个工具调用都带当前 context，避免跨集群误操作
- Prometheus/Loki 地址也跟随集群 context 切换（不同集群的观测栈地址不同）

**1i. 敏感数据合规分层**

明确哪些数据可以上云 LLM，哪些必须本地处理：

```
┌─────────────────────────────────────────────┐
│  数据分级                                     │
├─────────────┬───────────────────────────────┤
│  可上云      │ 资源名称、状态、事件、指标值    │
│  (强模型)    │ 错误类型、堆栈模式             │
├─────────────┼───────────────────────────────┤
│  仅本地      │ Secret 内容、环境变量值         │
│  (ollama)    │ 日志中的用户数据、内部 IP       │
│              │ kubeconfig 凭证                │
└─────────────┴───────────────────────────────┘
```

处理策略：
- langextract 提取层用本地 Ollama 模型 → 原始日志/kubectl 输出不出本地
- 提取后的结构化结果（错误类型、资源名、状态）可以安全发给云端强模型
- redact.py 作为最后一道防线，拦截漏网的敏感信息
- 配置开关：`compliance.local_only: true` 时全部使用本地模型，牺牲质量保合规

### Phase 2: SRE 诊断 Skill

hermes 的 skill 是 Markdown 文档（`SKILL.md`），包含指令和工作流。我们把 SRE 诊断流程编码为 skill，而不是硬编码状态机。

创建 `skills/sre/` 目录：

```
skills/sre/
├── triage/SKILL.md          # 告警分类：severity判断、影响范围评估
├── investigate/SKILL.md     # 诊断调查：日志+指标+事件关联分析
├── remediate/SKILL.md       # 修复执行：生成修复计划、dry-run、执行
├── postmortem/SKILL.md      # 事后复盘：时间线整理、根因总结、知识沉淀
└── runbooks/
    ├── pod-crashloop/SKILL.md
    ├── node-not-ready/SKILL.md
    ├── high-memory/SKILL.md
    ├── certificate-expiry/SKILL.md
    └── pvc-full/SKILL.md
```

优势：skill 是纯文本，运维人员可以直接编辑和扩展 runbook，不需要写代码。比硬编码状态机灵活得多。

**2a. Skill 动态生命周期 — 从 incident 自动沉淀经验**

静态 skill 只是起点。核心价值在于让系统"越用越聪明"的动态闭环：

```
真实故障处理完成
    │
    ├─ postmortem skill 整理时间线 + 根因
    │
    ├─ agent 自动抽取可复用的诊断步骤和修复动作
    │  （哪些工具调用有效、什么顺序最快定位、哪些指标是关键信号）
    │
    ├─ 生成 skill 草稿（Markdown SKILL.md 格式）
    │  存入 skills/sre/drafts/{incident_id}/SKILL.md
    │
    ├─ 推送给运维专家审核（飞书卡片：查看草稿 / 批准 / 修改 / 丢弃）
    │
    ├─ 审核通过 → 移入正式 skills/sre/runbooks/ 目录
    │
    └─ 后续类似故障 → agent 自动匹配并使用该 skill
       → 如果 skill 执行后被拒绝或修改 → 更新 skill 版本
```

实现要点：
- `skill_extractor.py`：从 incident_store 的完整时间线中提取可复用模式
- skill 草稿存储在 `skills/sre/drafts/`，审核通过后移入 `runbooks/`
- 每个 skill 带 metadata：来源 incident_id、创建时间、使用次数、成功率
- 匹配逻辑：新告警进来时，agent 根据 alertname + 症状在已有 skill 中检索相似案例
- skill 版本管理：修改后保留历史版本，可回退

### Phase 3: 主动告警 → 自动诊断 → 文本审批状态闭环

Phase 3 当前目标是把真实告警诊断和人工审批状态打通，而不是立即执行修复。

**3a. Alertmanager -> alert_webhook + incident dedup**

当前入口是父项目 `hooks/alert_webhook.py`，而不是 Hermes webhook CLI 订阅。处理流程：

```text
Alertmanager POST /webhooks/alertmanager
  |
  |- HMAC 校验
  |- alert_dedup.should_process()
  |- create/reuse incident
  |- collect targeted read-only evidence
  |- persist analysis/evidence/case recall
  |- publish Feishu thread summary
  `- maybe_request_phase3_approval()
```

**3b. 审批请求创建**

`alert_webhook` 从 persisted analysis 的 `next_best_actions` 中选择一个候选动作，写入 `approval_async` pending record，并在 incident timeline 中记录 `approval_requested`。同一 incident/action signature 会复用已有 pending approval，避免重复打扰。

**3c. 飞书文本审批回复**

当前 MVP 支持精确文本命令：

- `批准 <approval_id>`
- `拒绝 <approval_id> <reason>`

处理边界：

- 只在真实 Feishu gateway 入站路径中拦截。
- 必须存在 `sender.open_id`，只在缺失时回退 `sender.user_id`。
- 缺少审批人身份时回复失败，不修改审批状态。
- 审批 resolve 后回复同一 chat/thread。
- 已处理的审批文本不进入 LLM/session batching。

**3d. 当前不执行 Kubernetes 写操作**

审批通过当前只代表“人已经同意这个建议动作”，不代表系统已经具备执行资格。自动执行另列后续阶段，原因是它需要完整的安全链路：

1. 审批人授权和角色校验。
2. server-side dry-run 或明确降级策略。
3. operation lock。
4. 执行审计。
5. 执行后健康检查。
6. rollback 或人工介入路径。
7. 防重复执行的幂等记录。

**3e. 后续自动执行设计原则**

后续实现自动执行时，应新增 execution coordinator 或 queue，不应让 `hooks/approval_reply.py` 直接调用 `k8s_write` / `k8s_exec`。审批回复 handler 只负责状态变更，execution coordinator 订阅或轮询 approved approval，再按安全状态机推进。

### Phase 4: 审批审计与权限管理

**4a. 审计日志**
```
who:          运维人员ID（feishu:ou_xxx / dingtalk:uid_xxx）
what:         操作内容（kubectl scale deployment/nginx --replicas=3 -n production）
when:         时间戳
where:        集群 context + 命名空间
trigger:      manual | alert | cron
tool_level:   read | write | exec
dry_run:      dry-run 结果
result:       success | failed | rejected | timeout | rollback
approval_by:  审批人ID（可以是自己，也可以是上级）
approval_at:  审批时间
rollback:     是否触发了回滚
snapshot_path: 操作前快照路径（write/exec 操作）
```

存储：SQLite（`~/.hermes/sre/audit.db`），支持按时间/人员/集群/命名空间查询。

**4b. 事件时间线（Incident Record）**

区别于 hermes 的通用 memory，这是结构化的事件记录：
```
incident_id:   自动生成（告警触发时创建，手动诊断时创建）
timeline:      [{time, event_type, detail}, ...]
  - alert_fired:     告警触发
  - triage_start:    开始分类
  - diagnosis:       诊断结论
  - plan_proposed:   修复方案生成
  - approval_sent:   审批请求发出
  - approved_by:     某人批准
  - executed:        操作执行
  - health_check:    健康检查结果
  - rollback:        回滚（如有）
  - resolved:        问题解决
status:        open | investigating | pending_approval | executing | resolved | escalated
operator:      负责人
cluster:       集群
namespace:     命名空间
```

用途：
- 运维问"今早那个 nginx 问题怎么处理的" → agent 查 incident record 回答
- 事后复盘：完整时间线自动生成
- 跨会话：incident 不随 hermes session 消失

**4c. 权限配置**（`config.yaml` 扩展）
```yaml
sre_permissions:
  operators:
    - id: "feishu:ou_xxx"
      name: "张三"
      role: operator
      namespaces: ["default", "staging"]
      allowed_tools: ["k8s_read", "k8s_write"]  # 无 k8s_exec
    - id: "dingtalk:uid_xxx"
      name: "李四"
      role: admin
      namespaces: ["*"]
      allowed_tools: ["k8s_read", "k8s_write", "k8s_exec"]
      can_approve: true

  approval_rules:
    - tool: "k8s_exec"
      require_approval_from: "admin"        # exec 必须 admin 审批
    - tool: "k8s_write"
      namespace: "production"
      require_approval_from: "admin"        # 生产环境写操作需 admin
    - tool: "k8s_write"
      namespace: "staging"
      auto_approve: true                    # staging 写操作自动批准
    - tool: "k8s_write"
      command_match: "delete"
      require_approval_from: "admin"        # 删除操作必须 admin
```

**4d. 运维交接**

值班换人时：
- 新值班人发"接班" → agent 推送当前所有 open incident 摘要
- incident 的 operator 字段更新为新值班人
- 历史 incident 仍可查询

**4e. 会话中断恢复**

hermes gateway 重启、网络断开、agent 进程崩溃时：

- incident_store（SQLite）持久化，不丢失事件时间线
- 审批状态持久化在 SQLite，不随进程消失
- 重启后自动检查：
  - `status=pending_approval` 的 incident → 重新发送审批通知或 Thread 摘要（当前为文本审批指令）
  - `status=investigating` 的 incident → 通知运维"诊断因系统重启中断，是否继续？"
  - `status=executing` 的 incident → 检查操作是否已完成，未完成则标记异常并通知
- hermes 的 session 历史保存在 `~/.hermes/sessions/`，重启后可恢复上下文

### Phase 5: 语音交互 + 移动端体验

hermes 已有 voice mode（Edge TTS 免费 + faster-whisper STT）。配置即用：
- 运维人员在飞书发语音消息 → STT 转文字 → agent 处理 → TTS 回复
- 适合值班场景：半夜收到告警，语音问"怎么回事"，agent 语音回复诊断结果

### Phase 6: 运营健壮性

**6a. 通知疲劳防护**

agent 不能什么都推，否则运维会忽略所有通知：
```yaml
notification:
  severity_filter: warning    # 只有 warning 以上才主动推送，info 静默记录
  dedup_window: 4h            # 同一问题 4 小时内只推一次
  daily_digest: "09:00"       # 低优先级问题汇总成日报，每天 9 点推送
  quiet_hours:                # 非紧急问题不在深夜推送
    start: "23:00"
    end: "07:00"
    except: critical           # critical 不受限
  max_per_hour: 10            # 每小时最多 10 条主动通知，超出的排队到下一小时
```

**6b. 从拒绝中学习**

运维拒绝修复方案时：
1. 要求填写拒绝原因（飞书卡片输入框 / 语音说明）
2. 原因写入 hermes memory：`"在 {场景} 下不应该 {操作}，因为 {原因}"`
3. 下次类似场景 agent 会参考 memory 调整策略
4. 定期统计：拒绝率 > 30% 的 skill → 标记需要优化，通知管理员

**6c. LLM 降级策略**

```
正常模式：强模型诊断 + 便宜模型预处理
    ↓ LLM API 超时 3 次
降级模式：便宜模型独立诊断（质量下降但可用）
    ↓ 便宜模型也不可用
规则引擎模式：预定义的 alertname → 动作映射表
    ↓ 规则也无法匹配
透传模式：原样转发告警到运维，附带"AI 诊断暂时不可用"
```

规则引擎映射表（`config.yaml`）：
```yaml
fallback_rules:
  - alert: KubePodCrashLooping
    action: "kubectl describe pod {pod} -n {namespace} && kubectl logs {pod} -n {namespace} --tail=100"
    deliver: origin
  - alert: NodeNotReady
    action: "kubectl describe node {node} && kubectl get events --field-selector involvedObject.name={node}"
    deliver: origin
  - default:
    action: forward_raw    # 无匹配规则 → 原样转发
```

**6d. 成本监控**

hermes 已有 `usage_pricing.py`，扩展 SRE 专用预算：
```yaml
cost:
  daily_budget: 50.00         # 每天 $50 上限
  alert_threshold: 0.8        # 80% 时告警
  per_incident_budget: 5.00   # 单次 incident 最多 $5
  exceeded_action: degrade    # 超预算 → 降级到便宜模型
```

- 每次 LLM 调用记录 token 用量和成本
- 接近预算时通知运维
- 超预算自动降级，不是停止服务

**6e. Agent 自监控**

谁来监控监控者：
- hermes gateway 注册为 systemd service，配置 watchdog
- 每 5 分钟发心跳消息到独立监控通道（不经过 agent 自身）
- 心跳中断 3 次 → 独立告警（可以是简单的 cron + curl，不依赖 hermes）
- K8s 部署时用 liveness/readiness probe

**6f. 效果度量**

从 incident_store + audit_log 自动计算，不需要额外埋点：

| 指标 | 计算方式 | 目标 |
|------|---------|------|
| MTTD（平均诊断时间） | alert_fired → diagnosis 的时间差 | < 2 分钟 |
| 方案采纳率 | approved / (approved + rejected) | > 80% |
| 后续自动执行成功率 | executed+success / executed | > 95% |
| 后续回滚率 | rollback / executed | < 5% |
| 人工干预率 | escalated / total_incidents | < 20% |
| 误报率 | false_positive / total_alerts | 持续下降 |

展示方式：
- 每周自动生成统计摘要，推送到运维群
- 可选：接入 Grafana dashboard（agent 指标暴露为 Prometheus metrics）
- 拒绝率高的 skill 自动标记，提醒优化

### Phase 7: Helm / ArgoCD 扩展（可选）

初期只支持 kubectl，但预留扩展点。后续按需增加：

| 工具 | 审批级别 | 说明 |
|------|---------|------|
| `helm_read` | 无需审批 | helm list, helm status, helm history |
| `helm_write` | 标准审批 | helm upgrade, helm rollback, helm install |
| `argocd_read` | 无需审批 | argocd app list, argocd app get |
| `argocd_write` | 标准审批 | argocd app sync, argocd app rollback |

同样走 k8s_guard 分类校验 + approval gate。
初期不实现，但工具注册机制设计时预留位置。

## 需要新开发的模块清单

| 模块 | 工作量 | 说明 |
|------|--------|------|
| `tools/k8s_read.py` | 小 | 执行只读 kubectl，输出截断 |
| `tools/k8s_write.py` | 中 | 后续写操作执行链路：dry-run + 快照 + rollback |
| `tools/k8s_exec.py` | 小 | exec/cp/port-forward，高级审批 |
| `tools/k8s_guard.py` | 中 | 命令分类校验（read/write/exec/禁止） |
| `tools/prometheus.py` | 小 | PromQL + 强制 time range + timeout |
| `tools/loki.py` | 小 | LogQL + limit + 全量匹配拦截 |
| `tools/k8sgpt.py` | 小 | AI 集群扫描 |
| `tools/sre_extractor.py` | 中 | langextract 结构化提取（日志/K8s/指标三套 schema） |
| `skill_extractor.py` | 中 | 从 incident 时间线自动提取可复用 skill 草稿 |
| `alert_dedup.py` | 中 | 告警去重/聚合/风暴检测 |
| `incident_store.py` | 中 | 事件时间线结构化存储（SQLite） |
| `operation_lock.py` | 小 | 资源级并发操作锁 |
| 扩展 `approval.py` | 大 | 异步消息审批 + 超时升级 + 飞书/钉钉卡片 |
| 扩展 `redact.py` | 小 | K8s Secret 专用脱敏规则 |
| 审计日志 | 中 | SQLite 表 + 写入逻辑 + 查询接口 |
| 权限配置 | 中 | config.yaml 扩展 + 校验逻辑 |
| 会话中断恢复 | 中 | 重启后 pending incident 检测 + 恢复 |
| 告警集群路由 | 小 | webhook 层解析 cluster label → 自动切 context |
| 数据合规分层 | 中 | langextract 本地模型 + redact 兜底 + 配置开关 |
| K8s API 限流 | 小 | 全局 QPS + 指数退避 + 批量合并 |
| 工具部分失败降级 | 小 | 超时/报错优雅返回，不阻断诊断 |
| 效果度量 | 小 | 从 incident_store + audit_log 自动计算指标 |
| 通知疲劳防护 | 小 | severity filter + dedup + 日报 + quiet hours |
| 拒绝学习 | 小 | 拒绝原因 → hermes memory + 统计 |
| LLM 降级策略 | 中 | 三级降级：便宜模型 → 规则引擎 → 透传 |
| 成本监控 | 小 | 扩展 usage_pricing + 预算告警 + 自动降级 |
| Agent 自监控 | 小 | 心跳 + 独立告警通道 |
| SRE Skills（4+5 个 SKILL.md） | 小 | 纯 Markdown 编写 |
| Alertmanager webhook 配置 | 小 | hermes webhook subscribe + AM receiver |

## 不需要开发的（hermes 已有）

- 飞书/钉钉 gateway 适配器
- 语音模式（TTS + STT）
- Approval 审批基础框架
- Tirith 命令安全扫描
- Credential Pool 密钥管理
- Output Redaction 基础框架
- Cron 定时任务调度
- Webhook 订阅系统
- Sub-agent delegation
- 多 LLM provider 支持
- Skill 管理系统
- Memory 系统（替代原有 knowledge RAG）

## 验证方式

1. **Phase 0**：飞书发消息 → agent 回复，确认 gateway 通路
2. **Phase 1**：飞书发"查看 default 命名空间的 pods" → k8s_read 返回结果
3. **Phase 1 安全**：飞书发"帮我 exec 进 nginx pod" → agent 调用 k8s_exec → 高级审批触发
4. **Phase 1 脱敏**：飞书发"查看 secret" → 输出中 data 字段显示 [REDACTED]
5. **Phase 2**：飞书发"诊断 nginx pod 重启" → agent 自动走 triage → investigate 流程
6. **Phase 3 告警**：Alertmanager 发告警 → 创建/复用 incident → 生成 pending approval → 飞书回复"批准 <id>" → 更新审批状态和 timeline，不执行写操作
7. **Phase 3 去重**：连续发 10 条相同告警 → 只触发 1 次诊断
8. **Phase 3 超时**：发送审批后 30 分钟不回复 → 自动升级通知
9. **后续执行闭环**：在 dry-run、锁、审计、健康检查和 rollback 设计完成后，再验证审批通过后的真实执行与回滚
10. **Phase 4**：查看审计日志 + 事件时间线，确认完整记录
11. **Phase 5**：飞书发语音"集群怎么样" → 语音回复巡检结果

**自动化测试（每个 Phase 交付前必须通过）：**

| 模块 | 测试类型 | 覆盖内容 |
|------|---------|---------|
| k8s_guard | 单元测试 | 所有 kubectl 子命令分类正确，未知命令归为 write，delete 二次分级 |
| alert_dedup | 单元测试 | 去重窗口、聚合计数、风暴检测阈值 |
| operation_lock | 单元测试 | 加锁/释放/超时/续期、并发竞争 |
| identity hook | 集成测试 | 已知用户识别、未知用户拒绝、权限注入 |
| 工具权限校验 | 集成测试 | operator 调 k8s_exec 被拒、namespace 越权被拒 |
| 审批流程 | 集成测试 | 非阻塞发送、callback 匹配、超时升级 |
| redact | 单元测试 | Secret data 脱敏、环境变量脱敏、正常输出不误伤 |

## 项目定位

这是一个父项目集成 Hermes 上游能力的 AIOps 项目，不是 `hermes-agent` 业务 fork。

- 项目路径：`/home/mao/aiops`。
- `hermes-agent` 保持上游独立项目属性，父项目不在其源码内承载 AIOps 审批业务。
- 父项目负责 SRE toolsets、hooks、skills、部署入口和 runtime overlay。
- 主交互通道：飞书 Thread + Alertmanager webhook；Web UI 不是当前重点。
- 管理后台（审计日志、权限配置、incident 历史）后续按需考虑，初期用 CLI + 飞书消息覆盖。
- 后续如需改 Hermes 通用能力，优先设计成 upstream PR，而不是长期维护私有 fork。
