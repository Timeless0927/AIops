# AIOps Feishu SRE Agent Kubernetes Deployment Design

## Goal

将当前 AIOps Feishu SRE Agent 运行形态收敛为 Kubernetes 内可部署、可自举、最小改造的生产形态，同时保持现有 incident、Feishu 会话绑定、审批、消息补偿、system mode、Alertmanager webhook 等能力不重构。

本设计只覆盖运行与部署方案，不改变业务功能边界。

## Scope

本次设计覆盖以下内容：

- 容器镜像组成
- 运行进程模型
- Kubernetes 配置注入方式
- Hermes 无交互自举方式
- Kubernetes 访问授权方式
- `kubectl` 依赖边界
- Deployment/Service/ServiceAccount/RBAC 最小资源集合
- 单实例状态存储方案

本次设计不覆盖以下内容：

- 将 SQLite 替换为外部数据库
- 将 Kubernetes 工具从 `kubectl` 重构为 Python Kubernetes client
- 多副本高可用改造
- Helm Chart 或 GitOps 模板封装

## Constraints

- 不再挂载外部 `kubeconfig`
- 通过 Pod 的 `ServiceAccount` 访问 Kubernetes API
- 配置通过 `ConfigMap` 和 `Secret` 注入
- 不依赖 `hermes gateway setup` 或手工初始化 `~/.hermes`
- 保留当前 `k8s_read`、`k8s_write`、`k8s_exec` 工具接口
- 保留现有 incident/Feishu conversation closure 实现

## Recommended Approach

采用单镜像、单 Pod、双进程、单副本方案：

- 进程 1：`hermes gateway`
- 进程 2：`python3 -m hooks.alert_webhook_server --host 0.0.0.0 --port 8765`

镜像内包含：

- Python 运行时
- 项目代码
- Hermes CLI / gateway 运行依赖
- `kubectl` 二进制

运行期由 entrypoint 自动完成：

1. 创建 `~/.hermes/`
2. 根据环境变量渲染 `~/.hermes/config.yaml`
3. 启动 `gateway` 和独立 `webhook`

推荐该方案的原因：

- 与当前代码实现最一致，不需要改造现有 Kubernetes 工具模型
- 继续复用 incident SQLite 本地状态库
- 不依赖人工初始化 Hermes
- 最快落地到集群运行

## Architecture

逻辑组件如下：

- `gateway`：负责 Feishu 收消息、线程内回复、会话路由
- `alert_webhook_server`：负责接收 Alertmanager 事件，调用 `hooks.alert_webhook` 创建/复用/更新 incident
- `incident_store`：继续使用 SQLite WAL 持久化 incident、timeline、Feishu binding、system mode、message delivery 等本地状态
- `kubectl`：作为 `k8s_read`、`k8s_write`、`k8s_exec` 的执行底座，通过 in-cluster ServiceAccount 访问 API Server

数据流如下：

1. Alertmanager 调用 `/webhooks/alertmanager`
2. `alert_webhook_server` 写入或更新 incident，并将首条状态消息发送到 Feishu 主群或线程
3. Feishu 用户后续在同一线程继续交互
4. `gateway` 通过 thread/message/chat 反查 incident，并组装 incident-aware 上下文
5. Kubernetes 工具调用 `kubectl`，由 Pod 内 ServiceAccount 完成认证和授权

## Why Keep `kubectl`

当前代码明确依赖 `kubectl` 执行路径，而不是 Python Kubernetes client：

- `toolsets/k8s_read.py` 检查 `kubectl` 是否存在
- `toolsets/k8s_read.py` 通过 `_run_kubectl(...)` 执行命令
- `toolsets/k8s_write.py` 和 `toolsets/k8s_exec.py` 复用相同执行模型
- 审批、审计、脱敏、命令分级都围绕 `kubectl command string` 实现

因此本设计保留 `kubectl`，并将其视为镜像内的明确运行时依赖。

这样做的优点：

- 不重写现有 K8S 工具接口和审批模型
- 运行行为与人工运维排障一致
- Pod 内无需 `kubeconfig`，`kubectl` 自动使用 in-cluster 配置

局限也明确：

