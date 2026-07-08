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
  settings: ConsoleSettings
  diff?: unknown[]
  created_by?: string
  created_at?: number
  change_summary?: string
  reload_required?: boolean
}

type PolicyRule = {
  environment: string
  cluster?: string | null
  namespace?: string | null
  action_type: string
  risk_level: string
  approval_required: boolean
  auto_execution: boolean
  self_approval: boolean
  eligible_approver_roles: string[]
}

type ActionAllowlistEntry = {
  action_type: string
  backend: string
  template: string
  allowed_scopes: string[]
  default_risk: string
  preflight: boolean
  post_check: boolean
  rollback_required: boolean
  enabled: boolean
}

type ConsoleSettings = Record<string, unknown> & {
  clusters?: Array<{ cluster: string; environment: string }>
  approval_policy?: {
    rules?: PolicyRule[]
    allow_self_approval_low_risk?: boolean
    dev_low_risk_auto_execute?: boolean
    test_low_risk_auto_execute?: boolean
  }
  action_allowlist?: ActionAllowlistEntry[]
}

type SettingsPreview = {
  settings: ConsoleSettings
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
  action_allowlist: ActionAllowlistEntry[]
  policy: Record<string, unknown> & { rules?: PolicyRule[] }
  recent_policy_hits: PolicyHit[]
}

type ClusterRecord = {
  cluster_id: string
  display_name: string
  environment: string
  effective_environment: string
  default_namespace_scope: string
  owner_team: string
  automatic_actions_enabled: boolean
  openobserve_config_ref: string
  configuration_status: string
  mutation_enabled: boolean
  mutation_disabled_reason?: string | null
  runtime_state: {
    connector_id: string
    connector_status: string
    openobserve_status: string
    scope_field_mapping_health: string
    recent_query_health: string
    failure_summary: string
    last_heartbeat?: number | null
    updated_at?: number | null
  }
}

type EvidenceSource = {
  kind: string
  status: string
  summary: string
  refs?: Array<{ ref_id: string; source: string }>
  samples?: Record<string, unknown>[]
}

type EvidenceNode = {
  node_id: string
  kind: string
  title: string
  status: string
  summary: string
  detail?: {
    backend?: string
    status?: string
    row_count?: number
    chart?: Array<{ x: number; y: number }>
  }
  snippet?: string
  refs?: Array<{ ref_id: string; source?: string }>
  scope?: Record<string, string>
  time_range?: { start_ts?: number; end_ts?: number }
  why_it_mattered?: string
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
  nodes?: EvidenceNode[]
}

type AgentRun = {
  run_id: string
  title: string
  status: string
  runbook_skeleton: string
  metadata?: Record<string, unknown>
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
  payload?: Record<string, unknown>
  created_at: number
  promoted_from_event_id?: number | null
}

type AgentRunStep = {
  step_id: string
  name: string
  status: string
  metadata: Record<string, unknown>
  tool_calls: Record<string, unknown>[]
  evidence_refs: Record<string, unknown>[]
  stuck_reason?: string | null
}

type AgentRunSnapshot = {
  conversation: {
    title: string
    tags: string[]
    status: string
  }
  run: AgentRun
  steps: AgentRunStep[]
  timeline: AgentRunEvent[]
  evidence_refs: Record<string, unknown>[]
  action_refs: string[]
  approval_refs: string[]
  execution_refs: string[]
  permissions: { can_message: boolean; can_promote: boolean }
}

type ApprovalRequest = {
  approval_id: string
  action_proposal_id: string
  incident_id?: string
  session_id?: string
  status: string
  risk_level: string
  action_summary: string
  requested_by: string
  requested_at?: number | null
  assigned_approvers?: string[]
  approved_by?: string | null
  rejected_by?: string | null
  decided_at?: number | null
  resource_scope: Record<string, string>
  evidence_refs?: unknown[]
  audit_refs?: unknown[]
  rollback_plan?: string | null
  expected_impact?: string | null
  decision_reason?: string | null
  notification_status?: string | null
}

type ApprovalExecution = {
  execution_id: string
  status: string
  executor_id?: string
  preflight_result?: Record<string, unknown> | null
  execution_result?: Record<string, unknown> | null
  post_check_result?: Record<string, unknown> | null
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
  current_run?: AgentRun | null
  historical_runs?: Array<{ run_id: string; title?: string; status?: string; route?: string; created_at?: number | null }>
  panels: Record<string, WorkbenchPanelData>
  responsibility: Record<string, unknown>
  permissions?: {
    can_chat?: boolean
    can_start_run?: boolean
    can_control?: boolean
    can_request_action?: boolean
  }
}

type DiagnosisLine = {
  event_id?: string
  occurred_at?: string | null
  type?: string | null
  status?: string | null
  title?: string | null
  summary?: string | null
  refs?: Record<string, unknown>
}

