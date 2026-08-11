import { useState, type ReactNode } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"

import { mutateAdmin, type AdminMutation, type AdminTeam, type AdminUser } from "@/admin/admin-client"
import { useAdminAction } from "@/admin/admin-action"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { FieldGroup } from "@/components/ui/field"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

export function ResourceTable({headings, rows, empty}: {headings: ReactNode[]; rows: ReactNode[][]; empty?: string}) {
  return (
    <div className="overflow-x-auto rounded-xl bg-card ring-1 ring-foreground/10">
      <Table>
        <TableHeader><TableRow>{headings.map((heading, index) => <TableHead key={index}>{heading}</TableHead>)}</TableRow></TableHeader>
        <TableBody>
          {rows.length === 0 && empty ? (
            <TableRow><TableCell colSpan={headings.length} className="py-10 text-center text-muted-foreground">{empty}</TableCell></TableRow>
          ) : rows.map((cells, index) => <TableRow key={index}>{cells.map((cell, cellIndex) => <TableCell key={cellIndex}>{cell}</TableCell>)}</TableRow>)}
        </TableBody>
      </Table>
    </div>
  )
}

export function Status({active}: {active: boolean}) {
  return <Badge variant={active ? "positive" : "secondary"}>{active ? "启用" : "停用"}</Badge>
}

export function ToggleButton({active, disabled, onClick}: {active: boolean; disabled: boolean; onClick: () => void}) {
  return <Button type="button" size="sm" variant={active ? "destructive" : "outline"} disabled={disabled} onClick={onClick}>{active ? "停用" : "启用"}</Button>
}

export function FormDialog({open, onOpenChange, trigger, triggerVariant, triggerSize, contentClassName, title, description, submitLabel, submitDisabled, pending, onSubmit, children}: {
  /** 传入即为受控模式（无触发按钮，由外部状态驱动） */
  open?: boolean
  onOpenChange?: (open: boolean) => void
  trigger?: ReactNode
  triggerVariant?: React.ComponentProps<typeof Button>["variant"]
  triggerSize?: React.ComponentProps<typeof Button>["size"]
  contentClassName?: string
  title: string
  description?: string
  submitLabel: string
  submitDisabled?: boolean
  pending?: boolean
  /** 返回 false 时保持弹窗打开（用于表单内校验失败或保存后继续编辑） */
  onSubmit: (form: FormData) => void | false
  children: ReactNode
}) {
  const [internalOpen, setInternalOpen] = useState(false)
  const controlled = open !== undefined
  const setOpen = (next: boolean) => controlled ? onOpenChange?.(next) : setInternalOpen(next)
  return (
    <Dialog open={controlled ? open : internalOpen} onOpenChange={setOpen}>
      {controlled ? null : <DialogTrigger render={<Button type="button" variant={triggerVariant} size={triggerSize} />}>{trigger}</DialogTrigger>}
      <DialogContent className={contentClassName}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {description ? <DialogDescription>{description}</DialogDescription> : null}
        </DialogHeader>
        <form
          onSubmit={(event) => {
            event.preventDefault()
            if (onSubmit(new FormData(event.currentTarget)) !== false) setOpen(false)
          }}
        >
          <FieldGroup>{children}</FieldGroup>
          <DialogFooter className="mt-5">
            <Button type="submit" disabled={pending || submitDisabled}>{submitLabel}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

export function SelectionBar({count, onClear, children}: {count: number; onClear: () => void; children: ReactNode}) {
  if (count === 0) return null
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-muted/50 px-3 py-2 animate-in fade-in slide-in-from-top-1 duration-150 motion-reduce:animate-none">
      <span className="text-sm text-muted-foreground">已选 {count} 项</span>
      <div className="ml-auto flex flex-wrap gap-2">
        {children}
        <Button type="button" size="sm" variant="ghost" onClick={onClear}>清除</Button>
      </div>
    </div>
  )
}

export function useRowSelection() {
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set())
  const toggle = (id: string, checked: boolean) => setSelected((prev) => {
    const next = new Set(prev)
    if (checked) next.add(id)
    else next.delete(id)
    return next
  })
  const toggleAll = (ids: string[], checked: boolean) => setSelected(checked ? new Set(ids) : new Set())
  const clear = () => setSelected(new Set())
  return {selected, toggle, toggleAll, clear, count: selected.size}
}

type BatchOptions = {
  verb: string
  label: string
  ids: string[]
  destructive?: boolean
  onDone?: () => void
  run: (id: string, reason: string) => Promise<unknown>
}

/** 整批变更包成一次治理动作：共用一条变更原因，顺序执行，fresh-auth 重试安全（PATCH 幂等）。 */
export function useAdminBatch() {
  const requestAction = useAdminAction()
  return ({verb, label, ids, destructive, onDone, run}: BatchOptions) => {
    requestAction({
      title: `批量${verb}${label}`,
      summary: `将对 ${ids.length} 个${label}执行${verb}，整批共用一条变更原因。`,
      destructive,
      run: async (reason) => {
        for (const [index, id] of ids.entries()) {
          try {
            await run(id, reason)
          } catch (cause) {
            throw new Error(`已处理 ${index}/${ids.length}，${id} 失败：${cause instanceof Error ? cause.message : "请求失败"}`)
          }
        }
        onDone?.()
      },
    })
  }
}

type ActiveBatchResource = "users" | "teams" | "team-memberships" | "role-bindings" | "connector-enrollments"

export function useAdminMutation() {
  const queryClient = useQueryClient()
  const requestAction = useAdminAction()
  const batch = useAdminBatch()
  const [issuedCredential, setIssuedCredential] = useState("")
  const mutation = useMutation({
    mutationFn: mutateAdmin,
    onSuccess: (result) => {
      setIssuedCredential(result.credential ?? "")
      queryClient.invalidateQueries({queryKey: ["admin"]})
      queryClient.invalidateQueries({queryKey: ["connectors"]})
      queryClient.invalidateQueries({queryKey: ["resource-catalog"]})
    },
  })
  const submit = (change: AdminMutation) => {
    const details = adminMutationDetails(change)
    requestAction({
      ...details,
      run: (reason) => mutation.mutateAsync({...change, body: {...change.body, reason}} as AdminMutation),
    })
  }
  const submitActiveBatch = (resource: ActiveBatchResource, label: string, ids: string[], active: boolean, onDone?: () => void) => {
    batch({
      verb: active ? "启用" : "停用",
      label,
      ids,
      destructive: !active,
      onDone,
      run: (id, reason) => mutation.mutateAsync({resource, id, body: {active, reason}} as AdminMutation),
    })
  }
  return {submit, submitBatch: batch, submitActiveBatch, mutate: mutation.mutateAsync, issuedCredential, pending: mutation.isPending}
}

export function userName(users: AdminUser[], id: string) {
  return users.find((user) => user.id === id)?.display_name ?? id
}

export function teamName(teams: AdminTeam[], id: string) {
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
