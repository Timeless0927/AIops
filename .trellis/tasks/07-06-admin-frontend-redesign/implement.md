# Implement: AIOps Console frontend redesign

## Checklist

1. Read the current `App.tsx` return tree and identify reusable state/API
   functions.
2. Add `overview` to the `ViewName` union and `viewTitle`.
3. Render a standalone `LoginPage` when `token` is empty.
4. Make successful login set the active view to `overview`.
5. Add authenticated shell navigation with Chinese labels:
   `总览`, `事件工作台`, `审批中心`, `通知中心`, `审计历史`.
6. Add `OverviewDashboard` as a placeholder large-screen home with a CTA into
   `事件工作台`.
7. Replace the incidents view layout with the reference-style workbench:
   incident queue, center diagnosis/evidence/progress, right approval/agent/audit.
8. Reuse existing API state and formatter helpers; add only tiny view helpers for
   fallback display values if needed.
9. Keep approvals, notifications, and audit behavior intact; only adjust classes
   for the shared shell.
10. Replace `styles.css` with the new console visual system and responsive rules.
11. Update `tests/test_aiops_console_web.py` only for intentional UI labels and
   layout-contract changes.

## Validation

Run from repo root:

```bash
/root/.local/bin/rtk npm --prefix apps/aiops_console_web run build
/root/.local/bin/rtk test pytest -q tests/test_aiops_console_web.py
```

Optional smoke if time allows:

```bash
/root/.local/bin/rtk npm --prefix apps/aiops_console_web run dev -- --port 5173
```

Then inspect:

- login page at `/`
- overview after successful auth/token
- `事件工作台` desktop and mobile widths

## Risk Points

- `App.tsx` is already large. Keep the redesign in the existing file for this
  pass; do not add a component architecture refactor.
- Current fallback incidents should not create a demo login bypass.
- Do not remove required Gateway route strings from `App.tsx`; tests assert them.
- Avoid new libraries for charts/icons. CSS, text, tables, and inline SVG are
  enough for this pass.

## Review Gate

Before starting implementation, confirm these artifacts with the user:

- `prd.md`
- `design.md`
- `implement.md`

After approval, start the existing Trellis task and enter Phase 2.