type DiagnosisProcess = {
  diagnosis?: {
    status?: string
    summary?: string
    root_cause?: {
      category?: string
      statement?: string
      confidence?: number | null
    }
    redactions?: Record<string, unknown>
  } | null
  timeline?: DiagnosisLine[]
  evidence?: Array<{
    evidence_id?: string
    kind?: string
    status?: string
    summary?: string
    query?: { display?: string }
  }>
  missing_evidence?: unknown[]
  actions?: Array<{
    action_proposal_id?: string
    summary?: string
    risk_level?: string
    approval_required?: boolean
    execution_enabled?: boolean
  }>
  audit?: {
    status?: string
    summary?: string
    refs?: string[]
  }
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
  immutable_records?: Record<string, unknown>
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

type SearchResult = {
  type: string
  id: string
  title: string
  subtitle?: string
  route: string
  status?: string
  scope?: Record<string, unknown>
}

const LOCALE_KEY = 'aiops.console.locale'
const DEFAULT_ROUTE = '/'
const INCIDENTS_ROUTE = '/incidents'

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
    globalSearch: '全局搜索',
    searchResults: '搜索结果',
    noSearchResults: '暂无搜索结果',
    evidenceSummary: '证据摘要',
    processGraph: '过程图',
    callChain: '调用链',
    structuredDetail: '结构化详情',
    redactedSnippet: '脱敏片段',
    refs: '引用',
    whyItMattered: '为什么重要',
    approvalRemark: '审批备注',
    approvalTarget: '审批目标',
    rollbackPlan: '回滚计划',
    expectedImpact: '预期影响',
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
    defaultNamespaceScope: '默认命名空间范围',
    ownerTeam: 'Owner Team',
    automaticActions: '自动动作',
    openobserveConfigRef: 'OpenObserve 配置引用',
    connectorStatus: 'Connector 状态',
    openobserveStatus: 'OpenObserve 状态',
    scopeMappingHealth: '范围字段映射',
    recentQueryHealth: '最近查询健康',
    failureSummary: '失败摘要',
    lastHeartbeat: '最近心跳',
    runtimeUpdatedAt: '运行状态更新时间',
    configurationStatus: '配置状态',
    mutationDisabled: 'Mutation 已禁用',
    runtimeState: '运行状态',
    unconfigured: 'unconfigured',
    actionAllowlist: '动作允许列表',
    policyRules: '策略规则',
    addRule: '新增规则',
    addAllowlistEntry: '新增动作',
    remove: '删除',
    actionType: '动作类型',
    backend: '后端',
    template: '模板',
    allowedScopes: '允许范围',
    defaultRisk: '默认风险',
    preflight: '预检',
    postCheck: '后置检查',
    rollbackRequired: '要求回滚计划',
    autoExecution: '自动执行',
    selfApproval: '自审批',
    eligibleApproverRoles: '可审批角色',
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
    runbookSkeleton: 'Runbook skeleton',
    serviceHealth: 'service_health',
    k8sWorkload: 'k8s_workload',
    dependency: 'dependency',
    runMetadata: 'Run 指标',
    stepMetadata: 'Step 指标',
    toolCalls: '工具调用',
    evidenceRefs: '证据引用',
    startAgentRun: '启动 Agent Run',
    startInvestigation: '开始调查',
    agentChat: 'Agent Chat',
    btwThread: '/btw 旁路',
    send: '发送',
    sendBtw: '发送 /btw',
    currentRun: '当前 Run',
    historicalRuns: '历史 Runs',
    actionList: '推荐动作',
    incidentControls: '事件控制',
    tokens: 'Tokens',
    cost: 'Cost',
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
    approvalHistory: '审批历史',
    approvalRequired: '需要审批',
    frozenAction: '冻结 Action',
    executionStatus: '执行状态',
    approve: '批准',
    reject: '拒绝',
    actionHash: '动作 Hash',
    incidentList: '事件列表',
    incidentWorkbench: '事件工作台',
    timeline: '时间线',
    diagnosis: '诊断',
    agentOutput: 'Agent 输出',
    liveAgentOutput: '实时 Agent 输出',
    rootCause: '根因判断',
    evidenceCount: '证据数量',
    missingEvidence: '证据缺口',
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
    immutableRecords: '不可变记录',
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
      clusters: '集群',
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
      clusters: '集群',
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
    globalSearch: 'Global search',
    searchResults: 'Search results',
    noSearchResults: 'No search results',
    evidenceSummary: 'Evidence summary',
    processGraph: 'Process graph',
    callChain: 'Call chain',
    structuredDetail: 'Structured detail',
    redactedSnippet: 'Redacted snippet',
    refs: 'Refs',
    whyItMattered: 'Why it mattered',
    approvalRemark: 'Approval remark',
    approvalTarget: 'Approval target',
    rollbackPlan: 'Rollback plan',
    expectedImpact: 'Expected impact',
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
    defaultNamespaceScope: 'Default namespace scope',
    ownerTeam: 'Owner team',
    automaticActions: 'Automatic actions',
    openobserveConfigRef: 'OpenObserve config ref',
    connectorStatus: 'Connector status',
    openobserveStatus: 'OpenObserve status',
    scopeMappingHealth: 'Scope field mapping',
    recentQueryHealth: 'Recent query health',
    failureSummary: 'Failure summary',
    lastHeartbeat: 'Last heartbeat',
    runtimeUpdatedAt: 'Runtime updated at',
    configurationStatus: 'Configuration status',
    mutationDisabled: 'Mutation disabled',
    runtimeState: 'Runtime state',
    unconfigured: 'unconfigured',
    actionAllowlist: 'Action allowlist',
    policyRules: 'Policy rules',
    addRule: 'Add rule',
    addAllowlistEntry: 'Add action',
    remove: 'Remove',
    actionType: 'Action type',
    backend: 'Backend',
    template: 'Template',
    allowedScopes: 'Allowed scopes',
    defaultRisk: 'Default risk',
    preflight: 'Preflight',
    postCheck: 'Post-check',
    rollbackRequired: 'Rollback required',
    autoExecution: 'Auto-execution',
    selfApproval: 'Self-approval',
    eligibleApproverRoles: 'Eligible approver roles',
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
    runbookSkeleton: 'Runbook skeleton',
    serviceHealth: 'service_health',
    k8sWorkload: 'k8s_workload',
    dependency: 'dependency',
    runMetadata: 'Run metadata',
    stepMetadata: 'Step metadata',
    toolCalls: 'Tool calls',
    evidenceRefs: 'Evidence refs',
    startAgentRun: 'Start Agent Run',
    startInvestigation: 'Start investigation',
    agentChat: 'Agent chat',
    btwThread: '/btw side thread',
    send: 'Send',
    sendBtw: 'Send /btw',
    currentRun: 'Current run',
    historicalRuns: 'Historical runs',
    actionList: 'Recommended actions',
    incidentControls: 'Incident controls',
    tokens: 'Tokens',
    cost: 'Cost',
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
    approvalHistory: 'Approval history',
    approvalRequired: 'Approval required',
    frozenAction: 'Frozen Action',
    executionStatus: 'Execution status',
    approve: 'Approve',
    reject: 'Reject',
    actionHash: 'Action hash',
    incidentList: 'Incidents',
    incidentWorkbench: 'Incident workbench',
    timeline: 'Timeline',
    diagnosis: 'Diagnosis',
    agentOutput: 'Agent output',
    liveAgentOutput: 'Live agent output',
    rootCause: 'Root cause',
    evidenceCount: 'Evidence count',
    missingEvidence: 'Missing evidence',
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
    immutableRecords: 'Immutable records',
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
      clusters: 'Clusters',
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
      clusters: 'Clusters',
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
  { to: '/clusters', key: 'clusters', group: 'admin', permission: 'view_settings' },
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
                  <Route path="/" element={<Protected actor={actor} permission="view_incident"><DefaultIncidentRoute /></Protected>} />
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
                  <Route path="/clusters" element={<Protected actor={actor} permission="view_settings"><ClustersPage actor={actor} /></Protected>} />
                  <Route path="/users" element={<Protected actor={actor} permission="view_users"><UsersPage actor={actor} /></Protected>} />
                  <Route path="/users/:userId" element={<Protected actor={actor} permission="view_users"><Page title={String(t.pages.userDetail)} paramName="userId" /></Protected>} />
                  <Route path="/settings" element={<Protected actor={actor} permission="view_settings"><SettingsPage actor={actor} /></Protected>} />
                  <Route path="/search" element={<Protected actor={actor} permission="view_evidence"><SearchPage /></Protected>} />
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

function DefaultIncidentRoute() {
  const navigate = useNavigate()
  const t = useT()
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    async function openLatestIncident() {
      try {
        const data = await readJson<{ incidents: IncidentRow[] }>('/api/incidents/active')
        if (cancelled) {
          return
        }
        const first = data.incidents[0]
        navigate(first ? `/incidents/${encodeURIComponent(first.incident_id)}` : INCIDENTS_ROUTE, { replace: true })
      } catch (exc) {
        if (!cancelled) {
          setError(exc instanceof Error ? exc.message : String(t.loadFailed))
        }
      }
    }
    void openLatestIncident()
    return () => {
      cancelled = true
    }
  }, [navigate, t])

  return (
    <main className="center-state">
      {error ? <p className="form-error">{error}</p> : <p role="status">{String(t.loading)}</p>}
    </main>
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
  const navigate = useNavigate()
  const [query, setQuery] = useState('')
  const visibleRoutes = routes.filter((route) => canAccess(actor, route.permission))

  function submitSearch(event: FormEvent) {
    event.preventDefault()
    navigate(`/search?q=${encodeURIComponent(query.trim())}`)
  }

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
          <form className="top-search" onSubmit={submitSearch}>
            <label className="sr-only" htmlFor="global-search">{String(t.globalSearch)}</label>
            <input id="global-search" aria-label={String(t.globalSearch)} placeholder={String(t.search)} value={query} onChange={(event) => setQuery(event.target.value)} />
          </form>
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

function SearchPage() {
  const t = useT()
  const [params, setParams] = useSearchParams()
  const [query, setQuery] = useState(params.get('q') || '')
  const [results, setResults] = useState<SearchResult[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    const current = params.get('q') || ''
    setQuery(current)
    void loadSearch(current, params.get('type') || '')
  }, [params])

  async function loadSearch(nextQuery = query, type = params.get('type') || '') {
    setLoading(true)
    setError('')
    try {
      const suffix = new URLSearchParams({ q: nextQuery })
      if (type) {
        suffix.set('type', type)
      }
      const data = await readJson<{ results: SearchResult[] }>(`/api/search?${suffix.toString()}`)
      setResults(data.results)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.loadFailed))
    } finally {
      setLoading(false)
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    setParams(query.trim() ? { q: query.trim() } : {})
  }

  return (
    <main className="page search-page">
      <header className="page-header split-header">
        <div>
          <p className="eyebrow">{String(t.gatewayOnly)}</p>
          <h2>{String(t.globalSearch)}</h2>
        </div>
        <button className="text-action" type="button" onClick={() => void loadSearch()}>{String(t.refresh)}</button>
      </header>
      <form className="search-form" onSubmit={submit} role="search">
        <label>{String(t.globalSearch)}<input value={query} onChange={(event) => setQuery(event.target.value)} /></label>
        <button className="primary-action" type="submit">{String(t.searchResults)}</button>
      </form>
      {error ? <p className="form-error">{error}</p> : null}
      {loading ? <p role="status">{String(t.loading)}</p> : null}
      <section className="search-results" aria-label={String(t.searchResults)}>
        {!loading && results.length === 0 ? <p>{String(t.noSearchResults)}</p> : null}
        {results.map((item) => (
          <Link className="search-result" to={item.route} key={`${item.type}:${item.id}`}>
            <strong>{item.title || item.id}</strong>
            <span>{item.type} · {item.subtitle || item.id}</span>
            <span className="status-pill" aria-label={`${String(t.status)} ${item.status || '-'}`}>{item.status || '-'}</span>
          </Link>
        ))}
      </section>
    </main>
  )
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
  const canManage = canAccess(actor, 'manage_settings') && Boolean(actor?.roles?.includes('admin'))
  const [version, setVersion] = useState<SettingsVersion | null>(null)
  const [settings, setSettings] = useState<ConsoleSettings>({})
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
      setSettings(normalizeSettingsForUi(data.settings_version.settings))
      setPreview(null)
      setConfirmation('')
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.loadFailed))
    } finally {
      setLoading(false)
    }
  }

  async function previewSettings() {
    setError('')
    try {
      const data = await writeJson<{ preview: SettingsPreview }>('/api/settings/preview', { settings })
      setPreview(data.preview)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.actionFailed))
    }
  }

  async function saveSettings() {
    setError('')
    try {
      const data = await writeJson<{ settings_version: SettingsVersion }>('/api/settings', {
        settings,
        confirmation,
        change_summary: 'console settings save',
      })
      setVersion(data.settings_version)
      setSettings(normalizeSettingsForUi(data.settings_version.settings))
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
      setSettings(normalizeSettingsForUi(data.settings_version.settings))
      setPreview(null)
      setConfirmation('')
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.actionFailed))
    }
  }

  function updateRule(index: number, patch: Partial<PolicyRule>) {
    setPreview(null)
    setSettings((current) => {
      const next = normalizeSettingsForUi(current)
      const rules = [...(next.approval_policy?.rules || [])]
      rules[index] = { ...rules[index], ...patch }
      return { ...next, approval_policy: { ...(next.approval_policy || {}), rules } }
    })
  }

  function addRule() {
    setPreview(null)
    setSettings((current) => {
      const next = normalizeSettingsForUi(current)
      const rules = [
        ...(next.approval_policy?.rules || []),
        {
          environment: 'prod',
          cluster: null,
          namespace: null,
          action_type: '*',
          risk_level: 'low',
          approval_required: true,
          auto_execution: true,
          self_approval: false,
          eligible_approver_roles: ['approver', 'admin'],
        },
      ]
      return { ...next, approval_policy: { ...(next.approval_policy || {}), rules } }
    })
  }

  function removeRule(index: number) {
    setPreview(null)
    setSettings((current) => {
      const next = normalizeSettingsForUi(current)
      const rules = (next.approval_policy?.rules || []).filter((_, itemIndex) => itemIndex !== index)
      return { ...next, approval_policy: { ...(next.approval_policy || {}), rules } }
    })
  }

  function updateAllowlist(index: number, patch: Partial<ActionAllowlistEntry>) {
    setPreview(null)
    setSettings((current) => {
      const next = normalizeSettingsForUi(current)
      const actionAllowlist = [...(next.action_allowlist || [])]
      actionAllowlist[index] = { ...actionAllowlist[index], ...patch }
      return { ...next, action_allowlist: actionAllowlist }
    })
  }

  function addAllowlistEntry() {
    setPreview(null)
    setSettings((current) => {
      const next = normalizeSettingsForUi(current)
      const actionAllowlist = [
        ...(next.action_allowlist || []),
        {
          action_type: 'notify_only',
          backend: 'notification',
          template: 'send notification {channel}',
          allowed_scopes: ['prod', 'staging', 'dev', 'test'],
          default_risk: 'read_only',
          preflight: false,
          post_check: false,
          rollback_required: false,
          enabled: true,
        },
      ]
      return { ...next, action_allowlist: actionAllowlist }
    })
  }

  function removeAllowlistEntry(index: number) {
    setPreview(null)
    setSettings((current) => {
      const next = normalizeSettingsForUi(current)
      const action_allowlist = (next.action_allowlist || []).filter((_, itemIndex) => itemIndex !== index)
      return { ...next, action_allowlist }
    })
  }

  const rules = settings.approval_policy?.rules || []
  const actionAllowlist = settings.action_allowlist || []

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
        <div className="settings-main">
          {loading ? <p>{String(t.loading)}</p> : null}
          <SettingsPolicyRulesTable rules={rules} canManage={canManage} onChange={updateRule} onAdd={addRule} onRemove={removeRule} />
          <SettingsAllowlistTable entries={actionAllowlist} canManage={canManage} onChange={updateAllowlist} onAdd={addAllowlistEntry} onRemove={removeAllowlistEntry} />
        </div>
        <aside className="settings-side">
          {canManage ? (
            <div className="action-stack">
              <button className="primary-action" type="button" onClick={() => void previewSettings()}>{String(t.settingsPreview)}</button>
              <label>
                {String(t.confirmationText)}
                <input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} placeholder={preview?.confirmation_text || ''} />
              </label>
              <button className="primary-action" type="button" onClick={() => void saveSettings()} disabled={!preview}>{String(t.save)}</button>
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
          <h3>{String(t.policyRules)}</h3>
          <dl className="policy-list">
            <div><dt>{String(t.settingsVersion)}</dt><dd>v{policy?.version || '-'}</dd></div>
            <div><dt>{String(t.clusters)}</dt><dd>{policy?.cluster_environments.map((item) => `${item.cluster}:${item.environment}`).join(', ') || '-'}</dd></div>
          </dl>
          <PolicyRulesTable rules={policy?.policy.rules || []} />
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
        <h3>{String(t.actionAllowlist)}</h3>
        <ActionAllowlistTable entries={policy?.action_allowlist || []} />
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

function SettingsPolicyRulesTable({
  rules,
  canManage,
  onChange,
  onAdd,
  onRemove,
}: {
  rules: PolicyRule[]
  canManage: boolean
  onChange: (index: number, patch: Partial<PolicyRule>) => void
  onAdd: () => void
  onRemove: (index: number) => void
}) {
  const t = useT()
  return (
    <article className="policy-panel">
      <header>
        <h3>{String(t.policyRules)}</h3>
        {canManage ? <button className="text-action" type="button" onClick={onAdd}>{String(t.addRule)}</button> : null}
      </header>
      <div className="data-table policy-rules-table" role="table" aria-label={String(t.policyRules)}>
        <div className="data-row data-head" role="row">
          <span>{String(t.environment)}</span>
          <span>{String(t.clusters)}</span>
          <span>{String(t.namespaces)}</span>
          <span>{String(t.actionType)}</span>
          <span>{String(t.riskLevel)}</span>
          <span>{String(t.approvalRequired)}</span>
          <span>{String(t.autoExecution)}</span>
          <span>{String(t.selfApproval)}</span>
          <span>{String(t.eligibleApproverRoles)}</span>
          <span>{String(t.remove)}</span>
        </div>
        {rules.map((rule, index) => (
          <div className="data-row" role="row" key={`${rule.environment}-${rule.action_type}-${rule.risk_level}-${index}`}>
            <select value={rule.environment} disabled={!canManage} onChange={(event) => onChange(index, { environment: event.target.value })}>
              {['prod', 'staging', 'dev', 'test'].map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
            <input value={rule.cluster || ''} readOnly={!canManage} onChange={(event) => onChange(index, { cluster: event.target.value || null })} />
            <input value={rule.namespace || ''} readOnly={!canManage} onChange={(event) => onChange(index, { namespace: event.target.value || null })} />
            <input value={rule.action_type} readOnly={!canManage} onChange={(event) => onChange(index, { action_type: event.target.value })} />
            <select value={rule.risk_level} disabled={!canManage} onChange={(event) => onChange(index, { risk_level: event.target.value })}>
              {['read_only', 'low', 'medium', 'high'].map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
            <input type="checkbox" checked={rule.approval_required} disabled={!canManage} onChange={(event) => onChange(index, { approval_required: event.target.checked })} aria-label={String(t.approvalRequired)} />
            <input type="checkbox" checked={rule.auto_execution} disabled={!canManage} onChange={(event) => onChange(index, { auto_execution: event.target.checked })} aria-label={String(t.autoExecution)} />
            <input type="checkbox" checked={rule.self_approval} disabled={!canManage} onChange={(event) => onChange(index, { self_approval: event.target.checked })} aria-label={String(t.selfApproval)} />
            <input value={rule.eligible_approver_roles.join(', ')} readOnly={!canManage} onChange={(event) => onChange(index, { eligible_approver_roles: csvValues(event.target.value) })} />
            {canManage ? <button className="text-action" type="button" onClick={() => onRemove(index)}>{String(t.remove)}</button> : <span>-</span>}
          </div>
        ))}
      </div>
    </article>
  )
}

function SettingsAllowlistTable({
  entries,
  canManage,
  onChange,
  onAdd,
  onRemove,
}: {
  entries: ActionAllowlistEntry[]
  canManage: boolean
  onChange: (index: number, patch: Partial<ActionAllowlistEntry>) => void
  onAdd: () => void
  onRemove: (index: number) => void
}) {
  const t = useT()
  return (
    <article className="policy-panel">
      <header>
        <h3>{String(t.actionAllowlist)}</h3>
        {canManage ? <button className="text-action" type="button" onClick={onAdd}>{String(t.addAllowlistEntry)}</button> : null}
      </header>
      <div className="data-table allowlist-table" role="table" aria-label={String(t.actionAllowlist)}>
        <div className="data-row data-head" role="row">
          <span>{String(t.actionType)}</span>
          <span>{String(t.backend)}</span>
          <span>{String(t.template)}</span>
          <span>{String(t.allowedScopes)}</span>
          <span>{String(t.defaultRisk)}</span>
          <span>{String(t.preflight)}</span>
          <span>{String(t.postCheck)}</span>
          <span>{String(t.rollbackRequired)}</span>
          <span>{String(t.enabled)}</span>
          <span>{String(t.remove)}</span>
        </div>
        {entries.map((entry, index) => (
          <div className="data-row" role="row" key={`${entry.action_type}-${index}`}>
            <input value={entry.action_type} readOnly={!canManage} onChange={(event) => onChange(index, { action_type: event.target.value })} />
            <input value={entry.backend} readOnly={!canManage} onChange={(event) => onChange(index, { backend: event.target.value })} />
            <input value={entry.template} readOnly={!canManage} onChange={(event) => onChange(index, { template: event.target.value })} />
            <input value={entry.allowed_scopes.join(', ')} readOnly={!canManage} onChange={(event) => onChange(index, { allowed_scopes: csvValues(event.target.value) })} />
            <select value={entry.default_risk} disabled={!canManage} onChange={(event) => onChange(index, { default_risk: event.target.value })}>
              {['read_only', 'low', 'medium', 'high'].map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
            <input type="checkbox" checked={entry.preflight} disabled={!canManage} onChange={(event) => onChange(index, { preflight: event.target.checked })} aria-label={String(t.preflight)} />
            <input type="checkbox" checked={entry.post_check} disabled={!canManage} onChange={(event) => onChange(index, { post_check: event.target.checked })} aria-label={String(t.postCheck)} />
            <input type="checkbox" checked={entry.rollback_required} disabled={!canManage} onChange={(event) => onChange(index, { rollback_required: event.target.checked })} aria-label={String(t.rollbackRequired)} />
            <input type="checkbox" checked={entry.enabled} disabled={!canManage} onChange={(event) => onChange(index, { enabled: event.target.checked })} aria-label={String(t.enabled)} />
            {canManage ? <button className="text-action" type="button" onClick={() => onRemove(index)}>{String(t.remove)}</button> : <span>-</span>}
          </div>
        ))}
      </div>
    </article>
  )
}

function PolicyRulesTable({ rules }: { rules: PolicyRule[] }) {
  const t = useT()
  return (
    <div className="data-table policy-rules-table readonly" role="table" aria-label={String(t.policyRules)}>
      <div className="data-row data-head" role="row">
        <span>{String(t.environment)}</span>
        <span>{String(t.clusters)}</span>
        <span>{String(t.namespaces)}</span>
        <span>{String(t.actionType)}</span>
        <span>{String(t.riskLevel)}</span>
        <span>{String(t.approvalRequired)}</span>
        <span>{String(t.autoExecution)}</span>
        <span>{String(t.selfApproval)}</span>
        <span>{String(t.eligibleApproverRoles)}</span>
      </div>
      {rules.map((rule, index) => (
        <div className="data-row" role="row" key={`${rule.environment}-${rule.action_type}-${rule.risk_level}-${index}`}>
          <span>{rule.environment}</span>
          <span>{rule.cluster || '-'}</span>
          <span>{rule.namespace || '-'}</span>
          <span>{rule.action_type}</span>
          <span>{rule.risk_level}</span>
          <span>{yesNo(rule.approval_required)}</span>
          <span>{yesNo(rule.auto_execution)}</span>
          <span>{yesNo(rule.self_approval)}</span>
          <span>{rule.eligible_approver_roles.join(', ')}</span>
        </div>
      ))}
    </div>
  )
}

function ActionAllowlistTable({ entries }: { entries: ActionAllowlistEntry[] }) {
  const t = useT()
  return (
    <div className="data-table allowlist-table readonly" role="table" aria-label={String(t.actionAllowlist)}>
      <div className="data-row data-head" role="row">
        <span>{String(t.actionType)}</span>
        <span>{String(t.backend)}</span>
        <span>{String(t.template)}</span>
        <span>{String(t.allowedScopes)}</span>
        <span>{String(t.defaultRisk)}</span>
        <span>{String(t.preflight)}</span>
        <span>{String(t.postCheck)}</span>
        <span>{String(t.rollbackRequired)}</span>
        <span>{String(t.enabled)}</span>
      </div>
      {entries.map((entry, index) => (
        <div className="data-row" role="row" key={`${entry.action_type}-${index}`}>
          <span>{entry.action_type}</span>
          <span>{entry.backend}</span>
          <span>{entry.template}</span>
          <span>{entry.allowed_scopes.join(', ')}</span>
          <span>{entry.default_risk}</span>
          <span>{yesNo(entry.preflight)}</span>
          <span>{yesNo(entry.post_check)}</span>
          <span>{yesNo(entry.rollback_required)}</span>
          <span>{yesNo(entry.enabled)}</span>
        </div>
      ))}
    </div>
  )
}

function ClustersPage({ actor }: { actor: Actor | null }) {
  const t = useT()
  const canManage = canAccess(actor, 'manage_settings') && Boolean(actor?.roles?.includes('admin'))
  const [clusters, setClusters] = useState<ClusterRecord[]>([])
  const [form, setForm] = useState({
    cluster_id: '',
    display_name: '',
    environment: 'prod',
    default_namespace_scope: '',
    owner_team: '',
    automatic_actions_enabled: false,
    openobserve_config_ref: '',
  })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    void loadClusters()
  }, [])

  async function loadClusters() {
    setLoading(true)
    setError('')
    try {
      const data = await readJson<{ clusters: ClusterRecord[] }>('/api/clusters')
      setClusters(data.clusters)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.loadFailed))
    } finally {
      setLoading(false)
    }
  }

  async function saveCluster(event: FormEvent) {
    event.preventDefault()
    setError('')
    try {
      await writeJson<{ cluster: ClusterRecord }>('/api/clusters', form)
      setForm({ cluster_id: '', display_name: '', environment: 'prod', default_namespace_scope: '', owner_team: '', automatic_actions_enabled: false, openobserve_config_ref: '' })
      await loadClusters()
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.actionFailed))
    }
  }

  function editCluster(cluster: ClusterRecord) {
    setForm({
      cluster_id: cluster.cluster_id,
      display_name: cluster.display_name,
      environment: cluster.environment,
      default_namespace_scope: cluster.default_namespace_scope,
      owner_team: cluster.owner_team,
      automatic_actions_enabled: cluster.automatic_actions_enabled,
      openobserve_config_ref: cluster.openobserve_config_ref,
    })
  }

  return (
    <main className="page clusters-page">
      <header className="page-header split-header">
        <div>
          <p className="eyebrow">{String(t.gatewayOnly)}</p>
          <h2>{String(t.pages.clusters)}</h2>
        </div>
        <div className="header-actions">
          {!canManage ? <span className="status-pill">{String(t.readOnly)}</span> : null}
          <button className="text-action" type="button" onClick={() => void loadClusters()}>{String(t.refresh)}</button>
        </div>
      </header>
      {error ? <p className="form-error">{error}</p> : null}
      {canManage ? (
        <form className="action-form" onSubmit={saveCluster}>
          <label>{String(t.clusters)}<input value={form.cluster_id} onChange={(event) => setForm({ ...form, cluster_id: event.target.value })} /></label>
          <label>{String(t.displayName)}<input value={form.display_name} onChange={(event) => setForm({ ...form, display_name: event.target.value })} /></label>
          <label>{String(t.defaultEnvironment)}<select value={form.environment} onChange={(event) => setForm({ ...form, environment: event.target.value })}>
            <option value="prod">prod</option>
            <option value="staging">staging</option>
            <option value="dev">dev</option>
            <option value="test">test</option>
          </select></label>
          <label>{String(t.defaultNamespaceScope)}<input value={form.default_namespace_scope} onChange={(event) => setForm({ ...form, default_namespace_scope: event.target.value })} /></label>
          <label>{String(t.ownerTeam)}<input value={form.owner_team} onChange={(event) => setForm({ ...form, owner_team: event.target.value })} /></label>
          <label>{String(t.openobserveConfigRef)}<input value={form.openobserve_config_ref} onChange={(event) => setForm({ ...form, openobserve_config_ref: event.target.value })} /></label>
          <label className="checkbox-label"><input type="checkbox" checked={form.automatic_actions_enabled} onChange={(event) => setForm({ ...form, automatic_actions_enabled: event.target.checked })} />{String(t.automaticActions)}</label>
          <button className="primary-action" type="submit">{String(t.save)}</button>
        </form>
      ) : null}
      {loading ? <p>{String(t.loading)}</p> : null}
      <section className="run-list" aria-label={String(t.pages.clusters)}>
        {clusters.map((cluster) => (
          <article className="run-row" key={cluster.cluster_id}>
            <strong>{cluster.display_name || cluster.cluster_id}</strong>
            <span>
              {cluster.cluster_id} · {String(t.configurationStatus)}: {cluster.configuration_status}
              {cluster.configuration_status === 'unconfigured' ? ` · ${String(t.unconfigured)}` : ''}
              {' · '}{String(t.defaultEnvironment)}: {cluster.effective_environment}
              {' · '}{String(t.defaultNamespaceScope)}: {cluster.default_namespace_scope || '-'}
              {' · '}{String(t.ownerTeam)}: {cluster.owner_team || '-'}
              {' · '}{String(t.openobserveConfigRef)}: {cluster.openobserve_config_ref || '-'}
            </span>
            <span className={cluster.mutation_enabled ? 'status-pill' : 'status-pill danger'}>
              {cluster.mutation_enabled ? String(t.enabled) : `${String(t.mutationDisabled)}:${cluster.mutation_disabled_reason || cluster.configuration_status}`}
            </span>
            {canManage ? <button className="text-action" type="button" onClick={() => editCluster(cluster)}>{String(t.save)}</button> : null}
            <dl className="human-kv cluster-runtime">
              <div><dt>{String(t.connectorStatus)}</dt><dd>{cluster.runtime_state.connector_status}</dd></div>
              <div><dt>{String(t.openobserveStatus)}</dt><dd>{cluster.runtime_state.openobserve_status}</dd></div>
              <div><dt>{String(t.scopeMappingHealth)}</dt><dd>{cluster.runtime_state.scope_field_mapping_health}</dd></div>
              <div><dt>{String(t.recentQueryHealth)}</dt><dd>{cluster.runtime_state.recent_query_health}</dd></div>
              <div><dt>{String(t.failureSummary)}</dt><dd>{cluster.runtime_state.failure_summary || '-'}</dd></div>
              <div><dt>{String(t.lastHeartbeat)}</dt><dd>{formatTime(cluster.runtime_state.last_heartbeat)}</dd></div>
              <div><dt>{String(t.runtimeUpdatedAt)}</dt><dd>{formatTime(cluster.runtime_state.updated_at)}</dd></div>
            </dl>
          </article>
        ))}
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
  const [agentLines, setAgentLines] = useState<DiagnosisLine[]>([])
  const [streamState, setStreamState] = useState('')
  const [note, setNote] = useState('')
  const [chatMessage, setChatMessage] = useState('')
  const [btwMessage, setBtwMessage] = useState('')
  const [error, setError] = useState('')
  const reconnectingText = String(t.reconnecting)
  const staleRunText = String(t.staleRun)

  useEffect(() => {
    if (incidentId) {
      void loadWorkbench()
    }
  }, [incidentId])

  useEffect(() => {
    if (!incidentId) {
      return
    }
    setStreamState(reconnectingText)
    const stream = new EventSource(`/api/incidents/${encodeURIComponent(incidentId)}/diagnosis-process/stream`, { withCredentials: true })
    stream.addEventListener('message', (event) => {
      try {
        const item = JSON.parse(event.data) as DiagnosisLine
        setAgentLines((current) => appendDiagnosisLine(current, item))
        setStreamState('')
      } catch {
        setStreamState(staleRunText)
      }
    })
    stream.addEventListener('open', () => setStreamState(''))
    stream.addEventListener('error', () => setStreamState(staleRunText))
    return () => stream.close()
  }, [incidentId, reconnectingText, staleRunText])

  async function loadWorkbench() {
    setError('')
    try {
      const data = await readJson<{ workbench: IncidentWorkbench }>(`/api/incidents/${encodeURIComponent(incidentId)}/workbench`)
      setWorkbench(data.workbench)
      setAgentLines(diagnosisProcessFromWorkbench(data.workbench)?.timeline || [])
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

  async function startAgentRun() {
    setError('')
    try {
      await writeJson(`/api/incidents/${encodeURIComponent(incidentId)}/controls`, { action: 'restart_run', mode: 'start_new', note })
      await loadWorkbench()
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.actionFailed))
    }
  }

  async function sendRunMessage(message: string, side = false) {
    const runId = workbench?.current_run?.run_id
    if (!runId || !message.trim()) {
      return
    }
    setError('')
    try {
      await writeJson(`/api/agent-runs/${encodeURIComponent(runId)}/messages`, { message: side ? `/btw ${message}` : message })
      if (side) {
        setBtwMessage('')
      } else {
        setChatMessage('')
      }
      await loadWorkbench()
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.actionFailed))
    }
  }

  const incident = workbench?.incident
  const process = diagnosisProcessFromWorkbench(workbench)
  const permissions = workbench?.permissions || {}
  const canChat = Boolean(permissions.can_chat)
  const canStartRun = Boolean(permissions.can_start_run)
  const canControl = Boolean(permissions.can_control)
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
      <section className="incident-controls" aria-label={String(t.incidentControls)}>
        <label>{String(t.humanNote)}<input value={note} onChange={(event) => setNote(event.target.value)} /></label>
        <button type="button" className="text-action" disabled={!canControl} onClick={() => void control('pause_run')}>{String(t.pauseRun)}</button>
        <button type="button" className="text-action" disabled={!canControl} onClick={() => void control('terminate_run')}>{String(t.terminateRun)}</button>
        <button type="button" className="text-action" disabled={!canControl} onClick={() => void control('manual_takeover')}>{String(t.manualTakeover)}</button>
        <button type="button" className="text-action" disabled={!canControl} onClick={() => void control('human_note')}>{String(t.humanNote)}</button>
        <button type="button" className="primary-action" disabled={!canStartRun} onClick={() => void startAgentRun()}>{String(t.startAgentRun)}</button>
        <button type="button" className="text-action" disabled={!canStartRun} onClick={() => void control('restart_run', { mode: 'continue_current' })}>{String(t.restartRun)}</button>
        <button type="button" className="text-action" disabled={!canControl} onClick={() => void control('block_approvals')}>{String(t.blockApprovals)}</button>
        <button type="button" className="primary-action" disabled={!canControl} onClick={() => void control('resolve')}>{String(t.resolveIncident)}</button>
        <button type="button" className="text-action" disabled={!canControl} onClick={() => void control('reopen')}>{String(t.reopenIncident)}</button>
      </section>
      {workbench ? (
        <section className="workbench-grid" aria-label={String(t.incidentWorkbench)}>
          <section className="workbench-column workbench-left" aria-label={String(t.liveAgentOutput)}>
            <AgentChatPanel
              canChat={canChat}
              canStartRun={canStartRun}
              currentRun={workbench.current_run || null}
              chatMessage={chatMessage}
              btwMessage={btwMessage}
              setChatMessage={setChatMessage}
              setBtwMessage={setBtwMessage}
              startAgentRun={() => void startAgentRun()}
              sendChat={() => void sendRunMessage(chatMessage)}
              sendBtw={() => void sendRunMessage(btwMessage, true)}
            />
            <CurrentRunPanel currentRun={workbench.current_run || null} historicalRuns={workbench.historical_runs || []} />
          </section>
          <section className="workbench-column workbench-center" aria-label={String(t.processGraph)}>
            <EvidenceNodesPanel title={String(t.processGraph)} nodes={evidenceNodesFromPanel(workbench.panels.evidence)} />
            <WorkbenchPanel title={String(t.timeline)} panel={workbench.panels.timeline} />
          </section>
          <section className="workbench-column workbench-right" aria-label={String(t.responsibility)}>
            <DiagnosisPanel process={process} lines={agentLines} streamState={streamState} />
            <WorkbenchPanel title={String(t.actionList)} panel={{ name: 'actions', status: 'ok', data: process?.actions || [] }} />
            <WorkbenchPanel title={String(t.approvalList)} panel={workbench.panels.approvals} />
            <WorkbenchPanel title={String(t.executionStatus)} panel={workbench.panels.executions} />
            <article className="workbench-panel">
              <h3>{String(t.responsibility)}</h3>
              <HumanValue value={workbench.responsibility} />
            </article>
          </section>
        </section>
      ) : <p>{String(t.loading)}</p>}
    </main>
  )
}

