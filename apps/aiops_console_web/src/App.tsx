import { createContext, FormEvent, ReactNode, useContext, useEffect, useState } from 'react'
import {
  BrowserRouter,
  Link,
  Navigate,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from 'react-router'

type Locale = 'zh-CN' | 'en-US'

type Actor = {
  username: string
  display_name?: string
  roles?: string[]
  permissions?: string[]
}

type MeResponse = {
  actor: Actor
  role_permission_matrix?: Record<string, string[]>
}

type LoginResponse = MeResponse & {
  token?: string
}

type UserRecord = {
  username: string
  display_name?: string
  email?: string | null
  roles: string[]
  scope: {
    clusters: string[]
    services: string[]
    teams: string[]
    namespaces: string[]
  }
  disabled: boolean
  source: string
  last_login_at?: number | null
  recent_permission_audit?: {
    action: string
    result?: string
    actor?: string
    when_ts?: number
  } | null
}

type SettingsVersion = {
  version_number: number
  settings: Record<string, unknown>
  diff?: unknown[]
  created_by?: string
  created_at?: number
  change_summary?: string
  reload_required?: boolean
}

type SettingsPreview = {
  settings: Record<string, unknown>
  diff: unknown[]
  critical: boolean
  confirmation_text: string
  reload_required: boolean
}

type PolicyHit = {
  id: number
  when_ts: number
  actor: string
  action_type: string
  cluster: string
  namespace?: string | null
  environment: string
  decision: string
  reason: string
  settings_version: number
}

type PolicyState = {
  version: number
  cluster_environments: Array<{ cluster: string; environment: string }>
  default_environment: string
  action_allowlist: string[]
  policy: Record<string, unknown>
  recent_policy_hits: PolicyHit[]
}

type EvidenceSource = {
  kind: string
  status: string
  summary: string
  refs?: Array<{ ref_id: string; source: string }>
  samples?: Record<string, unknown>[]
}

type EvidenceResponse = {
  status: string
  scope: Record<string, string>
  limits: {
    limit: number
    time_range_seconds: number
    timeout_seconds: number
  }
  sources: EvidenceSource[]
}

type AgentRun = {
  run_id: string
  title: string
  status: string
  conversation_status: string
  incident_id?: string | null
  tags: string[]
  scope: Record<string, string>
  created_at: number
}

type AgentRunEvent = {
  id: number
  event_type: string
  thread_type: string
  message: string
  created_at: number
  promoted_from_event_id?: number | null
}

type AgentRunSnapshot = {
  conversation: {
    title: string
    tags: string[]
    status: string
  }
  run: AgentRun
  timeline: AgentRunEvent[]
  evidence_refs: string[]
  action_refs: string[]
  approval_refs: string[]
  execution_refs: string[]
  permissions: { can_message: boolean; can_promote: boolean }
}

type ApprovalRequest = {
  approval_id: string
  action_proposal_id: string
  status: string
  risk_level: string
  action_summary: string
  requested_by: string
  resource_scope: Record<string, string>
  decision_reason?: string | null
}

type ApprovalExecution = {
  execution_id: string
  status: string
  error_message?: string | null
}

type ActionProposal = {
  action_id: string
  action_hash: string
  action_type: string
  status: string
  target: Record<string, string | number>
}

type IncidentRow = {
  incident_id: string
  title: string
  severity?: string
  status: string
  service?: string | null
  team?: string | null
  namespace?: string | null
  cluster?: string | null
  impact?: string
  tags?: string[]
}

type WorkbenchPanelData = {
  name: string
  status: string
  data?: unknown
  error?: string
}

type IncidentWorkbench = {
  incident: IncidentRow
  panels: Record<string, WorkbenchPanelData>
  responsibility: Record<string, unknown>
}

type AuditChain = {
  chain_id: string
  time?: number | null
  incident_id?: string | null
  run_id?: string | null
  agent?: string | null
  requested_action?: string | null
  risk?: string | null
  target_resource?: Record<string, unknown>
  approver?: string | null
  approval_decision?: string | null
  gateway_execution_result?: string | null
  responsibility_status: string
  action_hash?: string | null
  approval_id?: string | null
  action_proposal_id?: string | null
  agent_request?: Record<string, unknown>
  evidence_refs?: unknown[]
  risk_classification?: Record<string, unknown>
  frozen_action?: Record<string, unknown>
  approver_snapshot?: Record<string, unknown>
  approval_remark?: string | null
  execution?: Record<string, unknown>
  notifications?: unknown[]
  delete_tombstones?: unknown[]
  raw_audit_refs?: unknown[]
}

type AuditRawRow = {
  id: number
  what: string
  result?: string | null
  actor?: string | null
  when_ts?: number | null
  cluster?: string | null
  namespace?: string | null
}

type AuditTombstone = {
  conversation_id: string
  run_id: string
  incident_id?: string | null
  deleted_by?: string | null
  deleted_at?: number | null
  reason?: string | null
  linked_approval_ids?: string[]
  linked_execution_ids?: string[]
}

type ReportVersion = {
  version_id: string
  version_number: number
  status: string
  html: string
  unknowns: string[]
  evidence_refs: unknown[]
  created_by: string
  created_at: number
  published_by?: string | null
  published_at?: number | null
}

type Feedback = {
  feedback_id: string
  target_type: string
  target_id: string
  incident_id?: string | null
  run_id?: string | null
  rating: string
  comment: string
  actor_id: string
  created_at: number
}

type ReportSnapshot = {
  latest_report?: ReportVersion | null
  versions: ReportVersion[]
  feedback: Feedback[]
}

type NotificationRecord = {
  id: string
  notification_type: string
  incident_id?: string | null
  approval_id?: string | null
  service_id?: string | null
  team_id?: string | null
  delivery_status: string
  delivery_attempts: number
  max_attempts: number
  dedupe_key: string
  next_retry_at?: number | null
  target_message_id?: string | null
  last_delivery_error?: string | null
  created_at: number
  summary?: string | null
}

const LOCALE_KEY = 'aiops.console.locale'
const DEFAULT_ROUTE = '/incidents'

const messages = {
  'zh-CN': {
    brand: 'AIOps 控制台',
    loginTitle: '登录 Gateway',
    loginHint: '使用 Gateway 会话继续访问控制台。',
    username: '用户名',
    password: '密码',
    signIn: '登录',
    signingIn: '登录中',
    signOut: '退出',
    gatewayOnly: 'Gateway-only',
    environment: 'prod',
    search: '搜索事件、审批或审计',
    loading: '正在检查会话。',
    forbiddenTitle: '403 无权访问',
    forbiddenText: '当前用户没有访问该页面的权限。',
    notFoundTitle: '404 页面不存在',
    notFoundText: '该控制台路由不存在。',
    comingSoon: '该页面的业务内容由后续切片实现。',
    resource: '资源',
    unsaved: '未保存备注',
    routeState: '路由状态',
    userCreate: '新建用户',
    userList: '用户列表',
    displayName: '显示名',
    roles: '角色',
    source: '来源',
    scope: '范围',
    status: '状态',
    enabled: '启用',
    disabled: '停用',
    lastLogin: '最近登录',
    recentAudit: '最近权限审计',
    clusters: '集群',
    services: '服务',
    teams: '团队',
    namespaces: '命名空间',
    save: '保存',
    disable: '停用',
    resetPassword: '重置密码',
    newPassword: '新密码',
    refresh: '刷新',
    create: '创建',
    settingsVersion: '设置版本',
    settingsEditor: '设置 JSON',
    settingsPreview: '差异预览',
    settingsConfirm: '关键变更确认',
    confirmationText: '请输入精确确认文本',
    rollback: '回滚上一版本',
    readOnly: '只读模式',
    reloadRequired: '需要重新加载',
    noReload: '无需重启',
    policySummary: '策略说明',
    policyTest: '测试策略',
    recentPolicyHits: '最近策略命中',
    defaultEnvironment: '默认环境',
    actionAllowlist: '动作允许列表',
    actionType: '动作类型',
    riskLevel: '风险等级',
    decision: '决策',
    reason: '原因',
    evidenceQuery: '证据查询',
    evidencePanels: '证据面板',
    evidenceTemplate: '查询模板',
    queryEvidence: '查询证据',
    timeRange: '时间范围',
    partialEvidence: '部分可用',
    emptyEvidence: '暂无证据',
    staleEvidence: '证据可能已过期',
    limit: '行数上限',
    metricsEvidence: '指标',
    logsEvidence: '日志',
    tracesEvidence: 'Trace',
    kubernetesEvidence: 'Kubernetes',
    topologyEvidence: '拓扑',
    changesEvidence: '变更',
    toolOutputEvidence: '工具输出',
    runList: 'Run 列表',
    runCreate: '新建 Run',
    runTitle: 'Run 标题',
    runMessage: '消息',
    runTimeline: '时间线',
    sideThread: '旁路',
    mainline: '主线',
    promote: '提升到主线',
    archive: '归档会话',
    deleteConversation: '删除会话内容',
    reconnecting: '正在连接事件流',
    staleRun: '事件流可能已过期',
    noRuns: '暂无 Run',
    actionRequest: '请求动作',
    deployment: 'Deployment',
    replicas: '副本数',
    approvalList: '审批列表',
    approvalRequired: '需要审批',
    executionStatus: '执行状态',
    approve: '批准',
    reject: '拒绝',
    actionHash: '动作 Hash',
    incidentList: '事件列表',
    incidentWorkbench: '事件工作台',
    timeline: '时间线',
    diagnosis: '诊断',
    responsibility: '责任摘要',
    pauseRun: '暂停 Run',
    terminateRun: '终止 Run',
    manualTakeover: '人工接管',
    humanNote: '人工备注',
    restartRun: '重启 Run',
    blockApprovals: '阻止新审批',
    resolveIncident: '恢复事件',
    reopenIncident: '重开事件',
    auditChains: '责任链列表',
    rawLogs: '原始日志',
    deletedConversations: '删除会话记录',
    requestedAction: '请求动作',
    gatewayExecution: 'Gateway 执行',
    responsibilityStatus: '责任状态',
    approver: '审批人',
    tombstone: '删除墓碑',
    notificationsRef: '通知记录',
    rawAuditRefs: '原始审计引用',
    reportDraft: '生成草稿',
    reportPublish: '发布版本',
    reportVersions: '报告版本',
    reportPreview: 'HTML 预览',
    reportExportHtml: '导出 HTML',
    reportUnknowns: '未知项',
    feedback: '人工反馈',
    feedbackTarget: '反馈目标',
    feedbackRating: '评分',
    feedbackComment: '反馈备注',
    deliveryStatus: '投递状态',
    deliveryAttempts: '投递次数',
    retryDelivery: '重试投递',
    liveNotifications: '实时通知',
    noNotifications: '暂无通知',
    loadFailed: '加载失败',
    actionFailed: '操作失败',
    nav: {
      incidents: '事件工作台',
      agentRuns: 'Agent Runs',
      approvals: '审批中心',
      audit: '审计',
      policies: '策略',
      users: '用户',
      settings: '设置',
      search: '搜索',
      notifications: '通知',
    },
    groups: {
      operations: '运维',
      evidence: '证据',
      governance: '治理',
      admin: '管理',
    },
    pages: {
      incidents: '事件工作台',
      incidentDetail: '事件详情',
      incidentReport: '事件报告',
      agentRuns: 'Agent Runs',
      newAgentRun: '新建 Agent Run',
      agentRunDetail: 'Agent Run 详情',
      approvals: '审批中心',
      approvalDetail: '审批详情',
      audit: '责任链审计',
      auditDetail: '审计链详情',
      policies: '策略',
      users: '用户',
      userDetail: '用户详情',
      settings: '设置',
      search: '搜索',
      notifications: '通知中心',
    },
  },
  'en-US': {
    brand: 'AIOps Console',
    loginTitle: 'Log in to Gateway',
    loginHint: 'Use a Gateway session to continue to the Console.',
    username: 'Username',
    password: 'Password',
    signIn: 'Sign in',
    signingIn: 'Signing in',
    signOut: 'Sign out',
    gatewayOnly: 'Gateway-only',
    environment: 'prod',
    search: 'Search incidents, approvals, or audit',
    loading: 'Checking session.',
    forbiddenTitle: '403 Forbidden',
    forbiddenText: 'Your user cannot access this page.',
    notFoundTitle: '404 Not found',
    notFoundText: 'This Console route does not exist.',
    comingSoon: 'The business content for this page belongs to a later slice.',
    resource: 'Resource',
    unsaved: 'Unsaved note',
    routeState: 'Route state',
    userCreate: 'Create user',
    userList: 'Users',
    displayName: 'Display name',
    roles: 'Roles',
    source: 'Source',
    scope: 'Scope',
    status: 'Status',
    enabled: 'Enabled',
    disabled: 'Disabled',
    lastLogin: 'Recent login',
    recentAudit: 'Recent permission audit',
    clusters: 'Clusters',
    services: 'Services',
    teams: 'Teams',
    namespaces: 'Namespaces',
    save: 'Save',
    disable: 'Disable',
    resetPassword: 'Reset password',
    newPassword: 'New password',
    refresh: 'Refresh',
    create: 'Create',
    settingsVersion: 'Settings version',
    settingsEditor: 'Settings JSON',
    settingsPreview: 'Diff preview',
    settingsConfirm: 'Critical change confirmation',
    confirmationText: 'Enter the exact confirmation text',
    rollback: 'Roll back previous version',
    readOnly: 'Read only',
    reloadRequired: 'Reload required',
    noReload: 'No restart required',
    policySummary: 'Policy summary',
    policyTest: 'Test policy',
    recentPolicyHits: 'Recent policy hits',
    defaultEnvironment: 'Default environment',
    actionAllowlist: 'Action allowlist',
    actionType: 'Action type',
    riskLevel: 'Risk level',
    decision: 'Decision',
    reason: 'Reason',
    evidenceQuery: 'Evidence query',
    evidencePanels: 'Evidence panels',
    evidenceTemplate: 'Query template',
    queryEvidence: 'Query evidence',
    timeRange: 'Time range',
    partialEvidence: 'Partially available',
    emptyEvidence: 'No evidence',
    staleEvidence: 'Evidence may be stale',
    limit: 'Row limit',
    metricsEvidence: 'Metrics',
    logsEvidence: 'Logs',
    tracesEvidence: 'Trace',
    kubernetesEvidence: 'Kubernetes',
    topologyEvidence: 'Topology',
    changesEvidence: 'Changes',
    toolOutputEvidence: 'Tool output',
    runList: 'Runs',
    runCreate: 'New Run',
    runTitle: 'Run title',
    runMessage: 'Message',
    runTimeline: 'Timeline',
    sideThread: 'Side',
    mainline: 'Mainline',
    promote: 'Promote',
    archive: 'Archive conversation',
    deleteConversation: 'Delete chat content',
    reconnecting: 'Connecting event stream',
    staleRun: 'Event stream may be stale',
    noRuns: 'No runs',
    actionRequest: 'Request action',
    deployment: 'Deployment',
    replicas: 'Replicas',
    approvalList: 'Approvals',
    approvalRequired: 'Approval required',
    executionStatus: 'Execution status',
    approve: 'Approve',
    reject: 'Reject',
    actionHash: 'Action hash',
    incidentList: 'Incidents',
    incidentWorkbench: 'Incident workbench',
    timeline: 'Timeline',
    diagnosis: 'Diagnosis',
    responsibility: 'Responsibility',
    pauseRun: 'Pause run',
    terminateRun: 'Terminate run',
    manualTakeover: 'Manual takeover',
    humanNote: 'Human note',
    restartRun: 'Restart run',
    blockApprovals: 'Block approvals',
    resolveIncident: 'Resolve incident',
    reopenIncident: 'Reopen incident',
    auditChains: 'Responsibility chains',
    rawLogs: 'Raw logs',
    deletedConversations: 'Deleted conversations',
    requestedAction: 'Requested action',
    gatewayExecution: 'Gateway execution',
    responsibilityStatus: 'Responsibility status',
    approver: 'Approver',
    tombstone: 'Deletion tombstone',
    notificationsRef: 'Notification records',
    rawAuditRefs: 'Raw audit refs',
    reportDraft: 'Generate draft',
    reportPublish: 'Publish version',
    reportVersions: 'Report versions',
    reportPreview: 'HTML preview',
    reportExportHtml: 'Export HTML',
    reportUnknowns: 'Unknowns',
    feedback: 'Human feedback',
    feedbackTarget: 'Feedback target',
    feedbackRating: 'Rating',
    feedbackComment: 'Feedback comment',
    deliveryStatus: 'Delivery status',
    deliveryAttempts: 'Attempts',
    retryDelivery: 'Retry delivery',
    liveNotifications: 'Live notifications',
    noNotifications: 'No notifications',
    loadFailed: 'Load failed',
    actionFailed: 'Action failed',
    nav: {
      incidents: 'Incidents',
      agentRuns: 'Agent Runs',
      approvals: 'Approvals',
      audit: 'Audit',
      policies: 'Policies',
      users: 'Users',
      settings: 'Settings',
      search: 'Search',
      notifications: 'Notifications',
    },
    groups: {
      operations: 'Operations',
      evidence: 'Evidence',
      governance: 'Governance',
      admin: 'Admin',
    },
    pages: {
      incidents: 'Incident workbench',
      incidentDetail: 'Incident detail',
      incidentReport: 'Incident report',
      agentRuns: 'Agent Runs',
      newAgentRun: 'New Agent Run',
      agentRunDetail: 'Agent Run detail',
      approvals: 'Approval center',
      approvalDetail: 'Approval detail',
      audit: 'Responsibility audit',
      auditDetail: 'Audit chain detail',
      policies: 'Policies',
      users: 'Users',
      userDetail: 'User detail',
      settings: 'Settings',
      search: 'Search',
      notifications: 'Notification center',
    },
  },
} satisfies Record<Locale, Record<string, unknown>>

type T = typeof messages['zh-CN']
const LocaleContext = createContext<Locale>('zh-CN')

const routes = [
  { to: '/incidents', key: 'incidents', group: 'operations', permission: 'view_incident' },
  { to: '/agent-runs', key: 'agentRuns', group: 'operations', permission: 'view_incident' },
  { to: '/approvals', key: 'approvals', group: 'governance', permission: 'approve_action' },
  { to: '/audit', key: 'audit', group: 'governance', permission: 'query_audit' },
  { to: '/policies', key: 'policies', group: 'governance', permission: 'view_policy' },
  { to: '/users', key: 'users', group: 'admin', permission: 'view_users' },
  { to: '/settings', key: 'settings', group: 'admin', permission: 'view_settings' },
  { to: '/search', key: 'search', group: 'evidence', permission: 'view_evidence' },
  { to: '/notifications', key: 'notifications', group: 'operations', permission: 'view_incident' },
] as const

function storedLocale(): Locale {
  return localStorage.getItem(LOCALE_KEY) === 'en-US' ? 'en-US' : 'zh-CN'
}

function useT(): T {
  return messages[useContext(LocaleContext)] as T
}

async function readJson<TPayload>(url: string, init?: RequestInit): Promise<TPayload> {
  const response = await fetch(url, {
    credentials: 'same-origin',
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init?.headers || {}),
    },
  })
  const payload = (await response.json()) as TPayload & { error?: { message?: string } }
  if (!response.ok) {
    throw new Error(payload.error?.message || `HTTP ${response.status}`)
  }
  return payload
}

