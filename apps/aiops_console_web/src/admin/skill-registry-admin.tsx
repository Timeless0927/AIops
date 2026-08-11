import { useId, useState } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { CirclePowerIcon, PlusIcon, Trash2Icon } from "lucide-react"

import {
  createSkill,
  createSkillVersion,
  disableSkill,
  enableSkill,
  getAdminAudit,
  getSkills,
  type AdminAuditEntry,
  type Skill,
  type SkillCreate,
  type SkillVersionCreate,
} from "@/admin/admin-client"
import { useAdminAction } from "@/admin/admin-action"
import { FormDialog } from "@/admin/admin-shared"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"


type SkillContent = Omit<SkillVersionCreate, "reason">
type SkillCreateInput = Omit<SkillCreate, "reason">
type SkillScope = SkillContent["applicable_scope"][number]
type MCPReference = SkillContent["required_mcp"][number]


export function SkillRegistryAdmin() {
  const queryClient = useQueryClient()
  const requestAction = useAdminAction()
  const skills = useQuery({queryKey: ["skills"], queryFn: getSkills, retry: false})
  const audit = useQuery({queryKey: ["admin-audit"], queryFn: getAdminAudit, retry: false})
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const run = async (work: () => Promise<unknown>) => {
    setPending(true)
    setError(null)
    try {
      await work()
      await Promise.all([
        queryClient.invalidateQueries({queryKey: ["skills"]}),
        queryClient.invalidateQueries({queryKey: ["admin-audit"]}),
      ])
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Skill 操作失败")
      throw cause
    } finally {
      setPending(false)
    }
  }

  if (skills.isPending || audit.isPending) {
    return <div className="py-10 text-center text-sm text-muted-foreground" role="status">正在加载 Skill</div>
  }
  if (skills.isError || audit.isError) {
    return <div className="py-10 text-center text-sm text-destructive">无法读取 Skill</div>
  }

  return <SkillRegistryAdminView
    skills={skills.data}
    audit={audit.data.filter((entry) => entry.target_type === "skill")}
    pending={pending}
    error={error}
    onCreate={(body) => requestAction({title: "创建 Skill", summary: `将创建 Skill ${body.name} 的初始版本。`, run: (reason) => run(() => createSkill({...body, reason}))})}
    onCreateVersion={(id, body) => requestAction({title: "创建 Skill 新版本", summary: `将为 Skill ${id} 创建新的不可变版本。`, run: (reason) => run(() => createSkillVersion(id, {...body, reason}))})}
    onEnable={(id, version, expected) => requestAction({title: `启用 Skill v${version}`, summary: `将把 Skill ${id} 的活动版本切换为 v${version}。`, run: (reason) => run(() => enableSkill(id, version, expected, reason))})}
    onDisable={(id, expected) => requestAction({title: "停用 Skill", summary: `将停用 Skill ${id}，新 Investigation 将无法使用该 Skill。`, destructive: true, run: (reason) => run(() => disableSkill(id, expected, reason))})}
  />
}


export function SkillRegistryAdminView({
  skills,
  audit,
  pending,
  error,
  onCreate,
  onCreateVersion,
  onEnable,
  onDisable,
}: {
  skills: Skill[]
  audit: AdminAuditEntry[]
  pending: boolean
  error: string | null
  onCreate: (body: SkillCreateInput) => void
  onCreateVersion: (id: string, body: SkillContent) => void
  onEnable: (id: string, version: number, expected: number | null) => void
  onDisable: (id: string, expected: number | null) => void
}) {
  return <div className="flex min-w-0 flex-col gap-7">
    {error ? <Alert variant="destructive"><AlertTitle>Skill 操作失败</AlertTitle><AlertDescription>{error}</AlertDescription></Alert> : null}

    <section className="flex flex-col gap-5" aria-labelledby="skills-heading">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="skills-heading" className="text-base font-semibold">已注册 Skill</h2>
          <p className="mt-1 text-sm text-muted-foreground">新请求仅使用当前启用且 exact MCP 依赖可用的版本。</p>
        </div>
        <CreateSkillDialog pending={pending} onCreate={onCreate} />
      </div>
      {skills.map((skill) => <SkillEditor
        key={skill.id}
        skill={skill}
        pending={pending}
        onCreateVersion={onCreateVersion}
        onEnable={onEnable}
        onDisable={onDisable}
      />)}
      {skills.length === 0 ? <div className="border-y py-10 text-center text-sm text-muted-foreground">暂无 Skill</div> : null}
    </section>

    <AuditHistory audit={audit} />
  </div>
}


