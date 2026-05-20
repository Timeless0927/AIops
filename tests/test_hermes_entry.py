"""兼容入口基础测试。"""

from __future__ import annotations

import hermes.__main__ as hermes_main


def test_project_root_points_to_repo() -> None:
    """确保兼容入口可以定位到项目根目录。"""
    root = hermes_main._project_root()

    assert root.is_dir()
    assert (root / "hermes-agent").is_dir()
