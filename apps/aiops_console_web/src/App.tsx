import { FormEvent, useEffect, useMemo, useState } from 'react'

type Actor = {
  username: string
  display_name?: string
  roles?: string[]
}

type Incident = {
  incident_id: string
  title: string
  severity: string
  status: string
  service: string
  impact: string
  age: string
  tags: string
}

type LoginResponse = {
  token: string
  actor: Actor
}

type IncidentsResponse = {
  incidents: Incident[]
}

type ApprovalRequest = {
  approval_id: string
  incident_id?: string
  session_id?: string
  action_proposal_id?: string
  status?: string
  risk_level?: string
  requested_by?: string
  assigned_approvers?: string[]
  approved_by?: string | null
  decided_at?: number | string | null
  expires_at?: number | string | null
  action_summary?: string
  rollback_plan?: string
  resource_scope?: Record<string, unknown>
  evidence_refs?: string[]
  audit_refs?: string[]
  execution_grant?: ExecutionGrant | null
}

type ApprovalsResponse = {
  approval_requests: ApprovalRequest[]
}

type ApprovalResponse = {
  approval_request: ApprovalRequest
}

type ExecutionGrant = {
  approval_id?: string
  incident_id?: string
  session_id?: string
  action_proposal_id?: string
  approved_by?: string | null
  decided_at?: number | string | null
  resource_scope?: Record<string, unknown>
}

type ApprovalExecution = {
  execution_id: string
  approval_id?: string
  incident_id?: string
  action_proposal_id?: string
  idempotency_key?: string
  status?: string
  cluster_id?: string
  namespace?: string
  requested_by?: string
  action?: Record<string, unknown> | null
  preflight?: Record<string, unknown> | null
  post_check?: Record<string, unknown> | null
  preflight_result?: Record<string, unknown> | null
  execution_result?: Record<string, unknown> | null
  post_check_result?: Record<string, unknown> | null
  error_code?: string | null
  error_message?: string | null
  created_at?: number
  updated_at?: number
  completed_at?: number | null
}

type ApprovalExecutionResponse = {
  execution_grant?: ExecutionGrant | null
  execution?: ApprovalExecution | null
}

type DiagnosisProcess = {
  incident?: {
    latest_session_id?: string | null
  }
  diagnosis?: {
    session_id?: string | null
    status?: string
    summary?: string
    markdown?: string
    root_cause?: { category?: string; statement?: string; summary?: string; confidence?: number | null }
    diagnosed_at?: string | null
  } | null
  evidence?: EvidenceItem[]
  timeline?: TimelineItem[]
  missing_evidence?: MissingEvidence[]
  actions?: ActionProposal[]
  audit?: AuditSummary
}

type EvidenceItem = {
  evidence_id?: string
  kind?: string
  status?: string
  summary?: string
  collected_at?: string | null
  query?: { display?: string; time_range?: { from?: string | null; to?: string | null } }
  result_ref?: string | null
  failure?: { code?: string; message?: string; retryable?: boolean } | null
}

type TimelineItem = {
  event_id?: string
  occurred_at?: string | null
  type?: string
  status?: string
  title?: string
  summary?: string
  refs?: Record<string, unknown>
}

type MissingEvidence = {
  source_type?: string
  tool?: string
  reason?: string
  audit?: { error_code?: string }
}

type ActionProposal = {
  action_proposal_id?: string
  summary?: string
  risk_level?: string
  approval_required?: boolean
  approval_id?: string | null
  execution_enabled?: boolean
}

type AuditSummary = {
  status?: string
  summary?: string
  refs?: string[]
}

type DiagnosisProcessResponse = {
  process: DiagnosisProcess
}

type NotificationCatalogResponse = {
  notification_types: string[]
}

type NotificationDelivery = {
  id: string
  notification_id?: string
  notification_type?: string
  incident_id?: string | null
  approval_id?: string | null
  service_id?: string | null
  team_id?: string | null
  platform?: string
  receive_id_type?: string
  chat_id?: string
  template_id?: string
  delivery_status?: string
  delivery_attempts?: number
  max_attempts?: number
  next_retry_at?: number | null
  last_delivery_error?: string | null
  last_delivery_at?: number | null
  target_message_id?: string | null
  suppressed_reason?: string | null
  created_at?: number
  updated_at?: number
  sent_at?: number | null
  payload?: Record<string, unknown>
}

type NotificationDeliveriesResponse = {
  deliveries: NotificationDelivery[]
}

const TOKEN_KEY = 'aiops.console.token'
type ViewName = 'overview' | 'incidents' | 'approvals' | 'notifications' | 'audit'

const fallbackIncidents: Incident[] = [
  {
    incident_id: 'demo-checkout-latency',
    title: '结算链路延迟升高',
    severity: 'warning',
    status: 'active',
    service: 'checkout',
    impact: 'P95 响应时间超过阈值',
    age: '12m',
    tags: 'warning active checkout default',
  },
  {
    incident_id: 'demo-payment-retry',
    title: '支付重试率异常',
    severity: 'critical',
    status: 'active',
    service: 'payment',
    impact: '错误预算消耗过快',
    age: '4m',
    tags: 'critical active payment demo-apps',
  },
]

function tokenFromStorage(): string {
  return sessionStorage.getItem(TOKEN_KEY) || ''
}

async function readJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init?.headers || {}),
    },
  })
  const payload = (await response.json()) as T & { error?: { message?: string } }
  if (!response.ok) {
    throw new Error(payload.error?.message || `请求失败: ${response.status}`)
  }
  return payload
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    active: '处理中',
    investigating: '诊断中',
    resolved: '已恢复',
    failed: '失败',
    unknown: '未知',
  }
  return labels[status] || status
}

function severityLabel(severity: string): string {
  const labels: Record<string, string> = {
    critical: '严重',
    warning: '告警',
    info: '提示',
    unknown: '未知',
  }
  return labels[severity] || severity
}

function diagnosisStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    succeeded: '已完成',
    partial: '证据不足',
    failed: '失败',
    running: '进行中',
    unknown: '未知',
  }
  return labels[status] || statusLabel(status)
}

function evidenceKindLabel(kind?: string): string {
  const labels: Record<string, string> = {
    prometheus: '指标',
    loki: '日志',
    k8s: 'K8S',
    topology: '拓扑',
    evidence: '证据',
  }
  return labels[kind || ''] || kind || '证据'
}

function riskLabel(risk?: string): string {
  const labels: Record<string, string> = {
    high: '高风险',
    medium: '中风险',
    low: '低风险',
  }
  return labels[risk || ''] || risk || '未标注'
}

function approvalStatusLabel(status?: string): string {
  const labels: Record<string, string> = {
    pending: '待审批',
    approved: '已通过',
    rejected: '已拒绝',
    expired: '已过期',
    cancelled: '已取消',
  }
  return labels[status || ''] || status || '未知'
}

function notificationTypeLabel(type?: string): string {
  const labels: Record<string, string> = {
    new_incident: '新事件',
    diagnosis_ready: '诊断完成',
    approval_required: '审批提醒',
    approval_result: '审批结果',
    execution_result: '执行结果',
    unowned_alert: '未归属告警',
  }
  return labels[type || ''] || type || '未知类型'
}

function deliveryStatusLabel(status?: string): string {
  const labels: Record<string, string> = {
    pending: '待投递',
    sent: '已送达',
    failed: '投递失败',
    dead_letter: '死信',
    suppressed: '已抑制',
  }
  return labels[status || ''] || status || '未知'
}

function executionStatusLabel(status?: string): string {
  const labels: Record<string, string> = {
    pending: '等待',
    running: '进行中',
    queued: '已排队',
    preflight_running: '预检中',
    preflight_failed: '预检失败',
    executing: '执行中',
    post_checking: '复检中',
    succeeded: '执行成功',
    failed: '执行失败',
    rollback_required: '需要回滚',
  }
  return labels[status || ''] || status || '未执行'
}

function commandSummary(command?: Record<string, unknown> | null): string {
  const argv = command?.argv
  if (Array.isArray(argv)) {
    return argv.map((item) => String(item)).join(' ')
  }
  return '-'
}

