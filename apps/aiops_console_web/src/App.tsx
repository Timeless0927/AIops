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
}

type MeResponse = {
  actor: Actor
  role_permission_matrix?: Record<string, string[]>
}

type LoginResponse = MeResponse & {
  token?: string
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
  { to: '/incidents', key: 'incidents', group: 'operations', access: 'user' },
  { to: '/agent-runs', key: 'agentRuns', group: 'operations', access: 'user' },
  { to: '/approvals', key: 'approvals', group: 'governance', access: 'approver' },
  { to: '/audit', key: 'audit', group: 'governance', access: 'auditor' },
  { to: '/policies', key: 'policies', group: 'governance', access: 'admin' },
  { to: '/users', key: 'users', group: 'admin', access: 'admin' },
  { to: '/settings', key: 'settings', group: 'admin', access: 'admin' },
  { to: '/search', key: 'search', group: 'evidence', access: 'user' },
  { to: '/notifications', key: 'notifications', group: 'operations', access: 'user' },
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

function safeNext(value: string | null): string {
  if (!value || !value.startsWith('/') || value.startsWith('//') || value.startsWith('/auth/')) {
    return DEFAULT_ROUTE
  }
  return value
}

function canAccess(actor: Actor | null, access: (typeof routes)[number]['access']): boolean {
  if (!actor) {
    return false
  }
  const roles = new Set(actor.roles || [])
  if (roles.has('admin')) {
    return true
  }
  if (access === 'admin' || access === 'auditor') {
    return false
  }
  if (access === 'approver') {
    return roles.has('oncall_approver')
  }
  return true
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
                  <Route path="/incidents" element={<Protected actor={actor} access="user"><Page title={String(t.pages.incidents)} /></Protected>} />
                  <Route path="/incidents/:incidentId" element={<Protected actor={actor} access="user"><Page title={String(t.pages.incidentDetail)} paramName="incidentId" /></Protected>} />
                  <Route path="/incidents/:incidentId/report" element={<Protected actor={actor} access="user"><Page title={String(t.pages.incidentReport)} paramName="incidentId" /></Protected>} />
                  <Route path="/agent-runs" element={<Protected actor={actor} access="user"><Page title={String(t.pages.agentRuns)} /></Protected>} />
                  <Route path="/agent-runs/new" element={<Protected actor={actor} access="user"><Page title={String(t.pages.newAgentRun)} /></Protected>} />
                  <Route path="/agent-runs/:runId" element={<Protected actor={actor} access="user"><Page title={String(t.pages.agentRunDetail)} paramName="runId" /></Protected>} />
                  <Route path="/approvals" element={<Protected actor={actor} access="approver"><Page title={String(t.pages.approvals)} /></Protected>} />
                  <Route path="/approvals/:approvalId" element={<Protected actor={actor} access="approver"><Page title={String(t.pages.approvalDetail)} paramName="approvalId" /></Protected>} />
                  <Route path="/audit" element={<Protected actor={actor} access="auditor"><Page title={String(t.pages.audit)} /></Protected>} />
                  <Route path="/audit/:chainId" element={<Protected actor={actor} access="auditor"><Page title={String(t.pages.auditDetail)} paramName="chainId" /></Protected>} />
                  <Route path="/policies" element={<Protected actor={actor} access="admin"><Page title={String(t.pages.policies)} /></Protected>} />
                  <Route path="/users" element={<Protected actor={actor} access="admin"><Page title={String(t.pages.users)} /></Protected>} />
                  <Route path="/users/:userId" element={<Protected actor={actor} access="admin"><Page title={String(t.pages.userDetail)} paramName="userId" /></Protected>} />
                  <Route path="/settings" element={<Protected actor={actor} access="admin"><Page title={String(t.pages.settings)} /></Protected>} />
                  <Route path="/search" element={<Protected actor={actor} access="user"><Page title={String(t.pages.search)} /></Protected>} />
                  <Route path="/notifications" element={<Protected actor={actor} access="user"><Page title={String(t.pages.notifications)} /></Protected>} />
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
  const visibleRoutes = routes.filter((route) => canAccess(actor, route.access))

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

function Protected({ actor, access, children }: { actor: Actor | null; access: (typeof routes)[number]['access']; children: ReactNode }) {
  const location = useLocation()
  if (!actor) {
    return <Navigate to={`/login?next=${encodeURIComponent(location.pathname + location.search)}`} replace />
  }
  if (!canAccess(actor, access)) {
    return <Forbidden />
  }
  return <>{children}</>
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
