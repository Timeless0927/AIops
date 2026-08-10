import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { KeyRoundIcon, LinkIcon, PlusIcon, RefreshCwIcon } from "lucide-react"
import { useSearchParams } from "react-router"

import {
  type AdminMutation,
  getAdminState,
  getConnectorAdminState,
  getResourceCatalog,
  mutateAdmin,
  type Cluster,
  type AdminTeam,
  type AdminUser,
} from "@/admin/admin-client"
import { AdminActionProvider, useAdminAction } from "@/admin/admin-action"
import { DetailSkeleton } from "@/components/page-skeleton"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { NotificationAdmin } from "@/admin/notification-admin"
import { KubernetesAuthoritiesAdmin } from "@/admin/kubernetes-authorities-admin"
import { ModelProviderAdmin } from "@/admin/model-provider-admin"
import { MCPRegistryAdmin } from "@/admin/mcp-registry-admin"
import { SkillRegistryAdmin } from "@/admin/skill-registry-admin"
import { AdminPicker as Picker } from "@/admin/admin-picker"

export function adminDefaultSection(params: URLSearchParams) {
  const section = params.get("section")
  return section && adminSections.some((item) => item.id === section)
    ? section : "users"
}

const adminSectionGroups = [
  {
    label: "身份与权限",
    items: [
      {id: "users", label: "用户", description: "管理 Console 人类身份与登录状态。"},
      {id: "teams", label: "团队", description: "维护 Service 的组织责任边界。"},
      {id: "memberships", label: "成员关系", description: "管理用户所属团队，不隐式授予操作权限。"},
      {id: "bindings", label: "角色绑定", description: "按平台或团队范围授予人类角色。"},
      {id: "kubernetes-authorities", label: "Kubernetes 变更权限", description: "显式管理环境与资源范围内的审批权限。"},
    ],
  },
  {
    label: "资源接入",
    items: [
      {id: "connectors", label: "Connector", description: "管理 Connector Enrollment、凭据和读取验证。"},
      {id: "clusters", label: "Cluster", description: "查看注册状态并维护 Cluster 治理策略。"},
      {id: "catalog", label: "资源目录", description: "确认团队、Service 与真实部署目标关系。"},
    ],
  },
  {
    label: "AI 能力",
    items: [
      {id: "model", label: "模型", description: "管理 Diagnosis 使用的单一 Model Provider revision。"},
      {id: "mcp", label: "MCP", description: "治理 MCP Integration、能力快照和允许范围。"},
      {id: "skills", label: "Skill", description: "管理可审计的 Skill 版本与 MCP 依赖。"},
    ],
  },
  {
    label: "通知",
    items: [
      {id: "notifications", label: "通知", description: "管理 Destination、Route、Template 与噪声控制。"},
    ],
  },
] as const

