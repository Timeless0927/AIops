import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { BellOffIcon, RotateCcwIcon } from "lucide-react"

import {
  createNotificationSilence,
  getNotificationDeliveries,
  getNotificationDestinations,
  getNotificationSilences,
  redeliverNotificationDelivery,
  updateNotificationNoiseControl,
} from "@/admin/notification-client"
import { useAdminAction } from "@/admin/admin-action"
import { FormDialog } from "@/admin/admin-shared"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"

export function NotificationNoiseAdmin() {
  const queryClient = useQueryClient()
  const requestAction = useAdminAction()
  const destinations = useQuery({queryKey: ["notification-destinations"], queryFn: getNotificationDestinations, retry: false})
  const silences = useQuery({queryKey: ["notification-silences"], queryFn: getNotificationSilences, retry: false})
  const deliveries = useQuery({queryKey: ["notification-deliveries"], queryFn: getNotificationDeliveries, retry: false})
  const [selectedId, setSelectedId] = useState("")
  const refresh = () => {
    queryClient.invalidateQueries({queryKey: ["notification-destinations"]})
    queryClient.invalidateQueries({queryKey: ["notification-silences"]})
    queryClient.invalidateQueries({queryKey: ["notification-deliveries"]})
  }
  const mutation = useMutation({mutationFn: (work: () => Promise<unknown>) => work(), onSuccess: refresh})

  if (destinations.isPending || silences.isPending || deliveries.isPending) return <div className="py-6 text-center text-sm text-muted-foreground" role="status">正在加载噪声控制</div>
  if (destinations.isError || silences.isError || deliveries.isError) return null
  const selected = destinations.data.destinations.find((item) => item.id === selectedId) ?? destinations.data.destinations[0]

  return <section className="flex flex-col gap-5 border-t pt-6" aria-labelledby="noise-heading">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <h2 id="noise-heading" className="text-lg font-semibold">噪声控制</h2>
      <div className="flex flex-wrap gap-2">
        {selected ? <FormDialog
          key={selected.id}
          trigger="编辑噪声控制"
          triggerVariant="outline"
          title="编辑噪声控制"
          description="配置 quiet hours、速率限制和 digest。"
          submitLabel="保存"
          pending={mutation.isPending}
          onSubmit={(form) => {
            const start = String(form.get("quiet_start") || "")
            const end = String(form.get("quiet_end") || "")
            const hourly = String(form.get("hourly_limit") || "")
            const digest = String(form.get("digest_minutes") || "")
            const body = {
              timezone: String(form.get("timezone") || "UTC"),
              quiet_hours: start && end ? {start, end} : null,
              hourly_limit: hourly ? Number(hourly) : null,
              digest_interval_seconds: digest ? Number(digest) * 60 : null,
            }
            requestAction({title: "保存 Notification 噪声控制", summary: `将更新 Destination ${selected.name} 的 quiet hours 与投递频率限制。`, run: (reason) => mutation.mutateAsync(() => updateNotificationNoiseControl(selected.id, {...body, reason}))})
          }}
        >
          <Field><FieldLabel htmlFor="noise-destination">Destination</FieldLabel><Select value={selected.id} onValueChange={(value) => setSelectedId(value ?? "")}><SelectTrigger id="noise-destination" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectGroup>{destinations.data.destinations.map((item) => <SelectItem key={item.id} value={item.id}>{item.name}</SelectItem>)}</SelectGroup></SelectContent></Select></Field>
          <Field><FieldLabel htmlFor="noise-timezone">Timezone</FieldLabel><Input id="noise-timezone" name="timezone" defaultValue={selected.noise_control.timezone} required /></Field>
          <Field><FieldLabel htmlFor="quiet-start">Quiet hours</FieldLabel><div className="grid grid-cols-2 gap-2"><Input id="quiet-start" name="quiet_start" type="time" defaultValue={selected.noise_control.quiet_hours?.start} aria-label="Quiet hours start" /><Input name="quiet_end" type="time" defaultValue={selected.noise_control.quiet_hours?.end} aria-label="Quiet hours end" /></div></Field>
          <Field><FieldLabel htmlFor="hourly-limit">Hourly limit</FieldLabel><Input id="hourly-limit" name="hourly_limit" type="number" min="1" max="10000" defaultValue={selected.noise_control.hourly_limit ?? ""} /></Field>
          <Field><FieldLabel htmlFor="digest-minutes">Digest（分钟）</FieldLabel><Input id="digest-minutes" name="digest_minutes" type="number" min="1" max="1440" defaultValue={selected.noise_control.digest_interval_seconds ? selected.noise_control.digest_interval_seconds / 60 : ""} /></Field>
        </FormDialog> : null}
        <FormDialog
          trigger={<><BellOffIcon data-icon="inline-start" />创建 Silence</>}
          title="创建 Notification Silence"
          description="在明确范围和期限内抑制匹配通知。"
          submitLabel="创建 Silence"
          pending={mutation.isPending}
          onSubmit={(form) => {
            const match = Object.fromEntries(["event", "severity", "environment", "team", "service"].map((key) => [key, split(form.get(key))]).filter(([, values]) => (values as string[]).length))
            const expiresAt = new Date(String(form.get("expires_at"))).getTime() / 1000
            requestAction({title: "创建 Notification Silence", summary: `将抑制匹配范围的通知，直到 ${new Date(expiresAt * 1000).toLocaleString()}。`, destructive: true, run: (reason) => mutation.mutateAsync(() => createNotificationSilence({match, expires_at: expiresAt, reason}))})
          }}
        >
          <Field><FieldLabel htmlFor="silence-event">Event</FieldLabel><Input id="silence-event" name="event" /></Field>
          <Field><FieldLabel htmlFor="silence-severity">Severity</FieldLabel><Input id="silence-severity" name="severity" /></Field>
          <Field><FieldLabel htmlFor="silence-environment">Environment</FieldLabel><Input id="silence-environment" name="environment" /></Field>
          <Field><FieldLabel htmlFor="silence-team">Team</FieldLabel><Input id="silence-team" name="team" /></Field>
          <Field><FieldLabel htmlFor="silence-service">Service</FieldLabel><Input id="silence-service" name="service" /></Field>
          <Field><FieldLabel htmlFor="silence-expiry">到期时间</FieldLabel><Input id="silence-expiry" name="expires_at" type="datetime-local" required /></Field>
        </FormDialog>
      </div>
    </div>
    {selected ? null : <div className="text-sm text-muted-foreground">暂无 Destination</div>}

    <Table><TableHeader><TableRow><TableHead>Scope</TableHead><TableHead>原因</TableHead><TableHead>到期时间</TableHead><TableHead>状态</TableHead></TableRow></TableHeader><TableBody>{silences.data.silences.map((silence) => <TableRow key={silence.id}><TableCell className="text-xs text-muted-foreground">{Object.entries(silence.match).map(([key, value]) => `${key}=${value.join("|")}`).join(" · ") || "全部"}</TableCell><TableCell>{silence.reason}</TableCell><TableCell>{new Date(silence.expires_at * 1000).toLocaleString()}</TableCell><TableCell><Badge variant={silence.active ? "warning" : "secondary"}>{silence.active ? "生效中" : "已到期"}</Badge></TableCell></TableRow>)}</TableBody></Table>

    <div className="flex flex-wrap items-center justify-between gap-2 border-t pt-5"><h3 className="text-sm font-semibold">Delivery results</h3><Badge variant="outline">At least once</Badge></div>
    <Table><TableHeader><TableRow><TableHead>Event</TableHead><TableHead>Destination</TableHead><TableHead>结果</TableHead><TableHead>状态</TableHead><TableHead>尝试</TableHead><TableHead>下次尝试</TableHead><TableHead className="w-12"><span className="sr-only">操作</span></TableHead></TableRow></TableHeader><TableBody>{deliveries.data.deliveries.map((delivery) => <TableRow key={delivery.id}><TableCell><div className="font-medium">{delivery.summary}</div><div className="text-xs text-muted-foreground">{delivery.event_id}</div></TableCell><TableCell>{delivery.destination_id}</TableCell><TableCell><Badge variant="outline">{delivery.noise_result}</Badge>{delivery.noise_reason ? <div className="mt-1 text-xs text-muted-foreground">{delivery.noise_reason}</div> : null}</TableCell><TableCell>{delivery.status}</TableCell><TableCell><div>{delivery.attempt_count}</div>{delivery.last_error ? <div className="max-w-64 text-xs text-destructive">{delivery.last_error}</div> : null}</TableCell><TableCell>{delivery.next_attempt_at ? new Date(delivery.next_attempt_at * 1000).toLocaleString() : "-"}</TableCell><TableCell>{delivery.status === "dead_letter" ? <Button type="button" size="icon" variant="outline" title="重新投递" aria-label="重新投递" disabled={mutation.isPending} onClick={() => requestAction({title: "重新投递 Notification Delivery", summary: `将重新投递 dead-letter Delivery ${delivery.id}，可能再次产生外部通知。`, run: (reason) => mutation.mutateAsync(() => redeliverNotificationDelivery(delivery.id, reason))})}><RotateCcwIcon /></Button> : null}</TableCell></TableRow>)}</TableBody></Table>
  </section>
}

function split(value: FormDataEntryValue | null) { return String(value || "").split(",").map((item) => item.trim()).filter(Boolean) }