function resultSummary(result?: Record<string, unknown> | null): string {
  if (!result) {
    return '暂无结果。'
  }
  const status = result.status || result.ok
  const message = result.error_message || result.stderr || result.stdout || result.message
  return [status ? `状态：${String(status)}` : '', message ? `结果：${String(message)}` : ''].filter(Boolean).join('；') || compactObject(result)
}

function formatTime(value?: string | null): string {
  if (!value) {
    return '-'
  }
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

function formatAuditTime(value?: string | number | null): string {
  if (typeof value === 'number') {
    return formatTime(new Date(value * 1000).toISOString())
  }
  return formatTime(value || null)
}

function formatConfidence(value?: number | null): string {
  return typeof value === 'number' ? `${Math.round(value * 100)}%` : '-'
}

function compactRefs(refs?: Record<string, unknown>): string {
  if (!refs) {
    return '-'
  }
  const pairs = Object.entries(refs).filter(([, value]) => value !== undefined && value !== null && value !== '')
  return pairs.length ? pairs.map(([key, value]) => `${key}: ${Array.isArray(value) ? value.join(', ') : String(value)}`).join('；') : '-'
}

function auditRefs(process: DiagnosisProcess, evidence: EvidenceItem[], actions: ActionProposal[]): string[] {
  return Array.from(
    new Set(
      [
        ...(process.audit?.refs || []),
        ...evidence.flatMap((item) => [item.evidence_id || '', item.result_ref || '']).filter(Boolean),
        ...actions.flatMap((action) => [action.approval_id || '', action.action_proposal_id || '']).filter(Boolean),
      ],
    ),
  )
}

function compactObject(value?: Record<string, unknown>): string {
  if (!value) {
    return '-'
  }
  const pairs = Object.entries(value).filter(([, item]) => item !== undefined && item !== null && item !== '')
  return pairs.length ? pairs.map(([key, item]) => `${key}: ${String(item)}`).join('；') : '-'
}

function viewTitle(view: ViewName): string {
  const labels: Record<ViewName, string> = {
    overview: '总览',
    incidents: '事件工作台',
    approvals: '审批中心',
    notifications: '通知中心',
    audit: '审计历史',
  }
  return labels[view]
}

const navItems: Array<{ view: ViewName; label: string; count?: string }> = [
  { view: 'overview', label: '总览', count: 'HOME' },
  { view: 'incidents', label: '事件工作台', count: 'INC' },
  { view: 'approvals', label: '审批中心', count: 'APR' },
  { view: 'notifications', label: '通知中心', count: 'MSG' },
  { view: 'audit', label: '审计历史', count: 'LOG' },
]

const fallbackProgress = [
  { title: '确认症状', detail: 'checkout-api p95 从 420ms 升至 4.8s，错误预算消耗速率 14.2x。', status: 'done', time: '14:21' },
  { title: '收敛影响面', detail: '异常集中在 prod-checkout / ap-southeast-1，移动端结算流量受影响更高。', status: 'done', time: '14:23' },
  { title: '关联根因候选', detail: 'revision 184 发布后，payment-service 连接池等待时间和 5xx 同步抬升。', status: 'running', time: '14:27' },
  { title: '等待受限动作审批', detail: '回滚 deployment/checkout-api 到 revision 183 需要 Incident Commander 确认。', status: 'blocked', time: '现在' },
]

function incidentSeverityClass(severity?: string): string {
  return severity === 'critical' ? 'sev1' : severity === 'warning' ? 'sev2' : 'info'
}

function incidentRiskScore(incident?: Incident, process?: DiagnosisProcess | null): string {
  const confidence = process?.diagnosis?.root_cause?.confidence
  if (typeof confidence === 'number') {
    return Math.round(confidence * 100).toString()
  }
  return incident?.severity === 'critical' ? '86' : incident?.severity === 'warning' ? '64' : '42'
}

export default function App() {
  const [activeView, setActiveView] = useState<ViewName>('overview')
  const [token, setToken] = useState(tokenFromStorage)
  const [actor, setActor] = useState<Actor | null>(null)
  const [username, setUsername] = useState('alice')
  const [password, setPassword] = useState('')
  const [incidents, setIncidents] = useState<Incident[]>(fallbackIncidents)
  const [selectedId, setSelectedId] = useState(fallbackIncidents[0]?.incident_id || '')
  const [notice, setNotice] = useState('使用演示数据。登录后会从 Gateway 读取实时事件。')
  const [loading, setLoading] = useState(false)
  const [process, setProcess] = useState<DiagnosisProcess | null>(null)
  const [processNotice, setProcessNotice] = useState('选择实时事件后加载诊断过程。')
  const [approvals, setApprovals] = useState<ApprovalRequest[]>([])
  const [selectedApprovalId, setSelectedApprovalId] = useState('')
  const [approvalNotice, setApprovalNotice] = useState('登录后从 Gateway 读取审批请求。')
  const [approvalLoading, setApprovalLoading] = useState(false)
  const [decisionLoading, setDecisionLoading] = useState('')
  const [executionByApproval, setExecutionByApproval] = useState<Record<string, ApprovalExecution | null>>({})
  const [executionLoading, setExecutionLoading] = useState('')
  const [notificationTypes, setNotificationTypes] = useState<string[]>([])
  const [deliveries, setDeliveries] = useState<NotificationDelivery[]>([])
  const [selectedDeliveryId, setSelectedDeliveryId] = useState('')
  const [deliveryStatusFilter, setDeliveryStatusFilter] = useState('')
  const [deliveryTypeFilter, setDeliveryTypeFilter] = useState('')
  const [notificationNotice, setNotificationNotice] = useState('登录后从 Gateway 读取通知投递记录。')
  const [notificationLoading, setNotificationLoading] = useState(false)

  const selectedIncident = useMemo(
    () => incidents.find((incident) => incident.incident_id === selectedId) || incidents[0],
    [incidents, selectedId],
  )
  const evidence = process?.evidence || []
  const timeline = process?.timeline || []
  const missingEvidence = process?.missing_evidence || []
  const actions = process?.actions || []
  const auditReferenceIds = process ? auditRefs(process, evidence, actions) : []
  const selectedApproval = useMemo(
    () => approvals.find((approval) => approval.approval_id === selectedApprovalId) || approvals[0],
    [approvals, selectedApprovalId],
  )
  const selectedExecution = selectedApproval?.approval_id ? executionByApproval[selectedApproval.approval_id] : null
  const selectedDelivery = useMemo(
    () => deliveries.find((delivery) => delivery.id === selectedDeliveryId) || deliveries[0],
    [deliveries, selectedDeliveryId],
  )

  useEffect(() => {
    if (!token) {
      return
    }
    void refreshIncidents(token)
    void refreshApprovals(token)
    void refreshNotifications(token)
  }, [token])

  useEffect(() => {
    if (!token || !selectedIncident || selectedIncident.incident_id.startsWith('demo-')) {
      setProcess(null)
      setProcessNotice(token ? '演示事件没有诊断过程。' : '登录后可读取诊断过程。')
      return
    }
    void refreshDiagnosisProcess(selectedIncident.incident_id, token)
  }, [selectedIncident?.incident_id, token])

  useEffect(() => {
    if (!token || !selectedApprovalId) {
      return
    }
    void refreshExecutionDetail(selectedApprovalId, token)
  }, [selectedApprovalId, token])

  async function refreshIncidents(activeToken = token) {
    if (!activeToken) {
      setNotice('请先登录，页面会继续保留演示数据。')
      return
    }
    setLoading(true)
    try {
      const data = await readJson<IncidentsResponse>('/api/incidents/active', {
        headers: { Authorization: `Bearer ${activeToken}` },
      })
      setIncidents(data.incidents)
      setSelectedId(data.incidents[0]?.incident_id || '')
      setProcess(null)
      setNotice(data.incidents.length ? '已连接 Gateway，展示当前可见事件。' : '已连接 Gateway，当前没有可见活跃事件。')
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '读取事件失败')
    } finally {
      setLoading(false)
    }
  }

  async function refreshDiagnosisProcess(incidentId: string, activeToken = token) {
    setProcess(null)
    setProcessNotice('正在加载诊断过程。')
    try {
      const data = await readJson<DiagnosisProcessResponse>(`/api/incidents/${incidentId}/diagnosis-process`, {
        headers: { Authorization: `Bearer ${activeToken}` },
      })
      setProcess(data.process)
      setProcessNotice(data.process.diagnosis ? '诊断过程已加载。' : '该事件暂无诊断结果。')
    } catch (error) {
      setProcess(null)
      setProcessNotice(error instanceof Error ? error.message : '诊断过程读取失败')
    }
  }

  async function refreshApprovals(activeToken = token) {
    if (!activeToken) {
      setApprovalNotice('请先登录，审批中心保持只读空状态。')
      return
    }
    setApprovalLoading(true)
    try {
      const data = await readJson<ApprovalsResponse>('/api/approval-requests', {
        headers: { Authorization: `Bearer ${activeToken}` },
      })
      setApprovals(data.approval_requests)
      setSelectedApprovalId(data.approval_requests[0]?.approval_id || '')
      setApprovalNotice(data.approval_requests.length ? '已连接 Gateway，展示当前审批请求。' : '已连接 Gateway，当前没有审批请求。')
    } catch (error) {
      setApprovalNotice(error instanceof Error ? error.message : '读取审批请求失败')
    } finally {
      setApprovalLoading(false)
    }
  }

  async function refreshNotifications(activeToken = token) {
    if (!activeToken) {
      setNotificationNotice('请先登录，通知中心保持只读空状态。')
      return
    }
    setNotificationLoading(true)
    try {
      const query = new URLSearchParams()
      if (deliveryStatusFilter) {
        query.set('status', deliveryStatusFilter)
      }
      if (deliveryTypeFilter) {
        query.set('notification_type', deliveryTypeFilter)
      }
      const deliveryUrl = `/api/notifications/deliveries${query.toString() ? `?${query.toString()}` : ''}`
      const [catalog, deliveryData] = await Promise.all([
        readJson<NotificationCatalogResponse>('/api/notifications/types', {
          headers: { Authorization: `Bearer ${activeToken}` },
        }),
        readJson<NotificationDeliveriesResponse>(deliveryUrl, {
          headers: { Authorization: `Bearer ${activeToken}` },
        }),
      ])
      setNotificationTypes(catalog.notification_types)
      setDeliveries(deliveryData.deliveries)
      setSelectedDeliveryId(deliveryData.deliveries[0]?.id || '')
      setNotificationNotice(deliveryData.deliveries.length ? '已连接 Gateway，展示最近通知投递。' : '已连接 Gateway，当前没有通知投递记录。')
    } catch (error) {
      setNotificationNotice(error instanceof Error ? error.message : '读取通知中心失败')
    } finally {
      setNotificationLoading(false)
    }
  }

  async function refreshApprovalDetail(approvalId: string, activeToken = token) {
    if (!approvalId || !activeToken) {
      return
    }
    setApprovalLoading(true)
    try {
      const data = await readJson<ApprovalResponse>(`/api/approval-requests/${approvalId}`, {
        headers: { Authorization: `Bearer ${activeToken}` },
      })
      setApprovals((current) => {
        const exists = current.some((approval) => approval.approval_id === data.approval_request.approval_id)
        return exists
          ? current.map((approval) => (approval.approval_id === data.approval_request.approval_id ? data.approval_request : approval))
          : [data.approval_request, ...current]
      })
      setSelectedApprovalId(data.approval_request.approval_id)
      setApprovalNotice('审批详情已加载。')
      await refreshExecutionDetail(data.approval_request.approval_id, activeToken)
    } catch (error) {
      setApprovalNotice(error instanceof Error ? error.message : '审批详情读取失败')
    } finally {
      setApprovalLoading(false)
    }
  }

  async function refreshExecutionDetail(approvalId: string, activeToken = token) {
    if (!approvalId || !activeToken) {
      return
    }
    setExecutionLoading(approvalId)
    try {
      const data = await readJson<ApprovalExecutionResponse>(`/api/approval-requests/${approvalId}/execution`, {
        headers: { Authorization: `Bearer ${activeToken}` },
      })
      setExecutionByApproval((current) => ({
        ...current,
        [approvalId]: data.execution || null,
      }))
    } catch {
      setExecutionByApproval((current) => ({
        ...current,
        [approvalId]: null,
      }))
    } finally {
      setExecutionLoading('')
    }
  }

  async function decideApproval(approvalId: string, decision: 'approve' | 'reject') {
    if (!token) {
      setApprovalNotice('请先登录后再处理审批。')
      return
    }
    setDecisionLoading(`${approvalId}:${decision}`)
    try {
      const data = await readJson<ApprovalResponse>(`/api/approval-requests/${approvalId}/${decision}`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      })
      setApprovals((current) =>
        current.map((approval) => (approval.approval_id === data.approval_request.approval_id ? data.approval_request : approval)),
      )
      setApprovalNotice(decision === 'approve' ? '审批已通过。' : '审批已拒绝。')
      await refreshExecutionDetail(data.approval_request.approval_id, token)
    } catch (error) {
      setApprovalNotice(error instanceof Error ? error.message : '审批决策失败')
    } finally {
      setDecisionLoading('')
    }
  }

  async function handleLogin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setLoading(true)
    try {
      const data = await readJson<LoginResponse>('/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      })
      sessionStorage.setItem(TOKEN_KEY, data.token)
      setToken(data.token)
      setActor(data.actor)
      setActiveView('overview')
      setNotice('登录成功，正在读取事件列表。')
      setApprovalNotice('登录成功，正在读取审批请求。')
      setNotificationNotice('登录成功，正在读取通知中心。')
      await refreshIncidents(data.token)
      await refreshApprovals(data.token)
      await refreshNotifications(data.token)
    } catch (error) {
      setNotice(error instanceof Error ? error.message : '登录失败')
    } finally {
      setLoading(false)
    }
  }

  function logout() {
    sessionStorage.removeItem(TOKEN_KEY)
    setToken('')
    setActor(null)
    setIncidents(fallbackIncidents)
    setSelectedId(fallbackIncidents[0]?.incident_id || '')
    setApprovals([])
    setSelectedApprovalId('')
    setExecutionByApproval({})
    setExecutionLoading('')
    setNotificationTypes([])
    setDeliveries([])
    setSelectedDeliveryId('')
    setActiveView('overview')
    setNotice('已退出，页面切回演示数据。')
    setApprovalNotice('已退出，审批中心切回只读空状态。')
    setNotificationNotice('已退出，通知中心切回只读空状态。')
  }

  if (!token) {
    return (
      <LoginPage
        username={username}
        password={password}
        notice={notice}
        loading={loading}
        onUsername={setUsername}
        onPassword={setPassword}
        onSubmit={handleLogin}
      />
    )
  }

  const refreshActiveView = () => {
    if (activeView === 'approvals') {
      void refreshApprovals()
      return
    }
    if (activeView === 'notifications') {
      void refreshNotifications()
      return
    }
    void refreshIncidents()
  }
  const activeLoading = activeView === 'approvals' ? approvalLoading : activeView === 'notifications' ? notificationLoading : loading
  const activeNotice =
    activeView === 'approvals'
      ? approvalNotice
      : activeView === 'notifications'
        ? notificationNotice
        : activeView === 'audit'
          ? processNotice
          : notice

  return (
    <main className="console-shell">
      <aside className="side-panel">
        <div className="brand-block">
          <span className="brand-mark">AI</span>
          <div>
            <p className="eyebrow">运维指挥台</p>
            <h1>AIOps 控制台</h1>
          </div>
        </div>
        <nav className="nav-list" aria-label="控制台导航">
          {navItems.map((item) => (
            <button
              type="button"
              aria-current={activeView === item.view ? 'page' : undefined}
              key={item.view}
              onClick={() => setActiveView(item.view)}
            >
              <span>{item.label}</span>
              <small>{item.count}</small>
            </button>
          ))}
        </nav>
        <section className="session-panel" aria-label="会话">
          <div className="section-title compact">
            <span>Gateway 会话</span>
            <button className="text-action" type="button" onClick={logout}>
              退出
            </button>
          </div>
          <p className="session-copy">
            当前用户：{actor?.display_name || actor?.username || '已登录'}
            <br />
            权限角色：{actor?.roles?.join('、') || '已授权会话'}
          </p>
        </section>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div className="page-title">
            <h2>{viewTitle(activeView)}</h2>
            <p>{activeView === 'overview' ? '大屏总览占位，下一阶段接入完整运行态指标。' : '仅通过 Gateway /api 与 /auth 读取控制面数据。'}</p>
          </div>
          <label className="global-search">
            <span>搜索</span>
            <input aria-label="搜索事件、服务或审批" placeholder="incident / service / approval" />
          </label>
          <div className="top-actions">
            <button className="secondary-action" type="button" onClick={refreshActiveView} disabled={activeLoading}>
              {activeLoading ? '刷新中' : '刷新'}
            </button>
          </div>
        </header>

        <div className="notice" role="status">
          {activeNotice}
        </div>

        {activeView === 'overview' ? (
          <OverviewDashboard
            incidents={incidents}
            approvals={approvals}
            deliveries={deliveries}
            onOpenIncidents={() => setActiveView('incidents')}
          />
        ) : activeView === 'incidents' ? (
          <IncidentWorkbenchView
            incidents={incidents}
            selectedIncident={selectedIncident}
            process={process}
            processNotice={processNotice}
            approvals={approvals}
            selectedApproval={selectedApproval}
            selectedExecution={selectedExecution}
            auditReferenceIds={auditReferenceIds}
            onSelectIncident={setSelectedId}
          />
        ) : activeView === 'approvals' ? (
          <ApprovalCenterView
            approvals={approvals}
            selectedApproval={selectedApproval}
            selectedExecution={selectedExecution}
            selectedApprovalId={selectedApprovalId}
            decisionLoading={decisionLoading}
            executionLoading={executionLoading}
            onSelect={(approvalId) => void refreshApprovalDetail(approvalId)}
            onDecision={(approvalId, decision) => void decideApproval(approvalId, decision)}
          />
        ) : activeView === 'audit' ? (
          <AuditHistoryView
            incidents={incidents}
            selectedIncident={selectedIncident}
            timeline={timeline}
            auditReferenceIds={auditReferenceIds}
            selectedApproval={selectedApproval}
            onSelectIncident={setSelectedId}
          />
        ) : (
          <NotificationCenterView
            notificationTypes={notificationTypes}
            deliveries={deliveries}
            selectedDelivery={selectedDelivery}
            selectedDeliveryId={selectedDeliveryId}
            statusFilter={deliveryStatusFilter}
            typeFilter={deliveryTypeFilter}
            loading={notificationLoading}
            onSelect={setSelectedDeliveryId}
            onStatusFilter={setDeliveryStatusFilter}
            onTypeFilter={setDeliveryTypeFilter}
            onRefresh={() => void refreshNotifications()}
          />
        )}
      </section>
    </main>
  )
}

