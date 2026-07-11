export type IncidentSeverity = "critical" | "high" | "medium"
export type IncidentState = "investigating" | "stabilizing" | "waiting" | "resolved"

export interface IncidentFixture {
  id: string
  severity: IncidentSeverity
  title: string
  state: IncidentState
  environment: string
  service: string
  namespace: string
  cluster: string
  team: string
  owner: string
  duration: string
  startedAt: string
  signalCount: number
  bound: boolean
  summary: string
}

export const incidents: IncidentFixture[] = [
  {
    id: "INC-1842",
    severity: "critical",
    title: "支付网关错误率持续升高",
    state: "investigating",
    environment: "prod",
    service: "checkout-api",
    namespace: "payments",
    cluster: "prod-shanghai-01",
    team: "支付平台",
    owner: "王晨",
    duration: "38m",
    startedAt: "09:12",
    signalCount: 3,
    bound: true,
    summary: "5xx 峰值达到 18.7%，影响华东区域结算请求。",
  },
  {
    id: "INC-1839",
    severity: "high",
    title: "订单消费延迟超过 SLO",
    state: "waiting",
    environment: "prod",
    service: "order-consumer",
    namespace: "orders",
    cluster: "prod-beijing-02",
    team: "交易履约",
    owner: "待认领",
    duration: "1h 12m",
    startedAt: "08:38",
    signalCount: 2,
    bound: true,
    summary: "Kafka consumer lag 持续增长，等待 Loki 证据源恢复。",
  },
  {
    id: "INC-1834",
    severity: "medium",
    title: "证书将在 7 天内到期",
    state: "stabilizing",
    environment: "staging",
    service: "partner-gateway",
    namespace: "integration",
    cluster: "staging-shanghai-01",
    team: "开放平台",
    owner: "林澈",
    duration: "3h 04m",
    startedAt: "06:46",
    signalCount: 1,
    bound: true,
    summary: "新证书已部署，正在观察握手失败率。",
  },
  {
    id: "INC-1828",
    severity: "high",
    title: "未知工作负载内存压力",
    state: "investigating",
    environment: "prod",
    service: "资源未绑定",
    namespace: "risk-jobs",
    cluster: "prod-shenzhen-01",
    team: "待确认",
    owner: "赵宁",
    duration: "4h 19m",
    startedAt: "05:31",
    signalCount: 4,
    bound: false,
    summary: "告警标签无法映射到已确认 Deployment Target，只允许只读调查。",
  },
  {
    id: "INC-1817",
    severity: "medium",
    title: "搜索接口 P95 延迟抖动",
    state: "resolved",
    environment: "prod",
    service: "search-api",
    namespace: "discovery",
    cluster: "prod-beijing-02",
    team: "搜索体验",
    owner: "周岚",
    duration: "52m",
    startedAt: "昨天 22:14",
    signalCount: 2,
    bound: true,
    summary: "上游缓存连接池耗尽，扩容后稳定窗口已完成。",
  },
]

export type EvidenceKind = "signal" | "change" | "metric" | "log"
export type EvidenceRelation = "supports" | "challenges" | "context"
export type EvidenceStatus = "verified" | "partial" | "missing"
export type EvidencePhase = "发现" | "调查" | "缓解" | "恢复"

export interface EvidenceFixture {
  id: string
  time: string
  kind: EvidenceKind
  relation: EvidenceRelation
  status: EvidenceStatus
  phase: EvidencePhase
  title: string
  summary: string
  impact: string
  source: string
  scope: string
  query: string
  samples: Array<{ label: string; value: string }>
}

