"""临时诊断接收端：收 OTLP trace，按 instrumentation scope / span 名打分布，
并原样转发给 PalTrace（:8000），不丢失看板数据。

用法：
    ./.venv/bin/python span_tap.py                 # 监听 :9999
    # 另开终端，把 QwenPaw 的 endpoint 指向它（保持你当前的禁用 env 不变）：
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:9999 loongsuite-instrument ... qwenpaw app
    # 跑一个短任务后查看分布：
    curl -s http://localhost:9999/debug/breakdown | python3 -m json.tool
"""
import logging
import threading
import urllib.request
from collections import Counter

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("span_tap")

PALTRACE_URL = "http://localhost:8000/v1/traces"
_lock = threading.Lock()
scope_counter: Counter = Counter()          # scope.name -> 次数
scope_span_counter: Counter = Counter()     # (scope.name, span.name) -> 次数
genai_op_counter: Counter = Counter()       # gen_ai.operation.name -> 次数
requested_tools: Counter = Counter()       # gen_ai.tool.call.name（模型请求的工具）
executed_tool_spans: Counter = Counter()    # span 名以 "tool:" 开头的执行 span
websearch_attr_hits = 0                     # 任意属性值含 web_search 的 span 数
no_genai = 0
total = 0

app = FastAPI()


def _value_text(val: dict) -> str:
    for k in ("stringValue", "boolValue", "intValue", "doubleValue"):
        if k in val:
            return str(val[k])
    return ""


def _extract_genai(span: dict) -> str | None:
    for a in span.get("attributes", []):
        key = a.get("key", "")
        if key in ("gen_ai.operation.name", "gen_ai.operation.type"):
            return _value_text(a.get("value", {}))
    return None


def analyze(payload: dict) -> None:
    global no_genai, total, websearch_attr_hits
    local_scope = Counter()
    local_scope_span = Counter()
    local_genai = Counter()
    local_requested = Counter()
    local_executed = Counter()
    local_no = 0
    local_total = 0
    for rs in payload.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            scope_name = (ss.get("scope") or {}).get("name", "<none>")
            for span in ss.get("spans", []):
                local_total += 1
                name = span.get("name", "")
                local_scope[scope_name] += 1
                local_scope_span[(scope_name, name)] += 1
                op = _extract_genai(span)
                if op:
                    local_genai[op] += 1
                else:
                    local_no += 1
                if name.startswith("tool:"):
                    local_executed[name] += 1
                # 扫描属性：模型请求的工具名 + 任意属性里的 web_search
                for a in span.get("attributes", []):
                    key = a.get("key", "")
                    val = _value_text(a.get("value", {}))
                    if key == "gen_ai.tool.call.name":
                        local_requested[val] += 1
                    if "web_search" in (key + val).lower():
                        websearch_attr_hits += 1
    with _lock:
        scope_counter.update(local_scope)
        scope_span_counter.update(local_scope_span)
        genai_op_counter.update(local_genai)
        requested_tools.update(local_requested)
        executed_tool_spans.update(local_executed)
        no_genai += local_no
        total += local_total


def _forward(body: bytes, ctype: str) -> None:
    try:
        req = urllib.request.Request(
            PALTRACE_URL, data=body, headers={"Content-Type": ctype}
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as exc:  # 转发失败不影响诊断
        logger.warning("转发 PalTrace 失败: %s", exc)


@app.post("/v1/traces")
async def receive(request: Request):
    ctype = request.headers.get("content-type", "")
    body = await request.body()
    try:
        if "application/x-protobuf" in ctype:
            req = trace_service_pb2.ExportTraceServiceRequest()
            req.ParseFromString(body)
            payload = MessageToDict(req)
        else:
            import json

            payload = json.loads(body)
    except Exception as exc:
        logger.warning("解析失败: %s", exc)
        return JSONResponse(status_code=400, content={"error": "bad body"})
    analyze(payload)
    _forward(body, ctype)
    return {}


@app.get("/debug/breakdown")
def breakdown():
    with _lock:
        return {
            "total_spans": total,
            "no_genai_operation": no_genai,
            "by_scope": scope_counter.most_common(40),
            "by_scope_span": [
                {"scope": s, "span": n, "count": c}
                for (s, n), c in scope_span_counter.most_common(40)
            ],
            "by_genai_operation": genai_op_counter.most_common(20),
        }


@app.get("/debug/tools")
def tools():
    with _lock:
        return {
            "requested_tools (模型请求的工具名)": requested_tools.most_common(40),
            "executed_tool_spans (生成的 tool: span)": executed_tool_spans.most_common(40),
            "websearch_attr_hits (属性里出现 web_search 的 span 数)": websearch_attr_hits,
        }


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
