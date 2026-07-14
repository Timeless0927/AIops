"""Process entrypoint for the controlled verification fixture."""

from __future__ import annotations

import sys
from typing import Sequence

from verification_service.server import serve
from verification_service.trigger import trigger_job


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args or args == ["serve"]:
        serve()
        return 0
    if args == ["trigger"]:
        return trigger_job()
    raise SystemExit("usage: python -m verification_service [serve|trigger]")


if __name__ == "__main__":
    raise SystemExit(main())
