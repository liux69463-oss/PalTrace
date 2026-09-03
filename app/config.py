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
# 每次 bulk 后是否立即 refresh。
#   true  ：写入后立即可查，但每次 bulk 都强制 ES 生成新 segment —— ES 最贵的操作之一，
#           高吞吐下会把单次 bulk 从 ~10ms 拖到 ~100ms+。仅适合本地调试。
#   false ：由 ES 的 index.refresh_interval 兜底（默认 1s），代价是数据可见延迟约 1 秒。
# 生产默认 false。
ES_REFRESH = _bool("ES_REFRESH", False)

# 索引粒度：
#   day   -> <prefix>-YYYY.MM.DD（每天一个索引：数量多、单索引小、便于按天清理）
#   month -> <prefix>-YYYY.MM   （每月一个索引：数量少、元数据开销低、便于按月清理）
# 两种命名都落在 <prefix>-* 通配符下，可共存——切换后历史索引照常可查，无需迁移。
ES_INDEX_GRANULARITY = os.getenv("ES_INDEX_GRANULARITY", "month").strip().lower()

# 新建索引的分片/副本数。仅影响「新建」索引（已存在的索引需手动改 settings）。
# 月度索引的数据量约是按天索引的 30 倍，单分片会过大，故默认 3 分片。
# 单节点 ES（docker-compose）请设 ES_REPLICAS=0，否则副本无法分配、索引恒为 yellow。
ES_SHARDS = int(os.getenv("ES_SHARDS", "3"))
ES_REPLICAS = int(os.getenv("ES_REPLICAS", "1"))

MEMORY_MAX_SPANS = int(os.getenv("MEMORY_MAX_SPANS", "20000"))

# ---------- 服务端口 ----------
# FastAPI 单端口同时提供：OTLP/HTTP 接收(/v1/traces) + 统计 API(/api/*) + 看板(/)
HTTP_PORT = int(os.getenv("HTTP_PORT", "8000"))
GRPC_PORT = int(os.getenv("GRPC_PORT", "4317"))  # OTLP/gRPC 标准端口
# 默认关闭 gRPC：
#   1) 生产走 OTLP/HTTP——短连接，L4 负载均衡即可分摊；
#      gRPC 是 HTTP/2 长连接，L4 只做连接级均衡，加实例也分摊不了。
#   2) gRPC 与 uvicorn 多 worker 互斥：--workers N 会 fork 出 N 个进程，
#      只有第一个能 bind 4317，其余报 Address already in use
#      （已捕获为 warning，不影响 HTTP）。
# 需要 gRPC 时：设 ENABLE_GRPC=true，同时把 worker 数降为 1。
ENABLE_GRPC = _bool("ENABLE_GRPC", False)

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