function LoginPage({
  username,
  password,
  notice,
  loading,
  onUsername,
  onPassword,
  onSubmit,
}: {
  username: string
  password: string
  notice: string
  loading: boolean
  onUsername: (value: string) => void
  onPassword: (value: string) => void
  onSubmit: (event: FormEvent<HTMLFormElement>) => void
}) {
  return (
    <main className="login-page">
      <section className="login-shell" aria-label="登录 Gateway">
        <div className="login-brand">
          <span className="brand-mark">AI</span>
          <div>
            <p className="eyebrow">AIOps Control Plane</p>
            <h1>AIOps 控制台</h1>
          </div>
        </div>
        <div className="login-copy">
          <h2>登录 Gateway</h2>
          <p>使用内部账号进入运维控制台。浏览器只访问 Gateway `/auth/*` 与 `/api/*`。</p>
        </div>
        <form className="login-form" onSubmit={onSubmit}>
          <label>
            用户名
            <input value={username} onChange={(event) => onUsername(event.target.value)} autoComplete="username" />
          </label>
          <label>
            密码
            <input
              value={password}
              onChange={(event) => onPassword(event.target.value)}
              autoComplete="current-password"
              type="password"
              placeholder="输入 Gateway 密码"
            />
          </label>
          <button className="primary-action" type="submit" disabled={loading}>
            {loading ? '连接中' : '登录 Gateway'}
          </button>
        </form>
        <div className="notice" role="status">
          {notice}
        </div>
      </section>
    </main>
  )
}

