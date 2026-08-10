"""Release local service images from commit-bound CI artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_local_images.py"
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


def _tool(path: Path, source: str) -> None:
    path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def _run_release(
    tmp_path: Path,
    *,
    source_sha: str = SHA,
    mode: str = "--plan",
    dirty: bool = False,
    blocked_consumer: bool = False,
) -> subprocess.CompletedProcess[str]:
    tools = tmp_path / "bin"
    tools.mkdir()
    _tool(
        tools / "git",
        f"""\
        #!/bin/sh
        if [ "$1" = "rev-parse" ]; then
          echo "{SHA}"
        elif [ "$1" = "branch" ]; then
          echo "feature/release-script"
        elif [ "$1" = "status" ]; then
          {"echo ' M user-file'" if dirty else ":"}
        elif [ "$1" = "push" ]; then
          if [ -n "${{RELEASE_TEST_PUSH_LOG:-}}" ]; then
            printf '%s\\n' pushed > "$RELEASE_TEST_PUSH_LOG"
          fi
        fi
        """,
    )
    _tool(
        tools / "kubectl",
        """\
        #!/bin/sh
        case " $* " in
          *" config view "*)
            printf '%s\\n' '{"clusters":[{"cluster":{"server":"https://127.0.0.1:6443"}}]}'
            ;;
          *" get nodes "*)
            printf '%s\\n' 'local-node'
            ;;
          *" get statefulsets,daemonsets,jobs,cronjobs "*)
            if [ "${RELEASE_TEST_BLOCKED:-}" = "1" ]; then
              printf '%s\\n' '{"items":[{"kind":"StatefulSet","metadata":{"name":"blocked"},"spec":{"template":{"spec":{"containers":[{"name":"gateway","image":"registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-gateway:old"}]}}}}]}'
            else
              printf '%s\\n' '{"items":[]}'
            fi
            ;;
          *" get deployments "*)
            printf '%s\\n' '{"items":[{"metadata":{"name":"aiops-gateway"},"spec":{"template":{"spec":{"containers":[{"name":"gateway","image":"registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-gateway:old"},{"name":"sidecar","image":"example.test/sidecar:1"}],"initContainers":[{"name":"gateway-init","image":"registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-gateway:old"}]}}}}]}'
            ;;
        esac
        """,
    )
    _tool(
        tools / "gh",
        f"""\
        #!/bin/sh
        if [ "$1" = "run" ] && [ "$2" = "list" ]; then
          case " $* " in *" --event push "*) ;; *) exit 1 ;; esac
          printf '[{{"databaseId":41,"headSha":"{SHA}","status":"completed","conclusion":"success","url":"https://example.test/run/41"}}]'
          exit 0
        fi
        if [ "$1" = "run" ] && [ "$2" = "download" ]; then
          while [ "$#" -gt 0 ]; do
            if [ "$1" = "--dir" ]; then
              shift
              mkdir -p "$1"
              cat > "$1/digest.json" <<'JSON'
        {{"service":"gateway","repository":"registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-gateway","digest":"{DIGEST}","source_sha":"{source_sha}"}}
        JSON
              exit 0
            fi
            shift
          done
        fi
        exit 1
        """,
    )

    completed = subprocess.run(
        [
            sys.executable,
            SCRIPT,
            mode,
            "--context",
            "local-dev",
            "--namespace",
            "aiops-dev",
            "--service",
            "gateway",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "RELEASE_TEST_BLOCKED": "1" if blocked_consumer else "",
            "RELEASE_TEST_PUSH_LOG": str(tmp_path / "push.log"),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    return completed


def test_plan_uses_exact_ci_digest_for_matching_live_containers(tmp_path: Path) -> None:
    completed = _run_release(tmp_path)

    assert completed.returncode == 0, completed.stderr
    plan = json.loads(completed.stdout)
    assert plan["commit"] == SHA
    assert plan["ci_run"] == {"id": 41, "url": "https://example.test/run/41"}
    assert plan["cluster"] == {
        "context": "local-dev",
        "api_server": "https://127.0.0.1:6443",
        "nodes": ["local-node"],
        "namespace": "aiops-dev",
    }
    assert plan["blocked_consumers"] == []
    assert plan["updates"] == [
        {
            "deployment": "aiops-gateway",
            "container": "gateway",
            "image": f"registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-gateway@{DIGEST}",
        },
        {
            "deployment": "aiops-gateway",
            "container": "gateway-init",
            "image": f"registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-gateway@{DIGEST}",
        },
    ]


def test_plan_rejects_digest_from_another_commit(tmp_path: Path) -> None:
    completed = _run_release(tmp_path, source_sha="c" * 40)

    assert completed.returncode == 1
    assert f"CI digest for gateway is not bound to {SHA}" in completed.stderr


def test_apply_preserves_dirty_worktree(tmp_path: Path) -> None:
    completed = _run_release(
        tmp_path, mode="--apply", dirty=True, blocked_consumer=True,
    )

    assert completed.returncode == 1
    assert "uncommitted changes are not included" in completed.stderr
    assert (tmp_path / "push.log").read_text(encoding="utf-8").strip() == "pushed"


def test_apply_rejects_non_deployment_consumers(tmp_path: Path) -> None:
    completed = _run_release(tmp_path, mode="--apply", blocked_consumer=True)

    assert completed.returncode == 1
    assert "cannot safely refresh non-Deployment consumers: StatefulSet/blocked" in completed.stderr
