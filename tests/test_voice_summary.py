"""测试语音友好摘要工具。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load_module():
    """按文件路径加载模块。"""
    module_path = Path(__file__).resolve().parents[1] / "toolsets" / "voice_summary.py"
    spec = importlib.util.spec_from_file_location("test_voice_summary_module", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_short_text_returns_directly() -> None:
    """短文本应直接返回。"""
    module = _load_module()
    text = "default 命名空间里的 api 服务正常，没有发现明显异常。"

    result = await module.summarize_for_voice(text)

    assert result == {"summary": text, "truncated": False}


@pytest.mark.asyncio
async def test_long_text_is_truncated() -> None:
    """长文本应压缩并标记 truncated。"""
    module = _load_module()
    text = "。".join([f"第{i}句说明当前诊断结论" for i in range(1, 20)]) + "。"

    result = await module.summarize_for_voice(text, max_sentences=3)

    assert result["truncated"] is True
    assert result["summary"].count("。") <= 3


@pytest.mark.asyncio
async def test_code_block_is_removed() -> None:
    """代码块应从长文本中移除。"""
    module = _load_module()
    text = (
        "告警已经触发，下面是诊断详情。" + "A" * 220 +
        "```bash\nkubectl get pods\n```"
        "最终发现 api 服务在重启。需要继续检查内存。"
    )

    result = await module.summarize_for_voice(text, max_sentences=5)

    assert "kubectl get pods" not in result["summary"]


@pytest.mark.asyncio
async def test_table_lines_are_removed() -> None:
    """表格行应被移除。"""
    module = _load_module()
    text = (
        "诊断开始。" + "B" * 220 + "\n"
        "| pod | status |\n"
        "| api | CrashLoopBackOff |\n"
        "目前确认 api 服务异常，需要处理。"
    )

    result = await module.summarize_for_voice(text)

    assert "| pod | status |" not in result["summary"]
    assert "| api | CrashLoopBackOff |" not in result["summary"]


@pytest.mark.asyncio
async def test_max_sentences_is_respected() -> None:
    """最大句数参数应生效。"""
    module = _load_module()
    text = "第一句。第二句。第三句。第四句。" + "C" * 220

    result = await module.summarize_for_voice(text, max_sentences=2)

    assert result["summary"].count("。") <= 2


@pytest.mark.asyncio
async def test_empty_text_returns_empty_summary() -> None:
    """空文本应返回空摘要。"""
    module = _load_module()

    result = await module.summarize_for_voice("")

    assert result == {"summary": "", "truncated": False}


@pytest.mark.asyncio
async def test_tool_handler_invokes_summary_function() -> None:
    """工具入口应正确调用摘要函数。"""
    module = _load_module()

    result = await module._tool_sre_voice_summary({"text": "短文本", "max_sentences": 2})

    assert json.loads(result) == {"summary": "短文本", "truncated": False}
