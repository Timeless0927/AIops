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
}

type EvidenceItem = {
  evidence_id?: string
  kind?: string
  status?: string
  summary?: string
  collected_at?: string | null
  query?: { display?: string; time_range?: { from?: string | null; to?: string | null } }
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

type DiagnosisProcessResponse = {
  process: DiagnosisProcess
}

const TOKEN_KEY = 'aiops.console.token'

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

function formatTime(value?: string | null): string {
  if (!value) {
    return '-'
  }
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
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

export default function App() {
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

  const selectedIncident = useMemo(
    () => incidents.find((incident) => incident.incident_id === selectedId) || incidents[0],
    [incidents, selectedId],
  )
  const evidence = process?.evidence || []
  const timeline = process?.timeline || []
  const missingEvidence = process?.missing_evidence || []
  const actions = process?.actions || []

  useEffect(() => {
    if (!token) {
      return
    }
    void refreshIncidents(token)
  }, [token])

  useEffect(() => {
    if (!token || !selectedIncident || selectedIncident.incident_id.startsWith('demo-')) {
      setProcess(null)
      setProcessNotice(token ? '演示事件没有诊断过程。' : '登录后可读取诊断过程。')
      return
    }
    void refreshDiagnosisProcess(selectedIncident.incident_id, token)
  }, [selectedIncident?.incident_id, token])

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
      setNotice('登录成功，正在读取事件列表。')
      await refreshIncidents(data.token)
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
    setNotice('已退出，页面切回演示数据。')
  }

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
          <a aria-current="page">事件总览</a>
          <a aria-disabled="true">诊断过程</a>
          <a aria-disabled="true">审批中心</a>
          <a aria-disabled="true">通知记录</a>
          <a aria-disabled="true">审计历史</a>
        </nav>
        <section className="login-panel" aria-label="登录">
          <div className="section-title">
            <span>会话</span>
            {token ? <button onClick={logout}>退出</button> : null}
          </div>
          {token ? (
            <p className="session-copy">
              当前用户：{actor?.display_name || actor?.username || '已登录'}
              <br />
              权限角色：{actor?.roles?.join('、') || '已授权会话'}
            </p>
          ) : (
            <form onSubmit={handleLogin}>
              <label>
                用户名
                <input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" />
              </label>
              <label>
                密码
                <input
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  autoComplete="current-password"
                  type="password"
                  placeholder="输入 Gateway 密码"
                />
              </label>
              <button type="submit" disabled={loading}>
                {loading ? '连接中' : '登录 Gateway'}
              </button>
            </form>
          )}
        </section>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div>
            <p className="eyebrow">仅通过 Gateway API</p>
            <h2>活跃事件</h2>
          </div>
          <button className="secondary-action" onClick={() => void refreshIncidents()} disabled={loading || !token}>
            {loading ? '刷新中' : '刷新'}
          </button>
        </header>

        <div className="notice" role="status">
          {notice}
        </div>

        <div className="summary-grid">
          <Metric label="活跃事件" value={incidents.length.toString()} />
          <Metric label="严重事件" value={incidents.filter((item) => item.severity === 'critical').length.toString()} />
          <Metric label="涉及服务" value={new Set(incidents.map((item) => item.service)).size.toString()} />
        </div>

        <div className="content-grid">
          <section className="incident-list" aria-label="事件列表">
            {incidents.length ? (
              incidents.map((incident) => (
                <button
                  key={incident.incident_id}
                  className={incident.incident_id === selectedIncident?.incident_id ? 'incident-row active' : 'incident-row'}
                  onClick={() => setSelectedId(incident.incident_id)}
                >
                  <span className={`severity ${incident.severity}`}>{severityLabel(incident.severity)}</span>
                  <strong>{incident.title}</strong>
                  <span>{incident.service} · {incident.age}</span>
                </button>
              ))
            ) : (
              <div className="empty-state">当前没有活跃事件。</div>
            )}
          </section>

          <section className="detail-panel" aria-label="事件详情">
            {selectedIncident ? (
              <>
                <div className="detail-heading">
                  <span className={`severity ${selectedIncident.severity}`}>{severityLabel(selectedIncident.severity)}</span>
                  <h3>{selectedIncident.title}</h3>
                  <p>{selectedIncident.incident_id}</p>
                </div>
                <dl className="detail-list">
                  <div>
                    <dt>状态</dt>
                    <dd>{statusLabel(selectedIncident.status)}</dd>
                  </div>
                  <div>
                    <dt>服务</dt>
                    <dd>{selectedIncident.service}</dd>
                  </div>
                  <div>
                    <dt>影响</dt>
                    <dd>{selectedIncident.impact}</dd>
                  </div>
                  <div>
                    <dt>标签</dt>
                    <dd>{selectedIncident.tags || '-'}</dd>
                  </div>
                </dl>
                <div className="next-steps">
                  <span>诊断过程</span>
                  <p>{processNotice}</p>
                  {process ? (
                    <DiagnosisProcessView
                      process={process}
                      evidence={evidence}
                      timeline={timeline}
                      missingEvidence={missingEvidence}
                      actions={actions}
                    />
                  ) : null}
                </div>
              </>
            ) : (
              <div className="empty-state">选择一个事件查看详情。</div>
            )}
          </section>
        </div>
      </section>
    </main>
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

function DiagnosisProcessView({
  process,
  evidence,
  timeline,
  missingEvidence,
  actions,
}: {
  process: DiagnosisProcess
  evidence: EvidenceItem[]
  timeline: TimelineItem[]
  missingEvidence: MissingEvidence[]
  actions: ActionProposal[]
}) {
  const rootCause = process.diagnosis?.root_cause
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
