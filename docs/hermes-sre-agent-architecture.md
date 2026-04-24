# 基于 hermes-agent 重构 K8s AIOps SRE Agent 方案

## Context

当前 k8s-aiops 基于 LangGraph 状态机，有完整 K8s/Prometheus/Loki 工具链，但整体"玩具感"强。产品愿景是：每个运维人员通过飞书/钉钉绑定自己的 agent 实例，语音或消息远程遥控，agent 主动发现问题并汇报+附带修复方案，人工审批后自动修复，完整审批审计。

hermes-agent（NousResearch，95K stars，MIT，v0.10.0）天然匹配这个场景：个人 agent 模型、飞书/钉钉 gateway、approval 系统、webhook 订阅、cron 定时任务、语音模式、skill 系统。缺的只是 K8s SRE 领域能力。

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

审批不能阻塞 session（否则运维等 30 分钟审批期间什么都做不了）：

```
1. k8s_write 工具发送审批请求 → 写入 approvals 表 → 立即返回
   "审批请求已发送给李四，等待审批中"
2. Session A 保持可用，运维可以继续问其他问题
3. Admin B 在飞书点击审批卡片"批准"
   → 飞书回调 → gateway 匹配 approvals 表 → 向 Session A 注入消息：
   "李四已批准操作：kubectl scale deployment/nginx --replicas=3"
4. Agent 收到注入消息 → 从 approvals 表取完整命令 → 执行
```

飞书审批卡片实现要点：
- 扩展飞书 gateway，支持发送 Interactive Card（lark-oapi 已支持）
- 新增 HTTP 回调端点接收卡片点击事件
- 卡片 callback 数据带 approval_id，用于匹配 SQLite 记录

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

- Fork hermes-agent 到自己的 repo
- 本地跑通 CLI 模式，验证基本 tool-calling loop
- 配置一个 LLM provider（OpenAI 或 Anthropic）
- 跑通飞书或钉钉 gateway，验证消息收发

**0a. Fork 维护策略**

原则：尽量用插件/扩展方式接入，最小化对 hermes 核心代码的修改。

| 接入方式 | 模块 | 是否改 hermes 核心 |
|---------|------|-------------------|
| toolsets 注册 | K8s 工具集 | 否，新增文件 |
| skills 目录 | SRE 诊断流程 | 否，新增文件 |
| hooks 目录 | 告警去重、事件记录 | 否，新增文件 |
| config.yaml 扩展 | 权限、审批、K8s 配置 | 否，扩展配置 |
| 扩展 redact.py | K8s Secret 脱敏 | 是，提 PR 回 upstream |
| 扩展 approval.py | 异步消息审批 | 是，提 PR 回 upstream |

必须改核心的只有 redact 和 approval 两处，设计时做成可插拔扩展（注册自定义 handler），方便提 PR 被 upstream 接受。其余全部通过 hermes 的扩展机制接入。

定期 rebase upstream：每个 hermes 大版本发布后 merge，保持不超过 1 个大版本的差距。

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

### Phase 3: 主动告警 → 自动诊断 → 审批修复

产品核心流程，利用 hermes 已有机制串联。

**3a. Alertmanager → hermes webhook + 告警去重**

```bash
hermes webhook subscribe k8s-alerts \
  --events "alert" \
  --prompt "K8s 告警：{alertname} | {severity} | {namespace}\n{description}\n\n使用 sre/triage skill 诊断并生成修复方案。" \
  --skills "sre/triage,sre/investigate,sre/remediate" \
  --deliver origin
```

新增 `alert_dedup.py` — webhook 层告警去重/聚合：
- webhook 鉴权：Alertmanager 配置 `http_config.bearer_token`，hermes 端校验 token，拒绝未认证请求
- 同一 `alertname + namespace` 在 5 分钟内只触发一次 agent 诊断
- 5 分钟窗口内的后续告警聚合为计数，附加到当前诊断会话
- 告警风暴检测：1 分钟内超过 20 条不同告警 → 不逐条诊断，改为发送聚合摘要给运维人员，由人决定优先级
- 去重 key 可配置（默认 alertname+namespace，可加 pod/node 维度）

