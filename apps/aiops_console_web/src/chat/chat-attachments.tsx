import { useEffect, useState } from "react"
import {
  AttachmentPrimitive,
  ComposerPrimitive,
  MessagePrimitive,
  type Attachment,
  type CompleteAttachment,
  useAuiState,
} from "@assistant-ui/react"
import { Download, FileText, Image, RefreshCw, Trash2 } from "lucide-react"

import { chatAttachmentDownloadUrl, type ChatAttachment } from "@/chat/chat-client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"

const statusLabels: Record<ChatAttachment["status"], string> = {
  pending: "等待上传",
  uploading: "上传中",
  scanning: "安全扫描中",
  ready: "已就绪",
  rejected: "已拒绝",
  failed: "处理失败",
}

const rejectionLabels: Record<string, string> = {
  sensitive_content: "疑似凭据或 Secure Input",
  sensitive_filename: "疑似凭据或证书文件",
  malware_detected: "恶意文件扫描未通过",
  scanner_unavailable: "安全扫描服务不可用",
  scanner_error: "安全扫描失败",
  storage_unavailable: "附件存储暂时不可用",
  parse_failed: "文件解析失败",
  parse_limit: "文件内容超过解析限制",
  mime_mismatch: "文件格式与声明不一致",
  attachment_too_large: "文件超过 20MB",
}

const parseLabels: Record<ChatAttachment["parse_state"], string> = {
  pending: "等待解析",
  parsing: "解析中",
  ready: "解析完成",
  rejected: "已拒绝",
  failed: "解析失败",
}

function formatSize(size: number) {
  return size >= 1024 * 1024 ? `${(size / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.ceil(size / 1024))} KB`
}

function LocalImage({attachment}: {attachment: Attachment}) {
  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    if (!attachment.file || !attachment.contentType?.startsWith("image/")) return
    const next = URL.createObjectURL(attachment.file)
    setUrl(next)
    return () => URL.revokeObjectURL(next)
  }, [attachment.file, attachment.contentType])
  return url ? <img src={url} alt="" className="size-full object-cover" /> : null
}

function Status({attachment, gateway}: {attachment?: Attachment; gateway?: ChatAttachment}) {
  const failed = gateway?.status === "failed" || gateway?.status === "rejected" || attachment?.status.type === "incomplete"
  const label = gateway
    ? statusLabels[gateway.status]
    : attachment?.status.type === "running"
      ? "上传中"
      : attachment?.status.type === "incomplete" ? "处理失败" : "等待发送"
  return <Badge variant={failed ? "destructive" : "outline"} className={failed ? undefined : "bg-background text-foreground"}>{label}</Badge>
}

function Details({attachment}: {attachment: ChatAttachment}) {
  return (
    <details className="mt-1 text-xs text-muted-foreground">
      <summary className="w-fit cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">处理详情</summary>
      <p className="mt-1">解析状态：{parseLabels[attachment.parse_state]}；模型使用：{attachment.model_use_status === "included" ? "已纳入" : "未使用"}</p>
      {attachment.rejection_code ? <p className="mt-1 text-destructive">{rejectionLabels[attachment.rejection_code] ?? "无法处理附件"}</p> : null}
    </details>
  )
}

function IconAction({label, children}: {label: string; children: React.ReactElement}) {
  return <Tooltip><TooltipTrigger render={children} /><TooltipContent>{label}</TooltipContent></Tooltip>
}

