import { useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { KeyRoundIcon, LinkIcon, PlusIcon, RefreshCwIcon, ShieldAlertIcon } from "lucide-react"
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
} from "@/api/client"
import { ApiError } from "@/api/transport"
import { reauthenticate } from "@/auth/auth-client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { NotificationAdmin } from "@/admin/notification-admin"
import { KubernetesAuthoritiesAdmin } from "@/admin/kubernetes-authorities-admin"
import { ModelProviderAdmin } from "@/admin/model-provider-admin"
import { MCPRegistryAdmin } from "@/admin/mcp-registry-admin"
import { SkillRegistryAdmin } from "@/admin/skill-registry-admin"
import { AdminPicker as Picker } from "@/admin/admin-picker"

export function adminDefaultSection(params: URLSearchParams) {
  const section = params.get("section")
  return section && ["catalog", "connectors", "model", "mcp", "skills", "notifications"].includes(section)
    ? section : "users"
}

export function AdminPage() {
  const [params] = useSearchParams()
  const queryClient = useQueryClient()
  const state = useQuery({queryKey: ["admin"], queryFn: getAdminState, retry: false})
  const connectorState = useQuery({queryKey: ["connectors"], queryFn: getConnectorAdminState, retry: false})
  const catalogState = useQuery({queryKey: ["resource-catalog"], queryFn: getResourceCatalog, retry: false})
  const [issuedCredential, setIssuedCredential] = useState("")
  const [reason, setReason] = useState("")
  const [membershipUser, setMembershipUser] = useState("")
  const [membershipTeam, setMembershipTeam] = useState("")
  const [bindingUser, setBindingUser] = useState("")
  const [bindingTeam, setBindingTeam] = useState("")
  const [bindingRole, setBindingRole] = useState<"sre" | "platform_administrator">("sre")
  const [serviceTeam, setServiceTeam] = useState("")
  const [bindingService, setBindingService] = useState("")
  const reasonInput = useRef<HTMLInputElement>(null)
  const mutation = useMutation({
    mutationFn: mutateAdmin,
    onSuccess: (result) => {
      setIssuedCredential(result.credential ?? "")
      queryClient.invalidateQueries({queryKey: ["admin"]})
      queryClient.invalidateQueries({queryKey: ["connectors"]})
      queryClient.invalidateQueries({queryKey: ["resource-catalog"]})
    },
  })
  const reauth = useMutation({mutationFn: reauthenticate, onSuccess: () => mutation.reset()})

  if (state.isPending || connectorState.isPending || catalogState.isPending) {
    return <main className="grid min-h-screen place-items-center text-sm text-muted-foreground" role="status">正在加载管理数据</main>
  }
  if (state.isError || connectorState.isError || catalogState.isError) {
    return <main className="grid min-h-screen place-items-center text-sm text-destructive">无法读取平台管理数据</main>
  }

  const data = state.data
  const connectorData = connectorState.data
  const catalogData = catalogState.data
  const error = mutation.error instanceof ApiError ? mutation.error : reauth.error instanceof ApiError ? reauth.error : null
  const submit = (change: AdminMutation) => {
    if (!reason.trim()) {
      reasonInput.current?.setCustomValidity("请填写变更原因")
      reasonInput.current?.reportValidity()
      return
    }
    mutation.mutate(change)
  }

  return (
      <main className="mx-auto flex max-w-[1500px] flex-col gap-6 px-4 py-6 lg:px-6">
        <div className="flex flex-col gap-4 border-b pb-5 lg:flex-row lg:items-end">
          <div className="min-w-0 flex-1">
            <h1 className="text-2xl font-semibold">平台管理</h1>
            <p className="mt-1 text-sm text-muted-foreground">身份、团队与当前授权关系</p>
          </div>
          <Field className="max-w-md">
            <FieldLabel htmlFor="change-reason">变更原因</FieldLabel>
            <Input ref={reasonInput} id="change-reason" value={reason} onChange={(event) => { event.currentTarget.setCustomValidity(""); setReason(event.target.value) }} required />
          </Field>
          <form
            className="flex items-end gap-2"
            onSubmit={(event) => {
              event.preventDefault()
              const password = new FormData(event.currentTarget).get("password")?.toString() ?? ""
              if (password) reauth.mutate(password)
            }}
          >
            <Field>
              <FieldLabel htmlFor="reauth-password">重新认证</FieldLabel>
              <Input id="reauth-password" name="password" type="password" autoComplete="current-password" required />
            </Field>
            <Button type="submit" variant="outline" disabled={reauth.isPending}>
              <KeyRoundIcon data-icon="inline-start" />
              验证
            </Button>
          </form>
        </div>

        {error ? (
          <Alert variant="destructive">
            <ShieldAlertIcon />
            <AlertTitle>{error.code === "fresh_auth_required" ? "需要重新认证" : "变更未保存"}</AlertTitle>
            <AlertDescription>{error.message}</AlertDescription>
          </Alert>
        ) : null}
        {issuedCredential ? (
          <Alert>
            <KeyRoundIcon />
            <AlertTitle>Connector credential</AlertTitle>
            <AlertDescription><Input value={issuedCredential} readOnly aria-label="新 Connector credential" /></AlertDescription>
          </Alert>
        ) : null}

      <Tabs defaultValue={adminDefaultSection(params)}>
          <TabsList variant="line" className="max-w-full overflow-x-auto">
            <TabsTrigger value="users">用户</TabsTrigger>
            <TabsTrigger value="teams">团队</TabsTrigger>
            <TabsTrigger value="memberships">成员关系</TabsTrigger>
            <TabsTrigger value="bindings">角色绑定</TabsTrigger>
            <TabsTrigger value="kubernetes-authorities">Kubernetes 变更权限</TabsTrigger>
            <TabsTrigger value="connectors">Connector</TabsTrigger>
            <TabsTrigger value="clusters">Cluster</TabsTrigger>
            <TabsTrigger value="catalog">资源目录</TabsTrigger>
            <TabsTrigger value="model">模型</TabsTrigger>
            <TabsTrigger value="mcp">MCP</TabsTrigger>
            <TabsTrigger value="skills">Skill</TabsTrigger>
            <TabsTrigger value="notifications">通知</TabsTrigger>
          </TabsList>

          <TabsContent value="users" className="flex flex-col gap-5 pt-4">
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
                    reason,
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
            <ResourceTable
              headings={["用户", "状态", "角色绑定", "操作"]}
              rows={data.users.map((user) => [
                <div key="identity"><div className="font-medium">{user.display_name}</div><div className="text-xs text-muted-foreground">{user.username}</div></div>,
                <Status key="status" active={user.active} />,
                <div key="roles" className="flex gap-1">{data.role_bindings.filter((binding) => binding.user_id === user.id && binding.active).map((binding) => <Badge key={binding.id} variant="outline">{binding.role === "platform_administrator" ? "平台管理员" : "SRE"}</Badge>)}</div>,
                <ToggleButton key="action" active={user.active} disabled={mutation.isPending} onClick={() => submit({resource: "users", id: user.id, body: {active: !user.active, reason}})} />,
              ])}
            />
          </TabsContent>

          <TabsContent value="teams" className="flex flex-col gap-5 pt-4">
            <form onSubmit={(event) => { event.preventDefault(); const form = new FormData(event.currentTarget); submit({resource: "teams", body: {name: String(form.get("name") ?? ""), description: String(form.get("description") ?? ""), reason}}) }}>
              <FieldGroup className="grid gap-3 md:grid-cols-[1fr_2fr_auto]">
                <Field><FieldLabel htmlFor="team-name">团队名称</FieldLabel><Input id="team-name" name="name" required /></Field>
                <Field><FieldLabel htmlFor="team-description">说明</FieldLabel><Input id="team-description" name="description" /></Field>
                <div className="flex items-end"><Button type="submit" disabled={mutation.isPending}><PlusIcon data-icon="inline-start" />创建团队</Button></div>
              </FieldGroup>
            </form>
            <ResourceTable headings={["团队", "说明", "状态", "操作"]} rows={data.teams.map((team) => [<span key="name" className="font-medium">{team.name}</span>, <span key="description" className="text-muted-foreground">{team.description || "-"}</span>, <Status key="status" active={team.active} />, <ToggleButton key="action" active={team.active} disabled={mutation.isPending} onClick={() => submit({resource: "teams", id: team.id, body: {active: !team.active, reason}})} />])} />
          </TabsContent>

          <TabsContent value="memberships" className="flex flex-col gap-5 pt-4">
            <div className="grid gap-3 md:grid-cols-[1fr_1fr_auto]">
              <Picker label="用户" value={membershipUser} onValueChange={setMembershipUser} items={data.users.filter((user) => user.active).map((user) => ({value: user.id, label: user.display_name}))} />
              <Picker label="团队" value={membershipTeam} onValueChange={setMembershipTeam} items={data.teams.filter((team) => team.active).map((team) => ({value: team.id, label: team.name}))} />
              <div className="flex items-end"><Button disabled={!membershipUser || !membershipTeam || mutation.isPending} onClick={() => submit({resource: "team-memberships", body: {user_id: membershipUser, team_id: membershipTeam, reason}})}><PlusIcon data-icon="inline-start" />添加成员</Button></div>
            </div>
            <ResourceTable headings={["用户", "团队", "状态", "操作"]} rows={data.team_memberships.map((membership) => [<span key="user">{userName(data.users, membership.user_id)}</span>, <span key="team">{teamName(data.teams, membership.team_id)}</span>, <Status key="status" active={membership.active} />, <ToggleButton key="action" active={membership.active} disabled={mutation.isPending} onClick={() => submit({resource: "team-memberships", id: membership.id, body: {active: !membership.active, reason}})} />])} />
          </TabsContent>

          <TabsContent value="bindings" className="flex flex-col gap-5 pt-4">
            <div className="grid gap-3 md:grid-cols-[1fr_1fr_1fr_auto]">
              <Picker label="用户" value={bindingUser} onValueChange={setBindingUser} items={data.users.filter((user) => user.active).map((user) => ({value: user.id, label: user.display_name}))} />
              <Picker label="角色" value={bindingRole} onValueChange={(value) => setBindingRole(value as typeof bindingRole)} items={[{value: "sre", label: "SRE"}, {value: "platform_administrator", label: "平台管理员"}]} />
              <Picker label="团队范围" value={bindingTeam} onValueChange={setBindingTeam} disabled={bindingRole === "platform_administrator"} items={data.teams.filter((team) => team.active).map((team) => ({value: team.id, label: team.name}))} />
              <div className="flex items-end"><Button disabled={!bindingUser || (bindingRole === "sre" && !bindingTeam) || mutation.isPending} onClick={() => bindingRole === "sre" ? submit({resource: "role-bindings", body: {user_id: bindingUser, role: "sre", scope_type: "team", scope_id: bindingTeam, reason}}) : submit({resource: "role-bindings", body: {user_id: bindingUser, role: "platform_administrator", scope_type: "platform", reason}})}><PlusIcon data-icon="inline-start" />添加绑定</Button></div>
            </div>
            <ResourceTable headings={["用户", "角色", "范围", "状态", "操作"]} rows={data.role_bindings.map((binding) => [<span key="user">{userName(data.users, binding.user_id)}</span>, <span key="role">{binding.role === "platform_administrator" ? "平台管理员" : "SRE"}</span>, <span key="scope">{binding.scope_type === "platform" ? "平台" : teamName(data.teams, binding.scope_id ?? "")}</span>, <Status key="status" active={binding.active} />, <ToggleButton key="action" active={binding.active} disabled={mutation.isPending} onClick={() => submit({resource: "role-bindings", id: binding.id, body: {active: !binding.active, reason}})} />])} />
          </TabsContent>

          <TabsContent value="kubernetes-authorities" className="pt-4">
            <KubernetesAuthoritiesAdmin
              users={data.users}
              clusters={connectorData.clusters}
              services={catalogData.services}
              reason={reason}
            />
          </TabsContent>

          <TabsContent value="connectors" className="flex flex-col gap-5 pt-4">
            <form onSubmit={(event) => { event.preventDefault(); const form = new FormData(event.currentTarget); submit({resource: "connector-enrollments", body: {connector_id: String(form.get("connector_id") ?? ""), cluster_id: String(form.get("cluster_id") ?? ""), expected_revision: null, reason}}) }}>
              <FieldGroup className="grid gap-3 md:grid-cols-[1fr_1fr_auto]">
                <Field><FieldLabel htmlFor="connector-id">Connector ID</FieldLabel><Input id="connector-id" name="connector_id" required /></Field>
                <Field><FieldLabel htmlFor="cluster-id">Cluster ID</FieldLabel><Input id="cluster-id" name="cluster_id" required /></Field>
                <div className="flex items-end"><Button type="submit" disabled={mutation.isPending}><PlusIcon data-icon="inline-start" />创建 Enrollment</Button></div>
              </FieldGroup>
            </form>
            <ResourceTable headings={["Connector", "Cluster", "连接状态", "Read verification", "操作"]} rows={connectorData.connector_enrollments.map((enrollment) => [
              <span key="connector" className="font-medium">{enrollment.connector_id}</span>,
              <span key="cluster">{enrollment.cluster_id}</span>,
              <Badge key="status" variant={enrollment.state === "online" ? "positive" : enrollment.state === "rotation_pending" ? "warning" : "secondary"}>{enrollment.state}</Badge>,
              <Badge key="verification" variant={enrollment.read_verification === "verified" ? "positive" : enrollment.read_verification === "failed" ? "destructive" : "secondary"}>{enrollment.read_verification}</Badge>,
              <div key="actions" className="flex flex-wrap gap-2">
                <Button type="button" size="sm" variant="outline" disabled={!enrollment.active || enrollment.state === "rotation_pending" || mutation.isPending} onClick={() => submit({resource: "connector-enrollments", id: enrollment.id, body: {rotate_credential: true, reason}})}><RefreshCwIcon />轮换</Button>
                {enrollment.read_verification === "failed" ? <Button type="button" size="sm" variant="outline" disabled={mutation.isPending} onClick={() => submit({resource: "connector-enrollments", id: enrollment.id, body: {retry_read_verification: true, reason}})}>重试验证</Button> : null}
                <ToggleButton active={enrollment.active} disabled={mutation.isPending} onClick={() => submit({resource: "connector-enrollments", id: enrollment.id, body: {active: !enrollment.active, reason}})} />
              </div>,
            ])} />
          </TabsContent>

          <TabsContent value="clusters" className="flex flex-col gap-4 pt-4">
            {connectorData.clusters.map((cluster) => <ClusterEditor key={cluster.cluster_id} cluster={cluster} reason={reason} pending={mutation.isPending} submit={submit} />)}
            {connectorData.clusters.length === 0 ? <div className="border-y py-10 text-center text-sm text-muted-foreground">暂无已注册 Cluster</div> : null}
          </TabsContent>

          <TabsContent value="catalog" className="flex flex-col gap-6 pt-4">
            <form onSubmit={(event) => { event.preventDefault(); const form = new FormData(event.currentTarget); submit({resource: "services", body: {team_id: serviceTeam, name: String(form.get("name") ?? ""), description: String(form.get("description") ?? ""), reason}}) }}>
              <FieldGroup className="grid gap-3 md:grid-cols-[1fr_1fr_2fr_auto]">
                <Picker label="责任团队" value={serviceTeam} onValueChange={setServiceTeam} items={data.teams.filter((team) => team.active).map((team) => ({value: team.id, label: team.name}))} />
                <Field><FieldLabel htmlFor="catalog-service-name">Service 名称</FieldLabel><Input id="catalog-service-name" name="name" required /></Field>
                <Field><FieldLabel htmlFor="catalog-service-description">说明</FieldLabel><Input id="catalog-service-description" name="description" /></Field>
                <div className="flex items-end"><Button type="submit" disabled={!serviceTeam || mutation.isPending}><PlusIcon data-icon="inline-start" />创建 Service</Button></div>
              </FieldGroup>
            </form>
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
                <Button key="action" type="button" size="sm" variant="outline" disabled={!bindingService || binding?.service_id === bindingService || mutation.isPending} onClick={() => binding ? submit({resource: "resource-bindings", id: binding.id, body: {service_id: bindingService, reason}}) : submit({resource: "resource-bindings", body: {candidate_id: candidate.id, service_id: bindingService, reason}})}><LinkIcon />{binding ? "纠正" : "确认"}</Button>,
              ]
            })} />
          </TabsContent>
          <TabsContent value="model" className="pt-4"><ModelProviderAdmin reason={reason} /></TabsContent>
          <TabsContent value="mcp" className="pt-4"><MCPRegistryAdmin reason={reason} /></TabsContent>
          <TabsContent value="skills" className="pt-4"><SkillRegistryAdmin reason={reason} /></TabsContent>
          <TabsContent value="notifications" className="pt-4"><NotificationAdmin reason={reason} /></TabsContent>
        </Tabs>
      </main>
  )
}