function CreateSkillDialog({pending, onCreate}: {pending: boolean; onCreate: (body: SkillCreateInput) => void}) {
  const id = useId()
  const [scopes, setScopes] = useState<SkillScope[]>([{cluster_id: "", namespace: null}])
  const [references, setReferences] = useState<MCPReference[]>([])
  return <FormDialog
    trigger={<><PlusIcon data-icon="inline-start" />创建 Skill</>}
    title="创建 Skill"
    description="定义 instruction、workflow、适用范围和 MCP 依赖。"
    submitLabel="创建"
    pending={pending}
    contentClassName="sm:max-w-2xl"
    onSubmit={(form) => onCreate({
      name: String(form.get("name") ?? ""),
      instruction: String(form.get("instruction") ?? ""),
      workflow: String(form.get("workflow") ?? "").split("\n").map((step) => step.trim()).filter(Boolean),
      applicable_scope: scopes,
      required_mcp: references,
    })}
  >
    <Field><FieldLabel htmlFor={`${id}-name`}>名称</FieldLabel><Input id={`${id}-name`} name="name" required /></Field>
    <SkillContentFields id={id} scopes={scopes} onScopesChange={setScopes} references={references} onReferencesChange={setReferences} />
  </FormDialog>
}


function SkillEditor({
  skill,
  pending,
  onCreateVersion,
  onEnable,
  onDisable,
}: {
  skill: Skill
  pending: boolean
  onCreateVersion: (id: string, body: SkillContent) => void
  onEnable: (id: string, version: number, expected: number | null) => void
  onDisable: (id: string, expected: number | null) => void
}) {
  const latest = skill.versions.find((version) => version.version === skill.latest_version) ?? skill.versions.at(-1)
  const blocked = pending
  const versionDialogId = useId()
  const [scopes, setScopes] = useState<SkillScope[]>(latest?.applicable_scope ?? [{cluster_id: "", namespace: null}])
  const [references, setReferences] = useState<MCPReference[]>(latest?.required_mcp ?? [])
  return <article className="flex min-w-0 flex-col gap-4 border-t pt-5">
    <div className="flex flex-col gap-3 lg:flex-row lg:items-start">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="font-medium">{skill.name}</h3>
          <Badge variant={skill.enabled ? "positive" : "secondary"}>{skill.enabled ? `当前 v${skill.active_version}` : "已停用"}</Badge>
          <Badge variant="outline">最新 v{skill.latest_version}</Badge>
          <DependencyBadge dependency={skill.availability} />
        </div>
        <div className="mt-1 break-all text-xs text-muted-foreground">{skill.id}</div>
      </div>
      <div className="flex flex-wrap gap-2">
        {latest ? <FormDialog
          trigger={<><PlusIcon data-icon="inline-start" />创建新版本</>}
          triggerVariant="outline"
          triggerSize="sm"
          title={`${skill.name} · 创建新版本`}
          description="保留历史版本并创建新的不可变 Skill 内容。"
          submitLabel="保存版本"
          pending={pending}
          contentClassName="sm:max-w-2xl"
          onSubmit={(form) => onCreateVersion(skill.id, {
            instruction: String(form.get("instruction") ?? ""),
            workflow: String(form.get("workflow") ?? "").split("\n").map((step) => step.trim()).filter(Boolean),
            applicable_scope: scopes,
            required_mcp: references,
          })}
        >
          <SkillContentFields id={versionDialogId} initial={latest} scopes={scopes} onScopesChange={setScopes} references={references} onReferencesChange={setReferences} />
        </FormDialog> : null}
        {skill.enabled ? <Button type="button" size="sm" variant="outline" disabled={blocked} onClick={() => onDisable(skill.id, skill.active_version)}><CirclePowerIcon />停用</Button> : null}
      </div>
    </div>

    <section aria-labelledby={`versions-${safeId(skill.id)}`}>
      <h4 id={`versions-${safeId(skill.id)}`} className="text-sm font-medium">版本历史</h4>
      <div className="mt-2 divide-y border-y">
        {skill.versions.map((version) => <div key={version.version} className="py-4">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">v{version.version}</span>
            {version.version === skill.active_version ? <Badge variant="positive">当前</Badge> : null}
            <DependencyBadge dependency={version.dependency} />
            {version.version !== skill.active_version ? <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={blocked || version.dependency.state !== "ready"}
              onClick={() => onEnable(skill.id, version.version, skill.active_version)}
            ><CirclePowerIcon />切换到 v{version.version}</Button> : null}
          </div>
          {version.dependency.reason_code ? <p className="mt-2 text-xs text-destructive">{version.dependency.reason_code}</p> : null}
          <p className="mt-3 whitespace-pre-wrap text-sm">{version.instruction || "无 instruction"}</p>
          {version.workflow.length ? <ol className="mt-2 list-decimal space-y-1 pl-5 text-sm text-muted-foreground">{version.workflow.map((step) => <li key={step}>{step}</li>)}</ol> : null}
          <div className="mt-3 grid gap-3 text-xs text-muted-foreground lg:grid-cols-2">
            <div><div className="font-medium text-foreground">适用范围</div>{version.applicable_scope.map(scopeLabel).map((scope) => <div key={scope}>{scope}</div>)}</div>
            <div><div className="font-medium text-foreground">Required MCP</div>{version.required_mcp.length ? version.required_mcp.map((reference) => <div key={`${reference.integration_id}:${reference.name}`} className="break-all">{reference.integration_id} · {reference.integration_revision} · {reference.name} · {reference.version}</div>) : <div>无</div>}</div>
          </div>
          <div className="mt-3 text-xs text-muted-foreground">{version.created_by} · {version.reason} · {new Date(version.created_at * 1000).toLocaleString()}</div>
        </div>)}
      </div>
    </section>
  </article>
}