type AdminSection = {readonly id: string; readonly label: string; readonly description: string}
const adminSections = adminSectionGroups.flatMap<AdminSection>((group) => group.items)

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
  data: Awaited<ReturnType<typeof getAdminState>>
  connectorData: Awaited<ReturnType<typeof getConnectorAdminState>>
  catalogData: Awaited<ReturnType<typeof getResourceCatalog>>
}) {
  const [params, setParams] = useSearchParams()
  const queryClient = useQueryClient()
  const requestAction = useAdminAction()
  const [issuedCredential, setIssuedCredential] = useState("")
  const [membershipUser, setMembershipUser] = useState("")
  const [membershipTeam, setMembershipTeam] = useState("")
  const [bindingUser, setBindingUser] = useState("")
  const [bindingTeam, setBindingTeam] = useState("")
  const [bindingRole, setBindingRole] = useState<"sre" | "platform_administrator">("sre")
  const [serviceTeam, setServiceTeam] = useState("")
  const [bindingService, setBindingService] = useState("")
  const mutation = useMutation({
    mutationFn: mutateAdmin,
    onSuccess: (result) => {
      setIssuedCredential(result.credential ?? "")
      queryClient.invalidateQueries({queryKey: ["admin"]})
      queryClient.invalidateQueries({queryKey: ["connectors"]})
      queryClient.invalidateQueries({queryKey: ["resource-catalog"]})
    },
  })
  const section = adminDefaultSection(params)
  const currentSection = adminSections.find((item) => item.id === section)!
  const submit = (change: AdminMutation) => {
    const details = adminMutationDetails(change)
    requestAction({
      ...details,
      run: (reason) => mutation.mutateAsync({...change, body: {...change.body, reason}} as AdminMutation),
    })
  }

  return (
      <main className="mx-auto flex max-w-[1500px] flex-col gap-6 px-4 py-6 lg:px-6">
        <div className="border-b pb-5"><h1 className="text-2xl font-semibold">平台管理</h1></div>
        {issuedCredential ? (
          <Alert>
            <KeyRoundIcon />
            <AlertTitle>Connector credential</AlertTitle>
            <AlertDescription><Input value={issuedCredential} readOnly aria-label="新 Connector credential" /></AlertDescription>
          </Alert>
        ) : null}

      <div className="grid items-start gap-6 lg:grid-cols-[13rem_minmax(0,1fr)]">
        <div className="lg:hidden">
          <Select value={section} onValueChange={(value) => value && setParams({section: value}, {replace: true})}>
            <SelectTrigger className="w-full" aria-label="选择管理分区"><SelectValue>{currentSection.label}</SelectValue></SelectTrigger>
            <SelectContent>{adminSectionGroups.map((group) => <SelectGroup key={group.label}>{group.items.map((item) => <SelectItem key={item.id} value={item.id}>{group.label} · {item.label}</SelectItem>)}</SelectGroup>)}</SelectContent>
          </Select>
        </div>
        <nav aria-label="管理分区" className="hidden lg:sticky lg:top-16 lg:flex lg:flex-col lg:gap-5">
          {adminSectionGroups.map((group) => (
            <div key={group.label} className="flex gap-1 lg:flex-col lg:gap-0.5">
              <p className="sr-only lg:not-sr-only lg:px-2 lg:pb-1 lg:text-xs lg:font-medium lg:text-muted-foreground">{group.label}</p>
              {group.items.map((item) => (
                <Button
                  key={item.id}
                  type="button"
                  size="sm"
                  variant={section === item.id ? "secondary" : "ghost"}
                  className="shrink-0 justify-start whitespace-nowrap"
                  aria-current={section === item.id ? "true" : undefined}
                  onClick={() => setParams({section: item.id}, {replace: true})}
                >
                  {item.label}
                </Button>
              ))}
            </div>
          ))}
        </nav>

        <div className="min-w-0">
          <div className="mb-5 border-b pb-4"><h2 className="text-xl font-semibold">{currentSection.label}</h2><p className="mt-1 text-sm text-muted-foreground">{currentSection.description}</p></div>
          {section === "users" ? <div className="flex flex-col gap-5">
            <AdminFormAccordion value="create-user" title="新建用户" description="创建可登录 Console 的人类身份">
              <form
              onSubmit={(event) => {
                event.preventDefault()
                const form = new FormData(event.currentTarget)
                submit({
                  resource: "users",
                  body: {
                    username: String(form.get("username") ?? ""),
                    display_name: String(form.get("display_name") ?? ""),
                    password: String(form.get("password") ?? ""),
                    reason: "",
                  },
                })
              }}
            >
              <FieldGroup className="grid gap-3 md:grid-cols-4">
                <Field><FieldLabel htmlFor="username">用户名</FieldLabel><Input id="username" name="username" required /></Field>
                <Field><FieldLabel htmlFor="display-name">显示名称</FieldLabel><Input id="display-name" name="display_name" required /></Field>
                <Field><FieldLabel htmlFor="user-password">初始密码</FieldLabel><Input id="user-password" name="password" type="password" autoComplete="new-password" required /></Field>
                <div className="flex items-end"><Button type="submit" disabled={mutation.isPending}><PlusIcon data-icon="inline-start" />创建用户</Button></div>
              </FieldGroup>
              </form>
            </AdminFormAccordion>
            <ResourceTable
              headings={["用户", "状态", "角色绑定", "操作"]}
              rows={data.users.map((user) => [
                <div key="identity"><div className="font-medium">{user.display_name}</div><div className="text-xs text-muted-foreground">{user.username}</div></div>,
                <Status key="status" active={user.active} />,
                <div key="roles" className="flex gap-1">{data.role_bindings.filter((binding) => binding.user_id === user.id && binding.active).map((binding) => <Badge key={binding.id} variant="outline">{binding.role === "platform_administrator" ? "平台管理员" : "SRE"}</Badge>)}</div>,
                <ToggleButton key="action" active={user.active} disabled={mutation.isPending} onClick={() => submit({resource: "users", id: user.id, body: {active: !user.active, reason: ""}})} />,
              ])}
            />
          </div> : null}

          {section === "teams" ? <div className="flex flex-col gap-5">
            <form onSubmit={(event) => { event.preventDefault(); const form = new FormData(event.currentTarget); submit({resource: "teams", body: {name: String(form.get("name") ?? ""), description: String(form.get("description") ?? ""), reason: ""}}) }}>
              <FieldGroup className="grid gap-3 md:grid-cols-[1fr_2fr_auto]">
                <Field><FieldLabel htmlFor="team-name">团队名称</FieldLabel><Input id="team-name" name="name" required /></Field>
                <Field><FieldLabel htmlFor="team-description">说明</FieldLabel><Input id="team-description" name="description" /></Field>
                <div className="flex items-end"><Button type="submit" disabled={mutation.isPending}><PlusIcon data-icon="inline-start" />创建团队</Button></div>
              </FieldGroup>
            </form>
            <ResourceTable headings={["团队", "说明", "状态", "操作"]} rows={data.teams.map((team) => [<span key="name" className="font-medium">{team.name}</span>, <span key="description" className="text-muted-foreground">{team.description || "-"}</span>, <Status key="status" active={team.active} />, <ToggleButton key="action" active={team.active} disabled={mutation.isPending} onClick={() => submit({resource: "teams", id: team.id, body: {active: !team.active, reason: ""}})} />])} />
          </div> : null}

          {section === "memberships" ? <div className="flex flex-col gap-5">
            <div className="grid gap-3 md:grid-cols-[1fr_1fr_auto]">
              <Picker label="用户" value={membershipUser} onValueChange={setMembershipUser} items={data.users.filter((user) => user.active).map((user) => ({value: user.id, label: user.display_name}))} />
              <Picker label="团队" value={membershipTeam} onValueChange={setMembershipTeam} items={data.teams.filter((team) => team.active).map((team) => ({value: team.id, label: team.name}))} />
              <div className="flex items-end"><Button disabled={!membershipUser || !membershipTeam || mutation.isPending} onClick={() => submit({resource: "team-memberships", body: {user_id: membershipUser, team_id: membershipTeam, reason: ""}})}><PlusIcon data-icon="inline-start" />添加成员</Button></div>
            </div>
            <ResourceTable headings={["用户", "团队", "状态", "操作"]} rows={data.team_memberships.map((membership) => [<span key="user">{userName(data.users, membership.user_id)}</span>, <span key="team">{teamName(data.teams, membership.team_id)}</span>, <Status key="status" active={membership.active} />, <ToggleButton key="action" active={membership.active} disabled={mutation.isPending} onClick={() => submit({resource: "team-memberships", id: membership.id, body: {active: !membership.active, reason: ""}})} />])} />
          </div> : null}

          {section === "bindings" ? <div className="flex flex-col gap-5">
            <div className="grid gap-3 md:grid-cols-[1fr_1fr_1fr_auto]">
              <Picker label="用户" value={bindingUser} onValueChange={setBindingUser} items={data.users.filter((user) => user.active).map((user) => ({value: user.id, label: user.display_name}))} />
              <Picker label="角色" value={bindingRole} onValueChange={(value) => setBindingRole(value as typeof bindingRole)} items={[{value: "sre", label: "SRE"}, {value: "platform_administrator", label: "平台管理员"}]} />
              <Picker label="团队范围" value={bindingTeam} onValueChange={setBindingTeam} disabled={bindingRole === "platform_administrator"} items={data.teams.filter((team) => team.active).map((team) => ({value: team.id, label: team.name}))} />
              <div className="flex items-end"><Button disabled={!bindingUser || (bindingRole === "sre" && !bindingTeam) || mutation.isPending} onClick={() => bindingRole === "sre" ? submit({resource: "role-bindings", body: {user_id: bindingUser, role: "sre", scope_type: "team", scope_id: bindingTeam, reason: ""}}) : submit({resource: "role-bindings", body: {user_id: bindingUser, role: "platform_administrator", scope_type: "platform", reason: ""}})}><PlusIcon data-icon="inline-start" />添加绑定</Button></div>
            </div>
            <ResourceTable headings={["用户", "角色", "范围", "状态", "操作"]} rows={data.role_bindings.map((binding) => [<span key="user">{userName(data.users, binding.user_id)}</span>, <span key="role">{binding.role === "platform_administrator" ? "平台管理员" : "SRE"}</span>, <span key="scope">{binding.scope_type === "platform" ? "平台" : teamName(data.teams, binding.scope_id ?? "")}</span>, <Status key="status" active={binding.active} />, <ToggleButton key="action" active={binding.active} disabled={mutation.isPending} onClick={() => submit({resource: "role-bindings", id: binding.id, body: {active: !binding.active, reason: ""}})} />])} />
          </div> : null}

          {section === "kubernetes-authorities" ? <div>
            <KubernetesAuthoritiesAdmin
              users={data.users}
              clusters={connectorData.clusters}
              services={catalogData.services}
            />
          </div> : null}

          {section === "connectors" ? <div className="flex flex-col gap-5">
            <AdminFormAccordion value="create-connector-enrollment" title="创建 Connector Enrollment" description="预授权一个 Connector 与 Cluster 的注册绑定">
              <form onSubmit={(event) => { event.preventDefault(); const form = new FormData(event.currentTarget); submit({resource: "connector-enrollments", body: {connector_id: String(form.get("connector_id") ?? ""), cluster_id: String(form.get("cluster_id") ?? ""), expected_revision: null, reason: ""}}) }}>
              <FieldGroup className="grid gap-3 md:grid-cols-[1fr_1fr_auto]">
                <Field><FieldLabel htmlFor="connector-id">Connector ID</FieldLabel><Input id="connector-id" name="connector_id" required /></Field>
                <Field><FieldLabel htmlFor="cluster-id">Cluster ID</FieldLabel><Input id="cluster-id" name="cluster_id" required /></Field>
                <div className="flex items-end"><Button type="submit" disabled={mutation.isPending}><PlusIcon data-icon="inline-start" />创建 Enrollment</Button></div>
              </FieldGroup>
              </form>
            </AdminFormAccordion>
            <ResourceTable headings={["Connector", "Cluster", "连接状态", "Read verification", "操作"]} rows={connectorData.connector_enrollments.map((enrollment) => [
              <span key="connector" className="font-medium">{enrollment.connector_id}</span>,
              <span key="cluster">{enrollment.cluster_id}</span>,
              <Badge key="status" variant={enrollment.state === "online" ? "positive" : enrollment.state === "rotation_pending" ? "warning" : "secondary"}>{enrollment.state}</Badge>,
              <Badge key="verification" variant={enrollment.read_verification === "verified" ? "positive" : enrollment.read_verification === "failed" ? "destructive" : "secondary"}>{enrollment.read_verification}</Badge>,
              <div key="actions" className="flex flex-wrap gap-2">
                <Button type="button" size="sm" variant="outline" disabled={!enrollment.active || enrollment.state === "rotation_pending" || mutation.isPending} onClick={() => submit({resource: "connector-enrollments", id: enrollment.id, body: {rotate_credential: true, reason: ""}})}><RefreshCwIcon />轮换</Button>
                {enrollment.read_verification === "failed" ? <Button type="button" size="sm" variant="outline" disabled={mutation.isPending} onClick={() => submit({resource: "connector-enrollments", id: enrollment.id, body: {retry_read_verification: true, reason: ""}})}>重试验证</Button> : null}
                <ToggleButton active={enrollment.active} disabled={mutation.isPending} onClick={() => submit({resource: "connector-enrollments", id: enrollment.id, body: {active: !enrollment.active, reason: ""}})} />
              </div>,
            ])} />
          </div> : null}

          {section === "clusters" ? <div className="flex flex-col gap-4">
            {connectorData.clusters.map((cluster) => <ClusterEditor key={cluster.cluster_id} cluster={cluster} pending={mutation.isPending} submit={submit} />)}
            {connectorData.clusters.length === 0 ? <div className="border-y py-10 text-center text-sm text-muted-foreground">暂无已注册 Cluster</div> : null}
          </div> : null}

          {section === "catalog" ? <div className="flex flex-col gap-6">
            <AdminFormAccordion value="create-service" title="创建 Service" description="登记由团队负责的业务 Service">
              <form onSubmit={(event) => { event.preventDefault(); const form = new FormData(event.currentTarget); submit({resource: "services", body: {team_id: serviceTeam, name: String(form.get("name") ?? ""), description: String(form.get("description") ?? ""), reason: ""}}) }}>
              <FieldGroup className="grid gap-3 md:grid-cols-[1fr_1fr_2fr_auto]">
                <Picker label="责任团队" value={serviceTeam} onValueChange={setServiceTeam} items={data.teams.filter((team) => team.active).map((team) => ({value: team.id, label: team.name}))} />
                <Field><FieldLabel htmlFor="catalog-service-name">Service 名称</FieldLabel><Input id="catalog-service-name" name="name" required /></Field>
                <Field><FieldLabel htmlFor="catalog-service-description">说明</FieldLabel><Input id="catalog-service-description" name="description" /></Field>
                <div className="flex items-end"><Button type="submit" disabled={!serviceTeam || mutation.isPending}><PlusIcon data-icon="inline-start" />创建 Service</Button></div>
              </FieldGroup>
              </form>
            </AdminFormAccordion>
            <ResourceTable headings={["Service", "责任团队", "说明"]} rows={catalogData.services.map((service) => [<span key="name" className="font-medium">{service.name}</span>, <span key="team">{teamName(data.teams, service.team_id)}</span>, <span key="description" className="text-muted-foreground">{service.description || "-"}</span>])} />
            <div className="max-w-md">
              <Picker label="确认或纠正为" value={bindingService} onValueChange={setBindingService} items={catalogData.services.filter((service) => service.active).map((service) => ({value: service.id, label: `${service.name} · ${teamName(data.teams, service.team_id)}`}))} />
            </div>
            <ResourceTable headings={["Discovery Candidate", "实际 Service / label hint", "归属状态", "操作"]} rows={catalogData.discovery_candidates.map((candidate) => {
              const binding = candidate.resource_binding_id ? catalogData.resource_bindings.find((item) => item.id === candidate.resource_binding_id) : undefined
              const service = binding ? catalogData.services.find((item) => item.id === binding.service_id) : undefined
              return [
                <div key="target"><div className="font-medium">{candidate.workload_kind}/{candidate.workload_name}</div><div className="text-xs text-muted-foreground">{candidate.cluster_id} · {candidate.namespace}</div></div>,
                <div key="hints"><div>{candidate.service_name || "无匹配 Kubernetes Service"}</div><div className="text-xs text-muted-foreground">hint: {candidate.service_hint || "-"} / {candidate.team_hint || "-"}</div></div>,
                binding ? <div key="bound"><Badge variant="positive">已确认</Badge><div className="mt-1 text-xs text-muted-foreground">{service?.name ?? binding.service_id} · rev {binding.revision}</div></div> : <Badge key="unbound" variant="secondary">未绑定</Badge>,
                <Button key="action" type="button" size="sm" variant="outline" disabled={!bindingService || binding?.service_id === bindingService || mutation.isPending} onClick={() => binding ? submit({resource: "resource-bindings", id: binding.id, body: {service_id: bindingService, reason: ""}}) : submit({resource: "resource-bindings", body: {candidate_id: candidate.id, service_id: bindingService, reason: ""}})}><LinkIcon />{binding ? "纠正" : "确认"}</Button>,
              ]
            })} />
          </div> : null}

          {section === "model" ? <ModelProviderAdmin /> : null}
          {section === "mcp" ? <MCPRegistryAdmin /> : null}
          {section === "skills" ? <SkillRegistryAdmin /> : null}
          {section === "notifications" ? <NotificationAdmin /> : null}
        </div>
      </div>
      </main>
  )
}

