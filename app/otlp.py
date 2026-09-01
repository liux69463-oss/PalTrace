"""OTLP 解析：把 OTLP ExportTraceServiceRequest（dict 形态）摊平成 span 文档。

关键设计
--------
gRPC 与 HTTP 共用本模块：
    MessageToDict(proto_request)  # 默认 camelCase
产出的结构与 OTLP/HTTP JSON body 完全一致，因此只需一套解析逻辑。

注意 OTLP/HTTP JSON 按 proto3 JSON 规范把 int64 编码成**字符串**，
故所有数值统一走 _to_int / _to_float，兼容 "120" 与 120 两种形态。
"""
import logging
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# 当前真实纳秒时间戳约 1.7e18；1e16 ns ≈ 1970-04，低于此值说明单位不是纳秒。
# OTLP 规范明确规定 start/endTimeUnixNano 单位为纳秒，这里只做告警、不改变语义，
# 避免"静默丢数据"——否则数据会因落在统计时间窗口之外而凭空消失。
_NS_PLAUSIBLE_MIN = 10 ** 16
_ts_warned = False

SPAN_KIND_NAMES = {
    0: "UNSPECIFIED",
    1: "INTERNAL",
    2: "SERVER",
    3: "CLIENT",
    4: "PRODUCER",
    5: "CONSUMER",
}

_STATUS_CODE_NAMES = {
    0: "UNSET",
    1: "OK",
    2: "ERROR",
    "STATUS_CODE_UNSET": "UNSET",
    "STATUS_CODE_OK": "OK",
    "STATUS_CODE_ERROR": "ERROR",
}

# Jaeger 粒度复合 operation：把 operation 与 model/tool_name 拼成"操作名 + 主体"
#   chat/llm/generate/embedding/completion + model   → "{op} {model}"
#   tool                                           → "execute_tool {tool_name}"
#   其他（agent / react / invoke_agent / internal）  → 原样
# 写时（flatten_span）落库到 `operation` 字段，读时（list_operations / recent_traces）
# 用 ES runtime field 或 Python 端 _compound_op 派生同一份口径，
# 这样老数据（operation 还是粗粒度）和新数据（已复合）能在下拉里一致展示。
_LLM_OPS = ("chat", "llm", "generate", "embedding", "completion")


