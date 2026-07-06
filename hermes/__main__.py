"""Compatibility entrypoint for `python -m hermes`."""

from __future__ import annotations

from diagnosis_service.__main__ import main


if __name__ == "__main__":
    main()
