import { useState } from "react"
import { LinkIcon, PlusIcon } from "lucide-react"

import type { AdminTeam, ResourceCatalogState } from "@/admin/admin-client"
import { AdminPicker as Picker } from "@/admin/admin-picker"
import {
  FormDialog,
  ResourceTable,
  SelectionBar,
  teamName,
  useAdminMutation,
  useRowSelection,
} from "@/admin/admin-shared"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"

export function CatalogSection({teams, catalog}: {teams: AdminTeam[]; catalog: ResourceCatalogState}) {
  const {submit, submitBatch, mutate, pending} = useAdminMutation()
  const [serviceTeam, setServiceTeam] = useState("")
  const [bindingService, setBindingService] = useState("")
  const selection = useRowSelection()
  const candidates = catalog.discovery_candidates
  const ids = candidates.map((candidate) => candidate.id)
  const bindOne = (candidateId: string, reason: string) => {
    const candidate = candidates.find((item) => item.id === candidateId)
    const binding = candidate?.resource_binding_id ? catalog.resource_bindings.find((item) => item.id === candidate.resource_binding_id) : undefined
    return binding
      ? mutate({resource: "resource-bindings", id: binding.id, body: {service_id: bindingService, reason}})
      : mutate({resource: "resource-bindings", body: {candidate_id: candidateId, service_id: bindingService, reason}})
  }
  return (
    <div className="flex flex-col gap-8">
      <div className="flex flex-col gap-4">
        <div className="flex items-center justify-between gap-3">
          <h3 className="text-sm font-medium">Service</h3>
          <FormDialog
            trigger={<><PlusIcon data-icon="inline-start" />创建 Service</>}
            title="创建 Service"
            description="登记由团队负责的业务 Service。"
            submitLabel="创建 Service"
            submitDisabled={!serviceTeam}
            pending={pending}
            onSubmit={(form) => submit({resource: "services", body: {
              team_id: serviceTeam,
              name: String(form.get("name") ?? ""),
              description: String(form.get("description") ?? ""),
              reason: "",
            }})}
          >
            <Picker label="责任团队" value={serviceTeam} onValueChange={setServiceTeam} items={teams.filter((team) => team.active).map((team) => ({value: team.id, label: team.name}))} />
            <Field><FieldLabel htmlFor="catalog-service-name">Service 名称</FieldLabel><Input id="catalog-service-name" name="name" required /></Field>
            <Field><FieldLabel htmlFor="catalog-service-description">说明</FieldLabel><Input id="catalog-service-description" name="description" /></Field>
          </FormDialog>
        </div>
        <ResourceTable
          empty="暂无 Service"
          headings={["Service", "责任团队", "说明"]}
          rows={catalog.services.map((service) => [
            <span key="name" className="font-medium">{service.name}</span>,
            <span key="team">{teamName(teams, service.team_id)}</span>,
            <span key="description" className="text-muted-foreground">{service.description || "-"}</span>,
          ])}
        />
      </div>
      <div className="flex flex-col gap-4">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <h3 className="text-sm font-medium">Discovery 候选绑定</h3>
          <div className="w-full max-w-md">
            <Picker label="确认或纠正为" value={bindingService} onValueChange={setBindingService} items={catalog.services.filter((service) => service.active).map((service) => ({value: service.id, label: `${service.name} · ${teamName(teams, service.team_id)}`}))} />
          </div>
        </div>
        <SelectionBar count={selection.count} onClear={selection.clear}>
          <Button
            type="button" size="sm" variant="outline" disabled={!bindingService || pending}
            onClick={() => submitBatch({verb: "确认绑定", label: "Discovery Candidate", ids: [...selection.selected], onDone: selection.clear, run: bindOne})}
          ><LinkIcon data-icon="inline-start" />批量确认绑定</Button>
        </SelectionBar>
        <ResourceTable
          empty="暂无 Discovery 候选"
          headings={[
            <Checkbox key="select-all" aria-label="选择全部候选" checked={ids.length > 0 && selection.count === ids.length} onCheckedChange={(checked) => selection.toggleAll(ids, checked)} />,
            "Discovery Candidate", "实际 Service / label hint", "归属状态", "操作",
          ]}
          rows={candidates.map((candidate) => {
            const binding = candidate.resource_binding_id ? catalog.resource_bindings.find((item) => item.id === candidate.resource_binding_id) : undefined
            const service = binding ? catalog.services.find((item) => item.id === binding.service_id) : undefined
            return [
              <Checkbox key="select" aria-label={`选择候选 ${candidate.workload_name}`} checked={selection.selected.has(candidate.id)} onCheckedChange={(checked) => selection.toggle(candidate.id, checked)} />,
              <div key="target"><div className="font-medium">{candidate.workload_kind}/{candidate.workload_name}</div><div className="text-xs text-muted-foreground">{candidate.cluster_id} · {candidate.namespace}</div></div>,
              <div key="hints"><div>{candidate.service_name || "无匹配 Kubernetes Service"}</div><div className="text-xs text-muted-foreground">hint: {candidate.service_hint || "-"} / {candidate.team_hint || "-"}</div></div>,
              binding ? <div key="bound"><Badge variant="positive">已确认</Badge><div className="mt-1 text-xs text-muted-foreground">{service?.name ?? binding.service_id} · rev {binding.revision}</div></div> : <Badge key="unbound" variant="secondary">未绑定</Badge>,
              <Button key="action" type="button" size="sm" variant="outline" disabled={!bindingService || binding?.service_id === bindingService || pending} onClick={() => binding ? submit({resource: "resource-bindings", id: binding.id, body: {service_id: bindingService, reason: ""}}) : submit({resource: "resource-bindings", body: {candidate_id: candidate.id, service_id: bindingService, reason: ""}})}><LinkIcon />{binding ? "纠正" : "确认"}</Button>,
            ]
          })}
        />
      </div>
    </div>
  )
}
