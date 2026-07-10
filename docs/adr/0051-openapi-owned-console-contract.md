# Gateway owns the OpenAPI Console contract

Status: accepted

Gateway owns one OpenAPI 3.1 specification for the versioned Console product API. It defines `/api/v1/*` operations, domain DTOs, errors, actor capabilities, and the schemas carried by Incident SSE events. API changes update the specification first; additive V1 changes remain compatible, while removals, renames, and semantic changes require a new API version as established by ADR-0026.

Gateway contract tests validate representative requests and responses against the specification and publish it as a versioned build artifact. The `apps/aiops_console_web` workspace generates TypeScript types from the versioned specification in the same repository without manually maintaining duplicate DTOs. The generated surface is types only: a small handwritten client continues to own same-origin cookies, CSRF acquisition, request IDs, and normalized errors, avoiding a generated SDK while compatibility checks preserve independent runtime releases.