function ClusterEditor({cluster, pending, submit}: {cluster: Cluster; pending: boolean; submit: (change: AdminMutation) => void}) {
  const [environment, setEnvironment] = useState<Cluster["environment"]>(cluster.environment)
  const [mutationEnabled, setMutationEnabled] = useState(cluster.mutation_enabled)
  return <article className="border-b pb-4"><div className="mb-3 flex flex-wrap items-center gap-2 text-sm">
      <Badge variant={cluster.runtime_status === "online" ? "positive" : cluster.runtime_status === "degraded" ? "warning" : "secondary"}>{cluster.runtime_status}</Badge>
      <span className="text-muted-foreground">心跳 {new Date(cluster.last_heartbeat * 1000).toLocaleString()}</span>
      <Badge variant={cluster.pending_read_commands ? "warning" : "outline"}>待处理 read {cluster.pending_read_commands ?? 0}</Badge>
      <span className="text-muted-foreground">最新 read {cluster.last_read_command ? `${cluster.last_read_command.status} · ${cluster.last_read_command.namespace}` : "暂无"}</span>
      <span className="text-muted-foreground">最后结果 {cluster.last_read_result ? `${cluster.last_read_result.status} · ${cluster.last_read_result.namespace}${cluster.last_read_result.error_code ? ` · ${cluster.last_read_result.error_code}` : ""}` : "暂无"}</span>
    </div><AdminFormAccordion value={`edit-${cluster.cluster_id}`} title={`编辑 ${cluster.display_name}`} description={cluster.cluster_id}>
    <form className="grid gap-3 lg:grid-cols-[1fr_160px_2fr_auto_auto]" onSubmit={(event) => { event.preventDefault(); const form = new FormData(event.currentTarget); submit({resource: "clusters", id: cluster.cluster_id, body: {display_name: String(form.get("display_name") ?? ""), environment, governance_notes: String(form.get("governance_notes") ?? ""), mutation_enabled: mutationEnabled, reason: ""}}) }}>
    <Field><FieldLabel htmlFor={`cluster-name-${cluster.cluster_id}`}>{cluster.cluster_id}</FieldLabel><Input id={`cluster-name-${cluster.cluster_id}`} name="display_name" defaultValue={cluster.display_name} required /></Field>
    <Field><FieldLabel htmlFor={`cluster-env-${cluster.cluster_id}`}>Environment</FieldLabel><Select value={environment} onValueChange={(value) => setEnvironment(value as Cluster["environment"])}><SelectTrigger id={`cluster-env-${cluster.cluster_id}`} className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{["prod", "staging", "dev", "test"].map((value) => <SelectItem key={value} value={value}>{value}</SelectItem>)}</SelectGroup></SelectContent></Select></Field>
    <Field><FieldLabel htmlFor={`cluster-notes-${cluster.cluster_id}`}>治理备注</FieldLabel><Input id={`cluster-notes-${cluster.cluster_id}`} name="governance_notes" defaultValue={cluster.governance_notes} /></Field>
    <label className="flex items-center gap-2 self-end pb-2 text-sm"><Checkbox checked={mutationEnabled} onCheckedChange={setMutationEnabled} />允许 mutation</label>
    <div className="flex items-end"><Button type="submit" disabled={pending}>保存</Button></div>
    </form></AdminFormAccordion></article>
}