- 镜像需要包含 `kubectl`
- 返回结果偏文本，不是结构化 SDK 对象
- 暂不解决更复杂的 watch/patch/stream 编排问题

该局限当前可接受，因为本阶段目标是稳定部署，而不是重构 K8S 操作模型。

## Container Image Design

镜像应至少包含以下内容：

- Python 3.11 运行时
- 项目源码 `/app`
- Hermes 可执行入口 `hermes`
- `kubectl`
- 容器启动脚本，例如 `/app/deploy/entrypoint.sh`

推荐镜像职责边界：

- 镜像只负责运行，不在镜像构建阶段写入实际生产密钥
- 运行配置由环境变量注入
- SQLite 数据文件位于持久目录，例如 `/data`

## Configuration Model

配置拆分原则如下：

- `ConfigMap`：非敏感配置
- `Secret`：敏感凭据
- `env`：将两者注入为环境变量
- entrypoint：将环境变量渲染为 `~/.hermes/config.yaml`

建议放入 `ConfigMap` 的内容：

- `FEISHU_MAIN_CHAT_ID`
- `AIOPS_ALERT_WEBHOOK_HOST`
- `AIOPS_ALERT_WEBHOOK_PORT`
- 模型基础地址
- 非敏感默认参数，例如 `max_turns`、toolset 开关、notification 参数