function PrimitiveItem({
  attachment,
  gateway,
  busy,
  removable = false,
  onRetry,
}: {
  attachment: Attachment | CompleteAttachment
  gateway?: ChatAttachment
  busy?: boolean
  removable?: boolean
  onRetry?: (attachmentId: string) => void
}) {
  const image = attachment.contentType?.startsWith("image/")
  const remoteImage = gateway?.status === "ready" && image ? chatAttachmentDownloadUrl(gateway.session_id, gateway.id) : null
  return (
    <AttachmentPrimitive.Root className="flex min-w-0 items-start gap-2 py-2 text-sm">
      <AttachmentPrimitive.unstable_Thumb className="grid size-10 shrink-0 place-items-center overflow-hidden rounded border bg-muted text-xs font-medium uppercase">
        {remoteImage ? <img src={remoteImage} alt="" className="size-full object-cover" /> : attachment.file && image ? <LocalImage attachment={attachment} /> : image ? <Image className="size-4" aria-hidden="true" /> : <FileText className="size-4" aria-hidden="true" />}
      </AttachmentPrimitive.unstable_Thumb>
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium"><AttachmentPrimitive.Name /></p>
        <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span>{formatSize(gateway?.size ?? attachment.file?.size ?? 0)}</span>
          <Status attachment={attachment} gateway={gateway} />
        </div>
        {attachment.status.type === "running" ? <progress aria-label={`${attachment.name} 上传进度`} className="mt-2 h-1.5 w-full" /> : null}
        {gateway ? <Details attachment={gateway} /> : attachment.status.type === "incomplete" ? <p className="mt-1 break-words text-xs text-destructive">{attachment.status.message ?? "附件处理失败"}</p> : null}
      </div>
      {gateway?.status === "failed" && onRetry ? <IconAction label="重试附件"><Button type="button" size="icon-sm" variant="ghost" aria-label={`重试 ${attachment.name}`} onClick={() => onRetry(gateway.id)} disabled={busy}><RefreshCw /></Button></IconAction> : null}
      {gateway?.status === "ready" ? <IconAction label="下载附件"><Button type="button" size="icon-sm" variant="ghost" aria-label={`下载 ${attachment.name}`} render={<a href={chatAttachmentDownloadUrl(gateway.session_id, gateway.id)} />}><Download /></Button></IconAction> : null}
      {removable ? <IconAction label="移除附件"><AttachmentPrimitive.Remove render={<Button type="button" size="icon-sm" variant="ghost" aria-label={`移除 ${attachment.name}`} disabled={busy} />}><Trash2 /></AttachmentPrimitive.Remove></IconAction> : null}
    </AttachmentPrimitive.Root>
  )
}

function PersistedItem({attachment, busy, onRemove, onRetry}: {attachment: ChatAttachment; busy: boolean; onRemove: (id: string) => void; onRetry: (id: string) => void}) {
  const image = attachment.content_type.startsWith("image/")
  return <li className="flex min-w-0 items-start gap-2 py-2 text-sm">
    <div className="grid size-10 shrink-0 place-items-center overflow-hidden rounded border bg-muted">
      {image && attachment.status === "ready" ? <img src={chatAttachmentDownloadUrl(attachment.session_id, attachment.id)} alt="" className="size-full object-cover" /> : image ? <Image className="size-4" aria-hidden="true" /> : <FileText className="size-4" aria-hidden="true" />}
    </div>
    <div className="min-w-0 flex-1"><p className="truncate font-medium">{attachment.filename}</p><div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground"><span>{formatSize(attachment.size)}</span><Status gateway={attachment} /></div><Details attachment={attachment} /></div>
    {attachment.status === "failed" ? <IconAction label="重试附件"><Button type="button" size="icon-sm" variant="ghost" aria-label={`重试 ${attachment.filename}`} onClick={() => onRetry(attachment.id)} disabled={busy}><RefreshCw /></Button></IconAction> : null}
    {attachment.status === "ready" ? <IconAction label="下载附件"><Button type="button" size="icon-sm" variant="ghost" aria-label={`下载 ${attachment.filename}`} render={<a href={chatAttachmentDownloadUrl(attachment.session_id, attachment.id)} />}><Download /></Button></IconAction> : null}
    <IconAction label="移除附件"><Button type="button" size="icon-sm" variant="ghost" aria-label={`移除 ${attachment.filename}`} onClick={() => onRemove(attachment.id)} disabled={busy}><Trash2 /></Button></IconAction>
  </li>
}

export function ChatComposerAttachments({attachments, busy, onRemove, onRetry}: {attachments: ChatAttachment[]; busy: boolean; onRemove: (id: string) => void; onRetry: (id: string) => void}) {
  const localAttachments = useAuiState((state) => state.composer.attachments)
  const localIds = localAttachments.map((attachment) => attachment.id)
  const restored = attachments.filter((attachment) => !attachment.message_id && !localIds.includes(attachment.id))
  if (!localIds.length && !restored.length) return null
  return <ul className="mb-2 divide-y border-y" aria-label="待发送附件">
    <ComposerPrimitive.Attachments>{({attachment}) => <li><PrimitiveItem attachment={attachment} gateway={attachments.find((item) => item.id === attachment.id)} busy={busy} removable onRetry={onRetry} /></li>}</ComposerPrimitive.Attachments>
    {restored.map((attachment) => <PersistedItem key={attachment.id} attachment={attachment} busy={busy} onRemove={onRemove} onRetry={onRetry} />)}
  </ul>
}

export function ChatMessageAttachments({attachments}: {attachments: ChatAttachment[]}) {
  if (!attachments.length) return null
  return <details className="mt-3 border-t pt-2 text-xs"><summary className="w-fit cursor-pointer font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">附件（{attachments.length}）</summary>
    <ul className="mt-1 divide-y"><MessagePrimitive.Attachments>{({attachment}) => <li><PrimitiveItem attachment={attachment} gateway={attachments.find((item) => item.id === attachment.id)} /></li>}</MessagePrimitive.Attachments></ul>
  </details>
}
