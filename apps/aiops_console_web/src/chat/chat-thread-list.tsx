import { useState } from "react"
import { Archive, ArchiveRestore, MoreHorizontal, PanelLeftClose, Pencil, Pin, PinOff, Trash2 } from "lucide-react"
import { ThreadListItemPrimitive, ThreadListPrimitive } from "@assistant-ui/react"

import type { ChatSessionSummary } from "@/api/client"
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"

export function ChatThreadList({
  sessions,
  activeId,
  busy,
  actionBusy,
  query,
  filter,
  onCreate,
  onSelect,
  onQueryChange,
  onFilterChange,
  onRename,
  onPin,
  onArchive,
  onDelete,
  showCollapse = false,
  onCollapse,
}: {
  sessions: ChatSessionSummary[]
  activeId?: string
  busy: boolean
  actionBusy: boolean
  query: string
  filter: "all" | "normal" | "pinned" | "archived"
  onCreate: () => void
  onSelect: (id: string) => void
  onQueryChange: (query: string) => void
  onFilterChange: (filter: "all" | "normal" | "pinned" | "archived") => void
  onRename: (id: string, title: string) => void
  onPin: (id: string, pinned: boolean) => void
  onArchive: (id: string, archived: boolean) => void
  onDelete: (id: string) => void
  showCollapse?: boolean
  onCollapse?: () => void
}) {
  const [renameId, setRenameId] = useState<string | null>(null)
  const [renameTitle, setRenameTitle] = useState("")
  const [deleteId, setDeleteId] = useState<string | null>(null)
  const startRename = (item: ChatSessionSummary) => { setRenameId(item.id); setRenameTitle(item.title) }
  const commitRename = () => {
    const title = renameTitle.trim()
    if (!renameId || !title) return
    onRename(renameId, title)
    setRenameId(null)
  }
  return <div className="flex h-full min-h-0 flex-col p-4">
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0"><h1 className="text-xl font-semibold">AI 对话</h1><p className="mt-1 text-xs text-muted-foreground">对话长期保留，直到主动删除</p></div>
      <div className="flex shrink-0 items-center gap-1">
        {showCollapse ? <Tooltip><TooltipTrigger render={<Button type="button" size="icon-sm" variant="ghost" aria-label="折叠会话栏" onClick={onCollapse} />}><PanelLeftClose /></TooltipTrigger><TooltipContent>折叠会话栏</TooltipContent></Tooltip> : null}
        <Button size="sm" onClick={onCreate} disabled={busy || actionBusy}>新建对话</Button>
      </div>
    </div>
    <div className="mt-4 space-y-2">
      <Input aria-label="搜索 AI 对话" value={query} onChange={(event) => onQueryChange(event.target.value)} placeholder="搜索标题或消息" />
      <Select value={filter} onValueChange={(value) => onFilterChange(value as typeof filter)}>
        <SelectTrigger aria-label="筛选 AI 对话" className="w-full"><SelectValue>{filter === "all" ? "正常会话" : filter === "normal" ? "未置顶" : filter === "pinned" ? "已置顶" : "已归档"}</SelectValue></SelectTrigger>
        <SelectContent><SelectItem value="all">正常会话</SelectItem><SelectItem value="normal">未置顶</SelectItem><SelectItem value="pinned">已置顶</SelectItem><SelectItem value="archived">已归档</SelectItem></SelectContent>
      </Select>
    </div>
    <nav aria-label="AI 对话会话" className="mt-4 min-h-0 flex-1 overflow-y-auto">
      <ThreadListPrimitive.Root className="space-y-1">
        <ThreadListPrimitive.Items archived={filter === "archived"}>{({threadListItem}) => {
          const item = sessions.find((candidate) => candidate.id === threadListItem.id)
          if (!item) return null
          return <ThreadListItemPrimitive.Root key={item.id} className={`group flex min-w-0 items-center gap-1 rounded-lg ${item.id === activeId ? "bg-secondary" : "hover:bg-muted"}`}>
            {renameId === item.id ? <Input aria-label="重命名 AI 对话" autoFocus value={renameTitle} onChange={(event) => setRenameTitle(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") commitRename(); if (event.key === "Escape") setRenameId(null) }} className="h-8 min-w-0 flex-1" /> : <ThreadListItemPrimitive.Trigger render={<Button variant="ghost" className="h-auto min-w-0 flex-1 justify-start px-3 py-2 text-left hover:bg-transparent" />}>
              <span className="min-w-0"><span className="flex min-w-0 items-center gap-1 truncate">{item.pinned ? <Pin className="size-3 shrink-0 text-primary" aria-label="已置顶" /> : null}<span className="truncate">{item.title}</span></span><span className="block text-xs text-muted-foreground">{item.message_count} 条消息</span></span>
            </ThreadListItemPrimitive.Trigger>}
            {renameId === item.id ? <Button size="icon-sm" variant="ghost" aria-label="保存标题" onClick={commitRename} disabled={!renameTitle.trim()}><Pencil /></Button> : <DropdownMenu>
              <Tooltip><TooltipTrigger render={<DropdownMenuTrigger render={<Button size="icon-sm" variant="ghost" aria-label={`操作 ${item.title}`} />} />}><MoreHorizontal /></TooltipTrigger><TooltipContent>会话操作</TooltipContent></Tooltip>
              <DropdownMenuContent align="end" className="w-44"><DropdownMenuItem onClick={() => startRename(item)}><Pencil />重命名</DropdownMenuItem><DropdownMenuItem onClick={() => onPin(item.id, !item.pinned)}>{item.pinned ? <PinOff /> : <Pin />} {item.pinned ? "取消置顶" : "置顶"}</DropdownMenuItem><DropdownMenuItem onClick={() => onArchive(item.id, !item.archived)}>{item.archived ? <ArchiveRestore /> : <Archive />} {item.archived ? "恢复会话" : "归档会话"}</DropdownMenuItem><DropdownMenuSeparator /><DropdownMenuItem variant="destructive" onClick={() => setDeleteId(item.id)}><Trash2 />永久删除</DropdownMenuItem></DropdownMenuContent>
            </DropdownMenu>}
          </ThreadListItemPrimitive.Root>
        }}</ThreadListPrimitive.Items>
      </ThreadListPrimitive.Root>
      {!sessions.length ? <p className="py-8 text-center text-sm text-muted-foreground">{query.trim() ? "没有匹配的 AI 对话" : filter === "archived" ? "没有已归档会话" : "尚无 AI 对话会话"}</p> : null}
    </nav>
    <AlertDialog open={Boolean(deleteId)} onOpenChange={(open) => { if (!open) setDeleteId(null) }}>
      <AlertDialogContent><AlertDialogHeader><AlertDialogTitle>永久删除 AI 对话？</AlertDialogTitle><AlertDialogDescription>此操作会永久删除会话和未被事件调查引用的聊天数据，无法恢复。</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel>取消</AlertDialogCancel><AlertDialogAction onClick={() => { if (deleteId) onDelete(deleteId); setDeleteId(null) }}>永久删除</AlertDialogAction></AlertDialogFooter></AlertDialogContent>
    </AlertDialog>
  </div>
}
