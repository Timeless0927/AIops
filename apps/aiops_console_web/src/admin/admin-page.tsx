import { useQuery } from "@tanstack/react-query"
import { ChevronRightIcon } from "lucide-react"
import { Link, useSearchParams } from "react-router"

import {
  getAdminState,
  getConnectorAdminState,
  getResourceCatalog,
  type AdminState,
  type ConnectorAdminState,
  type ResourceCatalogState,
} from "@/admin/admin-client"
import { AdminActionProvider } from "@/admin/admin-action"
import { ClustersTab, ConnectorsTab } from "@/admin/admin-access"
import { CatalogSection } from "@/admin/admin-catalog"
import { BindingsTab, MembershipsTab, TeamsTab, UsersTab } from "@/admin/admin-identity"
import { KubernetesAuthoritiesAdmin } from "@/admin/kubernetes-authorities-admin"
import { MCPRegistryAdmin } from "@/admin/mcp-registry-admin"
import { ModelProviderAdmin } from "@/admin/model-provider-admin"
import { NotificationAdmin } from "@/admin/notification-admin"
import { SkillRegistryAdmin } from "@/admin/skill-registry-admin"
import { PageHeader } from "@/components/page-header"
import { DetailSkeleton } from "@/components/page-skeleton"
import { Card, CardContent } from "@/components/ui/card"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Button } from "@/components/ui/button"

type AdminTab = {readonly id: string; readonly label: string; readonly description: string}
type AdminNavItem = AdminTab & {readonly tabs: readonly AdminTab[]}

const adminNav: readonly AdminNavItem[] = [
  {id: "overview", label: "概览", description: "平台身份、接入与资源的关键状态一览。", tabs: []},
  {
    id: "identity",
    label: "身份与权限",
    description: "谁可以登录、归属哪个团队、拥有什么角色与变更权限。",
    tabs: [
      {id: "users", label: "用户", description: "管理 Console 人类身份与登录状态。"},
      {id: "teams", label: "团队", description: "维护 Service 的组织责任边界。"},
      {id: "memberships", label: "成员关系", description: "管理用户所属团队，不隐式授予操作权限。"},
      {id: "bindings", label: "角色绑定", description: "按平台或团队范围授予人类角色。"},
      {id: "kubernetes-authorities", label: "Kubernetes 变更权限", description: "显式管理环境与资源范围内的审批权限。"},
    ],
  },
  {
    id: "access",
    label: "集群接入",
    description: "Connector 注册、凭据轮换与 Cluster 治理策略。",
    tabs: [
      {id: "connectors", label: "Connector", description: "管理 Connector Enrollment、凭据和读取验证。"},
      {id: "clusters", label: "Cluster", description: "查看注册状态并维护 Cluster 治理策略。"},
    ],
  },
  {id: "catalog", label: "资源目录", description: "确认团队、Service 与真实部署目标的关系。", tabs: []},
  {
    id: "ai",
    label: "AI 能力",
    description: "Diagnosis 使用的模型、MCP 与 Skill。",
    tabs: [
      {id: "model", label: "模型", description: "管理 Diagnosis 使用的单一 Model Provider revision。"},
      {id: "mcp", label: "MCP", description: "治理 MCP Integration、能力快照和允许范围。"},
      {id: "skills", label: "Skill", description: "管理可审计的 Skill 版本与 MCP 依赖。"},
    ],
  },
  {id: "notifications", label: "通知", description: "管理 Destination、Route、Template 与噪声控制。", tabs: []},
]

// 旧 ?section= 深链（platform-status / resource-workspace 等页面在用）映射到新分区
const legacySectionRoutes: Record<string, AdminRoute> = {
  users: {section: "identity", tab: "users"},
  teams: {section: "identity", tab: "teams"},
  memberships: {section: "identity", tab: "memberships"},
  bindings: {section: "identity", tab: "bindings"},
  "kubernetes-authorities": {section: "identity", tab: "kubernetes-authorities"},
  connectors: {section: "access", tab: "connectors"},
  clusters: {section: "access", tab: "clusters"},
  catalog: {section: "catalog", tab: ""},
  model: {section: "ai", tab: "model"},
  mcp: {section: "ai", tab: "mcp"},
  skills: {section: "ai", tab: "skills"},
  notifications: {section: "notifications", tab: ""},
}

export type AdminRoute = {readonly section: string; readonly tab: string}

export function resolveAdminRoute(params: URLSearchParams): AdminRoute {
  const section = params.get("section") ?? ""
  if (section in legacySectionRoutes) return legacySectionRoutes[section]
  const nav = adminNav.find((item) => item.id === section)
  if (!nav) return {section: "overview", tab: ""}
  const tab = params.get("tab") ?? ""
  const resolvedTab = nav.tabs.some((item) => item.id === tab) ? tab : (nav.tabs[0]?.id ?? "")
  return {section: nav.id, tab: resolvedTab}
}