function ClusterEditor({cluster, reason, pending, submit}: {cluster: Cluster; reason: string; pending: boolean; submit: (change: AdminMutation) => void}) {
  const [environment, setEnvironment] = useState<Cluster["environment"]>(cluster.environment)
  const [mutationEnabled, setMutationEnabled] = useState(cluster.mutation_enabled)
  return <form className="grid gap-3 border-b pb-4 lg:grid-cols-[1fr_160px_2fr_auto_auto]" onSubmit={(event) => { event.preventDefault(); const form = new FormData(event.currentTarget); submit({resource: "clusters", id: cluster.cluster_id, body: {display_name: String(form.get("display_name") ?? ""), environment, governance_notes: String(form.get("governance_notes") ?? ""), mutation_enabled: mutationEnabled, reason}}) }}>
    <div className="flex flex-wrap items-center gap-2 text-sm lg:col-span-5">
      <Badge variant={cluster.runtime_status === "online" ? "positive" : cluster.runtime_status === "degraded" ? "warning" : "secondary"}>{cluster.runtime_status}</Badge>
      <span className="text-muted-foreground">心跳 {new Date(cluster.last_heartbeat * 1000).toLocaleString()}</span>
      <Badge variant={cluster.pending_read_commands ? "warning" : "outline"}>待处理 read {cluster.pending_read_commands ?? 0}</Badge>
      <span className="text-muted-foreground">最新 read {cluster.last_read_command ? `${cluster.last_read_command.status} · ${cluster.last_read_command.namespace}` : "暂无"}</span>
      <span className="text-muted-foreground">最后结果 {cluster.last_read_result ? `${cluster.last_read_result.status} · ${cluster.last_read_result.namespace}${cluster.last_read_result.error_code ? ` · ${cluster.last_read_result.error_code}` : ""}` : "暂无"}</span>
    </div>
    <Field><FieldLabel htmlFor={`cluster-name-${cluster.cluster_id}`}>{cluster.cluster_id}</FieldLabel><Input id={`cluster-name-${cluster.cluster_id}`} name="display_name" defaultValue={cluster.display_name} required /></Field>
    <Field><FieldLabel htmlFor={`cluster-env-${cluster.cluster_id}`}>Environment</FieldLabel><Select value={environment} onValueChange={(value) => setEnvironment(value as Cluster["environment"])}><SelectTrigger id={`cluster-env-${cluster.cluster_id}`} className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{["prod", "staging", "dev", "test"].map((value) => <SelectItem key={value} value={value}>{value}</SelectItem>)}</SelectGroup></SelectContent></Select></Field>
    <Field><FieldLabel htmlFor={`cluster-notes-${cluster.cluster_id}`}>治理备注</FieldLabel><Input id={`cluster-notes-${cluster.cluster_id}`} name="governance_notes" defaultValue={cluster.governance_notes} /></Field>
    <label className="flex items-center gap-2 self-end pb-2 text-sm"><Checkbox checked={mutationEnabled} onCheckedChange={setMutationEnabled} />允许 mutation</label>
    <div className="flex items-end"><Button type="submit" disabled={pending}>保存</Button></div>
  </form>
}

function ResourceTable({headings, rows}: {headings: string[]; rows: React.ReactNode[][]}) {
  return <div className="rounded-md border"><Table><TableHeader><TableRow>{headings.map((heading) => <TableHead key={heading}>{heading}</TableHead>)}</TableRow></TableHeader><TableBody>{rows.map((cells, index) => <TableRow key={index}>{cells.map((cell, cellIndex) => <TableCell key={cellIndex}>{cell}</TableCell>)}</TableRow>)}</TableBody></Table></div>
}

function Status({active}: {active: boolean}) {
  return <Badge variant={active ? "positive" : "secondary"}>{active ? "启用" : "停用"}</Badge>
}

function ToggleButton({active, disabled, onClick}: {active: boolean; disabled: boolean; onClick: () => void}) {
  return <Button type="button" size="sm" variant="outline" disabled={disabled} onClick={onClick}>{active ? "停用" : "启用"}</Button>
}

function userName(users: AdminUser[], id: string) {
  return users.find((user) => user.id === id)?.display_name ?? id
}

function teamName(teams: AdminTeam[], id: string) {
  return teams.find((team) => team.id === id)?.name ?? id
}
