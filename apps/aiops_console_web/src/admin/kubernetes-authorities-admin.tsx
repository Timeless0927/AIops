import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { PlusIcon } from "lucide-react"

import {
  createKubernetesChangeAuthority,
  getKubernetesChangeAuthorities,
  updateKubernetesChangeAuthority,
  type AdminUser,
  type CatalogService,
  type Cluster,
} from "@/admin/admin-client"
import { useAdminAction } from "@/admin/admin-action"
import {
  FormDialog,
  ResourceTable,
  SelectionBar,
  Status,
  ToggleButton,
  useAdminBatch,
  useRowSelection,
} from "@/admin/admin-shared"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { AdminPicker as Picker } from "@/admin/admin-picker"

type ScopeType = "object" | "namespace" | "service" | "cluster"

export function KubernetesAuthoritiesAdmin({
  users,
  clusters,
  services,
}: {
  users: AdminUser[]
  clusters: Cluster[]
  services: CatalogService[]
}) {
  const requestAction = useAdminAction()
  const batch = useAdminBatch()
  const queryClient = useQueryClient()
  const state = useQuery({
    queryKey: ["kubernetes-change-authorities"],
    queryFn: getKubernetesChangeAuthorities,
    retry: false,
  })
  const [userId, setUserId] = useState("")
  const [environment, setEnvironment] = useState<"prod" | "staging" | "dev" | "test">("prod")
  const [scopeType, setScopeType] = useState<ScopeType>("namespace")
  const [clusterId, setClusterId] = useState("")
  const [namespace, setNamespace] = useState("")
  const [apiVersion, setApiVersion] = useState("apps/v1")
  const [kind, setKind] = useState("Deployment")
  const [name, setName] = useState("")
  const [serviceId, setServiceId] = useState("")
  const mutation = useMutation({
    mutationFn: createKubernetesChangeAuthority,
    onSuccess: () => queryClient.invalidateQueries({queryKey: ["kubernetes-change-authorities"]}),
  })
  const toggle = useMutation({
    mutationFn: ({id, active, reason}: {id: string; active: boolean; reason: string}) => updateKubernetesChangeAuthority(id, {active, reason}),
    onSuccess: () => queryClient.invalidateQueries({queryKey: ["kubernetes-change-authorities"]}),
  })
  const selection = useRowSelection()
  const scopeReady = scopeType === "service"
    ? Boolean(serviceId)
    : Boolean(clusterId)
      && (scopeType === "cluster" || scopeType === "object" || Boolean(namespace))
      && (scopeType !== "object" || Boolean(apiVersion && kind && name))

  const submit = () => {
    const scope = scopeType === "service"
      ? {service_id: serviceId}
      : scopeType === "cluster"
        ? {cluster_id: clusterId}
        : scopeType === "namespace"
          ? {cluster_id: clusterId, namespace}
          : {cluster_id: clusterId, api_version: apiVersion, kind, namespace: namespace.trim() || null, name}
    requestAction({
      title: "授予 Kubernetes 变更权限",
      summary: `将为 ${userId} 授予 ${scopeType} 范围的 ${environment} 变更权限。`,
      run: (reason) => mutation.mutateAsync({user_id: userId, environment, scope_type: scopeType, scope, reason}),
    })
  }

  if (state.isPending) return <div className="py-8 text-sm text-muted-foreground" role="status">正在加载 Kubernetes 变更权限</div>
  if (state.isError) return <div className="py-8 text-sm text-destructive">无法读取 Kubernetes 变更权限</div>

  const authorities = state.data
  const ids = authorities.map((authority) => authority.id)
  const submitActiveBatch = (active: boolean) => batch({
    verb: active ? "启用" : "停用",
    label: "Kubernetes 变更权限",
    ids: [...selection.selected],
    destructive: !active,
    onDone: selection.clear,
    run: (id, reason) => toggle.mutateAsync({id, active, reason}),
  })

  return <div className="flex flex-col gap-4">
    <div className="flex justify-end">
      <FormDialog
        trigger={<><PlusIcon data-icon="inline-start" />授予变更权限</>}
        title="授予 Kubernetes 变更权限"
        description="选择用户、Environment 与真实资源范围。"
        submitLabel="授予变更权限"
        submitDisabled={!userId || !scopeReady}
        pending={mutation.isPending}
        onSubmit={submit}
      >
        <Picker label="用户" value={userId} onValueChange={setUserId} items={users.filter((user) => user.active).map((user) => ({value: user.id, label: user.display_name}))} />
        <Picker label="Environment" value={environment} onValueChange={(value) => setEnvironment(value as typeof environment)} items={["prod", "staging", "dev", "test"].map((value) => ({value, label: value}))} />
        <Picker label="Authority scope" value={scopeType} onValueChange={(value) => setScopeType(value as ScopeType)} items={[
          {value: "object", label: "Object"}, {value: "namespace", label: "Namespace"},
          {value: "service", label: "Service"}, {value: "cluster", label: "Cluster"},
        ]} />
        {scopeType === "service" ? <Picker label="Service" value={serviceId} onValueChange={setServiceId} items={services.filter((service) => service.active).map((service) => ({value: service.id, label: service.name}))} /> : <Picker label="Cluster" value={clusterId} onValueChange={setClusterId} items={clusters.map((cluster) => ({value: cluster.cluster_id, label: cluster.display_name}))} />}
        {scopeType === "namespace" || scopeType === "object" ? <Field><FieldLabel htmlFor="authority-namespace">Namespace</FieldLabel><Input id="authority-namespace" value={namespace} onChange={(event) => setNamespace(event.target.value)} required={scopeType === "namespace"} placeholder={scopeType === "object" ? "cluster-scoped 留空" : undefined} /></Field> : null}
        {scopeType === "object" ? <>
          <Field><FieldLabel htmlFor="authority-api-version">API version</FieldLabel><Input id="authority-api-version" value={apiVersion} onChange={(event) => setApiVersion(event.target.value)} required /></Field>
          <Field><FieldLabel htmlFor="authority-kind">Kind</FieldLabel><Input id="authority-kind" value={kind} onChange={(event) => setKind(event.target.value)} required /></Field>
          <Field><FieldLabel htmlFor="authority-name">Name</FieldLabel><Input id="authority-name" value={name} onChange={(event) => setName(event.target.value)} required /></Field>
        </> : null}
      </FormDialog>
    </div>
    <SelectionBar count={selection.count} onClear={selection.clear}>
      <Button type="button" size="sm" variant="outline" disabled={toggle.isPending} onClick={() => submitActiveBatch(true)}>批量启用</Button>
      <Button type="button" size="sm" variant="destructive" disabled={toggle.isPending} onClick={() => submitActiveBatch(false)}>批量停用</Button>
    </SelectionBar>
    <ResourceTable
      empty="暂无 Kubernetes 变更权限"
      headings={[
        <Checkbox key="select-all" aria-label="选择全部变更权限" checked={ids.length > 0 && selection.count === ids.length} onCheckedChange={(checked) => selection.toggleAll(ids, checked)} />,
        "用户", "Environment", "Scope", "状态", "操作",
      ]}
      rows={authorities.map((authority) => [
        <Checkbox key="select" aria-label={`选择变更权限 ${authority.user_id}`} checked={selection.selected.has(authority.id)} onCheckedChange={(checked) => selection.toggle(authority.id, checked)} />,
        <span key="user">{users.find((user) => user.id === authority.user_id)?.display_name ?? authority.user_id}</span>,
        <span key="environment">{authority.environment}</span>,
        <span key="scope" className="font-mono text-xs">{authority.scope_type}: {scopeLabel(authority.scope)}</span>,
        <Status key="status" active={authority.active} />,
        <ToggleButton key="action" active={authority.active} disabled={toggle.isPending} onClick={() => requestAction({
          title: authority.active ? "停用 Kubernetes 变更权限" : "启用 Kubernetes 变更权限",
          summary: `${authority.active ? "将停用" : "将启用"} ${authority.user_id} 的 ${authority.scope_type} 变更权限。`,
          destructive: authority.active,
          run: (reason) => toggle.mutateAsync({id: authority.id, active: !authority.active, reason}),
        })} />,
      ])}
    />
  </div>
}

function scopeLabel(scope: Record<string, unknown>) {
  if ("service_id" in scope) return String(scope.service_id)
  return [scope.cluster_id, scope.api_version, scope.kind, scope.namespace, scope.name].filter((value) => value !== undefined && value !== null).join("/")
}
