# Console is an independent release artifact

Status: superseded by ADR-0052

The rewritten Console lives in the independent `AIops-web` repository, owns its build and CI, and is published as an `aiops-console` OCI image for a separate Deployment. Gateway exposes API, authentication, and event-stream endpoints only; its existing optional static-file serving path is retired rather than carrying the new Console distribution.

Bundling frontend assets into the Python Gateway image would couple otherwise independent builds, releases, and rollbacks across repository boundaries. A separate artifact adds one image and Deployment but lets Console and Gateway advance or recover independently while their versioned API contract remains the integration boundary.