function AgentChatPanel({
  canChat,
  canStartRun,
  currentRun,
  chatMessage,
  btwMessage,
  setChatMessage,
  setBtwMessage,
  startAgentRun,
  sendChat,
  sendBtw,
}: {
  canChat: boolean
  canStartRun: boolean
  currentRun: AgentRun | null
  chatMessage: string
  btwMessage: string
  setChatMessage: (value: string) => void
  setBtwMessage: (value: string) => void
  startAgentRun: () => void
  sendChat: () => void
  sendBtw: () => void
}) {
  const t = useT()
  return (
    <article className="workbench-panel agent-chat-panel">
      <header>
        <h3>{String(t.liveAgentOutput)}</h3>
        <span className="status-pill">{currentRun?.status || 'idle'}</span>
      </header>
      <button type="button" className="primary-action" disabled={!canStartRun} onClick={startAgentRun}>{String(t.startInvestigation)}</button>
      <label>{String(t.agentChat)}
        <input value={chatMessage} disabled={!canChat || !currentRun} onChange={(event) => setChatMessage(event.target.value)} />
      </label>
      <button type="button" className="text-action" disabled={!canChat || !currentRun || !chatMessage.trim()} onClick={sendChat}>{String(t.send)}</button>
      <label>{String(t.btwThread)}
        <input value={btwMessage} disabled={!canChat || !currentRun} onChange={(event) => setBtwMessage(event.target.value)} />
      </label>
      <button type="button" className="text-action" disabled={!canChat || !currentRun || !btwMessage.trim()} onClick={sendBtw}>{String(t.sendBtw)}</button>
    </article>
  )
}

