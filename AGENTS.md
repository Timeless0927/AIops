# Repository Guidelines

## Project Structure & Module Organization
- `hooks/`: alert, approval, audit, identity, recovery, and voice hooks.
- `toolsets/`: AIOps tools and guards such as `k8s_read`, `k8s_write`, and `k8s_exec`.
- `runtime/`: gateway and overlay helpers.
- `skills/`: SRE skill drafts and runbooks.
- `tests/`: root pytest suite.
- `hermes-agent/`: vendored Hermes fork with its own [AGENTS.md](hermes-agent/AGENTS.md); `hermes-agent/web/` is the UI, and `deploy/k8s/` holds manifests.

## Build, Test, and Development Commands
- `pip install -r requirements.txt`: install the root editable stack.
- `python -m hermes --mode cli`: run the local CLI loop.
- `python -m hermes --mode gateway --platform feishu`: exercise the Feishu gateway path.
- `pytest tests/`: run the root test suite.
- `cd hermes-agent && uv pip install -e ".[all,dev]" && python -m pytest tests/ -q`: install and test the Hermes subtree.
- `cd hermes-agent/web && npm run dev`, `npm run build`, or `npm run lint`: start the UI, build assets, or run ESLint/TypeScript checks.
- `docker build -f Dockerfile.aiops -t aiops-agent:latest .` and `kubectl apply -f deploy/k8s`: package and deploy the AIOps image/manifests.

## Coding Style & Naming Conventions
- Use 4-space indentation in Python, `snake_case` for modules/functions, and `test_*.py` for tests.
- Keep guard, approval, and incident logic small and deterministic; prefer focused helpers over broad branching.
- Match the surrounding language in each area. Many SRE-facing Python files use Chinese docstrings/comments, while `hermes-agent/` follows Hermes' English style.
- For the web UI, follow `hermes-agent/web/eslint.config.js`; run `npm run lint` before merging UI changes.

## Testing Guidelines
- Add or update tests with every behavior change; approval, guard, webhook, and incident flows need focused regression tests.
- Use pytest naming conventions: files `test_*.py`, functions `test_*`.
- In `hermes-agent/`, `pytest` skips `integration` tests by default via `pyproject.toml`; run them explicitly when needed.

## Development Progress Tracking
- `docs/development-progress.md` is the source of truth for feature status: complete, partially complete, or not developed.
- Before starting feature work, read the progress table and reuse completed pieces instead of rediscovering the repo.
- After any feature change, update the table in the same diff: status, code/test evidence, remaining work, and latest verification.
- Do not mark a feature `完成` unless implementation, tests, and the relevant acceptance path are complete.
- In the final response, state whether `docs/development-progress.md` was updated. If not, state why.

## Commit & Pull Request Guidelines
- Recent commits mostly use short conventional prefixes such as `feat:`, `fix:`, `docs:`, and `test:`. Keep the subject concise and imperative.
- PRs should explain the change, list verification commands, and link the relevant issue or plan.
- Include screenshots or short recordings for web/UI changes, and call out config or secret updates.

## Security & Configuration Tips
- Never commit real secrets. Keep placeholders in `.env` and `config.yaml`.
- Prefer `AIOPS_DATA_DIR` for runtime state and SQLite files when available.
- Follow the nearest `AGENTS.md` first; `hermes-agent/AGENTS.md` governs that subtree.