async function writeJson<TPayload>(url: string, body: Record<string, unknown>, method = 'POST'): Promise<TPayload> {
  const csrf = await readJson<{ csrf_token: string }>('/auth/csrf')
  return readJson<TPayload>(url, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf.csrf_token },
    body: JSON.stringify(body),
  })
}

function csvValues(value: string): string[] {
  return value.split(',').map((item) => item.trim()).filter(Boolean)
}

function formatTime(value?: number | null): string {
  if (!value) {
    return '-'
  }
  return new Date(value * 1000).toLocaleString()
}

function safeNext(value: string | null): string {
  if (!value || !value.startsWith('/') || value.startsWith('//') || value.startsWith('/auth/')) {
    return DEFAULT_ROUTE
  }
  return value
}

function canAccess(actor: Actor | null, permission: string): boolean {
  if (!actor) {
    return false
  }
  return new Set(actor.permissions || []).has(permission)
}

function AppShell() {
  const [locale, setLocale] = useState<Locale>(storedLocale)
  const [actor, setActor] = useState<Actor | null>(null)
  const [checked, setChecked] = useState(false)
  const t = messages[locale] as T

  useEffect(() => {
    localStorage.setItem(LOCALE_KEY, locale)
    document.documentElement.lang = locale
  }, [locale])

  useEffect(() => {
    void refreshMe()
  }, [])

  async function refreshMe() {
    try {
      const data = await readJson<MeResponse>('/auth/me')
      setActor(data.actor)
    } catch {
      setActor(null)
    } finally {
      setChecked(true)
    }
  }

  async function logout() {
    try {
      const csrf = await readJson<{ csrf_token: string }>('/auth/csrf')
      await readJson('/auth/logout', {
        method: 'POST',
        headers: { 'X-CSRF-Token': csrf.csrf_token },
      })
    } finally {
      setActor(null)
    }
  }

  return (
    <LocaleContext.Provider value={locale}>
      <Routes>
        <Route path="/login" element={<LoginPage actor={actor} locale={locale} setLocale={setLocale} onLogin={setActor} />} />
        <Route
          path="/*"
          element={
            checked ? (
              <Shell actor={actor} locale={locale} setLocale={setLocale} onLogout={logout}>
                <Routes>
                  <Route path="/" element={<Navigate to={DEFAULT_ROUTE} replace />} />
                  <Route path="/incidents" element={<Protected actor={actor} permission="view_incident"><IncidentsPage /></Protected>} />
                  <Route path="/incidents/:incidentId" element={<Protected actor={actor} permission="view_incident"><IncidentWorkbenchPage /></Protected>} />
                  <Route path="/incidents/:incidentId/report" element={<Protected actor={actor} permission="view_incident"><IncidentReportPage /></Protected>} />
                  <Route path="/agent-runs" element={<Protected actor={actor} permission="view_incident"><AgentRunsPage /></Protected>} />
                  <Route path="/agent-runs/new" element={<Protected actor={actor} permission="view_incident"><NewAgentRunPage /></Protected>} />
                  <Route path="/agent-runs/:runId" element={<Protected actor={actor} permission="view_incident"><AgentRunDetailPage /></Protected>} />
                  <Route path="/approvals" element={<Protected actor={actor} permission="approve_action"><ApprovalsPage /></Protected>} />
                  <Route path="/approvals/:approvalId" element={<Protected actor={actor} permission="approve_action"><ApprovalDetailPage /></Protected>} />
                  <Route path="/audit" element={<Protected actor={actor} permission="query_audit"><AuditPage /></Protected>} />
                  <Route path="/audit/:chainId" element={<Protected actor={actor} permission="query_audit"><AuditDetailPage /></Protected>} />
                  <Route path="/policies" element={<Protected actor={actor} permission="view_policy"><PoliciesPage /></Protected>} />
                  <Route path="/users" element={<Protected actor={actor} permission="view_users"><UsersPage actor={actor} /></Protected>} />
                  <Route path="/users/:userId" element={<Protected actor={actor} permission="view_users"><Page title={String(t.pages.userDetail)} paramName="userId" /></Protected>} />
                  <Route path="/settings" element={<Protected actor={actor} permission="view_settings"><SettingsPage actor={actor} /></Protected>} />
                  <Route path="/search" element={<Protected actor={actor} permission="view_evidence"><EvidencePage /></Protected>} />
                  <Route path="/notifications" element={<Protected actor={actor} permission="view_incident"><NotificationsPage /></Protected>} />
                  <Route path="*" element={<NotFound />} />
                </Routes>
              </Shell>
            ) : (
              <main className="center-state">{String(t.loading)}</main>
            )
          }
        />
      </Routes>
    </LocaleContext.Provider>
  )
}