function AdminFormAccordion({value, title, description, children}: {value: string; title: string; description: string; children: React.ReactNode}) {
  return <Accordion><AccordionItem value={value}><AccordionTrigger><span><span className="block font-medium">{title}</span><span className="mt-1 block text-xs font-normal text-muted-foreground">{description}</span></span></AccordionTrigger><AccordionContent>{children}</AccordionContent></AccordionItem></Accordion>
}

function ResourceTable({headings, rows}: {headings: string[]; rows: React.ReactNode[][]}) {
  return <div className="overflow-x-auto rounded-xl bg-card ring-1 ring-foreground/10"><Table><TableHeader><TableRow>{headings.map((heading) => <TableHead key={heading}>{heading}</TableHead>)}</TableRow></TableHeader><TableBody>{rows.map((cells, index) => <TableRow key={index}>{cells.map((cell, cellIndex) => <TableCell key={cellIndex}>{cell}</TableCell>)}</TableRow>)}</TableBody></Table></div>
}

function Status({active}: {active: boolean}) {
  return <Badge variant={active ? "positive" : "secondary"}>{active ? "启用" : "停用"}</Badge>
}

function ToggleButton({active, disabled, onClick}: {active: boolean; disabled: boolean; onClick: () => void}) {
  return <Button type="button" size="sm" variant={active ? "destructive" : "outline"} disabled={disabled} onClick={onClick}>{active ? "停用" : "启用"}</Button>
}