function CurrentRunPanel({ currentRun, historicalRuns }: { currentRun: AgentRun | null; historicalRuns: Array<{ run_id: string; title?: string; status?: string; route?: string; created_at?: number | null }> }) {
  const t = useT()
  return (
    <article className="workbench-panel">
      <header>
        <h3>{String(t.currentRun)}</h3>
        <span className="status-pill">{currentRun?.status || '-'}</span>
      </header>
      {currentRun ? (
        <dl className="human-kv">
          <div><dt>Run</dt><dd><Link to={`/agent-runs/${encodeURIComponent(currentRun.run_id)}`}>{currentRun.run_id}</Link></dd></div>
          <div><dt>{String(t.runbookSkeleton)}</dt><dd>{currentRun.runbook_skeleton}</dd></div>
          <div><dt>{String(t.tokens)}</dt><dd>{humanText(currentRun.metadata?.total_tokens ?? 0)}</dd></div>
          <div><dt>{String(t.cost)}</dt><dd>{humanText(currentRun.metadata?.estimated_cost_usd ?? 0)}</dd></div>
        </dl>
      ) : <p>-</p>}
      <h4>{String(t.historicalRuns)}</h4>
      <div className="human-list">
        {historicalRuns.length ? historicalRuns.map((run) => (
          <Link key={run.run_id} to={run.route || `/agent-runs/${encodeURIComponent(run.run_id)}`}>{run.title || run.run_id} · {run.status || '-'}</Link>
        )) : <p>-</p>}
      </div>
    </article>
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
      <HumanValue value={panel?.data ?? []} />
    </article>
  )
}

