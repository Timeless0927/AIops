from __future__ import annotations

import json
import shutil
import subprocess
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from aiops.acceptance.adapters import PlaywrightBrowser
from aiops.acceptance.command import SubprocessCommands
from aiops.acceptance.credentials import RunCredentialStore


def _playwright_available() -> bool:
    if shutil.which("node") is None:
        return False
    return subprocess.run(
        ["node", "-e", "require('playwright')"],
        cwd=Path(__file__).resolve().parents[1] / "apps/aiops_console_web",
        capture_output=True,
        check=False,
    ).returncode == 0


class ConsoleFixture(BaseHTTPRequestHandler):
    logins: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/", "/platform"}:
            self._write(
                HTTPStatus.OK,
                b'<html><body><input type="password" value="must-be-masked"><a href="/platform">Platform</a><script src="/assets/app.js"></script></body></html>',
                "text/html",
            )
        elif self.path == "/assets/app.js":
            self._write(HTTPStatus.OK, b"", "text/javascript")
        elif self.path == "/api/v1/actor":
            self._write(HTTPStatus.UNAUTHORIZED, b'{"error":{"code":"unauthorized"}}')
        elif self.path == "/api/v1/platform/status/stream":
            if "session=accepted" in self.headers.get("Cookie", ""):
                self._write(HTTPStatus.OK, b"data: {}\n\n", "text/event-stream")
            else:
                self._write(HTTPStatus.UNAUTHORIZED, b'{"error":{"code":"unauthorized"}}')
        else:
            self._write(HTTPStatus.NOT_FOUND, b'{}')

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        if payload.get("username") == "admin" and payload.get("password"):
            self.logins.append("admin")
            body = b'{"request_id":"login-admin"}'
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "session=accepted; HttpOnly; SameSite=Strict")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._write(HTTPStatus.UNAUTHORIZED, b'{"error":{"code":"invalid_credentials"}}')

    def _write(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.mark.skipif(not _playwright_available(), reason="Playwright Node dependency unavailable")
def test_real_headless_browser_uses_tmpfs_credential_fresh_role_context_and_masking(
    tmp_path: Path,
) -> None:
    ConsoleFixture.logins.clear()
    store = RunCredentialStore(
        Path("/dev/shm") / f"aiops-browser-real-{uuid.uuid4().hex}",
        workspace=Path(__file__).resolve().parents[1],
        evidence_root=tmp_path / "evidence",
    ).create()
    password = store.generate_user_password("platform-administrator.password")
    server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleFixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = PlaywrightBrowser(
            commands=SubprocessCommands(), source_root=Path(__file__).resolve().parents[1],
        ).probe(
            f"http://127.0.0.1:{server.server_address[1]}",
            username="admin", password=password, role="platform_administrator",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        store.cleanup()

    assert result.summary["same_origin"] is True
    assert result.summary["screenshots_masked"] is True
    assert result.summary["browser_context"] == {
        "role": "platform_administrator", "persistent": False, "storage_state_loaded": False,
    }
    assert set(result.screenshots) == {"desktop.png", "mobile.png"}
    assert all(content.startswith(b"\x89PNG") for content in result.screenshots.values())
    assert "must-be-masked" not in json.dumps(result.summary)
    assert ConsoleFixture.logins == ["admin"]
