import os
import asyncio
import logging
import re

try:
    import langextract as lx
    LANGEXTRACT_AVAILABLE = True
except ImportError:
    LANGEXTRACT_AVAILABLE = False

logger = logging.getLogger(__name__)

# --- 正则降级提取 ---

_LOG_PATTERNS = [
    (re.compile(r"(?i)\b(error|critical|fatal)\b"), "error"),
    (re.compile(r"(?i)\b(warning|warn)\b"), "warning"),
    (re.compile(r"(?i)(exception|traceback|stack.?trace)"), "stack_trace"),
    (re.compile(r"(?i)(timeout|timed?\s*out)"), "timeout"),
    (re.compile(r"(?i)(connection\s*(refused|reset|closed))"), "connection_error"),
]

_K8S_STATUS_RE = re.compile(
    r"(\S+)\s+\d+/\d+\s+(CrashLoopBackOff|Error|ImagePullBackOff|Pending|OOMKilled|Evicted|Terminating)"
)
_K8S_RESTART_RE = re.compile(r"(\S+)\s+\d+/\d+\s+\S+\s+(\d+)\s+")

_METRIC_RE = re.compile(r"(\w+)\{([^}]+)\}\s+([\d.eE+\-]+)")


def _regex_extract_log(text: str) -> list[dict]:
    results = []
    for i, line in enumerate(text.splitlines()):
        for pattern, cls in _LOG_PATTERNS:
            if pattern.search(line):
                results.append({
                    "class": cls,
                    "text": line.strip(),
                    "attributes": {"line_number": i + 1},
                })
                break
    return results


def _regex_extract_k8s(text: str) -> list[dict]:
    results = []
    for line in text.splitlines():
        m = _K8S_STATUS_RE.search(line)
        if m:
            results.append({
                "class": "unhealthy_pod",
                "text": m.group(1),
                "attributes": {"status": m.group(2)},
            })
            continue
        m = _K8S_RESTART_RE.search(line)
        if m:
            restarts = int(m.group(2))
            if restarts > 5:
                results.append({
                    "class": "high_restart",
                    "text": m.group(1),
                    "attributes": {"restart_count": restarts},
                })
    return results


def _regex_extract_metric(text: str) -> list[dict]:
    results = []
    for line in text.splitlines():
        m = _METRIC_RE.search(line)
        if m:
            results.append({
                "class": "metric_point",
                "text": m.group(0),
                "attributes": {"name": m.group(1), "labels": m.group(2), "value": m.group(3)},
            })
    return results


_REGEX_EXTRACTORS = {
    "log": _regex_extract_log,
    "k8s": _regex_extract_k8s,
    "metric": _regex_extract_metric,
}

# --- langextract schema ---

LOG_PROMPT = "从日志中提取 error, warning, stack_trace, timeout, connection_error。"
K8S_PROMPT = "从 K8s 资源中提取 unhealthy_pod, high_restart, pending_resource, failed_event。"
METRIC_PROMPT = "从指标数据中提取 threshold_breach, spike, trend_anomaly。"

SOURCE_CONFIG = {
    "log": {"prompt": LOG_PROMPT},
    "k8s": {"prompt": K8S_PROMPT},
    "metric": {"prompt": METRIC_PROMPT},
}


async def extract_if_needed(output: str, source_type: str) -> dict:
    """
    根据输出长度决定是否进行结构化提取。

    source_type: "log" | "k8s" | "metric"
    返回 {"extracted": bool, "data": str | list[dict], "line_count": int}
    """
    if not output:
        return {"extracted": False, "data": "", "line_count": 0}

    line_count = len(output.splitlines())

    if line_count < 200:
        return {"extracted": False, "data": output, "line_count": line_count}

    # 优先尝试 langextract AI 提取
    if LANGEXTRACT_AVAILABLE:
        try:
            model_id = os.environ.get("EXTRACTOR_MODEL", "gemini-1.5-flash")
            config = SOURCE_CONFIG.get(source_type, {})

            def run_extraction():
                return lx.extract(
                    text_or_documents=output,
                    prompt_description=config.get("prompt", "Extract SRE anomalies"),
                    model_id=model_id,
                    max_workers=10,
                    extraction_passes=3,
                    max_char_buffer=1000,
                )

            result = await asyncio.get_event_loop().run_in_executor(None, run_extraction)
            extracted_data = []
            for ext in getattr(result, "extractions", []):
                extracted_data.append({
                    "class": ext.extraction_class,
                    "text": ext.extraction_text,
                    "attributes": ext.attributes,
                })
            if extracted_data:
                return {"extracted": True, "data": extracted_data, "line_count": line_count, "method": "ai"}
        except Exception as e:
            logger.warning(f"AI 提取失败，降级到正则: {e}")

    # 降级：正则提取
    extractor = _REGEX_EXTRACTORS.get(source_type)
    if extractor:
        extracted_data = extractor(output)
        if extracted_data:
            return {"extracted": True, "data": extracted_data, "line_count": line_count, "method": "regex"}

    return {"extracted": False, "data": output, "line_count": line_count}