function EvidenceNodesPanel({ title, nodes }: { title: string; nodes: EvidenceNode[] }) {
  const t = useT()
  const [selectedId, setSelectedId] = useState('')
  const selected = nodes.find((node) => node.node_id === selectedId) || nodes[0]
  useEffect(() => {
    if (nodes.length && !nodes.some((node) => node.node_id === selectedId)) {
      setSelectedId(nodes[0].node_id)
    }
  }, [nodes, selectedId])

  return (
    <article className="workbench-panel evidence-node-panel">
      <header>
        <h3>{title}</h3>
        <span className="status-pill">{nodes.length}</span>
      </header>
      {nodes.length ? (
        <>
          <div className="evidence-node-list" aria-label={String(t.callChain)}>
            {nodes.map((node) => (
              <button
                className={selected?.node_id === node.node_id ? 'evidence-node active' : 'evidence-node'}
                key={node.node_id}
                type="button"
                onClick={() => setSelectedId(node.node_id)}
              >
                <strong>{node.title || node.kind}</strong>
                <span>{node.summary}</span>
                <em>{node.status}</em>
              </button>
            ))}
          </div>
          {selected ? <EvidenceNodeDetail node={selected} /> : null}
        </>
      ) : <p>{String(t.emptyEvidence)}</p>}
    </article>
  )
}