const panelClass = "animate-in fade-in slide-in-from-bottom-1 duration-200 motion-reduce:animate-none"

export function AdminPage() {
  const state = useQuery({queryKey: ["admin"], queryFn: getAdminState, retry: false})
  const connectorState = useQuery({queryKey: ["connectors"], queryFn: getConnectorAdminState, retry: false})
  const catalogState = useQuery({queryKey: ["resource-catalog"], queryFn: getResourceCatalog, retry: false})
  if (state.isPending || connectorState.isPending || catalogState.isPending) {
    return <DetailSkeleton />
  }
  if (state.isError || connectorState.isError || catalogState.isError) {
    return <main className="grid min-h-screen place-items-center text-sm text-destructive">无法读取平台管理数据</main>
  }
  return <AdminActionProvider><AdminPageReady data={state.data} connectorData={connectorState.data} catalogData={catalogState.data} /></AdminActionProvider>
}

function AdminPageReady({data, connectorData, catalogData}: {
  data: AdminState
  connectorData: ConnectorAdminState
  catalogData: ResourceCatalogState
}) {
  const [params, setParams] = useSearchParams()
  const route = resolveAdminRoute(params)
  const nav = adminNav.find((item) => item.id === route.section)!
  const activeTab = nav.tabs.find((item) => item.id === route.tab)
  const select = (section: string, tab = "") => setParams(tab ? {section, tab} : {section}, {replace: true})

  return (
    <main className="mx-auto flex max-w-[1500px] flex-col gap-6 px-4 py-6 lg:px-6">
      <PageHeader title="平台管理" description="身份、集群接入、资源目录与 AI 能力的治理入口。" />

      <div className="grid items-start gap-6 lg:grid-cols-[13rem_minmax(0,1fr)]">
        <div className="lg:hidden">
          <Select value={route.section} onValueChange={(value) => value && select(value)}>
            <SelectTrigger className="w-full" aria-label="选择管理分区"><SelectValue>{nav.label}</SelectValue></SelectTrigger>
            <SelectContent><SelectGroup>{adminNav.map((item) => <SelectItem key={item.id} value={item.id}>{item.label}</SelectItem>)}</SelectGroup></SelectContent>
          </Select>
        </div>
        <nav aria-label="管理分区" className="hidden lg:sticky lg:top-16 lg:flex lg:flex-col lg:gap-0.5">
          {adminNav.map((item) => (
            <Button
              key={item.id}
              type="button"
              size="sm"
              variant={route.section === item.id ? "secondary" : "ghost"}
              className="justify-start whitespace-nowrap"
              aria-current={route.section === item.id ? "true" : undefined}
              onClick={() => select(item.id)}
            >
              {item.label}
            </Button>
          ))}
        </nav>

        <div className="min-w-0">
          <div className="mb-5">
            <h2 className="text-xl font-semibold">{activeTab?.label ?? nav.label}</h2>
            <p className="mt-1 text-sm text-muted-foreground">{activeTab?.description ?? nav.description}</p>
          </div>

          {route.section === "overview" ? (
            <OverviewSection data={data} connectorData={connectorData} catalogData={catalogData} />
          ) : null}

          {route.section === "identity" ? (
            <Tabs value={route.tab} onValueChange={(tab) => select("identity", tab)}>
              <AdminTabsBar tabs={nav.tabs} />
              <TabsContent value="users" className={panelClass}><UsersTab users={data.users} roleBindings={data.role_bindings} /></TabsContent>
              <TabsContent value="teams" className={panelClass}><TeamsTab teams={data.teams} /></TabsContent>
              <TabsContent value="memberships" className={panelClass}><MembershipsTab users={data.users} teams={data.teams} memberships={data.team_memberships} /></TabsContent>
              <TabsContent value="bindings" className={panelClass}><BindingsTab users={data.users} teams={data.teams} bindings={data.role_bindings} /></TabsContent>
              <TabsContent value="kubernetes-authorities" className={panelClass}><KubernetesAuthoritiesAdmin users={data.users} clusters={connectorData.clusters} services={catalogData.services} /></TabsContent>
            </Tabs>
          ) : null}

          {route.section === "access" ? (
            <Tabs value={route.tab} onValueChange={(tab) => select("access", tab)}>
              <AdminTabsBar tabs={nav.tabs} />
              <TabsContent value="connectors" className={panelClass}><ConnectorsTab enrollments={connectorData.connector_enrollments} /></TabsContent>
              <TabsContent value="clusters" className={panelClass}><ClustersTab clusters={connectorData.clusters} /></TabsContent>
            </Tabs>
          ) : null}

          {route.section === "catalog" ? <CatalogSection teams={data.teams} catalog={catalogData} /> : null}

          {route.section === "ai" ? (
            <Tabs value={route.tab} onValueChange={(tab) => select("ai", tab)}>
              <AdminTabsBar tabs={nav.tabs} />
              <TabsContent value="model" className={panelClass}><ModelProviderAdmin /></TabsContent>
              <TabsContent value="mcp" className={panelClass}><MCPRegistryAdmin /></TabsContent>
              <TabsContent value="skills" className={panelClass}><SkillRegistryAdmin /></TabsContent>
            </Tabs>
          ) : null}

          {route.section === "notifications" ? <NotificationAdmin /> : null}
        </div>
      </div>
    </main>
  )
}

