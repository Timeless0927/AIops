# Console and browser APIs share one origin

Status: accepted

The public browser entry point uses one HTTPS origin. Its edge router sends `/` and static assets to the Console Deployment, and sends `/api/v1/*` and `/auth/*` to Gateway; Console calls these endpoints with relative URLs. Gateway has no separate browser-facing hostname or directly exposed Service, which preserves the existing first-party session-cookie and CSRF model without adding cross-origin authentication paths.

SSE remains under `/api/v1/*`; the edge route disables response buffering and permits long-lived reads while retaining normal authentication and authorization. Console links emitted by Notification Engine use the same public origin. Deployment-specific ingress technology is not part of this contract.
