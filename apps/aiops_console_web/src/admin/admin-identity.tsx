import { useState } from "react"
import { PlusIcon } from "lucide-react"

import type { AdminRoleBinding, AdminTeam, AdminTeamMembership, AdminUser } from "@/admin/admin-client"
import { AdminPicker as Picker } from "@/admin/admin-picker"
import {
  FormDialog,
  ResourceTable,
  SelectionBar,
  Status,
  ToggleButton,
  teamName,
  useAdminMutation,
  useRowSelection,
  userName,
} from "@/admin/admin-shared"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"

export function UsersTab({users, roleBindings}: {users: AdminUser[]; roleBindings: AdminRoleBinding[]}) {
  const {submit, submitActiveBatch, pending} = useAdminMutation()
  const selection = useRowSelection()
  const ids = users.map((user) => user.id)
  return (
    <div className="flex flex-col gap-4">
      <div className="flex justify-end">
        <FormDialog
          trigger={<><PlusIcon data-icon="inline-start" />新建用户</>}
          title="新建用户"
          description="创建可登录 Console 的人类身份。"
          submitLabel="创建用户"
          pending={pending}
          onSubmit={(form) => submit({resource: "users", body: {
            username: String(form.get("username") ?? ""),
            display_name: String(form.get("display_name") ?? ""),
            password: String(form.get("password") ?? ""),
            reason: "",
          }})}
        >
          <Field><FieldLabel htmlFor="username">用户名</FieldLabel><Input id="username" name="username" required /></Field>
          <Field><FieldLabel htmlFor="display-name">显示名称</FieldLabel><Input id="display-name" name="display_name" required /></Field>
          <Field><FieldLabel htmlFor="user-password">初始密码</FieldLabel><Input id="user-password" name="password" type="password" autoComplete="new-password" required /></Field>
        </FormDialog>
      </div>
      <SelectionBar count={selection.count} onClear={selection.clear}>
        <Button type="button" size="sm" variant="outline" disabled={pending} onClick={() => submitActiveBatch("users", "用户", [...selection.selected], true, selection.clear)}>批量启用</Button>
        <Button type="button" size="sm" variant="destructive" disabled={pending} onClick={() => submitActiveBatch("users", "用户", [...selection.selected], false, selection.clear)}>批量停用</Button>
      </SelectionBar>
      <ResourceTable
        empty="暂无用户"
        headings={[
          <Checkbox key="select-all" aria-label="选择全部用户" checked={ids.length > 0 && selection.count === ids.length} onCheckedChange={(checked) => selection.toggleAll(ids, checked)} />,
          "用户", "状态", "角色绑定", "操作",
        ]}
        rows={users.map((user) => [
          <Checkbox key="select" aria-label={`选择用户 ${user.display_name}`} checked={selection.selected.has(user.id)} onCheckedChange={(checked) => selection.toggle(user.id, checked)} />,
          <div key="identity"><div className="font-medium">{user.display_name}</div><div className="text-xs text-muted-foreground">{user.username}</div></div>,
          <Status key="status" active={user.active} />,
          <div key="roles" className="flex gap-1">{roleBindings.filter((binding) => binding.user_id === user.id && binding.active).map((binding) => <Badge key={binding.id} variant="outline">{binding.role === "platform_administrator" ? "平台管理员" : "SRE"}</Badge>)}</div>,
          <ToggleButton key="action" active={user.active} disabled={pending} onClick={() => submit({resource: "users", id: user.id, body: {active: !user.active, reason: ""}})} />,
        ])}
      />
    </div>
  )
}