def _compound_op(operation: Optional[str], model: Optional[str], tool_name: Optional[str]) -> str:
    """把 (operation, model, tool_name) 派生成 Jaeger 粒度的 operation 名。"""
    op = (operation or "").strip()
    md = (model or "").strip()
    tn = (tool_name or "").strip()
    # 兼容两种来源：raw "tool" 和已经带前缀的 "execute_tool"
    if op in ("tool", "execute_tool") and tn:
        return f"execute_tool {tn}"
    if op in _LLM_OPS and md:
        return f"{op} {md}"
    return op or "internal"


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def attr_value(value: Any) -> Any:
    """OTLP AnyValue -> Python 标量。"""
    if not isinstance(value, dict):
        return value
    if "stringValue" in value:
        return value["stringValue"]
    if "boolValue" in value:
        return value["boolValue"]
    if "intValue" in value:
        return _to_int(value["intValue"])
    if "doubleValue" in value:
        return _to_float(value["doubleValue"])
    if "arrayValue" in value:
        return [attr_value(v) for v in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return {
            item["key"]: attr_value(item.get("value"))
            for item in value["kvlistValue"].get("values", [])
            if "key" in item
        }
    if "bytesValue" in value:
        return value["bytesValue"]
    return None


def _warn_if_not_nanos(*values: int) -> None:
    global _ts_warned
    if _ts_warned:
        return
    for value in values:
        if value and 0 < value < _NS_PLAUSIBLE_MIN:
            logger.warning(
                "时间戳 %s 量级异常：OTLP 的 start/endTimeUnixNano 单位是【纳秒】，"
                "收到疑似毫秒/微秒值。数据仍会入库，但会落在统计时间窗口之外而不可见。"
                "请检查发送端是否按规范填充纳秒。",
                value,
            )
            _ts_warned = True
            return


def _kind_name(raw: Any) -> str:
    if isinstance(raw, str):
        return raw.replace("SPAN_KIND_", "") or "UNSPECIFIED"
    return SPAN_KIND_NAMES.get(_to_int(raw), "UNSPECIFIED")


def _status_name(raw: Any) -> str:
    if isinstance(raw, str):
        return _STATUS_CODE_NAMES.get(raw, "UNSET")
    return _STATUS_CODE_NAMES.get(_to_int(raw), "UNSET")


def _first(attrs: Dict[str, Any], *keys: str) -> Any:
    """按优先级取第一个非空值——用于 gen_ai 的 experimental / stable 双版本兜底。"""
    for key in keys:
        value = attrs.get(key)
        if value is None or value == "":
            continue
        # 0 视为「未填充」，继续回退到下一版本；但 False 是有效值，不能被 0 吃掉
        if value == 0 and not isinstance(value, bool):
            continue
        return value
    return None


def flatten_span(span: Dict[str, Any], service: str, drop_attributes: Iterable[str]) -> Dict[str, Any]:
    drop = set(drop_attributes)
    attrs: Dict[str, Any] = {}
    for item in span.get("attributes") or []:
        key = item.get("key")
        if not key or key in drop:
            continue
        attrs[key] = attr_value(item.get("value"))

    start_ns = _to_int(span.get("startTimeUnixNano"))
    end_ns = _to_int(span.get("endTimeUnixNano"))
    _warn_if_not_nanos(start_ns, end_ns)
    duration_ms = round((end_ns - start_ns) / 1_000_000, 3) if start_ns and end_ns else 0.0

    model = _first(attrs, "gen_ai.response.model", "gen_ai.request.model")
    tool_name = _first(attrs, "gen_ai.tool.call.name", "gen_ai.tool.name")
    input_tokens = _to_int(
        _first(attrs, "gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens") or 0
    )
    output_tokens = _to_int(
        _first(attrs, "gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens") or 0
    )

    operation = _first(attrs, "gen_ai.operation.name", "gen_ai.request.type")
    if not operation:
        # 缺失时按启发式推断
        if model or input_tokens or output_tokens:
            operation = "chat"
        elif tool_name:
            operation = "tool"
        else:
            operation = "internal"
    # Jaeger 粒度：chat 类拼 model、tool 类拼 tool_name（统一为 execute_tool 前缀）
    operation = _compound_op(operation, model, tool_name)

    status = _status_name((span.get("status") or {}).get("code"))

    return {
        "trace_id": span.get("traceId", ""),
        "span_id": span.get("spanId", ""),
        "parent_span_id": span.get("parentSpanId") or "",
        "service": service or "unknown",
        "span_name": span.get("name", ""),
        "kind": _kind_name(span.get("kind")),
        "operation": operation,
        "model": model,
        "tool_name": tool_name,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "duration_ms": duration_ms,
        "status": status,
        "start_time": start_ns // 1_000_000 if start_ns else 0,
        "@timestamp": end_ns // 1_000_000 if end_ns else 0,
        "attributes": attrs,
    }


# 哪些 resource 属性算"process 级"（对齐 Jaeger Process 面板的展示范围）。
# 注意：service.name 单独抽出来做 service 字段；这里只放附属信息。
_PROCESS_KEY_PREFIXES = (
    "deployment.", "process.", "telemetry.", "host.", "os.",
    "service.namespace", "service.version", "service.instance.",
)


def _extract_process(resource_attrs: Dict[str, Any]) -> Dict[str, Any]:
    """从 resource attrs 里抽出 process 级属性（deployment.* / service.* / telemetry.* 等）。

    Jaeger 的 Process 面板展示的是这些"对一条 trace 整体生效"的属性。
    """
    return {
        k: v for k, v in resource_attrs.items()
        if any(k == p.rstrip(".") or k.startswith(p) for p in _PROCESS_KEY_PREFIXES)
    }


def flatten_payload(payload: Dict[str, Any], drop_attributes: Iterable[str]) -> List[Dict[str, Any]]:
    """OTLP ExportTraceServiceRequest(dict) -> span 文档列表。"""
    docs: List[Dict[str, Any]] = []
    for resource_span in payload.get("resourceSpans") or []:
        resource = resource_span.get("resource") or {}
        resource_attrs = {
            item["key"]: attr_value(item.get("value"))
            for item in resource.get("attributes") or []
            if "key" in item
        }
        service = resource_attrs.get("service.name") or "unknown"
        # 抽 process 级属性，挂到每个 span 上（同一 trace 内 resource 一致，所以重复存也没问题；
        # 否则 ES 查时还得 join，复杂度上升一档）
        process = _extract_process(resource_attrs)
        for scope_span in resource_span.get("scopeSpans") or []:
            for span in scope_span.get("spans") or []:
                doc = flatten_span(span, service, drop_attributes)
                if process:
                    doc["process"] = process
                docs.append(doc)
    return docs