function userName(users: AdminUser[], id: string) {
  return users.find((user) => user.id === id)?.display_name ?? id
}

function teamName(teams: AdminTeam[], id: string) {
  return teams.find((team) => team.id === id)?.name ?? id
}

const adminResourceLabels: Record<AdminMutation["resource"], string> = {
  users: "用户",
  teams: "团队",
  "team-memberships": "成员关系",
  "role-bindings": "角色绑定",
  "connector-enrollments": "Connector Enrollment",
  clusters: "Cluster 治理配置",
  services: "Service",
  "resource-bindings": "资源绑定",
}

function adminMutationDetails(change: AdminMutation) {
  const label = adminResourceLabels[change.resource]
  const target = change.id ? ` ${change.id}` : ""
  const body = change.body as Record<string, unknown>
  if (body.active === false) {
    return {
      title: `停用${label}`,
      summary: `将停用${label}${target}，依赖此项的现有平台能力可能不可用。`,
      destructive: true,
    }
  }
  if (body.rotate_credential) {
    return {
      title: "轮换 Connector credential",
      summary: `将为 Connector Enrollment${target} 启动凭据轮换，Connector 必须切换到新凭据。`,
      destructive: true,
    }
  }
  const verb = change.id ? "更新" : "创建"
  return {title: `${verb}${label}`, summary: `将${verb}${label}${target}。`}
}