function OverviewDashboard({
  incidents,
  approvals,
  deliveries,
  onOpenIncidents,
}: {
  incidents: Incident[]
  approvals: ApprovalRequest[]
  deliveries: NotificationDelivery[]
  onOpenIncidents: () => void
}) {
  return (
    <div className="overview-dashboard">
      <section className="overview-hero" aria-label="总览大屏占位">
        <div>
          <span className="pill info">总览占位</span>
          <h2>运行态大屏后续接入</h2>
          <p>本页先保留大屏首页入口，下一阶段接入全局 SLO、集群风险、值班态势和跨服务影响面。</p>
        </div>
        <button className="primary-action" type="button" onClick={onOpenIncidents}>
          进入事件工作台
        </button>
      </section>
      <div className="summary-grid">
        <Metric label="活跃事件" value={incidents.length.toString()} />
        <Metric label="严重事件" value={incidents.filter((item) => item.severity === 'critical').length.toString()} />
        <Metric label="待审批" value={approvals.filter((item) => item.status === 'pending').length.toString()} />
        <Metric label="通知失败" value={deliveries.filter((item) => ['failed', 'dead_letter'].includes(item.delivery_status || '')).length.toString()} />
      </div>
      <section className="panel">
        <div className="panel-header">
          <h2>近期事件入口</h2>
          <span className="meta">top {Math.min(incidents.length, 4)}</span>
        </div>
        <div className="overview-list">
          {incidents.slice(0, 4).map((incident) => (
            <article className="overview-row" key={incident.incident_id}>
              <span className={`pill ${incidentSeverityClass(incident.severity)}`}>{severityLabel(incident.severity)}</span>
              <strong>{incident.title}</strong>
              <span>{incident.service} · {incident.age}</span>
            </article>
          ))}
        </div>
      </section>
    </div>
  )
}

