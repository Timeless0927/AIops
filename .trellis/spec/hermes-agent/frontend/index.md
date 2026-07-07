# Frontend Development Guidelines

> Console V1 frontend is a **vanilla JS + HTML static vertical slice** in
> `apps/aiops_console/static/`. Console Next is the separate React/Vite shell in
> `apps/aiops_console_web/`, built into the Gateway image and served by the
> Gateway in production.

There is exactly one slice today — `incident-detail` — and it doubles as the
reference pattern for any future Console V1 page. Browser must reach the Gateway
`/api/*` only; it must never call Hermes/Connector/MCP/Prometheus/Loki/Feishu
(see project `CLAUDE.md` and the slice `README.md`).

> Note: this directory is named `hermes-agent/frontend` for legacy reasons. The
> authoritative frontend packages are `apps/aiops_console/` for V1 static slices
> and `apps/aiops_console_web/` for Console Next.

---

## Guidelines Index

| Guide | What it covers |
|-------|----------------|
| [Directory Structure](./directory-structure.md) | `apps/aiops_console/` static + fixtures layout |
| [Component Rendering](./component-guidelines.md) | vanilla DOM-render fns, `replaceChildren`, status pills, empty states |
| [State Management](./state-management.md) | fixture-driven scenario data, `setScenario`, pure render |
| [Input & Forms](./input-forms.md) | scenario buttons (aria-pressed), read-only guarantees, no submit/mutation |
| [Quality Guidelines](./quality-guidelines.md) | Gateway-only browser calls, V1 static-slice rules, Console Next router/session shell rules |

Removed (unsupported by code, React/TS-only templates): `type-safety.md`,
`hook-guidelines.md`. There is no TypeScript, no React, no hooks in this
codebase — do not reintroduce them.