function EvidenceNodeDetail({ node }: { node: EvidenceNode }) {
  const t = useT()
  const refs = node.refs?.map((ref) => ref.ref_id).join(', ') || '-'
  const scope = node.scope ? `${node.scope.cluster || '-'} / ${node.scope.namespace || '-'} / ${node.scope.service || '-'} / ${node.scope.team || '-'}` : '-'
  const range = node.time_range ? `${formatTime(node.time_range.start_ts)} - ${formatTime(node.time_range.end_ts)}` : '-'
  return (
    <section className="evidence-node-detail">
      <h4>{node.summary}</h4>
      <dl className="human-kv">
        <div><dt>{String(t.structuredDetail)}</dt><dd>{node.detail?.backend || '-'} · {node.detail?.status || node.status} · rows {node.detail?.row_count ?? 0}</dd></div>
        <div><dt>{String(t.redactedSnippet)}</dt><dd>{node.snippet || '-'}</dd></div>
        <div><dt>{String(t.refs)}</dt><dd>{refs}</dd></div>
        <div><dt>{String(t.scope)}</dt><dd>{scope}</dd></div>
        <div><dt>{String(t.timeRange)}</dt><dd>{range}</dd></div>
        <div><dt>{String(t.whyItMattered)}</dt><dd>{node.why_it_mattered || '-'}</dd></div>
      </dl>
      {node.detail?.chart?.length ? (
        <div className="evidence-chart" aria-label={String(t.structuredDetail)}>
          {node.detail.chart.slice(0, 12).map((point) => (
            <span key={`${point.x}-${point.y}`} style={{ height: `${Math.max(8, Math.min(80, Number(point.y) * 10))}%` }} />
          ))}
        </div>
      ) : null}
    </section>
  )
}

function evidenceNodesFromPanel(panel?: WorkbenchPanelData): EvidenceNode[] {
  const data = panel?.data
  if (!Array.isArray(data)) {
    return []
  }
  return data.filter(isEvidenceNode) as EvidenceNode[]
}

function isEvidenceNode(value: unknown): value is EvidenceNode {
  return isRecord(value) && typeof value.node_id === 'string' && typeof value.summary === 'string'
}

function DiagnosisPanel({ process, lines, streamState }: { process: DiagnosisProcess | null; lines: DiagnosisLine[]; streamState: string }) {
  const t = useT()
  const diagnosis = process?.diagnosis
  const rootCause = diagnosis?.root_cause
  const evidence = process?.evidence || []
  const missing = process?.missing_evidence || []
  return (
    <article className="workbench-panel diagnosis-panel">
      <header>
        <h3>{String(t.liveAgentOutput)}</h3>
        <span className="status-pill">{diagnosis?.status || 'waiting'}</span>
      </header>
      {streamState ? <p role="status">{streamState}</p> : null}
      <div className="diagnosis-summary">
        <div>
          <span>{String(t.diagnosis)}</span>
          <strong>{diagnosis?.summary || '-'}</strong>
        </div>
        <div>
          <span>{String(t.rootCause)}</span>
          <strong>{rootCause?.statement || '-'}</strong>
        </div>
        <div>
          <span>{String(t.evidenceCount)}</span>
          <strong>{evidence.length}</strong>
        </div>
        <div>
          <span>{String(t.missingEvidence)}</span>
          <strong>{missing.length}</strong>
        </div>
      </div>
      <div className="agent-output" aria-label={String(t.agentOutput)}>
        {lines.length ? lines.map((line, index) => (
          <div className="agent-line" key={line.event_id || `${line.type || 'line'}-${index}`}>
            <span>{formatDisplayTime(line.occurred_at)}</span>
            <strong>{line.title || line.type || 'agent'}</strong>
            <em>{line.status || '-'}</em>
            <p>{line.summary || '-'}</p>
          </div>
        )) : <p>{String(t.loading)}</p>}
      </div>
    </article>
  )
}

function HumanValue({ value }: { value: unknown }) {
  if (Array.isArray(value)) {
    if (!value.length) {
      return <p>-</p>
    }
    return (
      <div className="human-list">
        {value.map((item, index) => (
          <div className="human-row" key={index}>
            <HumanValue value={item} />
          </div>
        ))}
      </div>
    )
  }
  if (isRecord(value)) {
    const entries = Object.entries(value).filter(([, item]) => item !== null && item !== undefined && item !== '')
    if (!entries.length) {
      return <p>-</p>
    }
    return (
      <dl className="human-kv">
        {entries.slice(0, 12).map(([key, item]) => (
          <div key={key}>
            <dt>{labelize(key)}</dt>
            <dd>{humanText(item)}</dd>
          </div>
        ))}
      </dl>
    )
  }
  return <p>{humanText(value)}</p>
}

