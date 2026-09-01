"""配置：全部走环境变量，便于内网/K8s 部署。"""
import os

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# ---------- 存储 ----------
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "es").strip().lower()  # es | memory

ES_URL = os.getenv("ES_URL", "http://localhost:9200")
ES_USER = os.getenv("ES_USER", "")
ES_PASSWORD = os.getenv("ES_PASSWORD", "")
ES_INDEX_PREFIX = os.getenv("ES_INDEX_PREFIX", "paltrace-spans")
ES_VERIFY_CERTS = _bool("ES_VERIFY_CERTS", False)
# 每次 bulk 后是否立即 refresh。MVP/低吞吐下开 True 便于立刻看到数据；
# 生产高吞吐建议置 false，改由 ES 默认刷新间隔（1s）兜底。
ES_REFRESH = _bool("ES_REFRESH", True)

MEMORY_MAX_SPANS = int(os.getenv("MEMORY_MAX_SPANS", "20000"))

# ---------- 服务端口 ----------
# FastAPI 单端口同时提供：OTLP/HTTP 接收(/v1/traces) + 统计 API(/api/*) + 看板(/)
HTTP_PORT = int(os.getenv("HTTP_PORT", "8000"))
GRPC_PORT = int(os.getenv("GRPC_PORT", "4317"))  # OTLP/gRPC 标准端口
ENABLE_GRPC = _bool("ENABLE_GRPC", True)

# ---------- 解析 ----------
# 隐私：默认丢弃可能包含用户输入正文的属性，不入库
DROP_ATTRIBUTES = {
    a.strip()
    for a in os.getenv(
        "DROP_ATTRIBUTES", "gen_ai.prompt,gen_ai.completion"
    ).split(",")
    if a.strip()
}

DEFAULT_HOURS = int(os.getenv("DEFAULT_HOURS", "24"))
