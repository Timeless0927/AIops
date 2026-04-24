# AIOps SRE Agent 用户手册

## 1. 项目简介
AIOps SRE Agent 是一个基于大模型的智能 Kubernetes 运维助手。它通过飞书/钉钉与你进行私聊或语音交互，能自动接收告警、排查集群故障（如 Pod 重启、节点异常等），提供明确的根因分析，并在取得你的授权（审批）后自动执行修复操作。

## 2. 环境准备

要开始使用本 Agent，你需要准备以下基础环境：

- **Python 环境**：确保系统已安装 Python 3.10 或更高版本。
- **依赖安装**：在项目根目录执行以下命令安装依赖：
  ```bash
  pip install -r requirements.txt
  ```
- **语音功能（可选）**：如果需要通过飞书发送语音消息并获得 Agent 的语音回复，请额外安装本地 STT 库：
  ```bash
  pip install faster-whisper
  ```
- **本地大模型（可选）**：出于合规或数据隐私考虑，对于包含敏感信息（如日志数据）的提取工作，Agent 可配置为使用本地 Ollama 模型进行脱敏。

## 3. 配置说明

Agent 的主要配置文件位于项目根目录的 `config.yaml`（实际运行时默认读取 `~/.hermes/config.yaml`）。以下是核心配置段说明：

### 模型配置 (`model`)
指定默认对话使用的云端强模型：
```yaml
model:
  default: "claude-sonnet-4-20250514"
  provider: "anthropic"
```

### 飞书接入 (`platforms.feishu`)
需要通过环境变量配置飞书机器人的鉴权信息：
```yaml
platforms:
  feishu:
    app_id: "${FEISHU_APP_ID}"
    app_secret: "${FEISHU_APP_SECRET}"
    verification_token: "${FEISHU_VERIFICATION_TOKEN}"
    encrypt_key: "${FEISHU_ENCRYPT_KEY}"
```

### SRE 权限与审批 (`sre_permissions`)
严格控制不同用户的操作权限及免批规则。
- **操作者 (`operators`)**：设定用户角色、可用命名空间和工具。
- **审批规则 (`approval_rules`)**：指定哪些操作需要谁来审批。
```yaml
sre_permissions:
  operators:
    - name: "管理员"
      platform: "feishu"
      platform_user_id: "ou_placeholder_admin"
      role: "admin"
      namespaces: ["*"]
      allowed_tools: ["k8s_read", "k8s_write", "k8s_exec"]
      can_approve: true
    - name: "运维员"
      platform: "feishu"
      platform_user_id: "ou_placeholder_operator"
      role: "operator"
      namespaces: ["default", "staging"]
      allowed_tools: ["k8s_read", "k8s_write"]
      can_approve: false

  approval_rules:
    - tool: "k8s_write"
      namespace: "production"
      require_approval_from: "admin"
    - tool: "k8s_write"
      namespace: "staging"
      auto_approve: true # 自动批准 Staging 环境的写操作
```

### 语音配置 (`stt`/`tts`)
开启语音交互：
```yaml
stt:
  enabled: true
  provider: "local"
  model: "base"
  language: "zh"

tts:
  provider: "edge"
  voice: "zh-CN-YunxiNeural"
  rate: "+10%"
  auto_reply_voice: true
```

### 通知防疲劳 (`notification`)
控制 Agent 主动推送告警的频率，防止信息轰炸：
```yaml
notification:
  severity_filter: "warning" # 只推送 warning 及以上级别
  dedup_window: 14400 # 同一告警 4 小时内去重
  daily_digest: "09:00" # 次要告警每天 9 点发摘要
  quiet_hours:
    start: "23:00"
    end: "07:00"
    except: "critical" # 夜间免打扰，critical 除外
  max_per_hour: 10 # 每小时最多推送 10 条
```

### LLM 降级规则 (`fallback_rules`)
当大模型 API 故障时，退化为规则引擎：
```yaml
fallback_rules:
  - alert: "KubePodCrashLooping"
    action: "kubectl describe pod {pod} -n {namespace} && kubectl logs {pod} -n {namespace} --tail=100"
    deliver: "origin"
```

