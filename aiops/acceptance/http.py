"""In-memory, direct-only Gateway HTTP adapter for acceptance gates."""

from __future__ import annotations

import http.cookiejar
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: Any
    headers: dict[str, str]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GatewaySession:
    """Keep cookies and CSRF in memory and never consult environment proxies."""

    def __init__(
        self, base_url: str, *, timeout: float = 15, follow_redirects: bool = True
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Gateway base URL must be absolute HTTP(S)")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Gateway base URL cannot contain credentials, query or fragment")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._cookies = http.cookiejar.CookieJar()
        handlers = [
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self._cookies),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        ]
        if not follow_redirects:
            handlers.append(_NoRedirect())
        self._opener = urllib.request.build_opener(*handlers)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        csrf: bool = True,
        request_id: str | None = None,
    ) -> HttpResponse:
        headers = {
            "Accept": "application/json",
            "X-Request-ID": request_id or f"acceptance-{uuid.uuid4()}",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if method.upper() not in {"GET", "HEAD"} and csrf and path != "/auth/login":
            token = self.request("GET", "/auth/csrf").body.get("csrf_token")
            if not isinstance(token, str) or not token:
                raise RuntimeError("Gateway did not return a CSRF token")
            headers["X-CSRF-Token"] = token
        request = urllib.request.Request(
            self.base_url + path,
            data=(json.dumps(body, separators=(",", ":")).encode() if body is not None else None),
            headers=headers,
            method=method.upper(),
        )
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            return self._response(exc.code, exc.read(1024 * 1024 + 1), exc.headers)
        with response:
            return self._response(response.status, response.read(1024 * 1024 + 1), response.headers)

    @staticmethod
    def _response(status: int, raw: bytes, headers: Any) -> HttpResponse:
        if len(raw) > 1024 * 1024:
            raise RuntimeError("Gateway response exceeded 1 MiB acceptance limit")
        content_type = headers.get("Content-Type", "")
        if "json" in content_type:
            try:
                body: Any = json.loads(raw or b"{}")
            except json.JSONDecodeError as exc:
                raise RuntimeError("Gateway returned invalid JSON") from exc
        else:
            body = raw.decode("utf-8", errors="replace")
        safe_headers = {
            key.lower(): headers[key]
            for key in ("Content-Type", "Location")
            if headers.get(key) is not None
        }
        return HttpResponse(status, body, safe_headers)
