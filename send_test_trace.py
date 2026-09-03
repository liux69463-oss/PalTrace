"""合成 OTLP trace 并发送给 Trace Hub，用于冒烟验证。

模拟 LoongSuite 采集 PalTrace 的 span 结构：
    PalTraceAgent.reply (agent)
      ├─ chat qwen-max      (chat, 带 token)
      ├─ tool: 工具调用      (tool, 带 tool_name)
      ├─ chat qwen-max      (chat, 带 token)
      └─ 一个 ERROR span     (用于验证错误率)

root 的时长由子 span 反推（子 span 排布完再回填结束时间），确保子 span
**严格嵌套**在 root 的时间范围内，树视图的瀑布图才不会出现「子比父长」。

刻意把 int64（时间戳、token）编码成**字符串**，以覆盖 OTLP/HTTP JSON 的
proto3 JSON 规范行为，验证接收端的 _to_int 兼容。
耗时带随机抖动，使 p99 与真实 max 可区分（否则统计口径问题验证不出来）。

用法：
    python send_test_trace.py                  # http + grpc 各发一次
    python send_test_trace.py --mode http
    python send_test_trace.py --mode grpc --traces 3
"""
import argparse
import random
import sys
import time
import uuid

import requests

NS = 1_000_000_000  # OTLP 的 start/endTimeUnixNano 单位是纳秒
GAP_MS = 50         # 相邻 span 之间的间隔
MARGIN_MS = 200     # root span 在最后一个子 span 结束之后预留的余量

TOOLS = ["execute_shell", "read_file", "web_search", "list_directory"]


def _hex(n_bytes: int) -> str:
    return uuid.uuid4().hex[: n_bytes * 2]


def _attrs(pairs: dict) -> list:
    """Python 值 -> OTLP AnyValue 列表（数值按 proto3 JSON 规范编码成字符串）。"""
    out = []
    for key, value in pairs.items():
        if isinstance(value, bool):
            out.append({"key": key, "value": {"boolValue": value}})
        elif isinstance(value, int):
            out.append({"key": key, "value": {"intValue": str(value)}})
        elif isinstance(value, float):
            out.append({"key": key, "value": {"doubleValue": value}})
        else:
            out.append({"key": key, "value": {"stringValue": str(value)}})
    return out


def build_payload(service: str, with_error: bool = False, user: str = "") -> dict:
    trace_id = _hex(16)
    spans: list = []
    cursor = int(time.time() * NS)

    def add(name, kind, duration_ms, attrs, parent=None, error=None):
        nonlocal cursor
        start = cursor
        end = start + int(duration_ms * 1_000_000)
        span = {
            "traceId": trace_id,
            "spanId": _hex(8),
            "name": name,
            "kind": kind,
            "startTimeUnixNano": str(start),
            "endTimeUnixNano": str(end),
            "attributes": _attrs(attrs),
            "status": {"code": 2, "message": error} if error else {"code": 1},
        }
        if parent:
            span["parentSpanId"] = parent
        spans.append(span)
        cursor = end + GAP_MS * 1_000_000
        return span

    # root 先占位（时长 0），等子 span 全部排布完再回填结束时间。
    # 这样能保证子 span 严格嵌套在 root 的时间范围内——否则树视图的瀑布图
    # 会出现「子 span 比父 span 还长」的畸形嵌套。
    root = add(
        "PalTraceAgent.reply", 1, 0,
        {"gen_ai.operation.name": "agent", "gen_ai.request.model": "qwen-max"},
    )

    add(
        "chat qwen-max", 3, random.randint(1200, 2000),
        {
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": "qwen-max",
            "gen_ai.response.model": "qwen-max",
            "gen_ai.usage.input_tokens": random.randint(600, 1400),
            "gen_ai.usage.output_tokens": random.randint(80, 320),
        },
        parent=root["spanId"],
    )

    tool_name = random.choice(TOOLS)
    add(
        f"tool:{tool_name}", 1, random.randint(300, 700),
        {"gen_ai.operation.name": "tool", "gen_ai.tool.call.name": tool_name},
        parent=root["spanId"],
    )

    add(
        "chat qwen-max", 3, random.randint(1200, 2000),
        {
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": "qwen-max",
            "gen_ai.response.model": "qwen-max",
            "gen_ai.usage.input_tokens": random.randint(900, 2200),
            "gen_ai.usage.output_tokens": random.randint(120, 480),
        },
        parent=root["spanId"],
    )

    if with_error:
        add(
            "chat qwen-plus", 3, random.randint(200, 500),
            {
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": "qwen-plus",
                "gen_ai.response.model": "qwen-plus",
                "gen_ai.usage.input_tokens": 300,
                "gen_ai.usage.output_tokens": 0,
            },
            parent=root["spanId"],
            error="upstream timeout",
        )

    # 此时 cursor 已越过最后一个子 span（含间隔），再加余量即为 root 的结束时间
    root["endTimeUnixNano"] = str(cursor + int(MARGIN_MS * 1_000_000))

    res_attrs = {"service.name": service, "telemetry.sdk.language": "python"}
    if user:
        res_attrs["qwenpaw.user"] = user
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": _attrs(res_attrs)},
                "scopeSpans": [
                    {"scope": {"name": "loongsuite.instrumentation.agentscope"}, "spans": spans}
                ],
            }
        ]
    }


def send_http(url: str, payload: dict) -> None:
    resp = requests.post(url.rstrip("/") + "/v1/traces", json=payload, timeout=10)
    resp.raise_for_status()
    print(f"  [http] -> {url}  OK ({resp.status_code})")


def send_grpc(target: str, payload: dict) -> None:
    import grpc
    from google.protobuf.json_format import ParseDict
    from opentelemetry.proto.collector.trace.v1 import (
        trace_service_pb2,
        trace_service_pb2_grpc,
    )

    request = ParseDict(payload, trace_service_pb2.ExportTraceServiceRequest())
    with grpc.insecure_channel(target) as channel:
        stub = trace_service_pb2_grpc.TraceServiceStub(channel)
        stub.Export(request, timeout=10)
    print(f"  [grpc] -> {target}  OK")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http-url", default="http://localhost:8000")
    ap.add_argument("--grpc-target", default="localhost:4317")
    ap.add_argument("--mode", choices=["both", "http", "grpc"], default="both")
    ap.add_argument("--traces", type=int, default=2, help="每种协议发送多少条 trace")
    ap.add_argument("--service", default="paltrace")
    ap.add_argument("--user", default="", help="设置 resource 属性 qwenpaw.user（用户标识，验证按用户采集）")
    args = ap.parse_args()

    ok = True
    modes = ["http", "grpc"] if args.mode == "both" else [args.mode]

    for mode in modes:
        for i in range(args.traces):
            payload = build_payload(args.service, with_error=(i % 2 == 1), user=args.user)
            try:
                if mode == "http":
                    send_http(args.http_url, payload)
                else:
                    send_grpc(args.grpc_target, payload)
            except Exception as exc:
                ok = False
                print(f"  [{mode}] FAILED: {exc}", file=sys.stderr)

    print("\n发送完毕。打开看板查看：", args.http_url)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