function LoginPage({
  actor,
  locale,
  setLocale,
  onLogin,
}: {
  actor: Actor | null
  locale: Locale
  setLocale: (locale: Locale) => void
  onLogin: (actor: Actor) => void
}) {
  const [username, setUsername] = useState('alice')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [search] = useSearchParams()
  const navigate = useNavigate()
  const t = messages[locale] as T
  const next = safeNext(search.get('next'))

  useEffect(() => {
    if (actor) {
      navigate(next, { replace: true })
    }
  }, [actor, navigate, next])

  async function submit(event: FormEvent) {
    event.preventDefault()
    setLoading(true)
    setError('')
    try {
      const data = await readJson<LoginResponse>('/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      })
      onLogin(data.actor)
      navigate(next, { replace: true })
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'login failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="login-page">
      <section className="login-shell" aria-label={String(t.loginTitle)}>
        <LocaleSwitch locale={locale} setLocale={setLocale} />
        <div>
          <p className="eyebrow">{String(t.gatewayOnly)}</p>
          <h1>{String(t.loginTitle)}</h1>
          <p>{String(t.loginHint)}</p>
        </div>
        <form className="login-form" onSubmit={submit}>
          <label>
            {String(t.username)}
            <input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" />
          </label>
          <label>
            {String(t.password)}
            <input value={password} onChange={(event) => setPassword(event.target.value)} type="password" autoComplete="current-password" />
          </label>
          {error ? <p className="form-error">{error}</p> : null}
          <button className="primary-action" type="submit" disabled={loading}>
            {loading ? String(t.signingIn) : String(t.signIn)}
          </button>
        </form>
      </section>
    </main>
  )
}

function Shell({
  actor,
  locale,
  setLocale,
  onLogout,
  children,
}: {
  actor: Actor | null
  locale: Locale
  setLocale: (locale: Locale) => void
  onLogout: () => void
  children: ReactNode
}) {
  const t = messages[locale] as T
  const visibleRoutes = routes.filter((route) => canAccess(actor, route.permission))

  return (
    <div className="console-shell">
      <aside className="side-panel">
        <div className="brand-block">
          <span className="brand-mark">AI</span>
          <div>
            <p className="eyebrow">{String(t.gatewayOnly)}</p>
            <h1>{String(t.brand)}</h1>
          </div>
        </div>
        {(['operations', 'evidence', 'governance', 'admin'] as const).map((group) => {
          const items = visibleRoutes.filter((route) => route.group === group)
          if (!items.length) {
            return null
          }
          return (
            <nav className="nav-group" aria-label={String((t.groups as Record<string, string>)[group])} key={group}>
              <p>{String((t.groups as Record<string, string>)[group])}</p>
              {items.map((route) => (
                <NavLinkItem key={route.to} to={route.to} label={String((t.nav as Record<string, string>)[route.key])} />
              ))}
            </nav>
          )
        })}
      </aside>
      <section className="workspace">
        <header className="topbar">
          <input aria-label={String(t.search)} placeholder={String(t.search)} readOnly />
          <span className="env-badge">{String(t.environment)}</span>
          <LocaleSwitch locale={locale} setLocale={setLocale} />
          <span className="user-chip">{actor?.display_name || actor?.username || '-'}</span>
          <button className="text-action" type="button" onClick={onLogout}>
            {String(t.signOut)}
          </button>
        </header>
        {children}
      </section>
    </div>
  )
}

function NavLinkItem({ to, label }: { to: string; label: string }) {
  const location = useLocation()
  const active = location.pathname === to || location.pathname.startsWith(`${to}/`)
  return (
    <Link className={active ? 'nav-link active' : 'nav-link'} to={to} aria-current={active ? 'page' : undefined}>
      {label}
    </Link>
  )
}

function Protected({ actor, permission, children }: { actor: Actor | null; permission: string; children: ReactNode }) {
  const location = useLocation()
  if (!actor) {
    return <Navigate to={`/login?next=${encodeURIComponent(location.pathname + location.search)}`} replace />
  }
  if (!canAccess(actor, permission)) {
    return <Forbidden />
  }
  return <>{children}</>
}

function UsersPage({ actor }: { actor: Actor | null }) {
  const t = useT()
  const canManage = canAccess(actor, 'manage_users')
  const [users, setUsers] = useState<UserRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [form, setForm] = useState({
    username: '',
    password: '',
    displayName: '',
    roles: 'viewer',
    clusters: '',
    services: '',
    teams: '',
    namespaces: '',
  })

  useEffect(() => {
    void loadUsers()
  }, [])

  async function loadUsers() {
    setLoading(true)
    setError('')
    try {
      const data = await readJson<{ users: UserRecord[] }>('/api/users')
      setUsers(data.users)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.loadFailed))
    } finally {
      setLoading(false)
    }
  }

  async function createUser(event: FormEvent) {
    event.preventDefault()
    setError('')
    try {
      await writeJson('/api/users', {
        username: form.username,
        password: form.password,
        display_name: form.displayName || form.username,
        roles: csvValues(form.roles),
        scope: {
          clusters: csvValues(form.clusters),
          services: csvValues(form.services),
          teams: csvValues(form.teams),
          namespaces: csvValues(form.namespaces),
        },
      })
      setForm({ username: '', password: '', displayName: '', roles: 'viewer', clusters: '', services: '', teams: '', namespaces: '' })
      await loadUsers()
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.actionFailed))
    }
  }

  return (
    <main className="page users-page">
      <header className="page-header">
        <p className="eyebrow">{String(t.gatewayOnly)}</p>
        <h2>{String(t.pages.users)}</h2>
        <button className="text-action" type="button" onClick={() => void loadUsers()}>
          {String(t.refresh)}
        </button>
      </header>
      {error ? <p className="form-error">{error}</p> : null}
      {canManage ? (
        <form className="user-create-form" onSubmit={createUser}>
          <h3>{String(t.userCreate)}</h3>
          <label>{String(t.username)}<input value={form.username} onChange={(event) => setForm({ ...form, username: event.target.value })} /></label>
          <label>{String(t.password)}<input type="password" value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} /></label>
          <label>{String(t.displayName)}<input value={form.displayName} onChange={(event) => setForm({ ...form, displayName: event.target.value })} /></label>
          <label>{String(t.roles)}<input value={form.roles} onChange={(event) => setForm({ ...form, roles: event.target.value })} /></label>
          <label>{String(t.clusters)}<input value={form.clusters} onChange={(event) => setForm({ ...form, clusters: event.target.value })} /></label>
          <label>{String(t.services)}<input value={form.services} onChange={(event) => setForm({ ...form, services: event.target.value })} /></label>
          <label>{String(t.teams)}<input value={form.teams} onChange={(event) => setForm({ ...form, teams: event.target.value })} /></label>
          <label>{String(t.namespaces)}<input value={form.namespaces} onChange={(event) => setForm({ ...form, namespaces: event.target.value })} /></label>
          <button className="primary-action" type="submit">{String(t.create)}</button>
        </form>
      ) : null}
      <section className="users-section" aria-label={String(t.userList)}>
        <h3>{String(t.userList)}</h3>
        {loading ? <p>{String(t.loading)}</p> : null}
        <div className="users-table">
          {users.map((user) => (
            <UserRow key={user.username} user={user} canManage={canManage} onRefresh={loadUsers} />
          ))}
        </div>
      </section>
    </main>
  )
}

