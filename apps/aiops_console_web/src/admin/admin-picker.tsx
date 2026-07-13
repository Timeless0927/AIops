import { useId } from "react"

import { Field, FieldLabel } from "@/components/ui/field"
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"

export function AdminPicker({
  label,
  value,
  onValueChange,
  items,
  disabled,
}: {
  label: string
  value: string
  onValueChange: (value: string) => void
  items: Array<{value: string; label: string}>
  disabled?: boolean
}) {
  const id = useId()
  return <Field data-disabled={disabled}>
    <FieldLabel htmlFor={id}>{label}</FieldLabel>
    <Select value={value} onValueChange={(next) => onValueChange(next ?? "")} disabled={disabled}>
      <SelectTrigger id={id} className="w-full"><SelectValue /></SelectTrigger>
      <SelectContent><SelectGroup>{items.map((item) => <SelectItem key={item.value} value={item.value}>{item.label}</SelectItem>)}</SelectGroup></SelectContent>
    </Select>
  </Field>
}