function diagnosisProcessFromWorkbench(workbench: IncidentWorkbench | null): DiagnosisProcess | null {
  const data = workbench?.panels?.diagnosis?.data
  if (isRecord(data) && isRecord(data.process)) {
    return data.process as DiagnosisProcess
  }
  if (isRecord(data) && (Array.isArray(data.timeline) || isRecord(data.diagnosis))) {
    return data as DiagnosisProcess
  }
  return null
}

function appendDiagnosisLine(current: DiagnosisLine[], item: DiagnosisLine): DiagnosisLine[] {
  const key = diagnosisLineKey(item)
  if (current.some((line) => diagnosisLineKey(line) === key)) {
    return current
  }
  return [...current, item]
}

function diagnosisLineKey(item: DiagnosisLine): string {
  return String(item.event_id || `${item.occurred_at || ''}:${item.type || ''}:${item.title || ''}:${item.summary || ''}`)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function normalizeSettingsForUi(value: unknown): ConsoleSettings {
  const source = isRecord(value) ? value : {}
  const approval = isRecord(source.approval_policy) ? source.approval_policy : {}
  return {
    ...source,
    clusters: Array.isArray(source.clusters)
      ? source.clusters.filter(isRecord).map((item) => ({
        cluster: String(item.cluster || item.cluster_id || ''),
        environment: String(item.environment || 'prod'),
      })).filter((item) => item.cluster)
      : [],
    approval_policy: {
      ...approval,
      rules: normalizeRulesForUi(approval.rules),
      allow_self_approval_low_risk: Boolean(approval.allow_self_approval_low_risk),
      dev_low_risk_auto_execute: Boolean(approval.dev_low_risk_auto_execute),
      test_low_risk_auto_execute: Boolean(approval.test_low_risk_auto_execute),
    },
    action_allowlist: normalizeAllowlistForUi(source.action_allowlist),
  }
}

function normalizeRulesForUi(value: unknown): PolicyRule[] {
  if (!Array.isArray(value)) {
    return []
  }
  return value.filter(isRecord).map((item) => ({
    environment: String(item.environment || 'prod'),
    cluster: stringOrNull(item.cluster),
    namespace: stringOrNull(item.namespace),
    action_type: String(item.action_type || '*'),
    risk_level: String(item.risk_level || 'low'),
    approval_required: Boolean(item.approval_required),
    auto_execution: Boolean(item.auto_execution),
    self_approval: Boolean(item.self_approval),
    eligible_approver_roles: stringArray(item.eligible_approver_roles),
  }))
}

function normalizeAllowlistForUi(value: unknown): ActionAllowlistEntry[] {
  if (!Array.isArray(value)) {
    return []
  }
  return value.filter(isRecord).map((item) => ({
    action_type: String(item.action_type || ''),
    backend: String(item.backend || ''),
    template: String(item.template || ''),
    allowed_scopes: stringArray(item.allowed_scopes),
    default_risk: String(item.default_risk || 'low'),
    preflight: Boolean(item.preflight),
    post_check: Boolean(item.post_check),
    rollback_required: Boolean(item.rollback_required),
    enabled: Boolean(item.enabled),
  })).filter((item) => item.action_type)
}

function stringArray(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map((item) => String(item)).filter(Boolean)
  }
  if (typeof value === 'string') {
    return csvValues(value)
  }
  return []
}

function stringOrNull(value: unknown): string | null {
  const text = String(value || '').trim()
  return text || null
}

function yesNo(value: boolean): string {
  return value ? 'yes' : 'no'
}

function labelize(value: string): string {
  return value.replace(/_/g, ' ')
}

function humanText(value: unknown): string {
  if (value === null || value === undefined || value === '') {
    return '-'
  }
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return String(value)
  }
  if (Array.isArray(value)) {
    return value.length ? value.map((item) => humanText(item)).join(', ') : '-'
  }
  if (isRecord(value)) {
    for (const key of ['summary', 'title', 'message', 'status', 'name', 'incident_id', 'run_id', 'service', 'namespace', 'cluster']) {
      if (value[key]) {
        return humanText(value[key])
      }
    }
    return Object.keys(value).join(', ') || '-'
  }
  return String(value)
}

function formatDisplayTime(value?: string | number | null): string {
  if (!value) {
    return '-'
  }
  if (typeof value === 'number') {
    return formatTime(value)
  }
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
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

  const evidenceNodes = approvalEvidenceNodes(approval)
  const frozenAction = approval ? frozenApprovalAction(approval) : null
  const responsibility = approvalResponsibility(approval, execution)
  const history = approvalHistory(approval)
  const progress = executionProgress(execution)

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
        <section className="approval-detail-grid mobile-approval">
          <article className="workbench-panel frozen-action-card">
            <header>
              <h3>{String(t.frozenAction)}</h3>
              <span className="status-pill">{approval.risk_level}</span>
            </header>
            <dl className="human-kv">
              <div><dt>{String(t.requestedAction)}</dt><dd>{approval.action_summary}</dd></div>
              <div><dt>{String(t.approvalTarget)}</dt><dd>{targetText(approval.resource_scope)}</dd></div>
              <div><dt>{String(t.actionHash)}</dt><dd>{String(frozenAction?.action_hash || '-')}</dd></div>
              <div><dt>{String(t.expectedImpact)}</dt><dd>{approval.expected_impact || '-'}</dd></div>
              <div><dt>{String(t.rollbackPlan)}</dt><dd>{approval.rollback_plan || '-'}</dd></div>
            </dl>
          </article>

          <EvidenceNodesPanel title={String(t.evidenceSummary)} nodes={evidenceNodes} />

          <aside className="approval-side">
            <article className="workbench-panel">
              <header>
                <h3>{String(t.responsibility)}</h3>
                <span className="status-pill">{String(responsibility.responsibility_status || '-')}</span>
              </header>
              <HumanValue value={responsibility} />
            </article>
            <article className="workbench-panel">
              <header>
                <h3>{String(t.approvalHistory)}</h3>
                <span className="status-pill">{history.length}</span>
              </header>
              <div className="timeline-list">
                {history.map((item, index) => (
                  <div className="run-event" key={`${item.event}-${index}`}>
                    <header>
                      <strong>{item.event}</strong>
                      <em>{formatDisplayTime(item.at)}</em>
                    </header>
                    <p>{item.actor || approval.requested_by || '-'}</p>
                    {item.reason ? <p>{item.reason}</p> : null}
                  </div>
                ))}
              </div>
            </article>
            <article className="workbench-panel">
              <header>
                <h3>{String(t.executionStatus)}</h3>
                <span className="status-pill" role="status">{execution?.status || approval.status}</span>
              </header>
              <div className="timeline-list">
                {progress.map((item) => (
                  <div className="run-event" key={item.name}>
                    <header>
                      <strong>{item.name}</strong>
                      <em>{item.status}</em>
                    </header>
                    <p>{item.summary}</p>
                  </div>
                ))}
              </div>
              {execution?.error_message ? <p className="form-error">{execution.error_message}</p> : null}
            </article>
            <article className="workbench-panel approval-actions">
              <label>{String(t.approvalRemark)}<textarea value={reason} onChange={(event) => setReason(event.target.value)} /></label>
              <div className="header-actions">
                <button className="primary-action" type="button" onClick={() => void decide('approve')}>{String(t.approve)}: {approval.action_summary}</button>
                <button className="text-action" type="button" onClick={() => void decide('reject')}>{String(t.reject)}: {approval.action_summary}</button>
              </div>
            </article>
          </aside>
        </section>
      ) : <p>{String(t.loading)}</p>}
    </main>
  )
}

function approvalEvidenceNodes(approval: ApprovalRequest | null): EvidenceNode[] {
  return (approval?.evidence_refs || []).map((item, index) => {
    const record = isRecord(item) ? item : {}
    const ref = String(record.ref_id || record.ref || record.source || item || `evidence-${index + 1}`)
    return {
      node_id: `approval-evidence-${index}`,
      kind: String(record.source || record.type || 'evidence'),
      title: ref,
      summary: String(record.summary || record.reason || ref),
      status: String(record.status || 'referenced'),
      refs: [{ ref_id: ref }],
      detail: {
        backend: String(record.source || record.type || 'gateway'),
        status: String(record.status || 'referenced'),
      },
      why_it_mattered: String(record.why || record.reason || approval?.action_summary || ''),
    }
  })
}

