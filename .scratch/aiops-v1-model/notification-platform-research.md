# V1 通知平台开源替代研究

> 核查时间：2026-07-11。只采用项目官方文档、官方仓库与许可证作为事实来源。

## 结论

**不能通过引入开源项目把 T17-T20 变成“只写 Web”。** 浏览器不能持有 Provider credential，也不能可靠承担持久队列、重试、dead-letter、redelivery 和审计；按照现有边界，Console 仍须只访问 Gateway，Gateway/Notification Engine 必须保留服务端管理 API 与可靠投递状态。

V1 推荐采用：

`Gateway durable channel-neutral request -> 最小 Notification Engine -> Apprise library -> Feishu / DingTalk / SMTP`

Apprise 负责三种渠道的协议、签名和发送，平台只保留自身不可外包的领域责任：route、template version、quiet hours/silence、delivery ledger、lease/restart recovery、retry/dead-letter/redelivery 和 audit。渠道 credential 以 authenticated-encryption ciphertext 保存在 `notification.db`，数据库加密密钥只从独立 Kubernetes Secret 读取。

这能省掉三套 Provider 实现，但不能省掉 Notification Engine。优先直接在现有 Python Engine 中调用 Apprise library；不再部署 Apprise API，少一个进程、网络跳转和鉴权面。

## 候选对比

| 候选 | 许可证/状态 | 三渠道 | 编排与噪声控制 | 可靠投递/审计 | 判断 |
| --- | --- | --- | --- | --- | --- |
| **Apprise** | BSD-2-Clause，活跃 | 原生 Feishu、DingTalk、SMTP | 只有 tags/config，不是领域工作流 | 不提供 durable queue、DLQ、审计 | **推荐作为 Provider adapter** |
| **Novu** | Open Core：core MIT，部分目录专有；活跃 | SMTP 原生；无官方 Feishu/DingTalk provider，可用 Chat Webhook 自行适配 | workflow、condition、digest、delay、preference 较完整 | Activity Feed 可查执行，但没有找到满足本项目 DLQ/manual redelivery 契约的官方保证 | 不推荐 V1，运维和适配成本高于保留最小 Engine |
| **Prometheus Alertmanager** | Apache-2.0，活跃 | SMTP 原生；飞书/钉钉需 webhook bridge | route、group、time interval、silence/inhibition 完整 | 有重试和 HA notification log，但不是本项目 delivery ledger/DLQ/audit | 不推荐，模型绑定 `firing/resolved` alert，无法自然承载 Approval、Execution、Report 事件 |
| **Knock** | 开源的是 SDK、组件和文档，未开源服务端 | 商业服务能力较全 | 较全 | 由 Knock Cloud 承担 | 不符合自托管开源前提 |
| **ntfy** | Apache-2.0，活跃 | 主要是 ntfy push/topic；不是飞书/钉钉/SMTP 编排器 | schedule/template，但无所需 route/digest/silence 模型 | message cache 不是 delivery ledger/DLQ | 不推荐，可作为额外 push channel，不能替代 T17-T20 |
| **Grafana OnCall OSS** | AGPL-3.0；2025-03-11 进入维护模式，2026-03-24 归档 | 偏 on-call 渠道 | escalation/route | 已停止维护 | **排除** |

## 候选事实

### Apprise / Apprise API