### 自监控 (`health`)
Agent 自身的存活心跳配置：
```yaml
health:
  heartbeat_interval: 300
  heartbeat_channel: "ops"
  max_missed_heartbeats: 3
```

## 4. 启动方式

启动 Agent 前，确保已设置必须的环境变量（如飞书凭证：`FEISHU_APP_ID`, `FEISHU_APP_SECRET`, `FEISHU_VERIFICATION_TOKEN`, `FEISHU_ENCRYPT_KEY` 等）。

- **飞书 Gateway 模式（生产使用）**：
  监听网络请求，通过飞书与用户交互：
  ```bash
  python -m hermes --mode gateway --platform feishu
  ```
- **CLI 模式（开发调试）**：
  在终端中直接进行文字交互测试：
  ```bash
  python -m hermes --mode cli
  ```

## 5. 日常使用

你只需在飞书客户端找到配置好的机器人进行私聊即可。

- **文字消息交互**：发送如“看下 default 命名空间的 pod 状态”或“帮我重启一下 api-server”，Agent 会进行分析并回复。
- **语音消息交互**：直接按住飞书的语音按钮说话（如“现在集群怎么样？”）。Agent 收到后会自动转换为文字、携带当前活跃事件上下文进行诊断，并以语音（TTS）形式向你播报经过压缩摘要后的结果。
- **语音开关命令**：可以通过在对话中输入特定命令调整语音模式：
  - `/voice on`：开启语音自动回复
  - `/voice off`：关闭语音自动回复
  - `/voice tts`：手动请求某段文本的 TTS

## 6. SRE 工具集

Agent 拥有专为 Kubernetes 运维打造的工具箱，它们按安全级别划分了明确的使用边界：

- **只读查询（无需审批）**：
  - `k8s_read`：执行 `get`, `describe`, `logs` 等。
  - `prometheus_query`：执行 PromQL 查询指标趋势。
  - `loki_query`：执行 LogQL 检索集中日志。
- **写操作（需标准审批）**：
  - `k8s_write`：执行 `scale`, `apply`, `delete` 等操作（受 `config.yaml` 里的规则拦截）。
- **特权操作（需高级审批）**：
  - `k8s_exec`：进入容器内部或执行宿主机网络等特权操作。
- **运维管理支持**：
  - `sre_shift_handoff`：执行运维交接，将当前未解决的事件分配给下一位值班员，并提供简报。
  - `sre_audit_record` / `sre_audit_query`：记录和查询一切变更的审计日志。
  - `sre_voice_summary`：将冗长的诊断文本压缩为适合语音播报的精简摘要。
  - `sre_health_check`：Agent 自身服务和数据库健康检查。
  - `sre_metrics` / `sre_weekly_summary`：统计采纳率、回滚率、平均诊断时间 (MTTD) 并生成中文周报。
  - `sre_cost_check`：检查模型调用的预算使用情况。
  - `sre_notification_check`：检查告警是否触碰了频率限制或静默时间。
  - `sre_fallback_match`：在模型异常时匹配应急排查脚本。
  - `sre_check_permission`：执行细粒度的工具和命名空间鉴权。

## 7. 告警自动处理流程

1. **告警接入**：确保 Alertmanager 配置了 Webhook 路由到 Agent：`POST /webhooks/alertmanager`。
2. **防风暴与去重**：Agent 首先拦截短时间内的重复告警，将突发告警风暴合并，避免打扰。
3. **AI 诊断闭环**：
   - **Triage (分类)**：评估严重性和影响范围。
   - **Investigate (调查)**：关联日志与指标，寻找根本原因。
   - **Remediate (修复)**：提出带预检（Dry-run）的修复方案。
4. **异步审批**：Agent 会推送交互式卡片到飞书，你需要点击“批准”或“拒绝”。在这个过程中，你的会话绝不会被阻塞。
5. **执行与回滚**：审批通过后 Agent 执行变更。若变更后监控显示服务不健康，Agent 有能力自动回滚（Rollback）。