function UserRow({ user, canManage, onRefresh }: { user: UserRecord; canManage: boolean; onRefresh: () => Promise<void> }) {
  const t = useT()
  const [roles, setRoles] = useState(user.roles.join(', '))
  const [clusters, setClusters] = useState(user.scope.clusters.join(', '))
  const [services, setServices] = useState(user.scope.services.join(', '))
  const [teams, setTeams] = useState(user.scope.teams.join(', '))
  const [namespaces, setNamespaces] = useState(user.scope.namespaces.join(', '))
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    setRoles(user.roles.join(', '))
    setClusters(user.scope.clusters.join(', '))
    setServices(user.scope.services.join(', '))
    setTeams(user.scope.teams.join(', '))
    setNamespaces(user.scope.namespaces.join(', '))
  }, [user])

  async function run(action: () => Promise<unknown>) {
    setError('')
    try {
      await action()
      await onRefresh()
      setPassword('')
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.actionFailed))
    }
  }

  return (
    <article className="user-row">
      <div className="user-summary">
        <div>
          <h3>{user.display_name || user.username}</h3>
          <p><code>{user.username}</code></p>
        </div>
        <span className={user.disabled ? 'status-pill danger' : 'status-pill'}>{user.disabled ? String(t.disabled) : String(t.enabled)}</span>
      </div>
      <dl className="user-meta">
        <div><dt>{String(t.source)}</dt><dd>{user.source}</dd></div>
        <div><dt>{String(t.roles)}</dt><dd>{user.roles.join(', ') || '-'}</dd></div>
        <div><dt>{String(t.scope)}</dt><dd>{[...user.scope.clusters, ...user.scope.namespaces, ...user.scope.services, ...user.scope.teams].join(' / ') || '-'}</dd></div>
        <div><dt>{String(t.lastLogin)}</dt><dd>{formatTime(user.last_login_at)}</dd></div>
        <div><dt>{String(t.recentAudit)}</dt><dd>{user.recent_permission_audit ? `${user.recent_permission_audit.action} ${user.recent_permission_audit.result || ''}` : '-'}</dd></div>
      </dl>
      {canManage ? (
        <div className="user-actions">
          <label>{String(t.roles)}<input value={roles} onChange={(event) => setRoles(event.target.value)} /></label>
          <label>{String(t.clusters)}<input value={clusters} onChange={(event) => setClusters(event.target.value)} /></label>
          <label>{String(t.services)}<input value={services} onChange={(event) => setServices(event.target.value)} /></label>
          <label>{String(t.teams)}<input value={teams} onChange={(event) => setTeams(event.target.value)} /></label>
          <label>{String(t.namespaces)}<input value={namespaces} onChange={(event) => setNamespaces(event.target.value)} /></label>
          <button
            className="primary-action"
            type="button"
            onClick={() => void run(() => writeJson(`/api/users/${encodeURIComponent(user.username)}`, {
              roles: csvValues(roles),
              scope: {
                clusters: csvValues(clusters),
                services: csvValues(services),
                teams: csvValues(teams),
                namespaces: csvValues(namespaces),
              },
            }, 'PATCH'))}
          >
            {String(t.save)}
          </button>
          <button className="text-action" type="button" disabled={user.disabled} onClick={() => void run(() => writeJson(`/api/users/${encodeURIComponent(user.username)}/disable`, {}))}>
            {String(t.disable)}
          </button>
          {user.source === 'local' ? (
            <>
              <label>{String(t.newPassword)}<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
              <button className="text-action" type="button" onClick={() => void run(() => writeJson(`/api/users/${encodeURIComponent(user.username)}/reset-password`, { password }))}>
                {String(t.resetPassword)}
              </button>
            </>
          ) : null}
        </div>
      ) : null}
      {error ? <p className="form-error">{error}</p> : null}
    </article>
  )
}

function SettingsPage({ actor }: { actor: Actor | null }) {
  const t = useT()
  const canManage = canAccess(actor, 'manage_settings')
  const [version, setVersion] = useState<SettingsVersion | null>(null)
  const [settingsText, setSettingsText] = useState('')
  const [preview, setPreview] = useState<SettingsPreview | null>(null)
  const [confirmation, setConfirmation] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    void loadSettings()
  }, [])

  async function loadSettings() {
    setLoading(true)
    setError('')
    try {
      const data = await readJson<{ settings_version: SettingsVersion }>('/api/settings')
      setVersion(data.settings_version)
      setSettingsText(JSON.stringify(data.settings_version.settings, null, 2))
      setPreview(null)
      setConfirmation('')
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.loadFailed))
    } finally {
      setLoading(false)
    }
  }

  function parseSettings(): Record<string, unknown> {
    const parsed = JSON.parse(settingsText) as unknown
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error('settings must be a JSON object')
    }
    return parsed as Record<string, unknown>
  }

  async function previewSettings() {
    setError('')
    try {
      const data = await writeJson<{ preview: SettingsPreview }>('/api/settings/preview', { settings: parseSettings() })
      setPreview(data.preview)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.actionFailed))
    }
  }

  async function saveSettings() {
    setError('')
    try {
      const data = await writeJson<{ settings_version: SettingsVersion }>('/api/settings', {
        settings: parseSettings(),
        confirmation,
        change_summary: 'console settings save',
      })
      setVersion(data.settings_version)
      setSettingsText(JSON.stringify(data.settings_version.settings, null, 2))
      setPreview(null)
      setConfirmation('')
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.actionFailed))
    }
  }

  async function rollbackSettings() {
    setError('')
    try {
      const data = await writeJson<{ settings_version: SettingsVersion }>('/api/settings/rollback', {})
      setVersion(data.settings_version)
      setSettingsText(JSON.stringify(data.settings_version.settings, null, 2))
      setPreview(null)
      setConfirmation('')
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.actionFailed))
    }
  }

  return (
    <main className="page settings-page">
      <header className="page-header split-header">
        <div>
          <p className="eyebrow">{String(t.gatewayOnly)}</p>
          <h2>{String(t.pages.settings)}</h2>
          {version ? (
            <p>
              {String(t.settingsVersion)}: <code>v{version.version_number}</code> · {version.reload_required ? String(t.reloadRequired) : String(t.noReload)}
            </p>
          ) : null}
        </div>
        <div className="header-actions">
          {!canManage ? <span className="status-pill">{String(t.readOnly)}</span> : null}
          <button className="text-action" type="button" onClick={() => void loadSettings()}>{String(t.refresh)}</button>
        </div>
      </header>
      {error ? <p className="form-error">{error}</p> : null}
      <section className="settings-grid">
        <label className="json-editor">
          {loading ? String(t.loading) : String(t.settingsEditor)}
          <textarea value={settingsText} onChange={(event) => setSettingsText(event.target.value)} readOnly={!canManage} />
        </label>
        <aside className="settings-side">
          {canManage ? (
            <div className="action-stack">
              <button className="primary-action" type="button" onClick={() => void previewSettings()}>{String(t.settingsPreview)}</button>
              <label>
                {String(t.confirmationText)}
                <input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} placeholder={preview?.confirmation_text || ''} />
              </label>
              <button className="primary-action" type="button" onClick={() => void saveSettings()}>{String(t.save)}</button>
              <button className="text-action" type="button" onClick={() => void rollbackSettings()}>{String(t.rollback)}</button>
            </div>
          ) : null}
          <div className="diff-panel">
            <h3>{String(t.settingsPreview)}</h3>
            {preview ? (
              <>
                <p>{preview.critical ? String(t.settingsConfirm) : String(t.noReload)}</p>
                <pre>{JSON.stringify(preview.diff, null, 2)}</pre>
              </>
            ) : (
              <p>{version?.change_summary || '-'}</p>
            )}
          </div>
        </aside>
      </section>
    </main>
  )
}