export const evidence: EvidenceFixture[] = [
  {
    id: "ev-01",
    time: "09:12",
    kind: "signal",
    relation: "supports",
    status: "verified",
    phase: "发现",
    title: "Checkout 5xx 从 0.4% 升至 18.7%",
    summary: "错误集中在华东流量，三个实例同时越过告警阈值。",
    impact: "确认用户可见故障仍在持续。",
    source: "Prometheus",
    scope: "prod-shanghai-01 / payments / checkout-api",
    query: "按实例对比 5xx 比例与请求量，窗口 09:05–09:20。",
    samples: [
      { label: "峰值", value: "18.7% @ 09:15" },
      { label: "受影响实例", value: "3 / 3" },
      { label: "请求量", value: "12.4k rpm，未见异常下降" },
    ],
  },
  {
    id: "ev-02",
    time: "09:14",
    kind: "change",
    relation: "supports",
    status: "verified",
    phase: "发现",
    title: "gateway-v42 在异常前 2 分钟完成部署",
    summary: "变更只涉及上游 TLS 连接复用与证书链配置。",
    impact: "时间相关性强，需继续验证连接错误。",
    source: "Kubernetes",
    scope: "Deployment/checkout-api · revision 42",
    query: "读取 rollout history、镜像 digest 与配置变更摘要。",
    samples: [
      { label: "完成时间", value: "09:10:21" },
      { label: "镜像", value: "checkout-api@sha256:9fe…a12" },
      { label: "配置变化", value: "TLS keepalive、CA bundle" },
    ],
  },
  {
    id: "ev-03",
    time: "09:17",
    kind: "metric",
    relation: "challenges",
    status: "verified",
    phase: "调查",
    title: "数据库延迟与连接数保持基线",
    summary: "主库 P95 为 21ms，连接利用率 46%，无锁等待异常。",
    impact: "暂时排除数据库退化为主因。",
    source: "Prometheus",
    scope: "prod / payments-db-primary",
    query: "对比当前窗口与过去 7 天同时间基线。",
    samples: [
      { label: "查询 P95", value: "21ms（基线 19–25ms）" },
      { label: "连接利用率", value: "46%" },
      { label: "锁等待", value: "0" },
    ],
  },
  {
    id: "ev-04",
    time: "09:19",
    kind: "log",
    relation: "supports",
    status: "partial",
    phase: "调查",
    title: "上游握手失败与连接复用错误同步增长",
    summary: "已取得两个实例样本；第三个实例的 Loki 查询仍在重试。",
    impact: "支持 TLS 配置回归，但证据尚未覆盖全部实例。",
    source: "Loki",
    scope: "checkout-api / upstream partner-gateway",
    query: "聚类 TLS handshake、connection reset 与 keepalive 错误。",
    samples: [
      { label: "主要模式", value: "x509: certificate signed by unknown authority" },
      { label: "样本数", value: "1,842 / 5m" },
      { label: "缺失", value: "checkout-api-7f6d9 · 查询超时" },
    ],
  },
  {
    id: "ev-05",
    time: "09:21",
    kind: "change",
    relation: "context",
    status: "verified",
    phase: "调查",
    title: "合作方同期变更记录核验完成",
    summary: "状态页与变更记录均未发现同期证书或流量操作。",
    impact: "缩小到 checkout-api 本次发布范围。",
    source: "合作方状态页 · 变更记录采集",
    scope: "partner-gateway",
    query: "验证人工输入 HI-01 中的合作方变更主张。",
    samples: [
      { label: "核验来源", value: "合作方变更记录、状态页" },
      { label: "记录时间", value: "09:21:44" },
    ],
  },
  {
    id: "ev-06",
    time: "09:24",
    kind: "metric",
    relation: "supports",
    status: "verified",
    phase: "缓解",
    title: "回滚 canary 后错误率立即下降",
    summary: "单实例回滚 revision 41 后，5xx 在 90 秒内降至 1.2%。",
    impact: "因果证据增强，可以准备受治理的全量回滚建议。",
    source: "Prometheus + Kubernetes",
    scope: "checkout-api canary / revision 41",
    query: "对比 canary 与未回滚实例的错误率和握手失败。",
    samples: [
      { label: "Canary 5xx", value: "18.1% → 1.2%" },
      { label: "对照组 5xx", value: "17.6%" },
      { label: "观察窗口", value: "90 秒" },
    ],
  },
  {
    id: "ev-07",
    time: "09:27",
    kind: "log",
    relation: "context",
    status: "missing",
    phase: "缓解",
    title: "第三实例日志仍未取回",
    summary: "Loki 返回可重试超时，现有结论不覆盖该实例。",
    impact: "Evidence Gate 仍阻止批准全量回滚。",
    source: "Loki",
    scope: "checkout-api-7f6d9",
    query: "重试相同时间范围并限制单次样本量。",
    samples: [
      { label: "状态", value: "查询超时" },
      { label: "下一步", value: "等待重试或由 Connector 获取限定日志" },
    ],
  },
]

export const currentIncident = incidents[0]
