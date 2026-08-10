import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { FlaskConicalIcon, SaveIcon, Trash2Icon } from "lucide-react"

import {
  deleteModelProvider,
  getModelProviderDetail,
  type ModelProviderDetail,
  type ModelProviderSave,
  saveModelProvider,
  testModelProvider,
} from "@/admin/admin-client"
import { useAdminAction } from "@/admin/admin-action"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"


type ModelProviderForm = Omit<ModelProviderSave, "reason">

export function ModelProviderAdmin() {
  const queryClient = useQueryClient()
  const requestAction = useAdminAction()
  const detail = useQuery({
    queryKey: ["model-provider"],
    queryFn: getModelProviderDetail,
    retry: false,
    refetchInterval: (query) => query.state.data?.verification.state === "verifying" ? 1000 : false,
  })
  const refresh = () => queryClient.invalidateQueries({queryKey: ["model-provider"]})
  const mutation = useMutation({mutationFn: (work: () => Promise<unknown>) => work(), onSuccess: refresh})

  if (detail.isPending) return <div className="py-10 text-center text-sm text-muted-foreground" role="status">正在加载模型配置</div>
  if (detail.isError) return <div className="py-10 text-center text-sm text-destructive">无法读取模型配置</div>

  return <ModelProviderAdminView
    detail={detail.data}
    pending={mutation.isPending}
    error={mutation.error instanceof Error ? mutation.error.message : null}
    onSave={(body) => requestAction({
      title: "保存 Model Provider revision",
      summary: `将保存 ${body.model} 的新配置 revision，并使新 Diagnosis Job 使用该配置。`,
      run: (reason) => mutation.mutateAsync(() => saveModelProvider({...body, reason})),
    })}
    onTest={() => {
      const revision = detail.data.configuration_revision
      if (revision) requestAction({
        title: "测试 Model Provider",
        summary: `将验证精确配置 revision ${revision} 的连通性与模型可用性。`,
        run: (reason) => mutation.mutateAsync(() => testModelProvider(revision, reason)),
      })
    }}
    onDelete={() => {
      const revision = detail.data.configuration_revision
      if (revision) requestAction({
        title: "删除 Model Provider",
        summary: `将删除配置 revision ${revision}，新的 Diagnosis Job 将无法启动模型推理。`,
        destructive: true,
        run: (reason) => mutation.mutateAsync(() => deleteModelProvider(revision, reason)),
      })
    }}
  />
}