function PoliciesPage() {
  const t = useT()
  const [policy, setPolicy] = useState<PolicyState | null>(null)
  const [result, setResult] = useState<Record<string, unknown> | null>(null)
  const [form, setForm] = useState({
    action_type: 'restart_deployment',
    cluster: 'prod-a',
    namespace: 'default',
    risk_level: 'low',
  })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    void loadPolicies()
  }, [])

  async function loadPolicies() {
    setLoading(true)
    setError('')
    try {
      const data = await readJson<{ policy_state: PolicyState }>('/api/policies')
      setPolicy(data.policy_state)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.loadFailed))
    } finally {
      setLoading(false)
    }
  }

  async function testPolicy(event: FormEvent) {
    event.preventDefault()
    setError('')
    try {
      const data = await writeJson<{ result: Record<string, unknown> }>('/api/policies/test', form)
      setResult(data.result)
      await loadPolicies()
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.actionFailed))
    }
  }

  return (
    <main className="page policies-page">
      <header className="page-header split-header">
        <div>
          <p className="eyebrow">{String(t.gatewayOnly)}</p>
          <h2>{String(t.pages.policies)}</h2>
          <p>{String(t.defaultEnvironment)}: <code>{policy?.default_environment || 'prod'}</code></p>
        </div>
        <button className="text-action" type="button" onClick={() => void loadPolicies()}>{String(t.refresh)}</button>
      </header>
      {error ? <p className="form-error">{error}</p> : null}
      {loading ? <p>{String(t.loading)}</p> : null}
      <section className="policy-grid">
        <article className="policy-panel">
          <h3>{String(t.policySummary)}</h3>
          <dl className="policy-list">
            <div><dt>{String(t.settingsVersion)}</dt><dd>v{policy?.version || '-'}</dd></div>
            <div><dt>{String(t.actionAllowlist)}</dt><dd>{policy?.action_allowlist.join(', ') || '-'}</dd></div>
            <div><dt>{String(t.clusters)}</dt><dd>{policy?.cluster_environments.map((item) => `${item.cluster}:${item.environment}`).join(', ') || '-'}</dd></div>
          </dl>
          <pre>{JSON.stringify(policy?.policy || {}, null, 2)}</pre>
        </article>
        <form className="policy-panel policy-test-form" onSubmit={testPolicy}>
          <h3>{String(t.policyTest)}</h3>
          <label>{String(t.actionType)}<input value={form.action_type} onChange={(event) => setForm({ ...form, action_type: event.target.value })} /></label>
          <label>{String(t.clusters)}<input value={form.cluster} onChange={(event) => setForm({ ...form, cluster: event.target.value })} /></label>
          <label>{String(t.namespaces)}<input value={form.namespace} onChange={(event) => setForm({ ...form, namespace: event.target.value })} /></label>
          <label>{String(t.riskLevel)}<input value={form.risk_level} onChange={(event) => setForm({ ...form, risk_level: event.target.value })} /></label>
          <button className="primary-action" type="submit">{String(t.policyTest)}</button>
          {result ? (
            <p>
              {String(t.decision)}: <code>{String(result.decision || '-')}</code> · {String(t.reason)}: <code>{String(result.reason || '-')}</code>
            </p>
          ) : null}
        </form>
      </section>
      <section className="policy-panel">
        <h3>{String(t.recentPolicyHits)}</h3>
        <div className="policy-hits">
          {(policy?.recent_policy_hits || []).map((hit) => (
            <article className="policy-hit" key={hit.id}>
              <strong>{hit.action_type}</strong>
              <span>{hit.cluster} / {hit.environment}</span>
              <span>{hit.decision}</span>
              <span>{hit.reason}</span>
            </article>
          ))}
        </div>
      </section>
    </main>
  )
}

function IncidentsPage() {
  const t = useT()
  const [incidents, setIncidents] = useState<IncidentRow[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    void loadIncidents()
  }, [])

  async function loadIncidents() {
    setLoading(true)
    setError('')
    try {
      const data = await readJson<{ incidents: IncidentRow[] }>('/api/incidents/active')
      setIncidents(data.incidents)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.loadFailed))
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="page incidents-page">
      <header className="page-header split-header">
        <div>
          <p className="eyebrow">{String(t.gatewayOnly)}</p>
          <h2>{String(t.pages.incidents)}</h2>
        </div>
        <button className="text-action" type="button" onClick={() => void loadIncidents()}>{String(t.refresh)}</button>
      </header>
      {error ? <p className="form-error">{error}</p> : null}
      {loading ? <p>{String(t.loading)}</p> : null}
      <section className="incident-list" aria-label={String(t.incidentList)}>
        {incidents.map((incident) => (
          <Link className="incident-row" to={`/incidents/${encodeURIComponent(incident.incident_id)}`} key={incident.incident_id}>
            <strong>{incident.title || incident.incident_id}</strong>
            <span>{incident.cluster} / {incident.namespace} / {incident.service}</span>
            <span className="status-pill">{incident.status}</span>
          </Link>
        ))}
      </section>
    </main>
  )
}

function IncidentWorkbenchPage() {
  const t = useT()
  const params = useParams()
  const incidentId = String(params.incidentId || '')
  const [workbench, setWorkbench] = useState<IncidentWorkbench | null>(null)
  const [note, setNote] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (incidentId) {
      void loadWorkbench()
    }
  }, [incidentId])

  async function loadWorkbench() {
    setError('')
    try {
      const data = await readJson<{ workbench: IncidentWorkbench }>(`/api/incidents/${encodeURIComponent(incidentId)}/workbench`)
      setWorkbench(data.workbench)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.loadFailed))
    }
  }

  async function control(action: string, extra: Record<string, unknown> = {}) {
    setError('')
    try {
      await writeJson(`/api/incidents/${encodeURIComponent(incidentId)}/controls`, { action, note, ...extra })
      await loadWorkbench()
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.actionFailed))
    }
  }

  const incident = workbench?.incident
  return (
    <main className="page incidents-page">
      <header className="page-header split-header">
        <div>
          <p className="eyebrow">{String(t.incidentWorkbench)}</p>
          <h2>{incident?.title || String(t.pages.incidentDetail)}</h2>
          {incident ? <p>{incident.cluster} / {incident.namespace} / {incident.service}</p> : null}
        </div>
        <span className="status-pill">{incident?.status || '-'}</span>
      </header>
      {error ? <p className="form-error">{error}</p> : null}
      <section className="incident-controls">
        <label>{String(t.humanNote)}<input value={note} onChange={(event) => setNote(event.target.value)} /></label>
        <button type="button" className="text-action" onClick={() => void control('pause_run')}>{String(t.pauseRun)}</button>
        <button type="button" className="text-action" onClick={() => void control('terminate_run')}>{String(t.terminateRun)}</button>
        <button type="button" className="text-action" onClick={() => void control('manual_takeover')}>{String(t.manualTakeover)}</button>
        <button type="button" className="text-action" onClick={() => void control('human_note')}>{String(t.humanNote)}</button>
        <button type="button" className="text-action" onClick={() => void control('restart_run', { mode: 'continue_current' })}>{String(t.restartRun)}</button>
        <button type="button" className="text-action" onClick={() => void control('block_approvals')}>{String(t.blockApprovals)}</button>
        <button type="button" className="primary-action" onClick={() => void control('resolve')}>{String(t.resolveIncident)}</button>
        <button type="button" className="text-action" onClick={() => void control('reopen')}>{String(t.reopenIncident)}</button>
      </section>
      {workbench ? (
        <section className="workbench-grid" aria-label={String(t.incidentWorkbench)}>
          <WorkbenchPanel title={String(t.evidencePanels)} panel={workbench.panels.evidence} />
          <WorkbenchPanel title={String(t.timeline)} panel={workbench.panels.timeline} />
          <WorkbenchPanel title={String(t.diagnosis)} panel={workbench.panels.diagnosis} />
          <WorkbenchPanel title={String(t.runList)} panel={workbench.panels.runs} />
          <WorkbenchPanel title={String(t.approvalList)} panel={workbench.panels.approvals} />
          <article className="workbench-panel">
            <h3>{String(t.responsibility)}</h3>
            <pre>{JSON.stringify(workbench.responsibility, null, 2)}</pre>
          </article>
        </section>
      ) : <p>{String(t.loading)}</p>}
    </main>
  )
}

function WorkbenchPanel({ title, panel }: { title: string; panel?: WorkbenchPanelData }) {
  return (
    <article className="workbench-panel">
      <header>
        <h3>{title}</h3>
        <span className="status-pill">{panel?.status || '-'}</span>
      </header>
      {panel?.error ? <p className="form-error">{panel.error}</p> : null}
      <pre>{JSON.stringify(panel?.data ?? [], null, 2)}</pre>
    </article>
  )
}

function IncidentReportPage() {
  const t = useT()
  const params = useParams()
  const incidentId = String(params.incidentId || '')
  const [snapshot, setSnapshot] = useState<ReportSnapshot | null>(null)
  const [htmlText, setHtmlText] = useState('')
  const [feedbackTarget, setFeedbackTarget] = useState('report')
  const [feedbackRating, setFeedbackRating] = useState('neutral')
  const [feedbackComment, setFeedbackComment] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (incidentId) {
      void loadReport()
    }
  }, [incidentId])

  async function loadReport() {
    setError('')
    try {
      const data = await readJson<{ report: ReportSnapshot }>(`/api/incidents/${encodeURIComponent(incidentId)}/report`)
      setSnapshot(data.report)
      setHtmlText(data.report.latest_report?.html || '')
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.loadFailed))
    }
  }

  async function createDraft() {
    setError('')
    try {
      await writeJson(`/api/incidents/${encodeURIComponent(incidentId)}/report/draft`, htmlText ? { html: htmlText } : {})
      await loadReport()
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.actionFailed))
    }
  }

  async function publishReport() {
    setError('')
    try {
      await writeJson(`/api/incidents/${encodeURIComponent(incidentId)}/report/publish`, {
        version_id: snapshot?.latest_report?.version_id || '',
      })
      await loadReport()
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.actionFailed))
    }
  }

  async function submitFeedback(event: FormEvent) {
    event.preventDefault()
    setError('')
    try {
      await writeJson('/api/feedback', {
        incident_id: incidentId,
        target_type: feedbackTarget,
        target_id: feedbackTarget === 'report' ? snapshot?.latest_report?.version_id || incidentId : incidentId,
        rating: feedbackRating,
        comment: feedbackComment,
      })
      setFeedbackComment('')
      await loadReport()
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.actionFailed))
    }
  }

  const latest = snapshot?.latest_report || null
  return (
    <main className="page report-page">
      <header className="page-header split-header">
        <div>
          <p className="eyebrow">{String(t.gatewayOnly)}</p>
          <h2>{String(t.pages.incidentReport)}</h2>
          <p>{String(t.resource)}: <code>{incidentId}</code></p>
        </div>
        <div className="header-actions">
          {latest ? <a className="primary-link" href={`/api/incidents/${encodeURIComponent(incidentId)}/report?format=html`}>{String(t.reportExportHtml)}</a> : null}
          <button className="text-action" type="button" onClick={() => void loadReport()}>{String(t.refresh)}</button>
        </div>
      </header>
      {error ? <p className="form-error">{error}</p> : null}
      <section className="settings-grid">
        <article className="workbench-panel">
          <h3>{String(t.reportPreview)}</h3>
          {latest ? <span className="status-pill">v{latest.version_number} {latest.status}</span> : null}
          <iframe className="report-frame" title={String(t.reportPreview)} sandbox="" srcDoc={htmlText || latest?.html || '<article><h1>unknown</h1></article>'} />
          <label className="json-editor">
            HTML
            <textarea value={htmlText} onChange={(event) => setHtmlText(event.target.value)} />
          </label>
          <div className="header-actions">
            <button className="primary-action" type="button" onClick={() => void createDraft()}>{String(t.reportDraft)}</button>
            <button className="text-action" type="button" disabled={!latest || latest.status !== 'draft'} onClick={() => void publishReport()}>{String(t.reportPublish)}</button>
          </div>
        </article>
        <aside className="settings-side">
          <article className="workbench-panel">
            <h3>{String(t.reportVersions)}</h3>
            <div className="policy-hits">
              {(snapshot?.versions || []).map((version) => (
                <article className="policy-hit" key={version.version_id}>
                  <strong>v{version.version_number}</strong>
                  <span>{version.status}</span>
                  <span>{formatTime(version.published_at || version.created_at)}</span>
                  <span>{version.created_by}</span>
                </article>
              ))}
            </div>
          </article>
          <article className="workbench-panel">
            <h3>{String(t.reportUnknowns)}</h3>
            <p>{latest?.unknowns?.join(', ') || '-'}</p>
          </article>
          <form className="workbench-panel" onSubmit={submitFeedback}>
            <h3>{String(t.feedback)}</h3>
            <label>{String(t.feedbackTarget)}<select value={feedbackTarget} onChange={(event) => setFeedbackTarget(event.target.value)}>
              <option value="diagnosis">diagnosis</option>
              <option value="evidence">evidence</option>
              <option value="action_proposal">action_proposal</option>
              <option value="report">report</option>
            </select></label>
            <label>{String(t.feedbackRating)}<select value={feedbackRating} onChange={(event) => setFeedbackRating(event.target.value)}>
              <option value="positive">positive</option>
              <option value="neutral">neutral</option>
              <option value="negative">negative</option>
              <option value="unknown">unknown</option>
            </select></label>
            <label>{String(t.feedbackComment)}<input value={feedbackComment} onChange={(event) => setFeedbackComment(event.target.value)} /></label>
            <button className="primary-action" type="submit">{String(t.save)}</button>
          </form>
          <article className="workbench-panel">
            <h3>{String(t.feedback)}</h3>
            <div className="policy-hits">
              {(snapshot?.feedback || []).map((item) => (
                <article className="policy-hit" key={item.feedback_id}>
                  <strong>{item.target_type}</strong>
                  <span>{item.rating}</span>
                  <span>{item.actor_id}</span>
                  <span>{item.comment || '-'}</span>
                </article>
              ))}
            </div>
          </article>
        </aside>
      </section>
    </main>
  )
}

