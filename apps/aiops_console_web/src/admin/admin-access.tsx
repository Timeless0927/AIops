import { useState } from "react"
import { KeyRoundIcon, PlusIcon, RefreshCwIcon } from "lucide-react"

import type { AdminMutation, Cluster, ConnectorEnrollment } from "@/admin/admin-client"
import {
  FormDialog,
  ResourceTable,
  SelectionBar,
  ToggleButton,
  useAdminMutation,
  useRowSelection,
} from "@/admin/admin-shared"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "@/components/ui/empty"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"

export function ConnectorsTab({enrollments}: {enrollments: ConnectorEnrollment[]}) {
  const {submit, submitActiveBatch, issuedCredential, pending} = useAdminMutation()
  const selection = useRowSelection()
  const ids = enrollments.map((enrollment) => enrollment.id)
  return (
    <div className="flex flex-col gap-4">
      {issuedCredential ? (
        <Alert>
          <KeyRoundIcon />
          <AlertTitle>Connector credential</AlertTitle>
          <AlertDescription><Input value={issuedCredential} readOnly aria-label="新 Connector credential" /></AlertDescription>
        </Alert>
      ) : null}
      <div className="flex justify-end">
        <FormDialog
          trigger={<><PlusIcon data-icon="inline-start" />创建 Enrollment</>}
          title="创建 Connector Enrollment"
          description="预授权一个 Connector 与 Cluster 的注册绑定。"
          submitLabel="创建 Enrollment"
          pending={pending}
          onSubmit={(form) => submit({resource: "connector-enrollments", body: {
            connector_id: String(form.get("connector_id") ?? ""),
            cluster_id: String(form.get("cluster_id") ?? ""),
            expected_revision: null,
            reason: "",
          }})}
        >
          <Field><FieldLabel htmlFor="connector-id">Connector ID</FieldLabel><Input id="connector-id" name="connector_id" required /></Field>
          <Field><FieldLabel htmlFor="cluster-id">Cluster ID</FieldLabel><Input id="cluster-id" name="cluster_id" required /></Field>
        </FormDialog>
      </div>
      <SelectionBar count={selection.count} onClear={selection.clear}>
        <Button type="button" size="sm" variant="outline" disabled={pending} onClick={() => submitActiveBatch("connector-enrollments", "Connector Enrollment", [...selection.selected], true, selection.clear)}>批量启用</Button>
        <Button type="button" size="sm" variant="destructive" disabled={pending} onClick={() => submitActiveBatch("connector-enrollments", "Connector Enrollment", [...selection.selected], false, selection.clear)}>批量停用</Button>
      </SelectionBar>
      <ResourceTable
        empty="暂无 Connector Enrollment"
        headings={[
          <Checkbox key="select-all" aria-label="选择全部 Connector" checked={ids.length > 0 && selection.count === ids.length} onCheckedChange={(checked) => selection.toggleAll(ids, checked)} />,
          "Connector", "Cluster", "连接状态", "Read verification", "操作",
        ]}
        rows={enrollments.map((enrollment) => [
          <Checkbox key="select" aria-label={`选择 Connector ${enrollment.connector_id}`} checked={selection.selected.has(enrollment.id)} onCheckedChange={(checked) => selection.toggle(enrollment.id, checked)} />,
          <span key="connector" className="font-medium">{enrollment.connector_id}</span>,
          <span key="cluster">{enrollment.cluster_id}</span>,
          <Badge key="status" variant={enrollment.state === "online" ? "positive" : enrollment.state === "rotation_pending" ? "warning" : "secondary"}>{enrollment.state}</Badge>,
          <Badge key="verification" variant={enrollment.read_verification === "verified" ? "positive" : enrollment.read_verification === "failed" ? "destructive" : "secondary"}>{enrollment.read_verification}</Badge>,
          <div key="actions" className="flex flex-wrap gap-2">
            <Button type="button" size="sm" variant="outline" disabled={!enrollment.active || enrollment.state === "rotation_pending" || pending} onClick={() => submit({resource: "connector-enrollments", id: enrollment.id, body: {rotate_credential: true, reason: ""}})}><RefreshCwIcon />轮换</Button>
            {enrollment.read_verification === "failed" ? <Button type="button" size="sm" variant="outline" disabled={pending} onClick={() => submit({resource: "connector-enrollments", id: enrollment.id, body: {retry_read_verification: true, reason: ""}})}>重试验证</Button> : null}
            <ToggleButton active={enrollment.active} disabled={pending} onClick={() => submit({resource: "connector-enrollments", id: enrollment.id, body: {active: !enrollment.active, reason: ""}})} />
          </div>,
        ])}
      />
    </div>
  )
}