export function TeamsTab({teams}: {teams: AdminTeam[]}) {
  const {submit, submitActiveBatch, pending} = useAdminMutation()
  const selection = useRowSelection()
  const ids = teams.map((team) => team.id)
  return (
    <div className="flex flex-col gap-4">
      <div className="flex justify-end">
        <FormDialog
          trigger={<><PlusIcon data-icon="inline-start" />新建团队</>}
          title="新建团队"
          description="维护 Service 的组织责任边界。"
          submitLabel="创建团队"
          pending={pending}
          onSubmit={(form) => submit({resource: "teams", body: {name: String(form.get("name") ?? ""), description: String(form.get("description") ?? ""), reason: ""}})}
        >
          <Field><FieldLabel htmlFor="team-name">团队名称</FieldLabel><Input id="team-name" name="name" required /></Field>
          <Field><FieldLabel htmlFor="team-description">说明</FieldLabel><Input id="team-description" name="description" /></Field>
        </FormDialog>
      </div>
      <SelectionBar count={selection.count} onClear={selection.clear}>
        <Button type="button" size="sm" variant="outline" disabled={pending} onClick={() => submitActiveBatch("teams", "团队", [...selection.selected], true, selection.clear)}>批量启用</Button>
        <Button type="button" size="sm" variant="destructive" disabled={pending} onClick={() => submitActiveBatch("teams", "团队", [...selection.selected], false, selection.clear)}>批量停用</Button>
      </SelectionBar>
      <ResourceTable
        empty="暂无团队"
        headings={[
          <Checkbox key="select-all" aria-label="选择全部团队" checked={ids.length > 0 && selection.count === ids.length} onCheckedChange={(checked) => selection.toggleAll(ids, checked)} />,
          "团队", "说明", "状态", "操作",
        ]}
        rows={teams.map((team) => [
          <Checkbox key="select" aria-label={`选择团队 ${team.name}`} checked={selection.selected.has(team.id)} onCheckedChange={(checked) => selection.toggle(team.id, checked)} />,
          <span key="name" className="font-medium">{team.name}</span>,
          <span key="description" className="text-muted-foreground">{team.description || "-"}</span>,
          <Status key="status" active={team.active} />,
          <ToggleButton key="action" active={team.active} disabled={pending} onClick={() => submit({resource: "teams", id: team.id, body: {active: !team.active, reason: ""}})} />,
        ])}
      />
    </div>
  )
}

export function MembershipsTab({users, teams, memberships}: {users: AdminUser[]; teams: AdminTeam[]; memberships: AdminTeamMembership[]}) {
  const {submit, submitActiveBatch, pending} = useAdminMutation()
  const [membershipUser, setMembershipUser] = useState("")
  const [membershipTeam, setMembershipTeam] = useState("")
  const selection = useRowSelection()
  const ids = memberships.map((membership) => membership.id)
  return (
    <div className="flex flex-col gap-4">
      <div className="flex justify-end">
        <FormDialog
          trigger={<><PlusIcon data-icon="inline-start" />添加成员</>}
          title="添加成员"
          description="管理用户所属团队，不隐式授予操作权限。"
          submitLabel="添加成员"
          submitDisabled={!membershipUser || !membershipTeam}
          pending={pending}
          onSubmit={() => submit({resource: "team-memberships", body: {user_id: membershipUser, team_id: membershipTeam, reason: ""}})}
        >
          <Picker label="用户" value={membershipUser} onValueChange={setMembershipUser} items={users.filter((user) => user.active).map((user) => ({value: user.id, label: user.display_name}))} />
          <Picker label="团队" value={membershipTeam} onValueChange={setMembershipTeam} items={teams.filter((team) => team.active).map((team) => ({value: team.id, label: team.name}))} />
        </FormDialog>
      </div>
      <SelectionBar count={selection.count} onClear={selection.clear}>
        <Button type="button" size="sm" variant="outline" disabled={pending} onClick={() => submitActiveBatch("team-memberships", "成员关系", [...selection.selected], true, selection.clear)}>批量启用</Button>
        <Button type="button" size="sm" variant="destructive" disabled={pending} onClick={() => submitActiveBatch("team-memberships", "成员关系", [...selection.selected], false, selection.clear)}>批量停用</Button>
      </SelectionBar>
      <ResourceTable
        empty="暂无成员关系"
        headings={[
          <Checkbox key="select-all" aria-label="选择全部成员关系" checked={ids.length > 0 && selection.count === ids.length} onCheckedChange={(checked) => selection.toggleAll(ids, checked)} />,
          "用户", "团队", "状态", "操作",
        ]}
        rows={memberships.map((membership) => [
          <Checkbox key="select" aria-label={`选择成员关系 ${userName(users, membership.user_id)}`} checked={selection.selected.has(membership.id)} onCheckedChange={(checked) => selection.toggle(membership.id, checked)} />,
          <span key="user">{userName(users, membership.user_id)}</span>,
          <span key="team">{teamName(teams, membership.team_id)}</span>,
          <Status key="status" active={membership.active} />,
          <ToggleButton key="action" active={membership.active} disabled={pending} onClick={() => submit({resource: "team-memberships", id: membership.id, body: {active: !membership.active, reason: ""}})} />,
        ])}
      />
    </div>
  )
}

