# Console uses shadcn/ui as its component foundation

Status: accepted

The rewritten Console uses shadcn/ui as its only general-purpose UI component system on React, Tailwind CSS, and accessible headless primitives. Components such as buttons, dialogs, forms, tables, tabs, menus, tooltips, sheets, skeletons, and notifications are added as project-owned source through the shadcn workflow; a second component library is not introduced.

The `components/ui` layer remains free of AIOps domain behavior. Incident, Evidence Step, Recommended Action, Approval, and administration interfaces compose those primitives in domain components with compact operational layouts and semantic status tokens. The Console keeps shadcn's interaction quality and visual restraint but does not copy generic dashboard blocks wholesale or migrate legacy CSS.