**3b. Cron 定时巡检**
```bash
hermes cron add "0 */4 * * *" \
  --prompt "集群健康巡检：k8sgpt_analyze + pending pods + node 状态 + 证书过期 + PVC 使用率。发现问题主动汇报。" \
  --skills "sre/investigate" \
  --deliver feishu:运维群ID
```

**3c. 审批流程（扩展 hermes approval）**

hermes 原生 approval 是 CLI 阻塞等待。需要扩展为异步消息审批：

1. agent 触发 k8s_write/k8s_exec → approval gate 拦截
2. 构造审批卡片消息（包含：操作内容、影响范围、dry-run 结果、风险等级）
3. 推送到运维人员的飞书/钉钉
4. 运维回复"批准"/"拒绝"（或点击卡片按钮）→ 回调解除 approval 阻塞
5. 记录审批到审计日志 + 事件时间线

**审批超时与升级机制：**
```yaml
approval:
  timeout: 30m              # 30 分钟无人审批
  timeout_action: escalate  # escalate | cancel | auto_approve_low_risk
  escalation:
    - after: 30m
      notify: "feishu:ou_leader"    # 升级到上级
      message: "操作等待审批超时，请处理"
    - after: 60m
      action: cancel                 # 1 小时仍无人 → 自动取消
      notify: "feishu:ou_operator"
      message: "操作已因超时自动取消"
```

**3d. dry-run 预检 + 自动 rollback**

k8s_write 执行流程：
1. 先自动执行 `kubectl ... --dry-run=server` → 验证命令合法性
2. dry-run=server 不支持（部分 CRD / 旧版 K8s）→ 降级到 dry-run=client，审批卡片标注"仅客户端校验"
3. dry-run 失败 → 直接拒绝，不进入审批流程，告知 LLM 原因
3. dry-run 成功 → 记录操作前快照（affected resource 的当前 YAML）
4. 进入审批流程（审批卡片包含 dry-run 结果）
5. 审批通过 → 执行真实操作
6. 执行后健康检查（等待 30s，检查 pod status/event）
7. 健康检查失败 → 自动回滚到快照状态，通知运维

快照存储：`~/.hermes/sre/snapshots/{timestamp}_{resource}.yaml`

**3e. 并发操作锁**

新增 `operation_lock.py`：
- 锁粒度：`{cluster}:{namespace}:{resource_type}/{resource_name}`
- 同一资源同时只允许一个写操作
- 第二个操作尝试时 → 告知"该资源正在被 {operator} 操作中，请稍后"
- 锁超时：默认 10 分钟，可配置；长时间操作（如 rolling restart）自动续期（每 30s heartbeat）
- 读操作不加锁

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
  - `status=pending_approval` 的 incident → 重新发送审批卡片
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
| 自动修复成功率 | executed+success / executed | > 95% |
| 回滚率 | rollback / executed | < 5% |
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
| `tools/k8s_write.py` | 中 | 写操作 + dry-run + 快照 + rollback |
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
6. **Phase 3 告警**：Alertmanager 发告警 → 自动诊断 → 飞书推送方案 → 回复"批准" → 执行
7. **Phase 3 去重**：连续发 10 条相同告警 → 只触发 1 次诊断
8. **Phase 3 超时**：发送审批后 30 分钟不回复 → 自动升级通知
9. **Phase 3 rollback**：批准一个会导致问题的操作 → 健康检查失败 → 自动回滚
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

这是一个全新独立项目，基于 hermes-agent fork 构建，不保留现有 k8s-aiops 的任何代码。

- 项目路径：`E:/git/aiops`（独立于现有 k8s-aiops 仓库）
- 新建独立 repo，fork hermes-agent 为起点
- 现有 k8s-aiops 的工具逻辑作为参考，但全部重写适配 hermes tool 格式
- 不保留 React Web UI、LangGraph 状态机、FastAPI 后端
- 主交互通道：飞书/钉钉，不做 Web 前端
- 管理后台（审计日志、权限配置、incident 历史）后续按需考虑，初期用 CLI + 飞书消息覆盖