export function ModelProviderAdminView({
  detail,
  pending,
  error,
  onSave,
  onTest,
  onDelete,
}: {
  detail: ModelProviderDetail
  pending: boolean
  error: string | null
  onSave: (body: ModelProviderForm) => void
  onTest: () => void
  onDelete: () => void
}) {
  const [scope, setScope] = useState<"external" | "cluster_internal">(
    detail.configuration?.endpoint_scope ?? "external",
  )
  const configuration = detail.configuration
  const revision = detail.configuration_revision

  return <div className="flex min-w-0 flex-col gap-6">
    {error ? <Alert variant="destructive"><AlertTitle>模型配置操作失败</AlertTitle><AlertDescription>{error}</AlertDescription></Alert> : null}

    <section className="grid gap-4 border-y py-4 md:grid-cols-3" aria-label="Model Provider 状态">
      <StatusItem label="Readiness" value={detail.readiness === "ready" ? "已就绪" : "未就绪"} positive={detail.readiness === "ready"} />
      <StatusItem label="Verification" value={verificationLabel(detail.verification.state)} positive={detail.verification.state === "verified"} />
      <StatusItem label="Availability" value={availabilityLabel(detail.availability.state)} positive={detail.availability.state === "available"} />
      {configuration ? <div className="min-w-0 md:col-span-3 text-sm">
        <span className="font-medium">{configuration.model}</span>
        <span className="ml-2 break-all text-muted-foreground">{configuration.endpoint}</span>
        <span className="ml-2 text-muted-foreground">{configuration.endpoint_scope} · {configuration.timeout_seconds}s</span>
      </div> : <div className="text-sm text-muted-foreground md:col-span-3">尚未配置</div>}
      {detail.verification.reason_code || detail.availability.reason_code ? <div className="text-xs text-muted-foreground md:col-span-3">{detail.verification.reason_code ?? detail.availability.reason_code}</div> : null}
    </section>

    <Accordion>
      <AccordionItem value="model-provider-configuration">
        <AccordionTrigger><span><span className="block font-medium">编辑 Model Provider</span><span className="mt-1 block text-xs font-normal text-muted-foreground">修改 endpoint、模型或凭据并创建新 revision</span></span></AccordionTrigger>
        <AccordionContent><form className="flex flex-col gap-4" onSubmit={(event) => {
      event.preventDefault()
      const form = new FormData(event.currentTarget)
      onSave({
        endpoint: String(form.get("endpoint") || ""),
        endpoint_scope: scope,
        model: String(form.get("model") || ""),
        timeout_seconds: Number(form.get("timeout_seconds")),
        api_key: String(form.get("api_key") || ""),
        expected_revision: revision,
      })
    }}>
      <FieldGroup className="grid gap-3 md:grid-cols-2 xl:grid-cols-[180px_2fr_1fr_140px]">
        <Field><FieldLabel htmlFor="model-endpoint-scope">Endpoint scope</FieldLabel><Select value={scope} onValueChange={(value) => setScope(value as typeof scope)}><SelectTrigger id="model-endpoint-scope" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectGroup><SelectItem value="external">external</SelectItem><SelectItem value="cluster_internal">cluster-internal</SelectItem></SelectGroup></SelectContent></Select></Field>
        <Field><FieldLabel htmlFor="model-endpoint">Endpoint</FieldLabel><Input id="model-endpoint" name="endpoint" type="url" defaultValue={configuration?.endpoint ?? ""} required /></Field>
        <Field><FieldLabel htmlFor="model-name">Model</FieldLabel><Input id="model-name" name="model" defaultValue={configuration?.model ?? ""} required /></Field>
        <Field><FieldLabel htmlFor="model-timeout">Timeout</FieldLabel><Input id="model-timeout" name="timeout_seconds" type="number" min="5" max="120" defaultValue={configuration?.timeout_seconds ?? 30} required /></Field>
        <Field className="md:col-span-2 xl:col-span-3"><FieldLabel htmlFor="model-api-key">API key</FieldLabel><Input id="model-api-key" name="api_key" type="password" autoComplete="new-password" required /></Field>
        <div className="flex items-end"><Button type="submit" disabled={pending}><SaveIcon />保存 revision</Button></div>
      </FieldGroup>
        </form></AccordionContent>
      </AccordionItem>
    </Accordion>

    <div className="flex flex-wrap gap-2">
      <Button type="button" variant="outline" disabled={!revision || pending || detail.verification.state === "verifying"} onClick={onTest}><FlaskConicalIcon />测试</Button>
      <Button type="button" variant="destructive" disabled={!revision || pending} onClick={onDelete}><Trash2Icon />删除</Button>
    </div>
  </div>
}


function StatusItem({label, value, positive}: {label: string; value: string; positive: boolean}) {
  return <div className="flex min-w-0 items-center justify-between gap-3"><span className="text-sm text-muted-foreground">{label}</span><Badge variant={positive ? "positive" : "secondary"}>{value}</Badge></div>
}

function verificationLabel(state: ModelProviderDetail["verification"]["state"]) {
  return ({not_applicable: "不适用", unverified: "未验证", verifying: "验证中", verified: "已验证", failed: "失败", stale: "已过期"})[state]
}

function availabilityLabel(state: ModelProviderDetail["availability"]["state"]) {
  return ({available: "可用", degraded: "降级", unavailable: "不可用"})[state]
}
