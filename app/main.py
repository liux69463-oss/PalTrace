"""PalTrace —— FastAPI 主应用。

单端口同时提供：
    POST /v1/traces   OTLP/HTTP 接收端点
    GET  /api/*       统计接口
    GET  /            看板页面
另有独立 gRPC 接收端跑在 :4317（OTLP/gRPC 标准端口）。
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from . import config
from .otlp import flatten_payload
from .storage import Storage, build_storage

# OTLP/HTTP 同时支持 JSON 与 protobuf 两种编码（按 Content-Type 区分）。
# protobuf 解码复用与 gRPC 端完全相同的逻辑，避免两套解析分叉。
from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("trace_hub")

STATIC_DIR = Path(__file__).parent / "static"

storage: Optional[Storage] = None
_grpc_server = None


def handle_payload(payload: Dict[str, Any]) -> int:
    """HTTP 与 gRPC 共用的落库入口。"""
    assert storage is not None, "storage 未初始化"
    docs = flatten_payload(payload, config.DROP_ATTRIBUTES)
    if docs:
        storage.index(docs)
        logger.info("接收 %s 个 span", len(docs))
    return len(docs)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global storage, _grpc_server
    storage = build_storage()

    if config.ENABLE_GRPC:
        try:
            from .grpc_server import start_grpc_server

            _grpc_server = start_grpc_server(config.GRPC_PORT, handle_payload)
        except Exception as exc:  # gRPC 不可用时不应拖垮 HTTP
            logger.warning("gRPC 接收端启动失败（HTTP 仍可用）: %s", exc)

    yield

    if _grpc_server is not None:
        _grpc_server.stop(0)


app = FastAPI(title="PalTrace", version="0.1.0", lifespan=lifespan)


def _safe(fn: Callable[[], Dict[str, Any]]) -> Any:
    """把后端异常转成 503，避免看板整页崩溃。"""
    try:
        return fn()
    except Exception as exc:
        logger.exception("统计查询失败")
        return JSONResponse(status_code=503, content={"error": str(exc)})


# ---------------- OTLP 接收 ----------------
@app.post("/v1/traces")
async def otlp_http(request: Request):
    # OTLP/HTTP 按 Content-Type 区分编码：
    #   application/x-protobuf  → 二进制 protobuf 包体（OTel SDK 的 http/protobuf 默认走这）
    #   application/json         → OTLP/JSON 包体
    # 非法 body 属于客户端错误，必须回 4xx：OTLP exporter 只把 5xx 当作可重试，
    # 若这里抛 500 会引发无意义的重试风暴。
    ctype = request.headers.get("content-type", "")
    try:
        if "application/x-protobuf" in ctype:
            body = await request.body()
            req = trace_service_pb2.ExportTraceServiceRequest()
            req.ParseFromString(body)
            payload = MessageToDict(req)  # 默认 camelCase，与 gRPC 端一致
        else:
            payload = await request.json()
    except Exception as exc:
        logger.warning("接收失败（非法 body）: %s", exc)
        return JSONResponse(status_code=400, content={"error": "invalid JSON body"})
    if not isinstance(payload, dict):
        return JSONResponse(status_code=400, content={"error": "body must be a JSON object"})

    handle_payload(payload)
    # 严格遵循 OTLP 响应语义：exporter 只看状态码，返回空对象即可
    return {}


# ---------------- 统计接口 ----------------
@app.get("/api/overview")
def api_overview(hours: int = Query(config.DEFAULT_HOURS, ge=1, le=24 * 30)):
    return _safe(lambda: storage.overview(hours))


@app.get("/api/tokens")
def api_tokens(
    hours: int = Query(config.DEFAULT_HOURS, ge=1, le=24 * 30),
    size: int = Query(20, ge=1, le=200),
):
    return _safe(lambda: storage.tokens_by_model(hours, size))


@app.get("/api/latency")
def api_latency(
    hours: int = Query(config.DEFAULT_HOURS, ge=1, le=24 * 30),
    size: int = Query(20, ge=1, le=200),
):
    return _safe(lambda: storage.latency(hours, size))


@app.get("/api/tools")
def api_tools(
    hours: int = Query(config.DEFAULT_HOURS, ge=1, le=24 * 30),
    size: int = Query(20, ge=1, le=200),
):
    return _safe(lambda: storage.tool_calls(hours, size))


MAX_RANGE_MS = 30 * 24 * 3600 * 1000  # 时间跨度上限：30 天（与 hours 上限 720h 一致）


def _range_error(start: Optional[int], end: Optional[int]) -> Optional[str]:
    """校验显式时间范围，返回错误文案（None 表示通过）。

    参数错误属于客户端问题，必须回 400 而非 500，否则看板只会笼统报错。
    """
    if start is None and end is None:
        return None
    if start is None or end is None:
        return "start 与 end 必须同时提供"
    if start >= end:
        return "start 必须早于 end"
    if end - start > MAX_RANGE_MS:
        return "时间跨度不能超过 30 天"
    return None


@app.get("/api/traces")
def api_traces(
    hours: int = Query(config.DEFAULT_HOURS, ge=1, le=24 * 30),
    size: int = Query(20, ge=1, le=200),
    operation: Optional[str] = Query(None, description="只看该 operation 的 trace"),
    start: Optional[int] = Query(None, description="起始时间（epoch 毫秒）"),
    end: Optional[int] = Query(None, description="结束时间（epoch 毫秒）"),
):
    """trace 列表：支持 operation 过滤 + 时间范围 + 两者组合。

    显式 start/end 优先于 hours。operation 只决定「哪些 trace 入选」，
    返回的 spans / total_tokens 仍是整条 trace 的真实统计（见 ADR-11）。
    """
    err = _range_error(start, end)
    if err:
        return JSONResponse(status_code=400, content={"error": err})
    return _safe(lambda: storage.recent_traces(hours, size, operation, start, end))


@app.get("/api/operations")
def api_operations(
    hours: int = Query(config.DEFAULT_HOURS, ge=1, le=24 * 30),
    size: int = Query(200, ge=1, le=1000),
    start: Optional[int] = Query(None, description="起始时间（epoch 毫秒）"),
    end: Optional[int] = Query(None, description="结束时间（epoch 毫秒）"),
    service: Optional[str] = Query(None, description="按 service 级联过滤"),
):
    """时间窗内出现过的 operation 列表，供看板过滤下拉框使用。

    默认 size 调到 200 —— 真实业务里一个 service 几十种 tool + 各种 chat 模型，
    50 不够。max=1000 防爆。
    """
    err = _range_error(start, end)
    if err:
        return JSONResponse(status_code=400, content={"error": err})
    return _safe(lambda: storage.list_operations(hours, size, start, end, service))


@app.get("/api/services")
def api_services(
    hours: int = Query(config.DEFAULT_HOURS, ge=1, le=24 * 30),
    size: int = Query(50, ge=1, le=200),
    start: Optional[int] = Query(None, description="起始时间（epoch 毫秒）"),
    end: Optional[int] = Query(None, description="结束时间（epoch 毫秒）"),
):
    """时间窗内出现过的 service 列表及 span 数，供看板过滤下拉框使用。"""
    err = _range_error(start, end)
    if err:
        return JSONResponse(status_code=400, content={"error": err})
    return _safe(lambda: storage.list_services(hours, size, start, end))


@app.get("/api/traces/{trace_id:path}")
def api_trace_detail(trace_id: str, limit: int = Query(1000, ge=1, le=5000)):
    """单条 trace 的全量 span + 元信息，供看板树视图渲染。

    trace_id 可能是 base64（含 '/' '+' '='），用 `:path` 转换器允许路径中
    出现 '/'，否则 URL 编码的 %2F 仍会被 Starlette 视作分隔符拒绝匹配。
    不带时间窗（见 storage.get_trace 说明）。查不到时回 404，
    让前端能区分「trace 不存在」与「查询失败」，避免把前者显示成故障。
    """

    def _get():
        data = storage.get_trace(trace_id, limit)
        if not data.get("spans"):
            return JSONResponse(status_code=404, content={"error": "trace not found"})
        return data

    return _safe(_get)


# ---------------- 运维 ----------------
@app.get("/healthz")
def healthz():
    return {
        "status": "ok",
        "backend": config.STORAGE_BACKEND,
        "grpc_enabled": config.ENABLE_GRPC,
        "grpc_port": config.GRPC_PORT,
    }


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
