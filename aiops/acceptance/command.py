"""Subprocess adapter for acceptance commands."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Protocol, Sequence

from .redaction import redact_text


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    started_at: str | None = None
    completed_at: str | None = None

    def evidence_text(self, *, known_secrets: Iterable[str] = ()) -> str:
        command = " ".join(self.command)
        text = (
            f"command: {command}\n"
            f"exit_code: {self.exit_code}\n"
            f"duration_seconds: {self.duration_seconds:.3f}\n"
            f"started_at: {self.started_at or 'unknown'}\n"
            f"completed_at: {self.completed_at or 'unknown'}\n"
            "stdout:\n"
            f"{self.stdout}\n"
            "stderr:\n"
            f"{self.stderr}\n"
        )
        return redact_text(text, known_secrets=known_secrets)


class CommandExecutor(Protocol):
    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        stdin: str | None = None,
        timeout: float = 900,
    ) -> CommandResult: ...


class SubprocessCommands:
    """Run an argument-vector command without a shell or implicit proxy."""

    _PROXY_ENV = {
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "npm_config_proxy",
        "npm_config_https_proxy",
    }

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        stdin: str | None = None,
        timeout: float = 900,
    ) -> CommandResult:
        started = time.monotonic()
        started_at = self._utc_now()
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.lower() not in self._PROXY_ENV
        }
        try:
            completed = subprocess.run(
                list(command),
                cwd=cwd,
                env=environment,
                input=stdin,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            return CommandResult(
                tuple(command),
                completed.returncode,
                completed.stdout,
                completed.stderr,
                time.monotonic() - started,
                started_at,
                self._utc_now(),
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            return CommandResult(
                tuple(command),
                124,
                stdout,
                f"{stderr}\ncommand timed out after {timeout:g}s".strip(),
                time.monotonic() - started,
                started_at,
                self._utc_now(),
            )

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