### Alertmanager Webhook MVP

启动 webhook 后，`POST /webhooks/alertmanager` 会为 firing 告警创建或复用 incident，并写入 `alert_fired` timeline。resolved 告警当前默认跳过；reopen 由 firing 告警命中同一 `dedup_key` 时触发。

关键配置：

- `sre.dedup_key_version`
- `notification.reopen_window_seconds`
- `notification.storm_threshold_per_minute`

## 8. SRE Runbook Skills

Agent 将常见的排障经验固化为 Markdown 格式的 Skill (Runbook) 指南：

- `pod-crashloop`：排查容器启动即退出的 CrashLoopBackOff 故障（涵盖 OOM、探针、镜像等）。
- `node-not-ready`：排查节点失联、驱逐等 NotReady 状态异常。
- `high-memory`：诊断内存压力过高、频繁 GC 或系统级 OOM 的问题。
- `certificate-expiry`：针对（如 cert-manager 相关的）证书即将过期告警的处置指南。
- `pvc-full`：存储卷空间告急时的扩容和清理排查流程。
- `voice-triage`：专为“语音询问集群状态”优化的诊断流，省略繁杂输出，只提炼最口语化的建议。

## 9. 运维交接

在值班轮替时，你可以通过文字或语音告诉 Agent“我要交接给某某”。
Agent 会调用 `sre_shift_handoff` 工具：
1. 找出当前所有尚未解决（Open）的故障事件（Incident）。
2. 为每一个事件生成时间线简报。
3. 在系统中将这些事件的负责人自动变更为新的值班员。
4. 记录完整的交接审计日志。

## 10. 审计与合规

安全与可追溯是本 Agent 的核心原则：
- **全程审计**：无论是通过语音、文字执行的每一个 K8s 操作（尤其是读/写/执行），Agent 都会自动生成不可篡改的 SQLite 审计日志记录。
- **权限模型**：用户的飞书 ID 在会话建立初期就被绑定。只有带有 `admin` 角色的用户能够执行高危的 `k8s_exec` 或生产环境写入。操作者只能访问其授权的 namespace。
- **审计查询**：你可以随时询问 Agent “帮我查一下今天上午谁动了 production 下的 nginx 部署”。

## 11. 监控与度量

- **自监控**：Agent 的 `health_check` 机制确保底层的事件库、审批库和审计库运转正常。如果心跳丢失，它会通过独立通道发送告警。
- **SRE 指标**：你可以让 Agent 统计过去几天的核心指标，如平均诊断时间 (MTTD)、操作采纳率、以及修复失败产生的回滚率。
- **周报**：发送“生成一份周报”，Agent 会汇总过去 7 天的处理数据，用中文形成运营周报返回给你。
- **成本监控**：Agent 会跟踪消耗的 Token 成本。如果事件排查耗费金额超过配置的日常/单次事件预算，会自动降级为更廉价的模型或本地处理策略。

## 12. 常见问题 (FAQ)

- **Q: 我发了语音但 Agent 回复的是大段文字？**
  A: 请检查 `config.yaml` 中 `tts.enabled` 与 `tts.auto_reply_voice` 是否为 `true`。
- **Q: 收到审批卡片但我没看到，导致超时了怎么办？**
  A: 如果审批请求超过配置时间（例如 30 分钟）未被处理，Agent 将自动触发升级策略（Escalation），转通知给你的主管或自动取消操作。
- **Q: 我让 Agent 查询，但它一直说没有权限？**
  A: 你的飞书身份并未在 `config.yaml` 的 `sre_permissions.operators` 列表里，或你请求操作了不属于你负责的命名空间。请联系管理员添加配置。
- **Q: 如何给 Agent 添加新的经验（Runbook）？**
  A: Agent 具备“拒绝学习”能力。如果你在卡片中拒绝了 Agent 的修复建议并填写了原因，它会自动沉淀到本地经验库。如需编写结构化排障流，直接在项目的 `skills/sre/runbooks/` 目录下新增一份 `SKILL.md` 文件即可即时生效。
