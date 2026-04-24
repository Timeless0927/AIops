"""语音友好摘要工具。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict


def _load_registry():
    """按文件路径加载 Hermes 工具注册器。"""
    module_name = "aiops_tools_registry"
    if module_name in sys.modules:
        return sys.modules[module_name].registry

    module_path = Path(__file__).resolve().parents[1] / "hermes-agent" / "tools" / "registry.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 registry: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.registry


registry = _load_registry()


SRE_VOICE_SUMMARY_SCHEMA = {
    "name": "sre_voice_summary",
    "description": "将诊断结果压缩为语音友好的简短摘要。",
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "原始诊断文本"},
            "max_sentences": {"type": "integer", "description": "最大句数，默认 5"},
        },
        "required": ["text"],
    },
}


def _strip_code_blocks(text: str) -> str:
    """移除 Markdown 代码块。"""
    return re.sub(r"```.*?```", "\n", text, flags=re.DOTALL)


def _strip_table_lines(text: str) -> str:
    """移除 Markdown 表格行。"""
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("|")]
    return "\n".join(lines)


def _strip_structured_blocks(text: str) -> str:
    """移除明显的 YAML/JSON 结构块。"""
    cleaned_lines: list[str] = []
    in_json_block = False
    brace_depth = 0

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if not stripped:
            cleaned_lines.append("")
            continue

        if in_json_block:
            brace_depth += stripped.count("{") + stripped.count("[")
            brace_depth -= stripped.count("}") + stripped.count("]")
            if brace_depth <= 0:
                in_json_block = False
                brace_depth = 0
            continue

        if stripped.startswith("{") or stripped.startswith("["):
            in_json_block = True
            brace_depth = stripped.count("{") + stripped.count("[")
            brace_depth -= stripped.count("}") + stripped.count("]")
            if brace_depth <= 0:
                in_json_block = False
                brace_depth = 0
            continue

        if re.match(r"^[A-Za-z0-9_-]+:\s*(\{|\[)?\s*$", stripped):
            continue
        if re.match(r"^\s{2,}[A-Za-z0-9_-]+:\s*.*$", line):
            continue
        if re.match(r"^-\s+[A-Za-z0-9_-]+:\s*.*$", stripped):
            continue

        cleaned_lines.append(line)

    return "\n".join(cleaned_lines)


def _normalize_plain_text(text: str) -> str:
    """清理并折叠多余空白。"""
    text = _strip_code_blocks(text)
    text = _strip_table_lines(text)
    text = _strip_structured_blocks(text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    return text.strip()


def _split_sentences(text: str) -> list[str]:
    """按常见中文句末符号切分句子。"""
    normalized = re.sub(r"([。！？!?])", r"\1\n", text)
    sentences = [sentence.strip() for sentence in normalized.splitlines() if sentence.strip()]
    return sentences


async def summarize_for_voice(text: str, max_sentences: int = 5) -> Dict[str, Any]:
    """将长文本压缩为语音友好摘要。"""
    if not text:
        return {"summary": "", "truncated": False}

    if len(text) < 200:
        return {"summary": text, "truncated": False}

    cleaned_text = _normalize_plain_text(text)
    if not cleaned_text:
        return {"summary": "", "truncated": True}

    sentences = _split_sentences(cleaned_text)
    limited = sentences[: max(1, max_sentences)]
    summary = " ".join(limited).strip()
    return {"summary": summary, "truncated": True}


async def _tool_sre_voice_summary(args: Dict[str, Any], **_: Any) -> str:
    """工具入口：生成语音摘要。"""
    text = str(args.get("text", ""))
    max_sentences = int(args.get("max_sentences", 5) or 5)
    result = await summarize_for_voice(text, max_sentences)
    return json.dumps(result, ensure_ascii=False)


registry.register(
    name="sre_voice_summary",
    toolset="sre",
    schema=SRE_VOICE_SUMMARY_SCHEMA,
    handler=_tool_sre_voice_summary,
    is_async=True,
)