建议放入 `Secret` 的内容：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_VERIFICATION_TOKEN`
- `FEISHU_ENCRYPT_KEY`
- 模型 `API_KEY`

## Hermes Bootstrap Model

容器内不使用交互式 `hermes gateway setup`。

entrypoint 负责生成 `~/.hermes/config.yaml`，至少要落下以下配置：

- `toolsets`
- `platform_toolsets.feishu`
- `sre.project_root`
- `sre.toolsets_dir`
- `sre.hooks_dir`
- `platforms.feishu.main_chat_id`
- 模型 provider/base_url/api_key
- SQLite 相关默认配置

这样启动时 Hermes 即处于可运行状态，不需要人工写配置文件。

## Kubernetes Access Model

Kubernetes 访问方式固定为：

- Pod 绑定专用 `ServiceAccount`
- `kubectl` 使用 in-cluster ServiceAccount token 和集群 CA
- 不挂载任何用户 `kubeconfig`

该模式的好处：

- 权限由 K8S 原生 RBAC 控制
- 不依赖外部凭据文件
- 镜像在任意集群环境都更容易迁移

## RBAC Design

推荐从最小权限开始，不直接授予 `cluster-admin`。

第一阶段建议：

- 如果只面向固定 namespace，使用 `Role + RoleBinding`
- 如果确实需要跨 namespace 读资源，再使用 `ClusterRole + ClusterRoleBinding`

权限设计要与当前工具能力对齐：

- `k8s_read` 对应只读资源权限
- `k8s_write` 对应变更资源权限
- `k8s_exec` 额外需要 `pods/exec`、`pods/attach`、可能的 `pods/log` 等权限

RBAC 只负责 Kubernetes API 层面的边界；业务层仍保留你现有的审批与审计控制。

## Runtime Topology

推荐先部署为单个 `Deployment`、单副本：

- 一个 Pod 内同时运行 `gateway` 与 `webhook`
- 共享同一份本地 SQLite 状态文件

这样设计的原因：

- 现阶段最简单
- 不需要引入外部数据库
- 可以直接复用当前 incident/message_delivery/system_mode 存储实现

限制：

- 当前不适合水平扩容到多副本
- Pod 重建时如果没有持久卷，会丢失本地 SQLite 状态

因此推荐同时挂载一个持久卷到 `/data`，并把 SQLite 文件放在该目录。

## Kubernetes Resources

最小资源集合如下：

- `Namespace`，如果需要独立命名空间
- `ConfigMap`
- `Secret`
- `ServiceAccount`
- `Role` / `RoleBinding` 或 `ClusterRole` / `ClusterRoleBinding`
- `PersistentVolumeClaim`
- `Deployment`
- `Service`

若 Alertmanager 从集群外访问，则再补：

- `Ingress` 或 `Gateway API` 路由

若 Alertmanager 在同集群内，则 `ClusterIP Service` 即可。

## Service Exposure

`alert_webhook_server` 暴露 HTTP 端口 `8765`。

建议：

- Pod 内部 `containerPort: 8765`
- `Service` 暴露 `8765`
- Alertmanager 指向该 Service 域名或 Ingress 域名

`hermes gateway` 走 Feishu WebSocket 出站连接，不需要额外 Service 暴露给外部用户。

## Health and Probes

建议为 Pod 配置探针，至少覆盖以下内容：

- `startupProbe`：给 `gateway` 足够时间建立 Feishu WebSocket 连接
- `livenessProbe`：检查 webhook HTTP 端口是否存活
- `readinessProbe`：检查 webhook 端口已可接受请求

如果后续需要更严格的健康检查，可将现有 `health_check` 能力暴露为 HTTP endpoint 再接入探针。

## Logging

日志策略保持简单：

- `gateway` 日志输出到 stdout/stderr
- `alert_webhook_server` 日志输出到 stdout/stderr
- 由 Kubernetes 日志采集系统统一收集

不建议在容器内继续做本地日志轮转。

## Error Handling

运行期主要失败模式如下：

1. `gateway` 无法连接 Feishu
2. webhook 服务未成功启动
3. `kubectl` 不存在或版本异常
4. ServiceAccount 权限不足导致 K8S 工具失败
5. SQLite 文件不可写或卷异常

建议处理方式：

- 启动脚本显式检查 `hermes` 和 `kubectl` 是否存在
- 启动脚本在生成配置前校验关键环境变量是否存在
- 缺少关键配置时直接失败退出，避免半启动状态
- K8S 权限不足由工具错误直接暴露，交给审计/日志和后续 RBAC 修正

## Testing Strategy

实施前后建议验证以下几类内容：

- 单元测试：继续使用当前 webhook、Feishu conversation、incident store、voice context 测试集
- 容器验证：镜像内确认 `hermes`、`kubectl`、Python 入口可执行
- 集群验证：Pod 内执行 `kubectl auth can-i` 和基础 `kubectl get` 检查 SA 权限
- 业务验证：
  - Alertmanager `firing` 能创建 incident 并推送到 Feishu 主群
  - 线程内追问能命中同一 incident
  - `resolved` 能闭环到同一 incident

## Alternatives Considered

### Alternative 1: Python Kubernetes Client Instead of `kubectl`

不推荐当前阶段采用。

原因：

- 现有工具接口和审批模型均基于 `kubectl` 命令字符串
- 切换到 SDK 不是替换执行器，而是要重做一层意图解析和工具接口
- 当前收益小于改造成本

### Alternative 2: Split `gateway` and `webhook` Into Separate Deployments

当前阶段不推荐。

原因：

- 会引入本地 SQLite 共享问题
- 若不先外置数据库，服务拆分会增加状态一致性复杂度
- 当前单副本控制面没有必要过早拆分

### Alternative 3: Horizontal Scaling With SQLite Kept Local

明确不采用。

原因：

- incident/message_delivery/system_mode 都依赖本地状态一致性
- 多副本下无法保证同一 incident 会话一致路由

## Decision Summary

最终采用以下决策：

- 保留 `kubectl` 作为 Kubernetes 工具执行底座
- 镜像内显式封装 `kubectl`
- 使用 Pod `ServiceAccount` + RBAC 访问集群
- 不挂载 `kubeconfig`
- 使用 `ConfigMap + Secret + env` 注入配置
- 由 entrypoint 自动渲染 `~/.hermes/config.yaml`
- 不依赖 `hermes gateway setup`
- 单镜像、单 Pod、双进程、单副本运行
- SQLite 继续作为当前阶段状态存储，并建议挂持久卷

## Implementation Boundaries

下一阶段实施应只做以下内容：

- Docker 镜像构建
- entrypoint 编写
- 配置模板渲染
- K8S manifests 编写
- 必要的路径/数据目录调整
- 面向集群运行的验证

下一阶段不应做以下内容：

- 重写 Kubernetes 工具为 SDK 模型
- 引入外部数据库
- 进行大规模目录重构