function ApprovalsPage() {
  const t = useT()
  const [approvals, setApprovals] = useState<ApprovalRequest[]>([])
  const [action, setAction] = useState<ActionProposal | null>(null)
  const [form, setForm] = useState({
    action_type: 'restart_deployment',
    cluster: 'prod-a',
    namespace: 'default',
    service: 'checkout',
    team: 'payments',
    deployment: 'checkout',
    replicas: '2',
    assigned_approvers: 'approver',
  })
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    void loadApprovals()
  }, [])

  async function loadApprovals() {
    setError('')
    try {
      const data = await readJson<{ approval_requests: ApprovalRequest[] }>('/api/approval-requests')
      setApprovals(data.approval_requests)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.loadFailed))
    }
  }

  async function propose(event: FormEvent) {
    event.preventDefault()
    setLoading(true)
    setError('')
    try {
      const data = await writeJson<{ action: ActionProposal; approval_request?: ApprovalRequest | null }>('/api/actions/propose', {
        ...form,
        replicas: Number(form.replicas),
        assigned_approvers: csvValues(form.assigned_approvers),
        idempotency_key: `console-${Date.now()}`,
      })
      setAction(data.action)
      await loadApprovals()
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.actionFailed))
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="page approvals-page">
      <header className="page-header split-header">
        <div>
          <p className="eyebrow">{String(t.gatewayOnly)}</p>
          <h2>{String(t.pages.approvals)}</h2>
        </div>
        <button className="text-action" type="button" onClick={() => void loadApprovals()}>{String(t.refresh)}</button>
      </header>
      {error ? <p className="form-error">{error}</p> : null}
      <form className="action-form" onSubmit={propose}>
        <label>{String(t.actionType)}<select value={form.action_type} onChange={(event) => setForm({ ...form, action_type: event.target.value })}>
          <option value="restart_deployment">restart_deployment</option>
          <option value="scale_deployment">scale_deployment</option>
          <option value="rollback_deployment">rollback_deployment</option>
        </select></label>
        <label>{String(t.clusters)}<input value={form.cluster} onChange={(event) => setForm({ ...form, cluster: event.target.value })} /></label>
        <label>{String(t.namespaces)}<input value={form.namespace} onChange={(event) => setForm({ ...form, namespace: event.target.value })} /></label>
        <label>{String(t.services)}<input value={form.service} onChange={(event) => setForm({ ...form, service: event.target.value })} /></label>
        <label>{String(t.teams)}<input value={form.team} onChange={(event) => setForm({ ...form, team: event.target.value })} /></label>
        <label>{String(t.deployment)}<input value={form.deployment} onChange={(event) => setForm({ ...form, deployment: event.target.value })} /></label>
        <label>{String(t.replicas)}<input value={form.replicas} onChange={(event) => setForm({ ...form, replicas: event.target.value })} /></label>
        <label>{String(t.approvalRequired)}<input value={form.assigned_approvers} onChange={(event) => setForm({ ...form, assigned_approvers: event.target.value })} /></label>
        <button className="primary-action" type="submit" disabled={loading}>{loading ? String(t.loading) : String(t.actionRequest)}</button>
      </form>
      {action ? <p className="status-line">{String(t.actionHash)}: {action.action_hash}</p> : null}
      <section className="approval-list" aria-label={String(t.approvalList)}>
        {approvals.map((approval) => (
          <Link className="approval-row" to={`/approvals/${encodeURIComponent(approval.approval_id)}`} key={approval.approval_id}>
            <strong>{approval.action_summary}</strong>
            <span>{approval.resource_scope.cluster || approval.resource_scope.cluster_id} / {approval.resource_scope.namespace}</span>
            <span className="status-pill">{approval.status}</span>
          </Link>
        ))}
      </section>
    </main>
  )
}

function ApprovalDetailPage() {
  const t = useT()
  const params = useParams()
  const approvalId = String(params.approvalId || '')
  const [approval, setApproval] = useState<ApprovalRequest | null>(null)
  const [execution, setExecution] = useState<ApprovalExecution | null>(null)
  const [reason, setReason] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (approvalId) {
      void loadApproval()
    }
  }, [approvalId])

  async function loadApproval() {
    setError('')
    try {
      const detail = await readJson<{ approval_request: ApprovalRequest }>(`/api/approval-requests/${encodeURIComponent(approvalId)}`)
      const executionDetail = await readJson<{ execution: ApprovalExecution | null }>(`/api/approval-requests/${encodeURIComponent(approvalId)}/execution`)
      setApproval(detail.approval_request)
      setExecution(executionDetail.execution)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.loadFailed))
    }
  }

  async function decide(action: 'approve' | 'reject') {
    setError('')
    try {
      const data = await writeJson<{ approval_request: ApprovalRequest; execution?: ApprovalExecution | null }>(
        `/api/approval-requests/${encodeURIComponent(approvalId)}/${action}`,
        { reason },
      )
      setApproval(data.approval_request)
      setExecution(data.execution || execution)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.actionFailed))
    }
  }

  return (
    <main className="page approvals-page">
      <header className="page-header split-header">
        <div>
          <p className="eyebrow">{String(t.gatewayOnly)}</p>
          <h2>{approval?.action_summary || String(t.pages.approvalDetail)}</h2>
        </div>
        <span className="status-pill">{approval?.status || '-'}</span>
      </header>
      {error ? <p className="form-error">{error}</p> : null}
      {approval ? (
        <section className="approval-detail">
          <p>{approval.resource_scope.cluster || approval.resource_scope.cluster_id} / {approval.resource_scope.namespace}</p>
          <p>{String(t.riskLevel)}: {approval.risk_level}</p>
          <p>{String(t.executionStatus)}: {execution?.status || '-'}</p>
          {execution?.error_message ? <p className="form-error">{execution.error_message}</p> : null}
          <label>{String(t.reason)}<input value={reason} onChange={(event) => setReason(event.target.value)} /></label>
          <div className="header-actions">
            <button className="primary-action" type="button" onClick={() => void decide('approve')}>{String(t.approve)}</button>
            <button className="text-action" type="button" onClick={() => void decide('reject')}>{String(t.reject)}</button>
          </div>
        </section>
      ) : <p>{String(t.loading)}</p>}
    </main>
  )
}

function AgentRunsPage() {
  const t = useT()
  const [runs, setRuns] = useState<AgentRun[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    void loadRuns()
  }, [])

  async function loadRuns() {
    setLoading(true)
    setError('')
    try {
      const data = await readJson<{ agent_runs: AgentRun[] }>('/api/agent-runs')
      setRuns(data.agent_runs)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.loadFailed))
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="page runs-page">
      <header className="page-header split-header">
        <div>
          <p className="eyebrow">{String(t.gatewayOnly)}</p>
          <h2>{String(t.pages.agentRuns)}</h2>
        </div>
        <div className="header-actions">
          <Link className="primary-link" to="/agent-runs/new">{String(t.runCreate)}</Link>
          <button className="text-action" type="button" onClick={() => void loadRuns()}>{String(t.refresh)}</button>
        </div>
      </header>
      {error ? <p className="form-error">{error}</p> : null}
      {loading ? <p>{String(t.loading)}</p> : null}
      {!loading && !runs.length ? <p>{String(t.noRuns)}</p> : null}
      <section className="run-list" aria-label={String(t.runList)}>
        {runs.map((run) => (
          <Link className="run-row" to={`/agent-runs/${encodeURIComponent(run.run_id)}`} key={run.run_id}>
            <strong>{run.title}</strong>
            <span>{run.scope.cluster} / {run.scope.namespace} / {run.scope.service}</span>
            <span className="status-pill">{run.status}</span>
          </Link>
        ))}
      </section>
    </main>
  )
}

