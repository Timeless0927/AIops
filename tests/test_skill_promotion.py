"""测试 Skill 草稿审核与上线模块。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_module(tmp_path: Path):
    """按文件路径加载模块，并重定向 skills 根目录。"""
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "skill_promotion.py"
    spec = importlib.util.spec_from_file_location("test_skill_promotion_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)

    skills_root = tmp_path / "skills" / "sre"
    (skills_root / "drafts").mkdir(parents=True, exist_ok=True)
    (skills_root / "runbooks").mkdir(parents=True, exist_ok=True)

    async def _fake_skills_root():
        return skills_root

    module._skills_root = _fake_skills_root
    return module, skills_root


@pytest.mark.asyncio
async def test_list_promote_and_discard_draft(tmp_path: Path, **_: object) -> None:
    """验证草稿枚举、上线和删除流程。"""
    module, skills_root = _load_module(tmp_path)
    draft_a = skills_root / "drafts" / "incident-a"
    draft_b = skills_root / "drafts" / "incident-b"
    draft_a.mkdir(parents=True, exist_ok=True)
    draft_b.mkdir(parents=True, exist_ok=True)
    (draft_a / "SKILL.md").write_text("# 草稿A\n\n第一行摘要\n", encoding="utf-8")
    (draft_b / "SKILL.md").write_text("# 草稿B\n\n第二行摘要\n", encoding="utf-8")

    drafts = await module.list_drafts()
    promoted = await module.promote_draft("incident-a", "pod-crashloop-custom")
    discarded = await module.discard_draft("incident-b")

    assert len(drafts) == 2
    assert drafts[0]["incident_id"] == "incident-a"
    assert promoted["ok"] is True
    assert (skills_root / "runbooks" / "pod-crashloop-custom" / "SKILL.md").exists()
    assert not draft_a.exists()
    assert discarded["ok"] is True
    assert not draft_b.exists()


@pytest.mark.asyncio
async def test_path_traversal_is_rejected(tmp_path: Path, **_: object) -> None:
    """验证 incident_id 和 target_name 拒绝路径穿越。"""
    module, _ = _load_module(tmp_path)

    with pytest.raises(ValueError, match="非法的 incident_id"):
        await module.promote_draft("../secret", "safe-name")

    with pytest.raises(ValueError, match="非法的 target_name"):
        await module.promote_draft("incident-1", "../escape")

    with pytest.raises(ValueError, match="非法的 incident_id"):
        await module.discard_draft("../secret")


@pytest.mark.asyncio
async def test_missing_draft_returns_graceful_result(tmp_path: Path, **_: object) -> None:
    """不存在的草稿应返回友好结果而不是抛异常。"""
    module, _ = _load_module(tmp_path)

    promoted = await module.promote_draft("missing", "target")
    discarded = await module.discard_draft("missing")

    assert promoted["ok"] is False
    assert "草稿不存在" in promoted["message"]
    assert discarded["ok"] is False
    assert "草稿不存在" in discarded["message"]