function IncidentWorkbenchView({
  incidents,
  selectedIncident,
  process,
  processNotice,
  approvals,
  selectedApproval,
  selectedExecution,
  auditReferenceIds,
  onSelectIncident,
}: {
  incidents: Incident[]
  selectedIncident?: Incident
  process: DiagnosisProcess | null
  processNotice: string
  approvals: ApprovalRequest[]
  selectedApproval?: ApprovalRequest
  selectedExecution?: ApprovalExecution | null
  auditReferenceIds: string[]
  onSelectIncident: (incidentId: string) => void
}) {
  const evidence = process?.evidence || []
  const timeline = process?.timeline || []
  const actions = process?.actions || []
  const rootCause = process?.diagnosis?.root_cause
  const activeApproval = selectedApproval || approvals[0]
  const progress = timeline.length
    ? timeline.slice(0, 4).map((item, index) => ({
        title: item.title || item.type || '诊断事件',
        detail: item.summary || '暂无事件摘要。',
        status: item.status === 'failed' ? 'blocked' : index === timeline.length - 1 ? 'running' : 'done',
        time: formatTime(item.occurred_at),
      }))
    : fallbackProgress

  return (
    <section className="incident-workspace" aria-label="事件工作台">
      <div className="stack left-col">
        <section className="panel">
          <div className="panel-header">
            <h2>运行概览</h2>
            <span className="meta">live</span>
          </div>
          <div className="overview-grid">
            <Metric label="活跃事件" value={incidents.length.toString()} />
            <Metric label="SEV-1" value={incidents.filter((item) => item.severity === 'critical').length.toString()} />
            <Metric label="待审批" value={approvals.filter((item) => item.status === 'pending').length.toString()} />
            <Metric label="平均诊断" value="06:42" />
          </div>
        </section>

        <section className="panel">
          <div className="panel-header">
            <h2>活跃 incident 队列</h2>
            <span className="meta">{incidents.length} shown</span>
          </div>
          <div className="filters">
            <button className="filter active" type="button">全部</button>
            <button className="filter" type="button">SEV-1</button>
            <button className="filter" type="button">待审批</button>
            <button className="filter" type="button">K8s</button>
          </div>
          <div className="incident-list compact-list">
            {incidents.length ? (
              incidents.map((incident) => (
                <button
                  key={incident.incident_id}
                  className={incident.incident_id === selectedIncident?.incident_id ? 'incident-row active' : 'incident-row'}
                  onClick={() => onSelectIncident(incident.incident_id)}
                >
                  <span className={`pill ${incidentSeverityClass(incident.severity)}`}>{severityLabel(incident.severity)}</span>
                  <strong>{incident.title}</strong>
                  <span>{incident.service} · {incident.age} · {incident.impact}</span>
                </button>
              ))
            ) : (
              <div className="empty-state">当前没有活跃事件。</div>
            )}
          </div>
        </section>
      </div>

      <div className="stack center-col">
        <section className="panel" aria-label="事件详情">
          <div className="detail-hero">
            <div className="incident-headline">
              <div>
                <div className="pill-row">
                  <span className={`pill ${incidentSeverityClass(selectedIncident?.severity)}`}>{severityLabel(selectedIncident?.severity || 'unknown')}</span>
                  <span className="pill info">{selectedIncident?.service || 'Kubernetes / API'}</span>
                  <span className="pill muted-pill">{selectedIncident?.incident_id || 'INC-2481'}</span>
                </div>
                <h2>{selectedIncident?.title || 'checkout-api 延迟升高，疑似 rollout 后 payment-service 连接池耗尽'}</h2>
                <p>
                  {process?.diagnosis?.summary ||
                    selectedIncident?.impact ||
                    'Agent 已关联指标、日志、K8s rollout 与服务拓扑。当前建议先暂停 rollout，并等待受限动作审批。'}
                </p>
              </div>
              <div className="risk-score">
                <span>风险评分</span>
                <strong>{incidentRiskScore(selectedIncident, process)}</strong>
              </div>
            </div>
            <div className="impact-grid">
              <div className="impact"><span>状态</span><strong>{statusLabel(selectedIncident?.status || 'unknown')}</strong></div>
              <div className="impact"><span>影响服务</span><strong>{selectedIncident?.service || '4'}</strong></div>
              <div className="impact"><span>根因</span><strong>{rootCause?.category || '待确认'}</strong></div>
              <div className="impact"><span>审批状态</span><strong>{approvalStatusLabel(activeApproval?.status)}</strong></div>
            </div>
          </div>
        </section>

        <section className="panel">
          <div className="panel-header">
            <h2>诊断进展</h2>
            <span className="meta">{formatConfidence(rootCause?.confidence)}</span>
          </div>
          <div className="progress">
            {progress.map((step, index) => (
              <article className={`step ${step.status}`} key={`${step.title}-${index}`}>
                <div className="step-dot">{step.status === 'done' ? '✓' : step.status === 'blocked' ? '!' : '…'}</div>
                <div>
                  <h3>{step.title}</h3>
                  <p>{step.detail}</p>
                </div>
                <time>{step.time}</time>
              </article>
            ))}
          </div>
        </section>

        <section className="panel">
          <div className="panel-header">
            <h2>多来源证据</h2>
            <span className="meta">metrics · logs · k8s · topology</span>
          </div>
          <div className="evidence-body">
            <div className="chart" aria-label="延迟与错误率曲线">
              <span className="chart-label">checkout-api p95 latency / payment 5xx</span>
              <svg viewBox="0 0 720 160" preserveAspectRatio="none" aria-hidden="true">
                <path d="M0,118 C90,116 130,112 180,115 C240,119 270,116 320,110 C360,102 390,58 430,42 C470,26 510,35 550,38 C610,42 660,50 720,45" fill="none" stroke="currentColor" strokeWidth="3" />
                <path d="M0,132 C80,132 140,130 210,131 C280,132 340,126 380,118 C420,98 470,86 520,88 C590,91 650,100 720,95" fill="none" stroke="var(--info)" strokeWidth="3" />
              </svg>
            </div>
            <div className="item-list">
              {evidence.length ? (
                evidence.slice(0, 4).map((item, index) => (
                  <article className="process-item" key={item.evidence_id || `${item.kind}-${index}`}>
                    <div className="item-heading">
                      <strong>{evidenceKindLabel(item.kind)}</strong>
                      <span className={`status-chip ${item.status || 'unknown'}`}>{diagnosisStatusLabel(item.status || 'unknown')}</span>
                    </div>
                    <p>{item.summary || '暂无证据摘要。'}</p>
                    <small>查询：{item.query?.display || '-'} · 采集时间：{formatTime(item.collected_at)}</small>
                  </article>
                ))
              ) : (
                <>
                  <article className="process-item"><strong>Metrics</strong><p>checkout-api p95 4.8s，payment-service 5xx 7.8%，与 rollout 时间匹配。</p></article>
                  <article className="process-item"><strong>Logs</strong><p>payment-service 出现 connection pool exhausted after 2500ms。</p></article>
                  <article className="process-item"><strong>Kubernetes</strong><p>deployment/checkout-api revision 184 canary 30%，pod 层未发现 crash。</p></article>
                  <article className="process-item"><strong>Topology</strong><p>web-gateway → checkout-api → payment-service 链路局部退化。</p></article>
                </>
              )}
            </div>
          </div>
        </section>
      </div>

      <div className="stack right-col">
        <section className="panel">
          <div className="panel-header">
            <h2>受限操作审批</h2>
            <span className="meta">{approvals.length} pending</span>
          </div>
          <div className="approval">
            <div className="approval-card">
              <span className={`pill ${activeApproval?.risk_level === 'high' ? 'sev1' : 'sev2'}`}>{riskLabel(activeApproval?.risk_level)}</span>
              <h3>{activeApproval?.action_summary || actions[0]?.summary || '回滚 checkout-api 到上一稳定 revision'}</h3>
              <p>{activeApproval?.rollback_plan || '预计影响：重建受影响 pod，恢复上一稳定版本。动作需通过 Gateway Approval Service 审批。'}</p>
            </div>
          </div>
          {activeApproval ? <ExecutionTrackingView approval={activeApproval} execution={selectedExecution} loading={false} /> : null}
        </section>

        <section className="panel">
          <div className="panel-header">
            <h2>Agent / Tool 执行过程</h2>
            <span className="meta">{process?.diagnosis?.session_id || 'run-preview'}</span>
          </div>
          <div className="agent-stream">
            {(evidence.length ? evidence.slice(0, 3) : [{ kind: 'prometheus', summary: '拉取 checkout-api p95 latency 与 payment-service 5xx 时间序列。' }, { kind: 'k8s', summary: '发现 revision 184 在异常开始前完成 30% 灰度。' }, { kind: 'loki', summary: '聚合 timeout 与 connection pool exhausted 日志。' }]).map((item, index) => (
              <article className="tool-call" key={`${item.kind}-${index}`}>
                <div className="tool-top">
                  <div className="tool-name"><span className="pill ok">done</span><strong>{evidenceKindLabel(item.kind)}</strong></div>
                </div>
                <p>{item.summary || '工具调用已完成。'}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="panel">
          <div className="panel-header">
            <h2>通知 / 执行 / 审计</h2>
            <span className="meta">last 10m</span>
          </div>
          <div className="audit-list">
            {(auditReferenceIds.length ? auditReferenceIds : ['已通知 #checkout-war-room。', 'Agent 完成影响面分析。', processNotice, '已生成客户影响摘要草稿。']).map((item, index) => (
              <div className="audit" key={`${item}-${index}`}>
                <div className={`dot ${index % 3 === 0 ? 'info' : index % 3 === 1 ? 'ok' : 'warn'}`}></div>
                <p>{item}</p>
                <span>{index === 0 ? '现在' : `-${index + 2}m`}</span>
              </div>
            ))}
          </div>
        </section>
      </div>
    </section>
  )
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  )
}

function AuditHistoryView({
  incidents,
  selectedIncident,
  timeline,
  auditReferenceIds,
  selectedApproval,
  onSelectIncident,
}: {
  incidents: Incident[]
  selectedIncident?: Incident
  timeline: TimelineItem[]
  auditReferenceIds: string[]
  selectedApproval?: ApprovalRequest
  onSelectIncident: (incidentId: string) => void
}) {
  const approvalRefs = Array.from(new Set([...(selectedApproval?.evidence_refs || []), ...(selectedApproval?.audit_refs || [])]))
  return (
    <div className="content-grid">
      <section className="incident-list" aria-label="审计事件列表">
        {incidents.length ? (
          incidents.map((incident) => (
            <button
              key={incident.incident_id}
              className={incident.incident_id === selectedIncident?.incident_id ? 'incident-row active' : 'incident-row'}
              onClick={() => onSelectIncident(incident.incident_id)}
            >
              <span className={`severity ${incident.severity}`}>{severityLabel(incident.severity)}</span>
              <strong>{incident.title}</strong>
              <span>{incident.service} · {statusLabel(incident.status)}</span>
            </button>
          ))
        ) : (
          <div className="empty-state">暂无可审计事件。</div>
        )}
      </section>

      <section className="detail-panel" aria-label="审计历史详情">
        {selectedIncident ? (
          <>
            <div className="detail-heading">
              <span className={`severity ${selectedIncident.severity}`}>{severityLabel(selectedIncident.severity)}</span>
              <h3>{selectedIncident.title}</h3>
              <p>{selectedIncident.incident_id}</p>
            </div>
            <div className="process-summary">
              <section className="process-section" aria-label="事件时间线">
                <div className="section-title compact">
                  <span>事件时间线</span>
                  <small>{timeline.length} 条</small>
                </div>
                <div className="item-list timeline-list">
                  {timeline.length ? (
                    timeline.map((event, index) => (
                      <article className="process-item" key={event.event_id || `${event.type}-${index}`}>
                        <div className="item-heading">
                          <strong>{event.title || event.type || '事件'}</strong>
                          <span className={`status-chip ${event.status || 'unknown'}`}>{diagnosisStatusLabel(event.status || 'unknown')}</span>
                        </div>
                        <p>{event.summary || '暂无事件摘要。'}</p>
                        <small>
                          {formatTime(event.occurred_at)} · 引用：{compactRefs(event.refs)}
                        </small>
                      </article>
                    ))
                  ) : (
                    <div className="empty-state compact">暂无事件时间线。</div>
                  )}
                </div>
              </section>

              <section className="process-section" aria-label="审计引用">
                <div className="section-title compact">
                  <span>审计引用</span>
                </div>
                <div className="audit-ref-list">
                  {auditReferenceIds.length ? (
                    auditReferenceIds.map((ref) => (
                      <span className="ref-chip" key={ref}>
                        {ref}
                      </span>
                    ))
                  ) : (
                    <div className="empty-state compact">暂无审计记录。</div>
                  )}
                </div>
              </section>

              <section className="process-section" aria-label="审批审计引用">
                <div className="section-title compact">
                  <span>审批审计引用</span>
                  <small>{selectedApproval?.approval_id || '-'}</small>
                </div>
                <div className="audit-ref-list">
                  {approvalRefs.length ? (
                    approvalRefs.map((ref) => (
                      <span className="ref-chip" key={ref}>
                        {ref}
                      </span>
                    ))
                  ) : (
                    <div className="empty-state compact">暂无审批引用。</div>
                  )}
                </div>
              </section>
            </div>
          </>
        ) : (
          <div className="empty-state">选择一个事件查看审计历史。</div>
        )}
      </section>
    </div>
  )
}

function ApprovalCenterView({
  approvals,
  selectedApproval,
  selectedExecution,
  selectedApprovalId,
  decisionLoading,
  executionLoading,
  onSelect,
  onDecision,
}: {
  approvals: ApprovalRequest[]
  selectedApproval?: ApprovalRequest
  selectedExecution?: ApprovalExecution | null
  selectedApprovalId: string
  decisionLoading: string
  executionLoading: string
  onSelect: (approvalId: string) => void
  onDecision: (approvalId: string, decision: 'approve' | 'reject') => void
}) {
  const approvalRefs = Array.from(new Set([...(selectedApproval?.evidence_refs || []), ...(selectedApproval?.audit_refs || [])]))
  return (
    <div className="content-grid">
      <section className="incident-list" aria-label="审批请求列表">
        {approvals.length ? (
          approvals.map((approval) => (
            <button
              key={approval.approval_id}
              className={approval.approval_id === selectedApprovalId ? 'incident-row active' : 'incident-row'}
              onClick={() => onSelect(approval.approval_id)}
            >
              <span className={`status-chip ${approval.status || 'unknown'}`}>{approvalStatusLabel(approval.status)}</span>
              <strong>{approval.action_summary || approval.action_proposal_id || approval.approval_id}</strong>
              <span>
                {riskLabel(approval.risk_level)} · {approval.incident_id || '-'}
              </span>
            </button>
          ))
        ) : (
          <div className="empty-state">暂无审批请求。</div>
        )}
      </section>

      <section className="detail-panel" aria-label="审批详情">
        {selectedApproval ? (
          <>
            <div className="detail-heading">
              <span className={`status-chip ${selectedApproval.status || 'unknown'}`}>
                {approvalStatusLabel(selectedApproval.status)}
              </span>
              <h3>{selectedApproval.action_summary || '审批请求'}</h3>
              <p>{selectedApproval.approval_id}</p>
            </div>
            <dl className="detail-list">
              <div>
                <dt>事件</dt>
                <dd>{selectedApproval.incident_id || '-'}</dd>
              </div>
              <div>
                <dt>风险</dt>
                <dd>{riskLabel(selectedApproval.risk_level)}</dd>
              </div>
              <div>
                <dt>请求人</dt>
                <dd>{selectedApproval.requested_by || '-'}</dd>
              </div>
              <div>
                <dt>审批人</dt>
                <dd>{selectedApproval.approved_by || selectedApproval.assigned_approvers?.join('、') || '-'}</dd>
              </div>
              <div>
                <dt>过期时间</dt>
                <dd>{formatAuditTime(selectedApproval.expires_at)}</dd>
              </div>
              <div>
                <dt>决策时间</dt>
                <dd>{formatAuditTime(selectedApproval.decided_at)}</dd>
              </div>
            </dl>

            <div className="process-summary">
              <section className="process-section" aria-label="审批上下文">
                <div className="section-title compact">
                  <span>审批上下文</span>
                  <small>{selectedApproval.session_id || '-'}</small>
                </div>
                <p>{selectedApproval.action_summary || '暂无动作摘要。'}</p>
                <span className="muted-line">资源范围：{compactObject(selectedApproval.resource_scope)}</span>
              </section>

              <section className="process-section" aria-label="回滚计划">
                <div className="section-title compact">
                  <span>回滚计划</span>
                </div>
                <p>{selectedApproval.rollback_plan || '暂无回滚计划。'}</p>
              </section>

              <ExecutionTrackingView
                approval={selectedApproval}
                execution={selectedExecution}
                loading={executionLoading === selectedApproval.approval_id}
              />

              <section className="process-section" aria-label="审批引用">
                <div className="section-title compact">
                  <span>证据 / 审计引用</span>
                </div>
                <div className="audit-ref-list">
                  {approvalRefs.length ? (
                    approvalRefs.map((ref) => (
                      <span className="ref-chip" key={ref}>
                        {ref}
                      </span>
                    ))
                  ) : (
                    <div className="empty-state compact">暂无审批引用。</div>
                  )}
                </div>
              </section>

              <section className="process-section" aria-label="审批决策">
                <div className="section-title compact">
                  <span>审批决策</span>
                  <small>{selectedApproval.status === 'pending' ? 'Gateway 授权后生效' : '只读'}</small>
                </div>
                <div className="decision-actions">
                  <button
                    className="secondary-action"
                    type="button"
                    disabled={selectedApproval.status !== 'pending' || Boolean(decisionLoading)}
                    onClick={() => onDecision(selectedApproval.approval_id, 'approve')}
                  >
                    {decisionLoading === `${selectedApproval.approval_id}:approve` ? '处理中' : '通过'}
                  </button>
                  <button
                    className="danger-action"
                    type="button"
                    disabled={selectedApproval.status !== 'pending' || Boolean(decisionLoading)}
                    onClick={() => onDecision(selectedApproval.approval_id, 'reject')}
                  >
                    {decisionLoading === `${selectedApproval.approval_id}:reject` ? '处理中' : '拒绝'}
                  </button>
                </div>
              </section>
            </div>
          </>
        ) : (
          <div className="empty-state">选择一个审批请求查看详情。</div>
        )}
      </section>
    </div>
  )
}

function ExecutionTrackingView({
  approval,
  execution,
  loading,
}: {
  approval: ApprovalRequest
  execution?: ApprovalExecution | null
  loading: boolean
}) {
  const grant = approval.execution_grant
  const status = execution?.status || ''
  const preflightDone = Boolean(execution?.preflight_result) || ['executing', 'post_checking', 'succeeded', 'failed', 'rollback_required'].includes(status)
  const mutationDone = Boolean(execution?.execution_result) || ['post_checking', 'succeeded', 'rollback_required'].includes(status)
  const postCheckDone = Boolean(execution?.post_check_result) || ['succeeded', 'rollback_required'].includes(status)
  return (
    <section className="process-section" aria-label="执行跟踪">
      <div className="section-title compact">
        <span>执行跟踪</span>
        <small>{loading ? '加载中' : executionStatusLabel(status)}</small>
      </div>
      <p>{grant ? '审批已生成 Gateway 执行授权，执行状态只从 Gateway 读取。' : '当前审批没有可执行授权。'}</p>
      <dl className="detail-list compact">
        <div>
          <dt>授权状态</dt>
          <dd>{grant ? '已授权' : '不可执行'}</dd>
        </div>
        <div>
          <dt>执行记录</dt>
          <dd>{execution?.execution_id || '尚未执行'}</dd>
        </div>
        <div>
          <dt>集群 / 命名空间</dt>
          <dd>{[execution?.cluster_id, execution?.namespace].filter(Boolean).join(' / ') || compactObject(grant?.resource_scope)}</dd>
        </div>
        <div>
          <dt>更新时间</dt>
          <dd>{formatAuditTime(execution?.updated_at || grant?.decided_at)}</dd>
        </div>
      </dl>
      <div className="execution-steps" aria-label="执行生命周期">
        <ExecutionStep title="预检" status={preflightDone ? 'succeeded' : status === 'preflight_running' ? 'running' : status === 'preflight_failed' ? 'failed' : 'pending'} detail={resultSummary(execution?.preflight_result)} command={commandSummary(execution?.preflight)} />
        <ExecutionStep title="执行" status={mutationDone ? 'succeeded' : status === 'executing' ? 'running' : status === 'failed' ? 'failed' : 'pending'} detail={resultSummary(execution?.execution_result)} command={commandSummary(execution?.action)} />
        <ExecutionStep title="复检" status={postCheckDone && status !== 'rollback_required' ? 'succeeded' : status === 'post_checking' ? 'running' : status === 'rollback_required' ? 'failed' : 'pending'} detail={resultSummary(execution?.post_check_result)} command={commandSummary(execution?.post_check)} />
        <ExecutionStep title="回滚" status={status === 'rollback_required' ? 'failed' : 'pending'} detail={execution?.error_message || '未触发回滚。'} command={execution?.error_code || '-'} />
      </div>
      <div className="decision-actions">
        <button className="secondary-action" type="button" disabled>
          {grant ? '执行入口未开放' : '无执行授权'}
        </button>
      </div>
    </section>
  )
}

function ExecutionStep({
  title,
  status,
  detail,
  command,
}: {
  title: string
  status: string
  detail: string
  command: string
}) {
  return (
    <article className="process-item">
      <div className="item-heading">
        <strong>{title}</strong>
        <span className={`status-chip ${status}`}>{executionStatusLabel(status)}</span>
      </div>
      <p>{detail}</p>
      <small>命令：{command}</small>
    </article>
  )
}

function NotificationCenterView({
  notificationTypes,
  deliveries,
  selectedDelivery,
  selectedDeliveryId,
  statusFilter,
  typeFilter,
  loading,
  onSelect,
  onStatusFilter,
  onTypeFilter,
  onRefresh,
}: {
  notificationTypes: string[]
  deliveries: NotificationDelivery[]
  selectedDelivery?: NotificationDelivery
  selectedDeliveryId: string
  statusFilter: string
  typeFilter: string
  loading: boolean
  onSelect: (deliveryId: string) => void
  onStatusFilter: (status: string) => void
  onTypeFilter: (type: string) => void
  onRefresh: () => void
}) {
  return (
    <div className="content-grid">
      <section className="incident-list" aria-label="通知投递列表">
        <div className="filter-row">
          <label>
            状态
            <select value={statusFilter} onChange={(event) => onStatusFilter(event.target.value)}>
              <option value="">全部</option>
              <option value="sent">已送达</option>
              <option value="pending">待投递</option>
              <option value="failed">投递失败</option>
              <option value="dead_letter">死信</option>
              <option value="suppressed">已抑制</option>
            </select>
          </label>
          <label>
            类型
            <select value={typeFilter} onChange={(event) => onTypeFilter(event.target.value)}>
              <option value="">全部</option>
              {notificationTypes.map((type) => (
                <option value={type} key={type}>
                  {notificationTypeLabel(type)}
                </option>
              ))}
            </select>
          </label>
          <button className="secondary-action" type="button" disabled={loading} onClick={onRefresh}>
            {loading ? '筛选中' : '应用筛选'}
          </button>
        </div>

        {deliveries.length ? (
          deliveries.map((delivery) => (
            <button
              key={delivery.id}
              className={delivery.id === selectedDeliveryId ? 'incident-row active' : 'incident-row'}
              onClick={() => onSelect(delivery.id)}
            >
              <span className={`status-chip ${delivery.delivery_status || 'unknown'}`}>{deliveryStatusLabel(delivery.delivery_status)}</span>
              <strong>{notificationTypeLabel(delivery.notification_type)}</strong>
              <span>
                {delivery.incident_id || delivery.approval_id || delivery.service_id || delivery.notification_id || '-'} · 尝试 {delivery.delivery_attempts ?? 0}/
                {delivery.max_attempts ?? '-'}
              </span>
            </button>
          ))
        ) : (
          <div className="empty-state">暂无通知投递记录。</div>
        )}
      </section>

      <section className="detail-panel" aria-label="通知投递详情">
        {selectedDelivery ? (
          <>
            <div className="detail-heading">
              <span className={`status-chip ${selectedDelivery.delivery_status || 'unknown'}`}>
                {deliveryStatusLabel(selectedDelivery.delivery_status)}
              </span>
              <h3>{notificationTypeLabel(selectedDelivery.notification_type)}</h3>
              <p>{selectedDelivery.id}</p>
            </div>
            <dl className="detail-list">
              <div>
                <dt>通知 ID</dt>
                <dd>{selectedDelivery.notification_id || '-'}</dd>
              </div>
              <div>
                <dt>模板</dt>
                <dd>{selectedDelivery.template_id || '-'}</dd>
              </div>
              <div>
                <dt>事件</dt>
                <dd>{selectedDelivery.incident_id || '-'}</dd>
              </div>
              <div>
                <dt>审批</dt>
                <dd>{selectedDelivery.approval_id || '-'}</dd>
              </div>
              <div>
                <dt>服务 / 团队</dt>
                <dd>{[selectedDelivery.service_id, selectedDelivery.team_id].filter(Boolean).join(' / ') || '-'}</dd>
              </div>
              <div>
                <dt>目标</dt>
                <dd>{selectedDelivery.chat_id || '-'}</dd>
              </div>
              <div>
                <dt>创建时间</dt>
                <dd>{formatAuditTime(selectedDelivery.created_at)}</dd>
              </div>
              <div>
                <dt>最后投递</dt>
                <dd>{formatAuditTime(selectedDelivery.last_delivery_at || selectedDelivery.sent_at)}</dd>
              </div>
            </dl>

            <div className="process-summary">
              <section className="process-section" aria-label="通知类型目录">
                <div className="section-title compact">
                  <span>通知类型目录</span>
                  <small>只读</small>
                </div>
                <div className="audit-ref-list">
                  {notificationTypes.length ? (
                    notificationTypes.map((type) => (
                      <span className="ref-chip" key={type}>
                        {notificationTypeLabel(type)}
                      </span>
                    ))
                  ) : (
                    <div className="empty-state compact">暂无通知类型。</div>
                  )}
                </div>
              </section>

              <section className="process-section" aria-label="失败与死信">
                <div className="section-title compact">
                  <span>失败 / 死信</span>
                  <small>{selectedDelivery.delivery_status === 'dead_letter' ? '需要人工处理' : '投递状态'}</small>
                </div>
                <p>{selectedDelivery.last_delivery_error || selectedDelivery.suppressed_reason || '暂无失败信息。'}</p>
                <span className="muted-line">
                  下次重试：{formatAuditTime(selectedDelivery.next_retry_at)} · 消息 ID：{selectedDelivery.target_message_id || '-'}
                </span>
              </section>

              <section className="process-section" aria-label="通知上下文">
                <div className="section-title compact">
                  <span>通知上下文</span>
                </div>
                <p>{String(selectedDelivery.payload?.summary || selectedDelivery.payload?.notification_type || '暂无通知上下文。')}</p>
                <span className="muted-line">Payload：{compactObject(selectedDelivery.payload)}</span>
              </section>
            </div>
          </>
        ) : (
          <div className="empty-state">选择一条通知投递查看详情。</div>
        )}
      </section>
    </div>
  )
}

function DiagnosisProcessView({
  process,
  evidence,
  timeline,
  missingEvidence,
  actions,
  auditReferenceIds,
}: {
  process: DiagnosisProcess
  evidence: EvidenceItem[]
  timeline: TimelineItem[]
  missingEvidence: MissingEvidence[]
  actions: ActionProposal[]
  auditReferenceIds: string[]
}) {
  const rootCause = process.diagnosis?.root_cause
  const timelineStart = timeline[0]?.occurred_at
  const timelineEnd = timeline[timeline.length - 1]?.occurred_at
  return (
    <div className="process-summary">
      <section className="process-section" aria-label="诊断摘要">
        <div className="section-title compact">
          <span>诊断摘要</span>
          <small>{diagnosisStatusLabel(process.diagnosis?.status || 'unknown')}</small>
        </div>
        <p>{process.diagnosis?.summary || process.diagnosis?.markdown || '暂无诊断摘要。'}</p>
        <dl className="detail-list compact">
          <div>
            <dt>会话</dt>
            <dd>{process.diagnosis?.session_id || process.incident?.latest_session_id || '-'}</dd>
          </div>
          <div>
            <dt>诊断时间</dt>
            <dd>{formatTime(process.diagnosis?.diagnosed_at)}</dd>
          </div>
        </dl>
      </section>

      <section className="process-section" aria-label="根因">
        <div className="section-title compact">
          <span>根因</span>
          <small>{rootCause?.category || 'unknown'}</small>
        </div>
        <p>{rootCause?.statement || rootCause?.summary || '暂无根因结论。'}</p>
        <span className="muted-line">置信度：{formatConfidence(rootCause?.confidence)}</span>
      </section>

      <div className="process-counts" aria-label="诊断计数">
        <Metric label="证据" value={evidence.length.toString()} />
        <Metric label="时间线" value={timeline.length.toString()} />
        <Metric label="缺失证据" value={missingEvidence.length.toString()} />
        <Metric label="建议动作" value={actions.length.toString()} />
      </div>

      <section className="process-section" aria-label="证据列表">
        <div className="section-title compact">
          <span>证据</span>
        </div>
        <div className="item-list">
          {evidence.length ? (
            evidence.map((item, index) => (
              <article className="process-item" key={item.evidence_id || `${item.kind}-${index}`}>
                <div className="item-heading">
                  <strong>{evidenceKindLabel(item.kind)}</strong>
                  <span className={`status-chip ${item.status || 'unknown'}`}>{diagnosisStatusLabel(item.status || 'unknown')}</span>
                </div>
                <p>{item.summary || '暂无证据摘要。'}</p>
                <small>
                  查询：{item.query?.display || '-'} · 采集时间：{formatTime(item.collected_at)}
                </small>
                {item.failure ? <small>失败原因：{item.failure.message || item.failure.code || '-'}</small> : null}
              </article>
            ))
          ) : (
            <div className="empty-state compact">暂无证据。</div>
          )}
        </div>
      </section>

      <section className="process-section" aria-label="诊断时间线">
        <div className="section-title compact">
          <span>时间线</span>
        </div>
        <div className="item-list timeline-list">
          {timeline.length ? (
            timeline.map((event, index) => (
              <article className="process-item" key={event.event_id || `${event.type}-${index}`}>
                <div className="item-heading">
                  <strong>{event.title || event.type || '事件'}</strong>
                  <span className={`status-chip ${event.status || 'unknown'}`}>{diagnosisStatusLabel(event.status || 'unknown')}</span>
                </div>
                <p>{event.summary || '暂无事件摘要。'}</p>
                <small>
                  {formatTime(event.occurred_at)} · 引用：{compactRefs(event.refs)}
                </small>
              </article>
            ))
          ) : (
            <div className="empty-state compact">暂无时间线。</div>
          )}
        </div>
      </section>

      <section className="process-section" aria-label="历史与审计">
        <div className="section-title compact">
          <span>历史 / 时间线 / 审计</span>
          <small>{diagnosisStatusLabel(process.audit?.status || 'unknown')}</small>
        </div>
        <dl className="detail-list compact">
          <div>
            <dt>历史范围</dt>
            <dd>{timeline.length ? `${formatTime(timelineStart)} 至 ${formatTime(timelineEnd)}` : '暂无时间线'}</dd>
          </div>
          <div>
            <dt>审计状态</dt>
            <dd>{process.audit?.summary || '暂无审计记录。'}</dd>
          </div>
        </dl>
        <div className="audit-ref-list" aria-label="审计引用">
          {auditReferenceIds.length ? (
            auditReferenceIds.map((ref) => (
              <span className="ref-chip" key={ref}>
                {ref}
              </span>
            ))
          ) : (
            <div className="empty-state compact">暂无审计记录。</div>
          )}
        </div>
      </section>

      <section className="process-section" aria-label="缺失证据">
        <div className="section-title compact">
          <span>缺失证据</span>
        </div>
        <div className="item-list">
          {missingEvidence.length ? (
            missingEvidence.map((item, index) => (
              <article className="process-item" key={`${item.source_type || item.tool || 'missing'}-${index}`}>
                <div className="item-heading">
                  <strong>{evidenceKindLabel(item.source_type || item.tool)}</strong>
                  <span className="status-chip partial">{item.audit?.error_code || '缺失'}</span>
                </div>
                <p>{item.reason || '该证据未采集。'}</p>
                <small>工具：{item.tool || '-'}</small>
              </article>
            ))
          ) : (
            <div className="empty-state compact">没有缺失证据。</div>
          )}
        </div>
      </section>

      <section className="process-section" aria-label="建议动作">
        <div className="section-title compact">
          <span>建议动作</span>
          <small>只读展示</small>
        </div>
        <div className="item-list">
          {actions.length ? (
            actions.map((action, index) => (
              <article className="process-item" key={action.action_proposal_id || `action-${index}`}>
                <div className="item-heading">
                  <strong>{action.summary || '暂无动作摘要。'}</strong>
                  <span className="status-chip readonly">只读</span>
                </div>
                <small>
                  风险：{riskLabel(action.risk_level)} · 需要审批：{action.approval_required ? '是' : '否'}
                </small>
              </article>
            ))
          ) : (
            <div className="empty-state compact">暂无建议动作。</div>
          )}
        </div>
      </section>
    </div>
  )
}