function NewAgentRunPage() {
  const t = useT()
  const navigate = useNavigate()
  const [form, setForm] = useState({
    title: 'Checkout investigation',
    message: 'Investigate checkout health',
    cluster: 'prod-a',
    namespace: 'default',
    service: 'checkout',
    team: 'payments',
    tags: 'checkout, prod',
  })
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function createRun(event: FormEvent) {
    event.preventDefault()
    setLoading(true)
    setError('')
    try {
      const data = await writeJson<{ snapshot: AgentRunSnapshot }>('/api/agent-runs', {
        title: form.title,
        message: form.message,
        tags: csvValues(form.tags),
        scope: {
          cluster: form.cluster,
          namespace: form.namespace,
          service: form.service,
          team: form.team,
          environment: 'prod',
        },
      })
      navigate(`/agent-runs/${encodeURIComponent(data.snapshot.run.run_id)}`)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.actionFailed))
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="page runs-page">
      <header className="page-header">
        <p className="eyebrow">{String(t.gatewayOnly)}</p>
        <h2>{String(t.runCreate)}</h2>
      </header>
      {error ? <p className="form-error">{error}</p> : null}
      <form className="run-form" onSubmit={createRun}>
        <label>{String(t.runTitle)}<input value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} /></label>
        <label>{String(t.runMessage)}<input value={form.message} onChange={(event) => setForm({ ...form, message: event.target.value })} /></label>
        <label>{String(t.clusters)}<input value={form.cluster} onChange={(event) => setForm({ ...form, cluster: event.target.value })} /></label>
        <label>{String(t.namespaces)}<input value={form.namespace} onChange={(event) => setForm({ ...form, namespace: event.target.value })} /></label>
        <label>{String(t.services)}<input value={form.service} onChange={(event) => setForm({ ...form, service: event.target.value })} /></label>
        <label>{String(t.teams)}<input value={form.team} onChange={(event) => setForm({ ...form, team: event.target.value })} /></label>
        <label>{String(t.scope)}<input value={form.tags} onChange={(event) => setForm({ ...form, tags: event.target.value })} /></label>
        <button className="primary-action" type="submit" disabled={loading}>{loading ? String(t.loading) : String(t.runCreate)}</button>
      </form>
    </main>
  )
}

function AgentRunDetailPage() {
  const t = useT()
  const params = useParams()
  const runId = String(params.runId || '')
  const [snapshot, setSnapshot] = useState<AgentRunSnapshot | null>(null)
  const [events, setEvents] = useState<AgentRunEvent[]>([])
  const [feedback, setFeedback] = useState<Feedback[]>([])
  const [message, setMessage] = useState('')
  const [streamState, setStreamState] = useState('')
  const [error, setError] = useState('')
  const reconnectingText = String(t.reconnecting)
  const staleRunText = String(t.staleRun)
  const loadFailedText = String(t.loadFailed)

  useEffect(() => {
    if (!runId) {
      return
    }
    let stream: EventSource | null = null
    let closed = false
    async function load() {
      setError('')
      setStreamState(reconnectingText)
      try {
        const data = await readJson<{ snapshot: AgentRunSnapshot }>(`/api/agent-runs/${encodeURIComponent(runId)}`)
        if (closed) {
          return
        }
        setSnapshot(data.snapshot)
        setEvents(data.snapshot.timeline)
        const feedbackData = await readJson<{ feedback: Feedback[] }>(`/api/agent-runs/${encodeURIComponent(runId)}/feedback`)
        setFeedback(feedbackData.feedback)
        const lastId = data.snapshot.timeline.at(-1)?.id || 0
        stream = new EventSource(`/api/agent-runs/${encodeURIComponent(runId)}/stream`, { withCredentials: true })
        stream.addEventListener('message', (event) => {
          const item = JSON.parse(event.data) as AgentRunEvent
          setEvents((current) => current.some((existing) => existing.id === item.id) ? current : [...current, item])
        })
        stream.addEventListener('open', () => setStreamState(''))
        stream.addEventListener('error', () => setStreamState(lastId ? staleRunText : reconnectingText))
      } catch (exc) {
        setError(exc instanceof Error ? exc.message : loadFailedText)
      }
    }
    void load()
    return () => {
      closed = true
      stream?.close()
    }
  }, [runId, reconnectingText, staleRunText, loadFailedText])

  async function sendMessage(event: FormEvent) {
    event.preventDefault()
    setError('')
    try {
      const data = await writeJson<{ result: AgentRunEvent }>(`/api/agent-runs/${encodeURIComponent(runId)}/messages`, { message })
      setEvents((current) => current.some((item) => item.id === data.result.id) ? current : [...current, data.result])
      setMessage('')
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.actionFailed))
    }
  }

  async function promote(eventId: number) {
    setError('')
    try {
      const data = await writeJson<{ result: AgentRunEvent }>(`/api/agent-runs/${encodeURIComponent(runId)}/promote`, { event_id: eventId })
      setEvents((current) => [...current, data.result])
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.actionFailed))
    }
  }

  return (
    <main className="page runs-page">
      <header className="page-header split-header">
        <div>
          <p className="eyebrow">{String(t.gatewayOnly)}</p>
          <h2>{snapshot?.conversation.title || String(t.pages.agentRunDetail)}</h2>
          {snapshot ? <p>{snapshot.run.scope.cluster} / {snapshot.run.scope.namespace} / {snapshot.run.scope.service}</p> : null}
        </div>
        <span className="status-pill">{snapshot?.run.status || '-'}</span>
      </header>
      {streamState ? <p role="status">{streamState}</p> : null}
      {error ? <p className="form-error">{error}</p> : null}
      <form className="run-message-form" onSubmit={sendMessage}>
        <label>{String(t.runMessage)}<input value={message} onChange={(event) => setMessage(event.target.value)} /></label>
        <button className="primary-action" type="submit" disabled={!snapshot?.permissions.can_message}>{String(t.save)}</button>
      </form>
      <section className="run-timeline" aria-label={String(t.runTimeline)}>
        {events.map((item) => (
          <article className="run-event" key={item.id}>
            <header>
              <strong>{item.event_type}</strong>
              <span className="status-pill">{item.thread_type === 'side' ? String(t.sideThread) : String(t.mainline)}</span>
            </header>
            <p>{item.message}</p>
            {item.thread_type === 'side' && snapshot?.permissions.can_promote ? (
              <button className="text-action" type="button" onClick={() => void promote(item.id)}>{String(t.promote)}</button>
            ) : null}
          </article>
        ))}
      </section>
      <section className="workbench-panel">
        <h3>{String(t.feedback)}</h3>
        <div className="policy-hits">
          {feedback.map((item) => (
            <article className="policy-hit" key={item.feedback_id}>
              <strong>{item.target_type}</strong>
              <span>{item.rating}</span>
              <span>{item.actor_id}</span>
              <span>{item.comment || '-'}</span>
            </article>
          ))}
        </div>
      </section>
    </main>
  )
}

function EvidencePage() {
  const t = useT()
  const [form, setForm] = useState({
    cluster: 'prod-a',
    namespace: 'default',
    service: 'checkout',
    team: 'payments',
    template: 'service_overview',
    limit: '20',
  })
  const [evidence, setEvidence] = useState<EvidenceResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const sourceLabels: Record<string, string> = {
    metrics: String(t.metricsEvidence),
    logs: String(t.logsEvidence),
    traces: String(t.tracesEvidence),
    kubernetes: String(t.kubernetesEvidence),
    topology: String(t.topologyEvidence),
    changes: String(t.changesEvidence),
    tool_output: String(t.toolOutputEvidence),
  }

  async function queryEvidence(event: FormEvent) {
    event.preventDefault()
    setLoading(true)
    setError('')
    const now = Math.floor(Date.now() / 1000)
    try {
      const data = await writeJson<{ evidence: EvidenceResponse }>('/api/evidence/query', {
        template: form.template,
        limit: Number(form.limit) || 20,
        time_range: { start_ts: now - 3600, end_ts: now },
        scope: {
          cluster: form.cluster,
          namespace: form.namespace,
          service: form.service,
          team: form.team,
          environment: 'prod',
        },
      })
      setEvidence(data.evidence)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.actionFailed))
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="page evidence-page">
      <header className="page-header split-header">
        <div>
          <p className="eyebrow">{String(t.gatewayOnly)}</p>
          <h2>{String(t.evidenceQuery)}</h2>
          {evidence ? (
            <p>
              {String(t.timeRange)}: <code>{evidence.limits.time_range_seconds}s</code> · {String(t.limit)}: <code>{evidence.limits.limit}</code>
            </p>
          ) : null}
        </div>
        {evidence?.status === 'partial' ? <span className="status-pill">{String(t.partialEvidence)}</span> : null}
      </header>
      {error ? <p className="form-error">{error}</p> : null}
      <form className="evidence-form" onSubmit={queryEvidence}>
        <label>{String(t.clusters)}<input value={form.cluster} onChange={(event) => setForm({ ...form, cluster: event.target.value })} /></label>
        <label>{String(t.namespaces)}<input value={form.namespace} onChange={(event) => setForm({ ...form, namespace: event.target.value })} /></label>
        <label>{String(t.services)}<input value={form.service} onChange={(event) => setForm({ ...form, service: event.target.value })} /></label>
        <label>{String(t.teams)}<input value={form.team} onChange={(event) => setForm({ ...form, team: event.target.value })} /></label>
        <label>
          {String(t.evidenceTemplate)}
          <select value={form.template} onChange={(event) => setForm({ ...form, template: event.target.value })}>
            <option value="service_overview">service_overview</option>
            <option value="error_logs">error_logs</option>
            <option value="trace_latency">trace_latency</option>
            <option value="k8s_state">k8s_state</option>
            <option value="topology_dependencies">topology_dependencies</option>
          </select>
        </label>
        <label>{String(t.limit)}<input value={form.limit} onChange={(event) => setForm({ ...form, limit: event.target.value })} /></label>
        <button className="primary-action" type="submit" disabled={loading}>{loading ? String(t.loading) : String(t.queryEvidence)}</button>
      </form>
      <section className="evidence-panels" aria-label={String(t.evidencePanels)}>
        {(evidence?.sources || []).map((source) => (
          <article className="evidence-panel" key={source.kind}>
            <header>
              <h3>{sourceLabels[source.kind] || source.kind}</h3>
              <span className={source.status === 'failed' ? 'status-pill danger' : 'status-pill'}>{source.status}</span>
            </header>
            <p>{source.status === 'stale' ? String(t.staleEvidence) : source.summary}</p>
            {source.refs?.length ? <p>{source.refs.map((ref) => ref.ref_id).join(', ')}</p> : null}
            {source.samples?.length ? <pre>{JSON.stringify(source.samples, null, 2)}</pre> : <p>{String(t.emptyEvidence)}</p>}
          </article>
        ))}
      </section>
    </main>
  )
}

