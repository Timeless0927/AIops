# Console images are immutable and manually promoted

Status: accepted

The `apps/aiops_console_web` workspace owns a path-aware GitHub Actions job that installs from its lockfile and runs type checks, tests, and a production build for pull requests. A main-branch build publishes `aiops-console` to the existing Aliyun registry with the immutable monorepo commit-SHA identity; release manifests select the verified image by digest. Mutable tags such as `latest` are limited to local or temporary development.

V1 does not automatically deploy a main-branch build to production. An operator explicitly promotes a verified digest, preserving a clear rollback target and preventing source, tag, or registry changes from silently changing a running Console.
