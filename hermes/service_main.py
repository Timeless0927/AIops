"""Compatibility shim for diagnosis_service.service_main."""

from __future__ import annotations

from diagnosis_service.service_main import *  # noqa: F403
from diagnosis_service.service_main import main


if __name__ == "__main__":
    main()