function AuditPage() {
  const t = useT()
  const [chains, setChains] = useState<AuditChain[]>([])
  const [rows, setRows] = useState<AuditRawRow[]>([])
  const [tombstones, setTombstones] = useState<AuditTombstone[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    void loadAudit()
  }, [])

  async function loadAudit() {
    setLoading(true)
    setError('')
    try {
      const [chainData, rawData, tombstoneData] = await Promise.all([
        readJson<{ chains: AuditChain[] }>('/api/audit/chains'),
        readJson<{ rows: AuditRawRow[] }>('/api/audit/raw?limit=20'),
        readJson<{ tombstones: AuditTombstone[] }>('/api/audit/tombstones'),
      ])
      setChains(chainData.chains)
      setRows(rawData.rows)
      setTombstones(tombstoneData.tombstones)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.loadFailed))
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="page audit-page">
      <header className="page-header split-header">
        <div>
          <p className="eyebrow">{String(t.gatewayOnly)}</p>
          <h2>{String(t.pages.audit)}</h2>
        </div>
        <button className="text-action" type="button" onClick={() => void loadAudit()}>{String(t.refresh)}</button>
      </header>
      {error ? <p className="form-error">{error}</p> : null}
      {loading ? <p>{String(t.loading)}</p> : null}
      <section className="audit-grid">
        <article className="workbench-panel">
          <h3>{String(t.auditChains)}</h3>
          <div className="run-list">
            {chains.map((chain) => (
              <Link className="run-row" to={`/audit/${encodeURIComponent(chain.chain_id)}`} key={chain.chain_id}>
                <strong>{chain.requested_action || chain.chain_id}</strong>
                <span>{chain.incident_id || '-'} · {chain.risk || '-'}</span>
                <span className="status-pill">{chain.responsibility_status}</span>
              </Link>
            ))}
          </div>
        </article>
        <article className="workbench-panel">
          <h3>{String(t.rawLogs)}</h3>
          <div className="policy-hits">
            {rows.map((row) => (
              <article className="policy-hit" key={row.id}>
                <strong>{row.what}</strong>
                <span>{row.actor || '-'}</span>
                <span>{row.result || '-'}</span>
                <span>{formatTime(row.when_ts)}</span>
              </article>
            ))}
          </div>
        </article>
        <article className="workbench-panel">
          <h3>{String(t.deletedConversations)}</h3>
          <div className="policy-hits">
            {tombstones.map((item) => (
              <article className="policy-hit" key={`${item.run_id}-${item.deleted_at}`}>
                <strong>{item.conversation_id}</strong>
                <span>{item.deleted_by || '-'}</span>
                <span>{item.reason || '-'}</span>
                <span>{formatTime(item.deleted_at)}</span>
              </article>
            ))}
          </div>
        </article>
      </section>
    </main>
  )
}

function AuditDetailPage() {
  const t = useT()
  const params = useParams()
  const chainId = String(params.chainId || '')
  const [chain, setChain] = useState<AuditChain | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    if (chainId) {
      void loadChain()
    }
  }, [chainId])

  async function loadChain() {
    setError('')
    try {
      const data = await readJson<{ chain: AuditChain }>(`/api/audit/chains/${encodeURIComponent(chainId)}`)
      setChain(data.chain)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.loadFailed))
    }
  }

  return (
    <main className="page audit-page">
      <header className="page-header split-header">
        <div>
          <p className="eyebrow">{String(t.pages.auditDetail)}</p>
          <h2>{chain?.requested_action || chainId}</h2>
          <p>{chain?.incident_id || '-'} · {formatTime(chain?.time)}</p>
        </div>
        <span className="status-pill">{chain?.responsibility_status || '-'}</span>
      </header>
      {error ? <p className="form-error">{error}</p> : null}
      {chain ? (
        <section className="workbench-grid">
          <AuditJsonPanel title={String(t.requestedAction)} value={chain.agent_request} />
          <AuditJsonPanel title={String(t.riskLevel)} value={chain.risk_classification} />
          <AuditJsonPanel title={String(t.actionHash)} value={chain.frozen_action} />
          <AuditJsonPanel title={String(t.approver)} value={chain.approver_snapshot} />
          <AuditJsonPanel title={String(t.gatewayExecution)} value={chain.execution} />
          <AuditJsonPanel title={String(t.notificationsRef)} value={chain.notifications} />
          <AuditJsonPanel title={String(t.tombstone)} value={chain.delete_tombstones} />
          <AuditJsonPanel title={String(t.rawAuditRefs)} value={chain.raw_audit_refs} />
        </section>
      ) : <p>{String(t.loading)}</p>}
    </main>
  )
}

function AuditJsonPanel({ title, value }: { title: string; value: unknown }) {
  return (
    <article className="workbench-panel">
      <h3>{title}</h3>
      <pre>{JSON.stringify(value ?? {}, null, 2)}</pre>
    </article>
  )
}

function NotificationsPage() {
  const t = useT()
  const [notifications, setNotifications] = useState<NotificationRecord[]>([])
  const [streamState, setStreamState] = useState('')
  const [error, setError] = useState('')
  const liveText = String(t.liveNotifications)
  const loadFailedText = String(t.loadFailed)

  useEffect(() => {
    let stream: EventSource | null = null
    let closed = false
    async function load() {
      setError('')
      try {
        const data = await readJson<{ notifications: NotificationRecord[] }>('/api/notifications')
        if (closed) {
          return
        }
        setNotifications(data.notifications)
        stream = new EventSource('/api/notifications/stream', { withCredentials: true })
        stream.addEventListener('message', (event) => {
          const item = JSON.parse(event.data) as NotificationRecord
          setNotifications((current) => current.some((existing) => existing.id === item.id) ? current : [item, ...current])
        })
        stream.addEventListener('open', () => setStreamState(liveText))
        stream.addEventListener('error', () => setStreamState(''))
      } catch (exc) {
        setError(exc instanceof Error ? exc.message : loadFailedText)
      }
    }
    void load()
    return () => {
      closed = true
      stream?.close()
    }
  }, [liveText, loadFailedText])

  async function retry(deliveryId: string) {
    setError('')
    try {
      const data = await writeJson<{ result: { delivery: NotificationRecord } }>('/api/notifications/retry', { delivery_id: deliveryId })
      setNotifications((current) => current.map((item) => item.id === deliveryId ? data.result.delivery : item))
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.actionFailed))
    }
  }

  return (
    <main className="page notifications-page">
      <header className="page-header split-header">
        <div>
          <p className="eyebrow">{String(t.gatewayOnly)}</p>
          <h2>{String(t.pages.notifications)}</h2>
          {streamState ? <p role="status">{streamState}</p> : null}
        </div>
        <button className="text-action" type="button" onClick={() => window.location.reload()}>{String(t.refresh)}</button>
      </header>
      {error ? <p className="form-error">{error}</p> : null}
      {!notifications.length ? <p>{String(t.noNotifications)}</p> : null}
      <section className="run-list" aria-label={String(t.pages.notifications)}>
        {notifications.map((item) => (
          <article className="run-row" key={item.id}>
            <strong>{item.notification_type}</strong>
            <span>
              {item.incident_id || '-'} · {item.service_id || '-'} · {String(t.deliveryAttempts)}: {item.delivery_attempts}/{item.max_attempts}
              {item.last_delivery_error ? ` · ${item.last_delivery_error}` : ''}
            </span>
            <span className={item.delivery_status === 'dead_letter' || item.delivery_status === 'failed' ? 'status-pill danger' : 'status-pill'}>{item.delivery_status}</span>
            {item.delivery_status === 'failed' || item.delivery_status === 'dead_letter' ? (
              <button className="text-action" type="button" onClick={() => void retry(item.id)}>{String(t.retryDelivery)}</button>
            ) : null}
          </article>
        ))}
      </section>
    </main>
  )
}

function Page({ title, paramName }: { title: string; paramName?: string }) {
  const params = useParams()
  const [note, setNote] = useState('')
  const t = useT()
  const resource = paramName ? params[paramName] : ''

  return (
    <main className="page">
      <header className="page-header">
        <p className="eyebrow">{String(t.routeState)}</p>
        <h2>{title}</h2>
        {resource ? (
          <p>
            {String(t.resource)}: <code>{resource}</code>
          </p>
        ) : null}
      </header>
      <section className="placeholder-panel">
        <p>{String(t.comingSoon)}</p>
        <label>
          {String(t.unsaved)}
          <textarea value={note} onChange={(event) => setNote(event.target.value)} />
        </label>
      </section>
    </main>
  )
}

function Forbidden() {
  const t = useT()
  return (
    <main className="center-state">
      <h2>{String(t.forbiddenTitle)}</h2>
      <p>{String(t.forbiddenText)}</p>
    </main>
  )
}

function NotFound() {
  const t = useT()
  return (
    <main className="center-state">
      <h2>{String(t.notFoundTitle)}</h2>
      <p>{String(t.notFoundText)}</p>
    </main>
  )
}

function LocaleSwitch({ locale, setLocale }: { locale: Locale; setLocale: (locale: Locale) => void }) {
  return (
    <div className="locale-switch" aria-label="language">
      <button className={locale === 'zh-CN' ? 'active' : ''} type="button" onClick={() => setLocale('zh-CN')}>
        中文
      </button>
      <span>|</span>
      <button className={locale === 'en-US' ? 'active' : ''} type="button" onClick={() => setLocale('en-US')}>
        EN
      </button>
    </div>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <AppShell />
    </BrowserRouter>
  )
}