function frozenApprovalAction(approval: ApprovalRequest): Record<string, unknown> {
  const frozenRef = (approval.audit_refs || []).find((item) => isRecord(item) && item.event === 'action_frozen')
  return {
    action_proposal_id: approval.action_proposal_id,
    action_hash: isRecord(frozenRef) ? frozenRef.action_hash : null,
    action_summary: approval.action_summary,
    target: approval.resource_scope,
  }
}

function approvalResponsibility(approval: ApprovalRequest | null, execution: ApprovalExecution | null): Record<string, unknown> {
  if (!approval) {
    return {}
  }
  return {
    incident: approval.incident_id,
    conversation_run: approval.session_id,
    agent: approval.requested_by,
    requested_action: approval.action_summary,
    risk: approval.risk_level,
    target: targetText(approval.resource_scope),
    approver: approval.approved_by || approval.rejected_by || (approval.assigned_approvers || []).join(', '),
    decision: approval.status,
    gateway_execution_result: execution?.status || 'not_started',
    responsibility_status: responsibilityStatus(approval.status, execution?.status),
  }
}

function approvalHistory(approval: ApprovalRequest | null): Array<{ event: string; actor?: string; at?: string | number | null; reason?: string | null }> {
  if (!approval) {
    return []
  }
  const history = (approval.audit_refs || [])
    .filter(isRecord)
    .map((item) => ({
      event: String(item.event || 'approval_event'),
      actor: item.actor ? String(item.actor) : undefined,
      at: typeof item.at === 'string' || typeof item.at === 'number' ? item.at : null,
      reason: item.reason ? String(item.reason) : null,
    }))
  if (!history.length) {
    history.push({ event: 'approval_requested', actor: approval.requested_by, at: approval.requested_at || null, reason: approval.action_summary })
  }
  if (approval.decided_at) {
    history.push({
      event: `approval_${approval.status}`,
      actor: approval.approved_by || approval.rejected_by || undefined,
      at: approval.decided_at,
      reason: approval.decision_reason || null,
    })
  }
  return history
}

function executionProgress(execution: ApprovalExecution | null): Array<{ name: string; status: string; summary: string }> {
  if (!execution) {
    return [
      { name: 'preflight', status: 'not_started', summary: '-' },
      { name: 'mutation', status: 'not_started', summary: '-' },
      { name: 'post-check', status: 'not_started', summary: '-' },
    ]
  }
  return [
    { name: 'preflight', status: resultStatus(execution.preflight_result), summary: humanText(execution.preflight_result) },
    { name: 'mutation', status: resultStatus(execution.execution_result), summary: humanText(execution.execution_result) },
    { name: 'post-check', status: resultStatus(execution.post_check_result), summary: humanText(execution.post_check_result) },
  ]
}

function resultStatus(value: unknown): string {
  return isRecord(value) ? String(value.status || 'recorded') : 'pending'
}

function responsibilityStatus(approvalStatus: string, executionStatus?: string | null): string {
  if (executionStatus === 'succeeded') {
    return 'closed'
  }
  if (executionStatus === 'rollback_required') {
    return 'rollback_required'
  }
  if (['rejected', 'expired', 'cancelled'].includes(approvalStatus)) {
    return approvalStatus
  }
  if (approvalStatus === 'approved') {
    return executionStatus || 'execution_pending'
  }
  return 'approval_pending'
}

function targetText(scope: Record<string, unknown>): string {
  return [scope.cluster || scope.cluster_id, scope.namespace, scope.service || scope.service_id, scope.team || scope.team_id]
    .map((item) => String(item || '-'))
    .join(' / ')
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
            <span>{run.runbook_skeleton}</span>
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
    runbook_skeleton: 'service_health',
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
        runbook_skeleton: form.runbook_skeleton,
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
        <label>{String(t.runbookSkeleton)}
          <select value={form.runbook_skeleton} onChange={(event) => setForm({ ...form, runbook_skeleton: event.target.value })}>
            <option value="service_health">{String(t.serviceHealth)}</option>
            <option value="k8s_workload">{String(t.k8sWorkload)}</option>
            <option value="dependency">{String(t.dependency)}</option>
          </select>
        </label>
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
  const [evidenceNodes, setEvidenceNodes] = useState<EvidenceNode[]>([])
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
        const [feedbackData, evidenceData] = await Promise.all([
          readJson<{ feedback: Feedback[] }>(`/api/agent-runs/${encodeURIComponent(runId)}/feedback`),
          readJson<{ evidence: EvidenceResponse }>(`/api/agent-runs/${encodeURIComponent(runId)}/evidence`),
        ])
        if (closed) {
          return
        }
        setFeedback(feedbackData.feedback)
        setEvidenceNodes(evidenceData.evidence.nodes || [])
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
          {snapshot ? <p>{snapshot.run.scope.cluster} / {snapshot.run.scope.namespace} / {snapshot.run.scope.service} · {snapshot.run.runbook_skeleton}</p> : null}
        </div>
        <span className="status-pill">{snapshot?.run.status || '-'}</span>
      </header>
      {streamState ? <p role="status">{streamState}</p> : null}
      {error ? <p className="form-error">{error}</p> : null}
      <form className="run-message-form" onSubmit={sendMessage}>
        <label>{String(t.runMessage)}<input value={message} onChange={(event) => setMessage(event.target.value)} /></label>
        <button className="primary-action" type="submit" disabled={!snapshot?.permissions.can_message}>{String(t.save)}</button>
      </form>
      {snapshot ? (
        <section className="workbench-grid" aria-label={String(t.stepMetadata)}>
          <article className="workbench-panel">
            <h3>{String(t.runMetadata)}</h3>
            <HumanValue value={snapshot.run.metadata || {}} />
          </article>
          {snapshot.steps.map((step) => (
            <article className="workbench-panel" key={step.step_id}>
              <header>
                <h3>{step.name}</h3>
                <span className="status-pill">{step.status}</span>
              </header>
              <HumanValue value={step.metadata} />
              {step.stuck_reason ? <p className="form-error">{step.stuck_reason}</p> : null}
              <details>
                <summary>{String(t.toolCalls)}</summary>
                <HumanValue value={step.tool_calls} />
              </details>
              <details>
                <summary>{String(t.evidenceRefs)}</summary>
                <HumanValue value={step.evidence_refs} />
              </details>
            </article>
          ))}
        </section>
      ) : null}
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
      <EvidenceNodesPanel title={String(t.processGraph)} nodes={evidenceNodes} />
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
        <EvidenceNodesPanel title={String(t.processGraph)} nodes={evidence?.nodes || []} />
      </section>
    </main>
  )
}

function AuditPage() {
  const t = useT()
  const [chains, setChains] = useState<AuditChain[]>([])
  const [rows, setRows] = useState<AuditRawRow[]>([])
  const [tombstones, setTombstones] = useState<AuditTombstone[]>([])
  const [tab, setTab] = useState<'chains' | 'raw' | 'deleted'>('chains')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    void loadAudit()
  }, [])

  async function loadAudit() {
    setLoading(true)
    setError('')
    try {
      const chainData = await readJson<{ chains: AuditChain[] }>('/api/audit/chains')
      setChains(chainData.chains)
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.loadFailed))
    } finally {
      setLoading(false)
    }
  }

  async function selectTab(next: 'chains' | 'raw' | 'deleted') {
    setTab(next)
    setError('')
    try {
      if (next === 'raw' && !rows.length) {
        const rawData = await readJson<{ rows: AuditRawRow[] }>('/api/audit/raw?limit=20')
        setRows(rawData.rows)
      }
      if (next === 'deleted' && !tombstones.length) {
        const tombstoneData = await readJson<{ tombstones: AuditTombstone[] }>('/api/audit/tombstones')
        setTombstones(tombstoneData.tombstones)
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(t.loadFailed))
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
      <div className="tab-strip" role="tablist" aria-label={String(t.pages.audit)}>
        <button className={tab === 'chains' ? 'active' : ''} type="button" role="tab" aria-selected={tab === 'chains'} onClick={() => void selectTab('chains')}>{String(t.auditChains)}</button>
        <button className={tab === 'raw' ? 'active' : ''} type="button" role="tab" aria-selected={tab === 'raw'} onClick={() => void selectTab('raw')}>{String(t.rawLogs)}</button>
        <button className={tab === 'deleted' ? 'active' : ''} type="button" role="tab" aria-selected={tab === 'deleted'} onClick={() => void selectTab('deleted')}>{String(t.deletedConversations)}</button>
      </div>
      <section className="audit-grid">
        {tab === 'chains' ? (
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
        ) : null}
        {tab === 'raw' ? (
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
        ) : null}
        {tab === 'deleted' ? (
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
        ) : null}
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
          <AuditJsonPanel title={String(t.immutableRecords)} value={chain.immutable_records} />
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