- Apprise 是 BSD-2-Clause 的通知发送 library，官方仓库当前仍活跃：[仓库](https://github.com/caronc/apprise)、[LICENSE](https://github.com/caronc/apprise/blob/master/LICENSE)。
- 官方源码内已有 [Feishu](https://github.com/caronc/apprise/blob/master/apprise/plugins/feishu.py)、[DingTalk](https://github.com/caronc/apprise/blob/master/apprise/plugins/dingtalk.py) 和 [Lark](https://github.com/caronc/apprise/blob/master/apprise/plugins/lark.py) provider；SMTP/Email 配置见[官方服务文档](https://appriseit.com/services/email/)。这正好删除本项目最不值得自研的渠道协议代码。
- Apprise API 是 MIT 的轻量 REST wrapper，提供容器、`/notify`、配置文件和简单管理页，也允许挂载 custom plugin：[官方仓库与部署说明](https://github.com/caronc/apprise-api)。但它仍只是发送网关，没有本项目需要的 route/template version/quiet hours/digest/silence/durable delivery/DLQ/audit。
- 因现有 Notification Engine 本身是 Python 进程，直接调用 library 比另起 Apprise API 更小。只有未来多个非 Python 服务需要共享同一发送网关时，才考虑 Apprise API。

### Novu

- Novu 是 Open Core，不是整个仓库统一 MIT：core 使用 [MIT](https://github.com/novuhq/novu/blob/next/LICENSE-MIT)，`enterprise` 等目录受[专有许可证](https://github.com/novuhq/novu/blob/next/LICENSE-ENTERPRISE)约束，范围由[官方 README License 段](https://github.com/novuhq/novu#license)说明。
- Community Self-Hosted 有 workflow、GUI、digest 与多渠道，但没有 RBAC、OIDC/MFA，且团队成员上限为 1；官方对照表还明确部分能力只在 Cloud/Enterprise：[Self-Hosted 与 Cloud 对照](https://docs.novu.co/community/self-hosted-and-novu-cloud)。因此不能把其 Dashboard 直接当成本项目 `/admin` 权限模型。
- 官方 self-host 要求 MongoDB、Redis、对象存储和 API/Worker/WebSocket/Dashboard 多个服务；单机建议资源也明显高于本项目当前单副本 SQLite Engine：[Self-host requirements](https://docs.novu.co/community/self-hosting-novu/overview)、[Docker deployment](https://docs.novu.co/community/self-hosting-novu/deploy-with-docker)。
- 官方 provider 清单包含 Custom SMTP 与 Chat Webhook，但不包含 Feishu/DingTalk：[Provider list](https://github.com/novuhq/novu#providers)。接入中国群机器人仍要维护 webhook/provider 适配。
- Novu 的 [Digest](https://docs.novu.co/framework/digest)、[Schedule](https://docs.novu.co/platform/inbox/features/schedule) 和 [Activity Feed](https://docs.novu.co/platform/workflow/monitor-and-debug-workflow) 能替代部分 T18-T19，但没有找到 Community Self-Hosted 对 dead-letter、修复 credential 后保留原失败历史并 manual redeliver 的官方契约。不能用 Activity Feed 推定满足 T20。

结论：Novu 是完整产品平台替换，不是轻量 dependency。为 V1 三渠道引入 MongoDB/Redis/多服务，再补中国 provider、Gateway proxy 和项目审计语义，比当前最小 Engine 更重。

### Knock

Knock 官方 GitHub organization 公开的是 docs、SDK、React components 和 examples，没有可部署的 Knock server：[官方 organization](https://github.com/knocklabs)。例如 Node SDK 是 Apache-2.0，但说明本身就是调用 Knock API 的 client：[knock-node](https://github.com/knocklabs/knock-node)。SDK 开源不等于平台后端开源，因此不满足“开源、自托管 Kubernetes”约束。

### ntfy

ntfy 是 Apache-2.0 的自托管 HTTP pub/sub push 服务：[仓库与许可证](https://github.com/binwiederhier/ntfy)。它有 SQLite message cache、HTTP API、Web UI、[scheduled delivery 和 message template](https://docs.ntfy.sh/publish/)，也可配置 SMTP 转发，但核心抽象是 topic/subscriber，不是多 Provider 领域通知编排。它没有飞书/钉钉 adapter，也没有项目要求的 route、delivery ledger、dead-letter/redelivery 与审计，因此不能替代 T17-T20。

### Grafana OnCall OSS

官方仓库明确写明：2025-03-11 进入 maintenance mode，2026-03-24 归档；仓库已迁入 cold storage 且标记 archived：[官方 README](https://github.com/grafana-cold-storage/oncall#readme)。许可证为 [AGPL-3.0](https://github.com/grafana-cold-storage/oncall/blob/dev/LICENSE)。新系统不应依赖已归档项目。

### Prometheus Alertmanager

- Alertmanager 是活跃的 Apache-2.0 项目：[仓库](https://github.com/prometheus/alertmanager)、[LICENSE](https://github.com/prometheus/alertmanager/blob/main/LICENSE)。
- 官方能力包括 deduplicate、group、routing、silence 与 inhibition；receiver 有 SMTP 和 generic webhook：[概览](https://prometheus.io/docs/alerting/latest/alertmanager/)、[配置](https://prometheus.io/docs/alerting/latest/configuration/)。`group_wait/group_interval` 可近似 digest，`mute_time_intervals/active_time_intervals` 可近似 quiet hours，Silence 有管理 API：[Management API](https://prometheus.io/docs/alerting/latest/management_api/)。
- 它的输入与生命周期固定为 Prometheus-style `firing/resolved` alerts。把 Approval、Execution、Connector、Report 等领域事件伪装成 alert 会产生第二套状态语义；飞书/钉钉还需外部 webhook bridge。Alertmanager 的 notification log 用于去重/HA，不是可由本项目管理员查询、修复和 redeliver 的 delivery ledger。

结论：如果范围只有 Prometheus Alert，Alertmanager 会是首选；本项目事件范围更广，因此不采用。

## 对 T17-T20 的最小调整建议

- **T17**：保留 first-match route、simulation、destination validation；credential 继续加密写入 `notification.db`，数据库加密密钥改为独立 Kubernetes Secret 只读挂载，不生成 installation key file。
- **T18**：保留 restricted template、preview、immutable rendered history；Provider presentation 统一交给一个 Apprise adapter，不保留现有 Feishu 专用实现和新的 DingTalk/SMTP 实现。
- **T19**：保留 quiet hours、hourly limit 与 bounded audited Silence。若首批用户没有真实 digest 需求，删除通用 digest；需要时只做固定窗口 aggregation，不做可扩展工作流。
- **T20**：必须保留 lease/restart recovery、bounded retry、dead-letter、manual redelivery、at-least-once 表述与完整 history；这些是本项目可靠性契约，Apprise 不提供。
- **Web**：Console 只管理上述配置与状态；浏览器继续只访问 Gateway。Gateway 调 Notification Engine 的内部管理 API，绝不把 Apprise URL、Webhook token、SMTP password 或内部服务地址发给浏览器。

## 决策

V1 选择 **Apprise library 作为唯一 Provider adapter**，不引入 Novu、Knock、ntfy、Grafana OnCall 或 Alertmanager，也不部署 Apprise API。该决策只替换渠道 transport，不改变 Gateway 的 channel-neutral durable request、Notification Engine 的数据库所有权和浏览器访问边界。
