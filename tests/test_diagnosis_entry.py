"""兼容入口基础测试。"""

from __future__ import annotations

import diagnosis_service.__main__ as diagnosis_main


def test_project_root_points_to_repo() -> None:
    """确保兼容入口可以定位到项目根目录。"""
    root = diagnosis_main._project_root()

    assert root.is_dir()
    assert (root / "diagnosis_service" / "service_main.py").is_file()
    assert not (root / "hermes-agent").exists()