function SkillContentFields({
  id,
  initial,
  scopes,
  onScopesChange,
  references,
  onReferencesChange,
}: {
  id: string
  initial?: SkillContent
  scopes: SkillScope[]
  onScopesChange: (scopes: SkillScope[]) => void
  references: MCPReference[]
  onReferencesChange: (references: MCPReference[]) => void
}) {
  return <>
    <div className="grid gap-3 lg:grid-cols-2">
      <Field><FieldLabel htmlFor={`${id}-instruction`}>Instruction</FieldLabel><Textarea id={`${id}-instruction`} name="instruction" defaultValue={initial?.instruction} maxLength={8000} /></Field>
      <Field><FieldLabel htmlFor={`${id}-workflow`}>Workflow（每行一步）</FieldLabel><Textarea id={`${id}-workflow`} name="workflow" defaultValue={initial?.workflow.join("\n")} /></Field>
    </div>
    <ScopeFields id={id} scopes={scopes} onChange={onScopesChange} />
    <ReferenceFields id={id} references={references} onChange={onReferencesChange} />
  </>
}


function ScopeFields({id, scopes, onChange}: {id: string; scopes: SkillScope[]; onChange: (scope: SkillScope[]) => void}) {
  return <fieldset className="flex flex-col gap-2">
    <legend className="text-sm font-medium">适用范围</legend>
    {scopes.map((scope, index) => <div key={index} className="grid gap-2 sm:grid-cols-[1fr_1fr_auto]">
      <Input aria-label={`Skill Cluster ${index + 1}`} value={scope.cluster_id} placeholder="Cluster ID" required onChange={(event) => onChange(scopes.map((item, itemIndex) => itemIndex === index ? {...item, cluster_id: event.target.value} : item))} />
      <Input aria-label={`Skill Namespace ${index + 1}`} value={scope.namespace ?? ""} placeholder="Namespace，留空表示整个 Cluster" onChange={(event) => onChange(scopes.map((item, itemIndex) => itemIndex === index ? {...item, namespace: event.target.value || null} : item))} />
      <Button type="button" size="icon-sm" variant="ghost" aria-label={`删除 Skill 范围 ${index + 1}`} disabled={scopes.length === 1} onClick={() => onChange(scopes.filter((_, itemIndex) => itemIndex !== index))}><Trash2Icon /></Button>
    </div>)}
    <div><Button type="button" size="sm" variant="outline" onClick={() => onChange([...scopes, {cluster_id: "", namespace: null}])}><PlusIcon />添加范围</Button></div>
  </fieldset>
}


