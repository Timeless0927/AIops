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
  diagnosis?: {
    status?: string
    summary?: string
    markdown?: string
    root_cause?: { category?: string; summary?: string }
  } | null
  evidence?: unknown[]
  timeline?: unknown[]
  missing_evidence?: unknown[]
  actions?: { title?: string; summary?: string; execution_enabled?: boolean }[]
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
                    <div className="process-summary">
                      <strong>{process.diagnosis?.summary || process.diagnosis?.markdown || '暂无诊断摘要'}</strong>
                      <span>
                        状态：{statusLabel(process.diagnosis?.status || 'unknown')} · 证据：
                        {process.evidence?.length || 0} · 时间线：{process.timeline?.length || 0} · 缺失：
                        {process.missing_evidence?.length || 0}
                      </span>
                      {process.actions?.length ? <span>建议动作：{process.actions.length} 条，默认只读。</span> : null}
                    </div>
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