export function ClustersTab({clusters}: {clusters: Cluster[]}) {
  const {submit, pending} = useAdminMutation()
  if (clusters.length === 0) {
    return (
      <Empty>
        <EmptyHeader>
          <EmptyTitle>暂无已注册 Cluster</EmptyTitle>
          <EmptyDescription>创建 Connector Enrollment 后，Connector 完成注册即可看到 Cluster。</EmptyDescription>
        </EmptyHeader>
      </Empty>
    )
  }
  return (
    <div className="flex flex-col gap-4">
      {clusters.map((cluster) => <ClusterEditor key={cluster.cluster_id} cluster={cluster} pending={pending} submit={submit} />)}
    </div>
  )
}

function ClusterEditor({cluster, pending, submit}: {cluster: Cluster; pending: boolean; submit: (change: AdminMutation) => void}) {
  const [environment, setEnvironment] = useState<Cluster["environment"]>(cluster.environment)
  const [mutationEnabled, setMutationEnabled] = useState(cluster.mutation_enabled)
  return (
    <article className="rounded-xl bg-card p-4 ring-1 ring-foreground/10">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium">{cluster.display_name}</span>
        <Badge variant={cluster.runtime_status === "online" ? "positive" : cluster.runtime_status === "degraded" ? "warning" : "secondary"}>{cluster.runtime_status}</Badge>
        <span className="text-muted-foreground">心跳 {new Date(cluster.last_heartbeat * 1000).toLocaleString()}</span>
        <Badge variant={cluster.pending_read_commands ? "warning" : "outline"}>待处理 read {cluster.pending_read_commands ?? 0}</Badge>
        <span className="text-muted-foreground">最新 read {cluster.last_read_command ? `${cluster.last_read_command.status} · ${cluster.last_read_command.namespace}` : "暂无"}</span>
        <span className="text-muted-foreground">最后结果 {cluster.last_read_result ? `${cluster.last_read_result.status} · ${cluster.last_read_result.namespace}${cluster.last_read_result.error_code ? ` · ${cluster.last_read_result.error_code}` : ""}` : "暂无"}</span>
        <div className="ml-auto">
          <FormDialog
            trigger="编辑"
            triggerVariant="outline"
            triggerSize="sm"
            title={`编辑 ${cluster.display_name}`}
            description={cluster.cluster_id}
            submitLabel="保存"
            pending={pending}
            onSubmit={(form) => submit({resource: "clusters", id: cluster.cluster_id, body: {
              display_name: String(form.get("display_name") ?? ""),
              environment,
              governance_notes: String(form.get("governance_notes") ?? ""),
              mutation_enabled: mutationEnabled,
              reason: "",
            }})}
          >
            <Field><FieldLabel htmlFor={`cluster-name-${cluster.cluster_id}`}>显示名称</FieldLabel><Input id={`cluster-name-${cluster.cluster_id}`} name="display_name" defaultValue={cluster.display_name} required /></Field>
            <Field>
              <FieldLabel htmlFor={`cluster-env-${cluster.cluster_id}`}>Environment</FieldLabel>
              <Select value={environment} onValueChange={(value) => setEnvironment(value as Cluster["environment"])}>
                <SelectTrigger id={`cluster-env-${cluster.cluster_id}`} className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent><SelectGroup>{["prod", "staging", "dev", "test"].map((value) => <SelectItem key={value} value={value}>{value}</SelectItem>)}</SelectGroup></SelectContent>
              </Select>
            </Field>
            <Field><FieldLabel htmlFor={`cluster-notes-${cluster.cluster_id}`}>治理备注</FieldLabel><Input id={`cluster-notes-${cluster.cluster_id}`} name="governance_notes" defaultValue={cluster.governance_notes} /></Field>
            <label className="flex items-center gap-2 text-sm"><Checkbox checked={mutationEnabled} onCheckedChange={setMutationEnabled} />允许 mutation</label>
          </FormDialog>
        </div>
      </div>
    </article>
  )
}