export function BindingsTab({users, teams, bindings}: {users: AdminUser[]; teams: AdminTeam[]; bindings: AdminRoleBinding[]}) {
  const {submit, submitActiveBatch, pending} = useAdminMutation()
  const [bindingUser, setBindingUser] = useState("")
  const [bindingTeam, setBindingTeam] = useState("")
  const [bindingRole, setBindingRole] = useState<"sre" | "platform_administrator">("sre")
  const selection = useRowSelection()
  const ids = bindings.map((binding) => binding.id)
  return (
    <div className="flex flex-col gap-4">
      <div className="flex justify-end">
        <FormDialog
          trigger={<><PlusIcon data-icon="inline-start" />添加绑定</>}
          title="添加角色绑定"
          description="按平台或团队范围授予人类角色。"
          submitLabel="添加绑定"
          submitDisabled={!bindingUser || (bindingRole === "sre" && !bindingTeam)}
          pending={pending}
          onSubmit={() => bindingRole === "sre"
            ? submit({resource: "role-bindings", body: {user_id: bindingUser, role: "sre", scope_type: "team", scope_id: bindingTeam, reason: ""}})
            : submit({resource: "role-bindings", body: {user_id: bindingUser, role: "platform_administrator", scope_type: "platform", reason: ""}})}
        >
          <Picker label="用户" value={bindingUser} onValueChange={setBindingUser} items={users.filter((user) => user.active).map((user) => ({value: user.id, label: user.display_name}))} />
          <Picker label="角色" value={bindingRole} onValueChange={(value) => setBindingRole(value as typeof bindingRole)} items={[{value: "sre", label: "SRE"}, {value: "platform_administrator", label: "平台管理员"}]} />
          <Picker label="团队范围" value={bindingTeam} onValueChange={setBindingTeam} disabled={bindingRole === "platform_administrator"} items={teams.filter((team) => team.active).map((team) => ({value: team.id, label: team.name}))} />
        </FormDialog>
      </div>
      <SelectionBar count={selection.count} onClear={selection.clear}>
        <Button type="button" size="sm" variant="outline" disabled={pending} onClick={() => submitActiveBatch("role-bindings", "角色绑定", [...selection.selected], true, selection.clear)}>批量启用</Button>
        <Button type="button" size="sm" variant="destructive" disabled={pending} onClick={() => submitActiveBatch("role-bindings", "角色绑定", [...selection.selected], false, selection.clear)}>批量停用</Button>
      </SelectionBar>
      <ResourceTable
        empty="暂无角色绑定"
        headings={[
          <Checkbox key="select-all" aria-label="选择全部角色绑定" checked={ids.length > 0 && selection.count === ids.length} onCheckedChange={(checked) => selection.toggleAll(ids, checked)} />,
          "用户", "角色", "范围", "状态", "操作",
        ]}
        rows={bindings.map((binding) => [
          <Checkbox key="select" aria-label={`选择角色绑定 ${userName(users, binding.user_id)}`} checked={selection.selected.has(binding.id)} onCheckedChange={(checked) => selection.toggle(binding.id, checked)} />,
          <span key="user">{userName(users, binding.user_id)}</span>,
          <span key="role">{binding.role === "platform_administrator" ? "平台管理员" : "SRE"}</span>,
          <span key="scope">{binding.scope_type === "platform" ? "平台" : teamName(teams, binding.scope_id ?? "")}</span>,
          <Status key="status" active={binding.active} />,
          <ToggleButton key="action" active={binding.active} disabled={pending} onClick={() => submit({resource: "role-bindings", id: binding.id, body: {active: !binding.active, reason: ""}})} />,
        ])}
      />
    </div>
  )
}
