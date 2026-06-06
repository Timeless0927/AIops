"""Smokeable Hermes service boundary for split-image packaging."""

from __future__ import annotations

import argparse
import os
from http import HTTPStatus

from apps.service_http import JsonHandler, connectivity_payload, serve


class HermesServiceHandler(JsonHandler):
    """Minimal Hermes HTTP surface used by image and compose smoke tests."""

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self.write_json(
                HTTPStatus.OK,
                {
                    "service": "hermes",
                    "status": "ok",
                    "gateway_url": os.getenv("AIOPS_GATEWAY_URL", ""),
                },
            )
            return

        if self.path in {"/readyz", "/connectivity/gateway"}:
            gateway_url = os.getenv("AIOPS_GATEWAY_URL", "")
            if not gateway_url:
                self.write_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {
                        "service": "hermes",
                        "status": "unavailable",
                        "peer": "gateway",
                        "error": "AIOPS_GATEWAY_URL is not set",
                    },
                )
                return
            status, payload = connectivity_payload(
                service="hermes",
                peer_name="gateway",
                peer_url=gateway_url,
            )
            self.write_json(status, payload)
            return

        self.write_not_found()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AIOps Hermes service smoke boundary")
    parser.add_argument("--host", default=os.getenv("AIOPS_HERMES_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("AIOPS_HERMES_PORT", "8082")))
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    serve(HermesServiceHandler, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
