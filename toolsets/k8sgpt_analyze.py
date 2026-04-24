"""k8sgpt 扫描工具。"""

from __future__ import annotations

import asyncio


async def k8sgpt_analyze(namespace: str | None = None, filters: str | None = None) -> dict:
    """调用 k8sgpt CLI 执行集群扫描。"""
    command = ["k8sgpt", "analyze"]
    if namespace:
        command.extend(["--namespace", namespace])
    if filters:
        command.extend(["--filter", filters])

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "command": command,
            "error": "未安装 k8sgpt CLI，无法执行扫描",
        }
    except Exception as exc:
        return {
            "ok": False,
            "command": command,
            "error": f"启动 k8sgpt 失败: {exc}",
        }

    stdout, stderr = await process.communicate()
    output = stdout.decode("utf-8", errors="replace").strip()
    error_output = stderr.decode("utf-8", errors="replace").strip()

    if process.returncode != 0:
        return {
            "ok": False,
            "command": command,
            "returncode": process.returncode,
            "error": error_output or "k8sgpt analyze 执行失败",
            "output": output,
        }

    return {
        "ok": True,
        "command": command,
        "returncode": process.returncode,
        "output": output,
        "error": error_output,
    }