function ReferenceFields({id, references, onChange}: {id: string; references: MCPReference[]; onChange: (references: MCPReference[]) => void}) {
  const update = (index: number, field: keyof MCPReference, value: string) => onChange(
    references.map((item, itemIndex) => itemIndex === index ? {...item, [field]: value} : item),
  )
  return <fieldset className="flex flex-col gap-2">
    <legend className="text-sm font-medium">Required MCP references</legend>
    {references.map((reference, index) => <div key={index} className="grid gap-2 lg:grid-cols-[1fr_1fr_1fr_1fr_auto]">
      {(["integration_id", "integration_revision", "name", "version"] as const).map((field) => <Input key={field} aria-label={`${id} MCP ${index + 1} ${field}`} value={reference[field]} placeholder={field} required onChange={(event) => update(index, field, event.target.value)} />)}
      <Button type="button" size="icon-sm" variant="ghost" aria-label={`删除 MCP reference ${index + 1}`} onClick={() => onChange(references.filter((_, itemIndex) => itemIndex !== index))}><Trash2Icon /></Button>
    </div>)}
    <div><Button type="button" size="sm" variant="outline" onClick={() => onChange([...references, {integration_id: "", integration_revision: "", name: "", version: ""}])}><PlusIcon />添加 MCP dependency</Button></div>
  </fieldset>
}


function DependencyBadge({dependency}: {dependency: Skill["availability"]}) {
  const ready = dependency.state === "ready"
  return <Badge variant={ready ? "positive" : dependency.state === "unavailable" ? "destructive" : "secondary"}>{ready ? "依赖可用" : dependency.state === "unavailable" ? "依赖不可用" : "依赖已停用"}</Badge>
}


function AuditHistory({audit}: {audit: AdminAuditEntry[]}) {
  return <section className="border-t pt-6" aria-labelledby="skill-audit-heading">
    <h2 id="skill-audit-heading" className="text-base font-semibold">最近审计</h2>
    <div className="mt-3 divide-y border-y text-sm">
      {audit.map((entry) => <div key={entry.id} className="grid gap-1 py-3 lg:grid-cols-[1.2fr_1fr_1fr_1fr] lg:gap-4">
        <div><div className="font-medium">{entry.action}</div><div className="text-xs text-muted-foreground">{entry.target_id ?? "-"} · {entry.result}</div></div>
        <div><div>{entry.actor_id ?? "未知 actor"}</div><div className="break-all text-xs text-muted-foreground">{entry.request_id}</div></div>
        <div className="break-words text-muted-foreground">{entry.reason}</div>
        <time className="text-muted-foreground" dateTime={new Date(entry.created_at * 1000).toISOString()}>{new Date(entry.created_at * 1000).toLocaleString()}</time>
      </div>)}
      {audit.length === 0 ? <div className="py-8 text-center text-muted-foreground">暂无 Skill 审计</div> : null}
    </div>
  </section>
}


function scopeLabel(scope: SkillScope) {
  return `${scope.cluster_id} / ${scope.namespace ?? "全部 namespace"}`
}


function safeId(value: string) {
  return value.replace(/[^a-zA-Z0-9_-]/g, "-")
}
