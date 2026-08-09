import { useState } from "react"
import { useMutation } from "@tanstack/react-query"
import { KeyRoundIcon, LockKeyholeIcon } from "lucide-react"

import { createSecureInput } from "@/api/client"
import { ApiError, newClientId } from "@/api/transport"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"

function secureInputError(error: Error | null) {
  if (!(error instanceof ApiError)) return error ? "Secure Input 创建失败" : null
  return error.message
}

export function SecureInputForm({ onCreated }: { onCreated: (placeholder: string) => void }) {
  const [keyName, setKeyName] = useState("")
  const [value, setValue] = useState("")
  const [source, setSource] = useState<"user" | "generated">("user")
  const create = useMutation({
    mutationFn: () => createSecureInput(
      source === "user"
        ? { key_name: keyName, value, idempotency_key: newClientId() }
        : { key_name: keyName, generated_bytes: 32, idempotency_key: newClientId() },
    ),
    onSuccess: (input) => {
      onCreated(input.placeholder)
      setValue("")
    },
  })

  return <form className="grid gap-3 border-t pt-4" onSubmit={(event) => { event.preventDefault(); create.mutate() }}>
    <div className="flex items-center gap-2 text-sm font-medium"><LockKeyholeIcon className="size-4" />敏感输入（Secure Input）</div>
    <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_10rem]">
      <label className="grid gap-1.5 text-sm font-medium">
        键名（Key name）
        <Input value={keyName} onChange={(event) => setKeyName(event.target.value)} pattern="[A-Za-z][A-Za-z0-9_.-]{0,127}" required />
      </label>
      <label className="grid gap-1.5 text-sm font-medium">
        来源
        <Select value={source} onValueChange={(next) => setSource(next as typeof source)}>
          <SelectTrigger><SelectValue>{source === "user" ? "用户输入" : "Gateway CSPRNG 生成"}</SelectValue></SelectTrigger>
          <SelectContent><SelectItem value="user">用户输入</SelectItem><SelectItem value="generated">Gateway CSPRNG 生成</SelectItem></SelectContent>
        </Select>
      </label>
    </div>
    {source === "user" ? <label className="grid gap-1.5 text-sm font-medium">
      敏感值
      <Input type="password" autoComplete="new-password" value={value} onChange={(event) => setValue(event.target.value)} required />
    </label> : null}
    <div className="flex items-center justify-end gap-3">
      {create.isError ? <span role="alert" className="text-xs text-destructive">{secureInputError(create.error)}</span> : null}
      <Button type="submit" variant="outline" disabled={!keyName.trim() || (source === "user" && !value) || create.isPending}><KeyRoundIcon />创建敏感输入（Secure Input）</Button>
    </div>
  </form>
}
