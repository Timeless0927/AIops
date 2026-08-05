# Ongrid 一手资料分析与对比要点

调研日期：2026-07-16
代码基线：Ongrid [`3fb37cc45dc7fad05c5dbfb009e570e303608649`](https://github.com/ongridio/ongrid/tree/3fb37cc45dc7fad05c5dbfb009e570e303608649)（2026-07-15）
资料范围：只使用 Ongrid 官方仓库、发布页、GitHub API 与官方网站；产品文案、已实现代码、测试快照和 roadmap 分开判断。

## 结论摘要

Ongrid 与本项目都覆盖 AIOps、告警调查、可观测证据、Agent 工具调用和 Kubernetes，但产品重心并不相同：

1. **Ongrid 是 host-first 的 ChatOps / AIOps 平台。** 官方把差异化定义为 Geminio 双向隧道和 Agent 在客户主机上执行动作，metrics/logs/traces 只是基础能力；Kubernetes 是后来扩展的 full-node 接入，而不是最初的领域中心。[README](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/README.md#L3-L5) [Roadmap](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/ROADMAP.md#L16-L22) [v0.10.0](https://github.com/ongridio/ongrid/releases/tag/v0.10.0)
2. **它的产品面更宽。** 除 RCA 外，还包含浏览器 SSH、主机命令、可视化工作流、MCP、知识库、代码搜索、技能市场、周期报告和多 IM 渠道。[README](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/README.md#L30-L41) [Product tour](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/README.md#L73-L145)
3. **运行架构是“模块化单体 Manager + 独立 Edge + Frontier broker + 多个数据后端”。** Manager 内部按 bounded context 组织，但 IAM、告警、Agent、K8s、审批、通知等仍在一个 Go 进程内；Edge 是另一二进制，主动外连 Frontier。[Architecture](https://ongrid.cloud/docs/get-started/architecture) [manager entrypoint](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/cmd/ongrid/main.go#L1-L12)
4. **最关键的安全差异：Ongrid 当前 K8s `execute_k8s_action` 不是人工审批后执行。** 它要求管理员身份、Kubernetes dry-run、短期一次性 preflight token，并经过一个只读 **LLM reviewer worker**；该 worker 输出 `Decision: approve` 后代码直接执行写工具。项目另有真正的 human approvals inbox，但 K8s path 没有接入它。[K8s tool](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/aiops/tools/execute_k8s_action.go#L201-L277) [ReviewGate](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/aiops/tools/decorators/review_gate.go#L15-L22) [human approval use case](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/approval/usecase.go#L1-L6)
5. **成熟度是“能力丰富、迭代很快、仍属年轻 v0.x”。** 最新正式版是 `v0.10.0`；截至调研日仓库约创建两个月，官方工作流测试目录仍记录若干已知问题。不能用功能清单或 star 数替代生产验收。[release](https://github.com/ongridio/ongrid/releases/tag/v0.10.0) [repository API](https://api.github.com/repos/ongridio/ongrid) [workflow known issues](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/docs/workflow-catalog.md#L112-L130)

## 1. 产品定位与核心工作流

### 1.1 产品定位

官方一句话定位是“理解基础设施、找到根因并修复问题的 Ops AI Agent”，强调可直接从 Slack 或 Telegram 使用；能力清单包括 metrics/logs/traces、拓扑爆炸半径、根因关联、远程执行、告警驱动调查、RAG、代码搜索和 specialist agents。[README](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/README.md#L3-L5)

其 roadmap 更直接地说明产品护城河是 Geminio 双向通道加“Agent 能在客户机器上真动手”，可观测三件套不是差异化。[Roadmap](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/ROADMAP.md#L16-L22)

因此 Ongrid 更准确的分类是：**以主机和混合基础设施为中心的自托管 ChatOps / AIOps 平台，最近扩展到 Kubernetes**。`v0.10.0` 才加入 Kubernetes full-node 模式，且发布说明明确 serverless / 无节点权限集群尚不支持。[v0.10.0 release](https://github.com/ongridio/ongrid/releases/tag/v0.10.0)

### 1.2 三条核心工作流

**会话式调查：** 操作员或 IM 用户提问，Coordinator 基于精简 toolbag 直接取简单事实，或通过 `AgentTool` 派发 specialist worker；worker 多轮调用 metrics、logs、traces、拓扑、主机和知识工具，Coordinator 汇总结论。[runtime](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/aiops/chatruntime/runtime.go#L1-L35) [AgentTool](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/aiops/tools/agent_tool.go#L155-L172)

**告警自动调查：** 新 incident 可异步触发 `incident-investigator` worker，调查报告和审计会话落库；worker persona 明确要求沿时间、变更、拓扑、trace、日志和指标反向追到“0 号病人”，自身严格只读。[investigator use case](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/alert/investigator/usecase.go#L1-L17) [persona](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/agents/incident-investigator.md#L14-L48)

**可视化自动化：** 手动、定时或告警触发一张 DAG；节点包括 Tool、Agent、Condition、Transform 和 Notify，并通过模板传递上游数据。[workflow catalog](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/docs/workflow-catalog.md#L7-L23) 工作流定义和运行记录持久化，但执行引擎在进程内；Manager 重启时，未完成运行会被标记失败而不是续跑。[flow use case](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/flow/usecase.go#L23-L50)

## 2. 进程、模块与部署边界

### 2.1 Manager

`cmd/ongrid` 是单个云端 Manager 二进制，组合 IAM 和 Manager bounded contexts，同时提供 HTTP API、Prometheus metrics listener，并作为 service-end 客户端连接独立 Frontier broker。[main.go](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/cmd/ongrid/main.go#L1-L12)

官方架构文档称 Manager 内部按 identity、edge、alert、incident、agent、knowledge、channels、audit、skills 等 bounded context 组织，内部跨 context 使用 Go 函数调用，没有进程内 RPC。[Architecture](https://ongrid.cloud/docs/get-started/architecture) 但装配入口本身集中导入并组装这些 context，当前文件约 4,878 行；这说明它是模块化单体，而不是按领域进程隔离的服务集合。[imports](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/cmd/ongrid/main.go#L71-L180) [server startup](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/cmd/ongrid/main.go#L2352-L2363)

### 2.2 Edge 与数据面

每台受管主机运行一个 `ongrid-edge`。Edge 主动连到 Frontier 的 `40012`，Frontier 终止 Geminio 隧道，Manager 通过 service-end 连接处理注册、心跳、指标上报和反向工具调用；受管主机无需开放 SSH/HTTP 入站端口。[README](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/README.md#L32-L41) [compose topology](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/deploy/docker-compose.yml#L9-L11)

高流量 logs/traces 由 Edge 插件经 nginx 的鉴权 HTTPS 数据面直传 Loki/Tempo；控制 RPC 走 Frontier 隧道。Edge 还监督 promtail、node_exporter、process_exporter 和 otelcol 等现成采集器，而不是重新实现全部采集逻辑。[Architecture](https://ongrid.cloud/docs/get-started/architecture)

### 2.3 Kubernetes 边界

Kubernetes full-node 模式安装一个 Controller Deployment 和 Node Edge DaemonSet。Controller watch Kubernetes API、同步资源快照并承担 K8s API 动作；每个 Node Edge 获得独立 identity，复用主机诊断能力。[Kubernetes RFC](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/docs/rfc/RFC-001-kubernetes-edge-adaptation.md#L29-L68)

Chart 默认开启 inventory watch、kube-state-metrics 和 OTLP telemetry gateway，并设置资源限额。[values](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/deploy/kubernetes/ongrid-edge/values.yaml#L21-L94) Controller 以 non-root 运行；Node Edge 则为了进入 host runtime，以 root、`hostPID`、`hostNetwork`、host root mount 和多项 Linux capability 运行。[Controller](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/deploy/kubernetes/ongrid-edge/templates/deployment.yaml#L43-L114) [Node DaemonSet](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/deploy/kubernetes/ongrid-edge/templates/daemonset.yaml#L24-L138) Controller 的 ClusterRole 也不只是读权限：它可以 patch Node 和 Deployment/StatefulSet/DaemonSet、删除 Pod、创建 eviction。[RBAC](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/deploy/kubernetes/ongrid-edge/templates/rbac.yaml#L57-L87)

### 2.4 发布形态

Manager 的推荐生产式路径是单机自托管 Docker Compose，另提供 systemd 安装模式；发布包同时带 nginx、Frontier、MySQL、Prometheus、Loki、Tempo、Grafana、Qdrant 和 SearXNG。Edge 不在 Manager compose 中运行。[install modes](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/deploy/install/README.md#L13-L27) [deploy README](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/deploy/README.md#L3-L12) [edge exclusion](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/deploy/docker-compose.yml#L365-L388)

官方 release 提供 amd64/arm64 tarball 和 `sudo ./install.sh`，支持 Ubuntu 22.04+、Debian 12+、RHEL/Rocky 9。[README](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/README.md#L43-L61) 仓库的本地开发 compose 明确不适合生产，原因包括无 TLS、弱默认 MySQL 密码、无 rate limit/auth proxy/log shipping；不能把该 compose 的安全属性套用到 production-style release。[deploy warning](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/deploy/README.md#L116-L129)

## 3. 数据与状态持久化

- **业务状态：** 默认 MySQL，SQLite 仅用于本地试用；所有 data package 的 `Migrate` 在 Manager 启动时通过 GORM `AutoMigrate` 执行，没有独立 migration step。[startup migrations](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/cmd/ongrid/main.go#L241-L274) [SQLite warning](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/deploy/README.md#L100-L114)
- **Agent 状态：** chat session、message、tool call 和 parent worker session 关系落 MySQL；每条 assistant message 可记录 model 与 token 用量。[AI models](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/model/aiops/model.go#L25-L81) [messages/tools](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/model/aiops/model.go#L93-L166) 实时 worker 状态仍以内存 map 为权威，不支持 worker 嵌套，也没有跨会话共享记忆。[worker.go](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/aiops/chatruntime/worker.go#L1-L15)
- **知识：** 注册信息在 MySQL，文档正文与向量在 Qdrant；支持手工 Markdown 和 Git 仓库浅克隆/同步。[knowledge use case](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/knowledge/usecase.go#L1-L14)
- **遥测：** metrics、logs、traces 分别由 Prometheus、Loki、Tempo 持久化，Grafana 提供视图。[compose](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/deploy/docker-compose.yml#L224-L289)
- **审批与审计：** `chat_mutating_proposals` 保存 LLM reviewer 的提案与决定；独立 `approvals` 表保存人工审批 inbox。两张表代表不同安全语义，不能混为一套审批状态机。[mutating proposal model](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/model/aiops/mutating_proposal.go#L10-L44) [human approval model](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/model/approval/model.go#L1-L8)

## 4. AI / Agent 机制

默认 Agent kernel 是 CloudWeGo Eino graph-kernel ReAct；模型路由支持多 provider，toolbag 超过阈值后会延迟暴露 specialty tool schema，由 `ToolSearch` 按需发现。[compose config](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/deploy/docker-compose.yml#L71-L84) [runtime assembly](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/cmd/ongrid/main.go#L3376-L3405)

Agent persona 是 Markdown frontmatter，定义名称、用途、tools allowlist、disallowed tools、permission mode、turn budget 和提示词。`incident-investigator` 是 40 turns 的只读因果回溯 worker；`reviewer` 是最多 5 turns、默认 reject 的只读二审 worker。[investigator](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/agents/incident-investigator.md#L1-L48) [reviewer](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/agents/reviewer.md#L1-L40)

Coordinator、specialist worker、RAG/code search、MCP server 与普通 tool 共用一个 Agent surface；这让 Ongrid 更像可扩展 Agent 工作台，而不是只有固定 diagnosis pipeline 的系统。[README](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/README.md#L91-L113) [MCP](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/README.md#L99-L105)

## 5. Kubernetes 写动作与审批：必须分清两条路径

### 5.1 K8s 写工具自身的硬约束

`execute_k8s_action` 只支持固定动作集：`rollout_restart`、`scale`、`delete_pod`、`evict_pod`、`cordon`、`uncordon`、`drain`；它不是任意 `kubectl` 或任意 patch 工具。[schema](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/aiops/tools/execute_k8s_action.go#L21-L121)

当前约束包括：

- Agent 写能力默认关闭，缺配置或读配置失败都返回 false，管理员必须显式开启。[write toggle](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/setting/agent.go#L13-L24)
- 调用者必须是 admin 或 superuser。[authorization](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/aiops/tools/execute_k8s_action.go#L201-L205)
- 真写之前必须先完成匹配动作的 Kubernetes dry-run；Manager 签发 5 分钟、一次性、绑定 user/session/完整参数指纹的 preflight token，参数变化、跨会话、过期或复用都会拒绝。[dry-run result](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/aiops/tools/execute_k8s_action.go#L256-L271) [token rules](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/aiops/tools/execute_k8s_action.go#L280-L328)
- 写动作通过 Controller Edge 调用 Kubernetes API，并验证 Controller 返回的 cluster/action/target 与请求一致。[dispatch/response validation](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/aiops/tools/execute_k8s_action.go#L226-L277)

这些是有效的防误操作约束，但它们不等于人工授权。

### 5.2 K8s `ReviewGate` 是 LLM reviewer，不是 human approval

所有 toolbag 工具在生产装配时经过统一 decorator chain，写/破坏类工具由 `ReviewGate` 拦截并记录 `chat_mutating_proposals`。[chain assembly](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/cmd/ongrid/main.go#L3376-L3405)

`ReviewGate` 的实际语义非常明确：

1. 启动 `agents/reviewer.md` 对应的 reviewer worker；它是一次 LLM graph 调用，默认最多 5 turns。[ReviewGate definition](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/aiops/tools/decorators/review_gate.go#L15-L49)
2. 解析 reviewer 最终自然语言中的 `Decision: approve|reject`。[result contract](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/aiops/tools/decorators/review_gate.go#L130-L145)
3. `approve` 时立即调用 inner tool；`reject`、无有效决定、worker error 或 timeout 时 fail closed。[execution branch](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/aiops/tools/decorators/review_gate.go#L309-L348)
4. K8s chat e2e 测试也是注入 reviewer 的 `Decision: approve` 后断言 Controller 调用与 proposal executed，而不是等待人工决策。[K8s e2e](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/aiops/chatruntime/k8s_action_e2e_test.go#L88-L166)

因此当前 K8s 变更链应准确表述为：

```text
管理员显式开启 Agent write
  → admin/superuser 发起固定 K8s 动作
  → LLM reviewer 二审
  → Kubernetes dry-run
  → 5 分钟一次性 preflight token
  → Controller 执行
  → proposal/tool audit
```

它不是“人点击 Approve 后才执行”。

### 5.3 通用 human approvals inbox 是另一条链

Ongrid 确实有真正的人工 propose-confirm inbox：producer 先创建 pending row，人在 UI 中 approve/reject，approve 后按 `kind` 调用已注册 executor，并记录 execution result。[human approval use case](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/approval/usecase.go#L1-L6) [approve execution](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/approval/usecase.go#L104-L142)

当前 Manager 明确为 `cloud_bash`、`host_bash`、`mcp_call`、`install_skill` 注册了 human-approval executor，没有注册 K8s executor。[registered executors](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/cmd/ongrid/main.go#L1856-L1971) `cloud_bash`/`host_bash` 还会在 SSE 流中发 approval card，并阻塞等待人工决定。[human wait path](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/cmd/ongrid/main.go#L3765-L3847)

结论：**不能把 Ongrid 的 K8s ReviewGate 与本项目要求的显式 human approval / execution authorization 视为同一强度。** 前者是模型二审加技术 preflight；后者才是人类授权事件。

## 6. 人机交互、通知与集成

Ongrid 同时提供 React Web Console 和 IM ChatOps。IM bridge 把 Slack、Telegram、飞书、钉钉会话映射到持久化 chat session，并把 Agent 流式结果渐进更新回 IM。[IM bridge](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/imbridge/bridge.go#L1-L5) [stream mapping](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/imbridge/bridge.go#L98-L117)

Slack 入站走 Socket Mode，由 Manager 主动建立 WebSocket；Telegram 走 `getUpdates` long poll，飞书支持长连接，均可在 provider 层配置 sender allowlist。[Slack](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/imbridge/provider/slack/client.go#L1-L14) [provider wiring](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/cmd/ongrid/main.go#L1418-L1442)

告警和工作流通知支持 Slack、飞书、钉钉、企业微信、Telegram 和 generic webhook；每次通知有 delivery 记录，后台 worker 对失败 delivery 最多重试 5 次。[channel builders](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/alert/usecase.go#L1394-L1431) [retry worker](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/internal/manager/biz/alert/retry.go#L14-L55)

它也支持外部 MCP server、模型 provider 和现有 observability stack 接入；README 列出的模型包括 Anthropic、OpenAI、Gemini、DeepSeek、GLM、Kimi。[integrations](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/README.md#L159-L167)

## 7. 成熟度与许可证

截至 2026-07-16：

- GitHub API 显示仓库创建于 2026-05-23，约 458 stars、106 forks、36 open issues；这些数字随时间变化，只能作为调研快照。[repository API](https://api.github.com/repos/ongridio/ongrid)
- 最新正式版 `v0.10.0` 发布于 2026-07-15，仍是快速演化的 v0.x 系列。[release](https://github.com/ongridio/ongrid/releases/tag/v0.10.0)
- 官方工作流目录记录 5 个多步只读流程和 31 个单工具样例，同时记录 Agent flow tool 缺失、`cloud_bash` 装配和 incident detail 挂起等已知问题；该文档是 2026-06-25 的测试快照，不能自动推断这些问题在当前 commit 仍全部存在，但足以说明产品仍处高频收敛期。[workflow verification](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/docs/workflow-catalog.md#L96-L130)
- CI 包含 Go build/vet/`go test -race ./...`、前端测试/build 与 Helm lint/render；真实外部环境的 `e2e` build-tag 测试不在常规 CI 中。[CI](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/.github/workflows/ci.yml#L47-L89)

代码许可证为 **AGPLv3**，项目 roadmap 自称 open-core。品牌名称、Logo 和视觉资产不在 AGPL 授权内；fork 或商业发行需要明确独立身份，不能暗示官方背书。[README license](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/README.md#L169-L174) [open-core statement](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/ROADMAP.md#L3-L7) [trademark policy](https://github.com/ongridio/ongrid/blob/3fb37cc45dc7fad05c5dbfb009e570e303608649/TRADEMARK.md#L42-L56)

## 8. 与本项目的直接对比

本项目事实以 [领域术语](../../CONTEXT.md)、[当前架构](../current-architecture.md)、[V1 规格](../../.scratch/aiops-v1-model/PRD.md) 和当前 [Pilot tickets](../../.scratch/aiops-pilot-ready/tickets.md) 为准。

| 维度 | Ongrid | 本项目 AIOps | 实际含义 |
| --- | --- | --- | --- |
| 产品中心 | Host-first 的 ChatOps/AIOps 工作台，K8s 是后加的 full-node 能力 | Kubernetes-only Incident 与受控变更 control plane | 功能有重叠，但替代关系没有表面看起来那么强；两者 wedge 不同。 |
| 管理对象 | Linux 主机、网络/数据库工具、K8s、知识库、工作流、MCP、WebSSH | 单组织、多 K8s Cluster、Service/Deployment Target、Incident/Investigation/Change | Ongrid 覆盖面更宽；本项目领域对象更少但关系和治理更深。 |
| 用户入口 | Web Console 加 Slack/Telegram/飞书/钉钉等双向 ChatOps | Console 是唯一调查与审批入口；通知只负责告知 | Ongrid 到达用户的路径明显更强；本项目避免 IM 文本被误当成执行授权。 |
| Incident 模型 | Alert Incident 关联一份 `InvestigationReport`；强制重跑会删除旧 report 再生成 | Incident 拥有有序 Investigations、不可变 Event/Evidence、恢复稳定窗口和版本化人工发布 Report | 本项目更适合重建完整事故治理历史；Ongrid 更像一次 Agent RCA 产物。 |
| 证据语义 | Agent transcript/tool calls 经第二次 LLM 提取为 report evidence，confidence 含模型自评 | Gateway canonicalize Evidence Step；确定性 Evidence Gate 校验 scope、freshness、reference integrity | 本项目把“模型判断”和“可审批证据”分开；这是核心差异，不应弱化。 |
| K8s 变更范围 | 固定动作：restart/scale、Pod delete/evict、Node cordon/uncordon/drain | 模型生成 canonical create、RFC 6902 patch 或 preconditioned delete，可覆盖 cluster-scoped resource/CRD | Ongrid 动作窄、容易理解；本项目能力更通用，也必须承担更强的 policy 与审计成本。[通用变更 ADR](../adr/0053-generic-kubernetes-change-contract.md) |
| K8s 授权 | write toggle + admin + LLM reviewer + dry-run/token；K8s 未接 human inbox | Environment 与真实资源 scope Authority、fresh auth、冻结 diff/hash、显式 human Approval、Execution Grant | 两边都有限制，但只有本项目把人工决定作为独立、持久、可重验的执行授权事实。 |
| 结果不确定性 | K8s tool 通过同步 tunnel call 执行；preflight grant 在 Manager 内存中 | Gateway durable Connector Command、Connector journal、lease、Unknown Outcome/Observed Effect reconciliation，mutation 不自动 retry | 网络响应丢失和进程重启时，本项目状态机更保守、可审计。 |
| 集群侧权限 | Controller K8s RBAC 是固定资源/动作；每节点 Edge 以 root + host namespace/mount/capabilities 运行 | Connector 使用 wildcard K8s API read/create/patch/delete ClusterRole，但不进入宿主机 | 风险形态不同：Ongrid host blast radius 更大，本项目 Kubernetes credential blast radius 更大；不能笼统称一方更安全。 |
| 运行架构 | 模块化单体 Go Manager + Edge + Frontier；业务状态集中 MySQL | Gateway、Diagnosis、Connector、MCP、Notification、Console 分进程；各 state owner 使用独立 SQLite/PVC | Ongrid 部署和事务更集中、MySQL 并发基础更成熟；本项目进程/状态 owner 隔离更清楚，但当前单副本 SQLite 不具备 HA。 |
| Agent 平台 | Coordinator/specialists、skills、MCP registry、RAG/code search、workflow builder、多 provider routing | 固定 Diagnosis pipeline 与受限 MCP evidence adapter；单一 OpenAI-compatible Provider revision | Ongrid 的扩展面和 BYOM 体验更领先；本项目更容易约束模型能做什么。 |
| 可观测性 | 内置 Prometheus/Loki/Tempo/Grafana/Qdrant，支持 metrics/logs/traces/topology | 当前以 Prometheus/Loki/Topology evidence 为主，未引入 tracing/RAG | Ongrid 的证据面更完整；本项目的 trace 与知识缺口会限制复杂 RCA 精度。 |
| 通知 | 多 IM 双向会话和多渠道发送，通知在 Manager 域内 | 独立 Notification Engine，route/template/destination/retry/dead-letter 自己持久化；Feishu/DingTalk/SMTP notification-only | Ongrid 渠道覆盖和 ChatOps 更强；本项目交付状态与业务状态解耦更彻底。 |
| 安装形态 | Manager 单机 Compose/systemd；K8s 只安装 Edge Helm chart | 整个 control plane 通过固定 Kustomize bundle 安装进 K8s，远端 Connector 主动外连 | Ongrid 更适合管理异构主机；本项目更适合已有 Kubernetes 平台团队。[安装 ADR](../adr/0054-single-command-pilot-release-installation.md) |
| 开源交付 | AGPLv3、九种 README 语言、产品 GIF、正式 release tarball、约 458 stars/106 forks | GitHub 仓库公开，但当前无根 README、无 LICENSE、无 GitHub Release，0 stars/0 forks | 即使本项目内部能力更深，外部用户现在几乎无法发现、理解、合法复用或快速试用。[repository API](https://api.github.com/repos/Timeless0927/AIops) |

## 9. 判断与建议

### 9.1 不要把产品改造成 Ongrid

本项目最有辨识度的不是“也能用 AI 查 Prometheus/Loki”，而是：**把 Incident、Evidence、Authority、Approval、Execution、Unknown Outcome 和 Report 做成 Gateway-owned、持久、可重验的治理事实。** 如果现在加入 WebSSH、通用工作流、技能市场、主机管理和多 Agent 自定义，会直接稀释 Pilot accepted path，也会扩大当前 wildcard Connector credential 之外的攻击面。

继续坚持两条边界：

1. 模型输出、LLM reviewer、Human Input 和通知都不构成 Kubernetes 执行授权。
2. Connector 继续是唯一 Kubernetes API Adapter，但必须正视 wildcard ClusterRole 的 credential compromise 风险；不要用更强的应用层审批来掩盖它。

### 9.2 最值得借鉴的三件事

1. **先补开源产品面。** 根 README、清楚的架构图/安全边界、可见 demo、版本化 release、校验和、中文与英文快速开始，收益远高于再加一个 Agent feature。当前仓库没有 LICENSE；在确定许可证前，不应声称项目可被开源复用。
2. **把 IM 用作调查入口，不用作授权入口。** 可增加 Incident 通知后的只读追问、状态订阅和 Console deep link；Approval 仍回 Console 完成 fresh auth、scope disclosure 和 exact target confirmation。
3. **按证据缺口增加 Adapter。** 优先用 Pilot 真实失败样本判断 Tempo/trace、代码/RAG 或更强 topology 哪一个最能提高 root-cause precision；不要先复制完整 Knowledge Vault、MCP marketplace 或 workflow builder。

### 9.3 暂时不要借鉴

- 不把 LLM reviewer 的 `Decision: approve` 接到执行链。
- 不增加浏览器 SSH、host root agent 或通用 shell。
- 不为“支持很多模型”建立 provider registry；当前单一 OpenAI-compatible endpoint 已覆盖大多数 provider，等真实切换/故障转移需求出现再建 seam。
- 不迁回模块化单体，也不为了追求服务数量继续拆进程；保持现有 owner 边界即可。

### 9.4 许可证提醒

Ongrid 代码是 AGPLv3，品牌资产另受商标限制。本项目当前没有 LICENSE。可以研究其公开接口与产品思路，但在本项目许可证策略确定前，不应直接复制 Ongrid 代码、前端组件或视觉资产；若未来需要代码级复用，应先完成法律/许可证兼容性评估。
