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
import { ApiError } from "@/api/transport"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { AdminPicker as Picker } from "@/admin/admin-picker"

type ScopeType = "object" | "namespace" | "service" | "cluster"

export function KubernetesAuthoritiesAdmin({
  users,
  clusters,
  services,
  reason,
}: {
  users: AdminUser[]
  clusters: Cluster[]
  services: CatalogService[]
  reason: string
}) {
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
    mutationFn: ({id, active}: {id: string; active: boolean}) => updateKubernetesChangeAuthority(
      id, {active, reason},
    ),
    onSuccess: () => queryClient.invalidateQueries({queryKey: ["kubernetes-change-authorities"]}),
  })
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
    mutation.mutate({user_id: userId, environment, scope_type: scopeType, scope, reason})
  }

  if (state.isPending) return <div className="py-8 text-sm text-muted-foreground" role="status">正在加载 Kubernetes 变更权限</div>
  if (state.isError) return <div className="py-8 text-sm text-destructive">无法读取 Kubernetes 变更权限</div>
  const error = mutation.error instanceof ApiError ? mutation.error : toggle.error instanceof ApiError ? toggle.error : null

  return <div className="flex flex-col gap-5">
    <form className="grid gap-3" onSubmit={(event) => { event.preventDefault(); submit() }}>
      <div className="grid gap-3 md:grid-cols-4">
        <Picker label="用户" value={userId} onValueChange={setUserId} items={users.filter((user) => user.active).map((user) => ({value: user.id, label: user.display_name}))} />
        <Picker label="Environment" value={environment} onValueChange={(value) => setEnvironment(value as typeof environment)} items={["prod", "staging", "dev", "test"].map((value) => ({value, label: value}))} />
        <Picker label="Authority scope" value={scopeType} onValueChange={(value) => setScopeType(value as ScopeType)} items={[
          {value: "object", label: "Object"}, {value: "namespace", label: "Namespace"},
          {value: "service", label: "Service"}, {value: "cluster", label: "Cluster"},
        ]} />
        {scopeType === "service" ? <Picker label="Service" value={serviceId} onValueChange={setServiceId} items={services.filter((service) => service.active).map((service) => ({value: service.id, label: service.name}))} /> : <Picker label="Cluster" value={clusterId} onValueChange={setClusterId} items={clusters.map((cluster) => ({value: cluster.cluster_id, label: cluster.display_name}))} />}
      </div>
      {scopeType === "namespace" || scopeType === "object" ? <div className="grid gap-3 md:grid-cols-4">
        <Field><FieldLabel htmlFor="authority-namespace">Namespace</FieldLabel><Input id="authority-namespace" value={namespace} onChange={(event) => setNamespace(event.target.value)} required={scopeType === "namespace"} placeholder={scopeType === "object" ? "cluster-scoped 留空" : undefined} /></Field>
        {scopeType === "object" ? <>
          <Field><FieldLabel htmlFor="authority-api-version">API version</FieldLabel><Input id="authority-api-version" value={apiVersion} onChange={(event) => setApiVersion(event.target.value)} required /></Field>
          <Field><FieldLabel htmlFor="authority-kind">Kind</FieldLabel><Input id="authority-kind" value={kind} onChange={(event) => setKind(event.target.value)} required /></Field>
          <Field><FieldLabel htmlFor="authority-name">Name</FieldLabel><Input id="authority-name" value={name} onChange={(event) => setName(event.target.value)} required /></Field>
        </> : null}
      </div> : null}
      <div className="flex flex-wrap items-center justify-end gap-3">
        {error ? <span role="alert" className="text-sm text-destructive">{error.message}</span> : null}
        <Button type="submit" disabled={!userId || !scopeReady || !reason.trim() || mutation.isPending}><PlusIcon />授予变更权限</Button>
      </div>
    </form>
    <div className="overflow-x-auto border-y">
      <Table>
        <TableHeader><TableRow><TableHead>用户</TableHead><TableHead>Environment</TableHead><TableHead>Scope</TableHead><TableHead>状态</TableHead><TableHead>操作</TableHead></TableRow></TableHeader>
        <TableBody>{state.data.map((authority) => <TableRow key={authority.id}>
          <TableCell>{users.find((user) => user.id === authority.user_id)?.display_name ?? authority.user_id}</TableCell>
          <TableCell>{authority.environment}</TableCell>
          <TableCell className="font-mono text-xs">{authority.scope_type}: {scopeLabel(authority.scope)}</TableCell>
          <TableCell><Badge variant={authority.active ? "positive" : "secondary"}>{authority.active ? "启用" : "停用"}</Badge></TableCell>
          <TableCell><Button type="button" size="sm" variant="outline" disabled={!reason.trim() || toggle.isPending} onClick={() => toggle.mutate({id: authority.id, active: !authority.active})}>{authority.active ? "停用" : "启用"}</Button></TableCell>
        </TableRow>)}</TableBody>
      </Table>
    </div>
  </div>
}

function scopeLabel(scope: Record<string, unknown>) {
  if ("service_id" in scope) return String(scope.service_id)
  return [scope.cluster_id, scope.api_version, scope.kind, scope.namespace, scope.name].filter((value) => value !== undefined && value !== null).join("/")
}