function AdminTabsBar({tabs}: {tabs: readonly AdminTab[]}) {
  return (
    <TabsList variant="line" className="mb-5 w-full justify-start overflow-x-auto">
      {tabs.map((tab) => <TabsTrigger key={tab.id} value={tab.id}>{tab.label}</TabsTrigger>)}
    </TabsList>
  )
}

function OverviewSection({data, connectorData, catalogData}: {
  data: AdminState
  connectorData: ConnectorAdminState
  catalogData: ResourceCatalogState
}) {
  const activeUsers = data.users.filter((user) => user.active).length
  const activeTeams = data.teams.filter((team) => team.active).length
  const enrollments = connectorData.connector_enrollments
  const onlineConnectors = enrollments.filter((item) => item.active && item.state === "online").length
  const failedVerifications = enrollments.filter((item) => item.read_verification === "failed").length
  const clusters = connectorData.clusters
  const onlineClusters = clusters.filter((cluster) => cluster.runtime_status === "online").length
  const offlineClusters = clusters.filter((cluster) => cluster.runtime_status !== "online").length
  const unbound = catalogData.discovery_candidates.filter((candidate) => !candidate.resource_binding_id).length

  const cards = [
    {
      label: "用户", metric: `${data.users.length}`,
      detail: `${activeUsers} 启用 · ${data.users.length - activeUsers} 停用`,
      to: "/admin?section=identity&tab=users", alert: false,
    },
    {
      label: "团队", metric: `${data.teams.length}`,
      detail: `${activeTeams} 启用`,
      to: "/admin?section=identity&tab=teams", alert: false,
    },
    {
      label: "Connector", metric: `${onlineConnectors}/${enrollments.length} 在线`,
      detail: failedVerifications > 0 ? `${failedVerifications} 个读取验证失败` : "读取验证全部通过",
      to: "/admin?section=access&tab=connectors", alert: failedVerifications > 0,
    },
    {
      label: "Cluster", metric: `${onlineClusters}/${clusters.length} 在线`,
      detail: offlineClusters > 0 ? `${offlineClusters} 个未在线` : "全部在线",
      to: "/admin?section=access&tab=clusters", alert: offlineClusters > 0,
    },
    {
      label: "Service", metric: `${catalogData.services.length}`,
      detail: "已登记的业务 Service",
      to: "/admin?section=catalog", alert: false,
    },
    {
      label: "待确认资源绑定", metric: `${unbound}`,
      detail: unbound > 0 ? "Discovery 候选需要确认归属" : "全部候选已确认",
      to: "/admin?section=catalog", alert: unbound > 0,
    },
  ]

  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
      {cards.map((card, index) => (
        <Link
          key={card.label}
          to={card.to}
          className="group rounded-xl outline-none animate-in fade-in slide-in-from-bottom-2 fill-mode-backwards motion-reduce:animate-none focus-visible:ring-3 focus-visible:ring-ring/50"
          style={{animationDelay: `${index * 40}ms`}}
        >
          <Card className="h-full transition-all duration-150 group-hover:-translate-y-0.5 group-hover:shadow-md motion-reduce:transition-none motion-reduce:group-hover:translate-y-0">
            <CardContent className="flex h-full flex-col gap-2">
              <div className="flex items-center justify-between">
                <span className="flex items-center gap-2 text-sm text-muted-foreground">
                  {card.alert ? <span className="size-1.5 animate-pulse rounded-full bg-warning motion-reduce:animate-none" /> : null}
                  {card.label}
                </span>
                <ChevronRightIcon className="size-4 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100 motion-reduce:transition-none" />
              </div>
              <p className="text-2xl font-semibold tabular-nums">{card.metric}</p>
              <p className="text-sm text-muted-foreground">{card.detail}</p>
            </CardContent>
          </Card>
        </Link>
      ))}
    </div>
  )
}
