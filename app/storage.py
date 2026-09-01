"""存储层：ES 7.x 主后端 + Memory 冒烟后端。

两者暴露同名方法，上层无感切换：
  - EsStorage     -> 用 ES 原生聚合（正确、可伸缩）
  - MemoryStorage -> 用 Python 侧聚合（无 ES 也能跑通验证）

注意：ES 7.x 必须用 7.x 客户端。8.x 客户端面向 ES 8 API，
且 bulk() 参数由 body= 改名为 operations=，混用会直接报错。
"""
import logging
import math
import time
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from elasticsearch import Elasticsearch

from .otlp import _compound_op

logger = logging.getLogger(__name__)

# ES runtime field：与 otlp._compound_op 同口径，用于查询时派生 `compound_op`。
# 这样老数据（operation 还是粗粒度 "chat"）和刚落库的新数据（"chat deepseek-..."）
# 都能在 /api/operations 与 /api/traces?operation=... 里用同一份口径。
# 字符串必须单行——ES 7.x 不接受多行 script。
_COMPOUND_OP_RT_SCRIPT = (
    "String op = doc.containsKey('operation') && doc['operation'].size() > 0 ? doc['operation'].value : ''; "
    "String model = doc.containsKey('model') && doc['model'].size() > 0 ? doc['model'].value : ''; "
    "String tool = doc.containsKey('tool_name') && doc['tool_name'].size() > 0 ? doc['tool_name'].value : ''; "
    "if ((op == 'tool' || op == 'execute_tool') && tool.length() > 0) { emit('execute_tool ' + tool); return; } "
    "if ((op == 'chat' || op == 'llm' || op == 'generate' || op == 'embedding' || op == 'completion') && model.length() > 0) { emit(op + ' ' + model); return; } "
    "if (op.length() > 0) { emit(op); return; } "
    "emit('internal');"
)

_COMPOUND_OP_RUNTIME = {
    "compound_op": {
        "type": "keyword",
        "script": _COMPOUND_OP_RT_SCRIPT,
    }
}


def _percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    k = (len(ordered) - 1) * (p / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return round(ordered[int(k)], 3)
    return round(ordered[lo] * (hi - k) + ordered[hi] * (k - lo), 3)


def _trace_meta(spans: List[Dict[str, Any]], truncated: bool) -> Dict[str, Any]:
    """由扁平 span 列表计算 trace 级元信息。

    ES 与 Memory 后端共用本函数，保证两个后端的返回口径完全一致。

    注意 start_time 是 epoch 毫秒、duration_ms 是毫秒，两者单位一致可直接相加。
    """
    stamps = [s.get("start_time") or 0 for s in spans]
    ends = [
        (s.get("start_time") or 0) + (s.get("duration_ms") or 0.0)
        for s in spans
        if s.get("start_time")
    ]
    start = min(stamps) if stamps else 0
    end = max(ends) if ends else 0
    return {
        "span_count": len(spans),
        "truncated": truncated,
        "services": sorted({(s.get("service") or "unknown") for s in spans}),
        "start": start,
        "end": int(end),
        "duration_ms": round(max(end - start, 0.0), 3),
        "total_tokens": sum(s.get("total_tokens") or 0 for s in spans),
        "errors": sum(1 for s in spans if s.get("status") == "ERROR"),
    }


def _resolve_range(
    hours: int,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> Tuple[int, int]:
    """把 hours 窗口 / 显式起止统一解析成 (start_ms, end_ms)。

    显式起止优先——前端的时间预设已换算成 epoch 毫秒；
    ES 与 Memory 两个后端共用本函数，保证时间口径完全一致。
    """
    if start_ms is not None and end_ms is not None:
        return int(start_ms), int(end_ms)
    now = int(time.time() * 1000)
    return now - int(hours) * 3600 * 1000, now


def _template_body(prefix: str) -> Dict[str, Any]:
    """固定 mapping，避免动态映射把 model/operation 推断成 text 导致无法聚合。"""
    return {
        "index_patterns": [f"{prefix}-*"],
        "priority": 200,
        "template": {
            "settings": {"number_of_shards": 1, "number_of_replicas": 0},
            "mappings": {
                "properties": {
                    "trace_id": {"type": "keyword"},
                    "span_id": {"type": "keyword"},
                    "parent_span_id": {"type": "keyword"},
                    "service": {"type": "keyword"},
                    "span_name": {"type": "keyword"},
                    "kind": {"type": "keyword"},
                    "operation": {"type": "keyword"},
                    "model": {"type": "keyword"},
                    "tool_name": {"type": "keyword"},
                    "status": {"type": "keyword"},
                    "input_tokens": {"type": "long"},
                    "output_tokens": {"type": "long"},
                    "total_tokens": {"type": "long"},
                    "duration_ms": {"type": "double"},
                    "start_time": {"type": "date", "format": "epoch_millis"},
                    "@timestamp": {"type": "date", "format": "epoch_millis"},
                    # 属性是开放字典：动态建索引会导致 field explosion（每个新 key 都进 mapping）。
                    # 设为 dynamic:false —— 仍完整保留在 _source 中可回查/重建索引，但不建索引。
                    "attributes": {"type": "object", "dynamic": False},
                    # process 级属性（resource 上的 deployment.*/service.*/telemetry.* 等），
                    # 跟 Jaeger Process 面板展示范围对齐；dynamic:false 防 field explosion
                    "process": {"type": "object", "dynamic": False},
                }
            },
        },
    }


class Storage(ABC):
    @abstractmethod
    def index(self, docs: List[Dict[str, Any]]) -> int:
        ...

    @abstractmethod
    def overview(self, hours: int) -> Dict[str, Any]:
        ...

    @abstractmethod
    def tokens_by_model(self, hours: int, size: int) -> Dict[str, Any]:
        ...

    @abstractmethod
    def latency(self, hours: int, size: int) -> Dict[str, Any]:
        ...

    @abstractmethod
    def tool_calls(self, hours: int, size: int) -> Dict[str, Any]:
        ...

    @abstractmethod
    def recent_traces(
        self,
        hours: int,
        size: int,
        operation: Optional[str] = None,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """近期 trace 列表，支持 operation 过滤与时间范围。

        时间范围：显式传 start_ms/end_ms（epoch 毫秒）时优先，否则回退到 hours 窗口。
        operation：只返回**包含**该 operation span 的 trace；
        但返回的 spans / total_tokens / service 仍是**整条 trace** 的真实统计
        （见 ADR-11，避免"列表 Span=3、点进去 13"的语义割裂）。
        """
        ...

    @abstractmethod
    def list_operations(
        self,
        hours: int,
        size: int = 50,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
        service: Optional[str] = None,
    ) -> Dict[str, Any]:
        """该时间窗内出现过的 operation 列表及计数，供前端过滤下拉框使用。

        service 非空时只统计该 service 的 span——用于前端 service→operation 级联。
        """
        ...

    @abstractmethod
    def list_services(
        self,
        hours: int,
        size: int = 50,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """该时间窗内出现过的 service 列表及 span 数，供前端过滤下拉框使用。"""
        ...

    @abstractmethod
    def get_trace(self, trace_id: str, limit: int = 1000) -> Dict[str, Any]:
        """单条 trace 的全量 span（按时间升序）+ trace 级元信息。

        与其他统计方法不同：本方法**不带 hours 时间窗**——
        列表里点开一条稍旧的 trace 时，受统计窗口限制会查不到，体验割裂。
        跨天由 paltrace-spans-* 通配索引天然覆盖。
        """
        ...


class MemoryStorage(Storage):
    """环形缓冲区，用于无 ES 环境的冒烟测试与本地开发。"""

    def __init__(self, max_spans: int = 20000):
        self._spans: deque = deque(maxlen=max_spans)

    def index(self, docs: List[Dict[str, Any]]) -> int:
        self._spans.extend(docs)
        return len(docs)

    def _window(self, hours: int) -> List[Dict[str, Any]]:
        cutoff = int(time.time() * 1000) - hours * 3600 * 1000
        return [s for s in self._spans if (s.get("@timestamp") or 0) >= cutoff]

    def overview(self, hours: int) -> Dict[str, Any]:
        window = self._window(hours)
        traces = {s["trace_id"] for s in window if s.get("trace_id")}
        errors = sum(1 for s in window if s.get("status") == "ERROR")
        stamps = [s["@timestamp"] for s in window if s.get("@timestamp")]
        return {
            "spans": len(window),
            "traces": len(traces),
            "errors": errors,
            "error_rate": round(errors / len(window) * 100, 2) if window else 0.0,
            "from": min(stamps) if stamps else None,
            "to": max(stamps) if stamps else None,
        }

    def tokens_by_model(self, hours: int, size: int = 20) -> Dict[str, Any]:
        acc: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        )
        for span in self._window(hours):
            model = span.get("model")
            if not model:
                continue
            # 与 EsStorage 保持同一口径：只统计真正产生 token 消耗的 span
            in_tok = span.get("input_tokens") or 0
            out_tok = span.get("output_tokens") or 0
            if not in_tok and not out_tok:
                continue
            item = acc[model]
            item["calls"] += 1
            item["input_tokens"] += in_tok
            item["output_tokens"] += out_tok
            item["total_tokens"] += in_tok + out_tok
        items = [{"model": k, **v} for k, v in acc.items()]
        items.sort(key=lambda x: -x["total_tokens"])
        return {"items": items[:size]}

    def latency(self, hours: int, size: int = 20) -> Dict[str, Any]:
        buckets: Dict[str, List[float]] = defaultdict(list)
        for span in self._window(hours):
            buckets[span.get("operation") or "internal"].append(span.get("duration_ms") or 0.0)
        items = [
            {
                "operation": op,
                "count": len(vals),
                "p50": _percentile(vals, 50),
                "p95": _percentile(vals, 95),
                "p99": _percentile(vals, 99),
                "max": round(max(vals), 3) if vals else 0.0,
            }
            for op, vals in buckets.items()
        ]
        items.sort(key=lambda x: -x["count"])
        return {"items": items[:size]}

    def tool_calls(self, hours: int, size: int = 20) -> Dict[str, Any]:
        counts: Dict[str, int] = defaultdict(int)
        for span in self._window(hours):
            tool = span.get("tool_name")
            if tool:
                counts[tool] += 1
        items = [{"tool_name": k, "calls": v} for k, v in counts.items()]
        items.sort(key=lambda x: -x["calls"])
        return {"items": items[:size]}

    def recent_traces(
        self,
        hours: int,
        size: int = 20,
        operation: Optional[str] = None,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        start, end = _resolve_range(hours, start_ms, end_ms)
        # 用 start_time 而不是 @timestamp：start_time 是 span 真正开始的时间，
        # 而 @timestamp = end_time，会比 now 略大（duration_ms），导致
        # _resolve_range 上界过滤把刚发的 trace 排除。这是 pre-existing bug，
        # 之前没人触发是因为 ES 端用 @timestamp range 仍能在毫秒漂移内命中，
        # 而内存端精确到毫秒就漏了。改用 start_time 语义更准：
        # 「这段时间内开始的 span 所属的 trace 都算命中」。
        window = [s for s in self._spans if start <= (s.get("start_time") or 0) <= end]

        # 阶段一：决定哪些 trace 入选。operation 只影响"入选"，不影响统计口径（ADR-11）
        if operation:
            # 用 _compound_op 派生后比对——与 /api/operations 下拉里看到的口径一致
            hit_ids = {
                s["trace_id"] for s in window
                if _compound_op(s.get("operation"), s.get("model"), s.get("tool_name")) == operation
                and s.get("trace_id")
            }
        else:
            hit_ids = {s["trace_id"] for s in window if s.get("trace_id")}

        # 阶段二：统计基于该 trace 的**全部** span
        acc: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {
                "spans": 0, "total_tokens": 0, "last_ts": 0,
                "start_time": None, "end_time": None,
                "services": set(), "service_spans": defaultdict(int),
                "operations": set(),
                "root_span": None,    # 最早开始的 span（最像 root）
            }
        )
        for span in window:
            tid = span.get("trace_id")
            if not tid or tid not in hit_ids:
                continue
            item = acc[tid]
            item["spans"] += 1
            item["total_tokens"] += span.get("total_tokens") or 0
            item["last_ts"] = max(item["last_ts"], span.get("@timestamp") or 0)
            svc = span.get("service") or "unknown"
            item["services"].add(svc)
            item["service_spans"][svc] += 1
            item["operations"].add(span.get("operation") or "internal")
            st = span.get("start_time") or 0
            et = st + (span.get("duration_ms") or 0.0)
            if item["start_time"] is None or st < item["start_time"]:
                item["start_time"] = st
                item["root_span"] = span    # 最早开始的就是最像 root 的
            if item["end_time"] is None or et > item["end_time"]:
                item["end_time"] = et

        items = []
        for tid, v in acc.items():
            duration = max((v["end_time"] or 0) - (v["start_time"] or 0), 0.0)
            services = sorted(v["services"])
            # trace_name: "<service>: <root_span_name>"，单服务时省略 service 前缀。
            # 用 span_name（"invoke_agent Default Agent"）而不是 operation（"agent"），
            # 与 Jaeger 风格一致——operation 是分类标签，span_name 才是这条 trace 的"标题"。
            root = v["root_span"] or {}
            root_name = root.get("span_name") or "unknown"
            trace_name = (
                f"{services[0]}: {root_name}" if len(services) == 1
                else f"{','.join(services)}: {root_name}"
            )
            items.append({
                "trace_id": tid,
                "spans": v["spans"],
                "total_tokens": v["total_tokens"],
                "last_ts": v["last_ts"],
                "service": ", ".join(services),
                "services": services,
                "service_spans": dict(v["service_spans"]),
                "operations": sorted(v["operations"]),
                "duration_ms": round(duration, 3),
                "start_time": int(v["start_time"]) if v["start_time"] else 0,
                "trace_name": trace_name,
            })
        items.sort(key=lambda x: -x["last_ts"])
        return {"items": items[:size]}

    def list_operations(
        self,
        hours: int,
        size: int = 50,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
        service: Optional[str] = None,
    ) -> Dict[str, Any]:
        start, end = _resolve_range(hours, start_ms, end_ms)
        counts: Dict[str, int] = defaultdict(int)
        # 用 start_time 过滤——见 recent_traces 的注释（@timestamp 比 now 略大，会被上界排除）
        for span in self._spans:
            if not (start <= (span.get("start_time") or 0) <= end):
                continue
            if service and (span.get("service") or "") != service:
                continue
            # 用 Jaeger 粒度复合 operation：chat {model} / execute_tool {tool_name} / ...
            counts[_compound_op(span.get("operation"), span.get("model"), span.get("tool_name"))] += 1
        items = [{"operation": k, "count": v} for k, v in counts.items()]
        items.sort(key=lambda x: (-x["count"], x["operation"]))
        return {"items": items[:size]}

    def list_services(
        self,
        hours: int,
        size: int = 50,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        start, end = _resolve_range(hours, start_ms, end_ms)
        counts: Dict[str, int] = defaultdict(int)
        # 用 start_time 过滤——见 recent_traces 的注释
        for span in self._spans:
            if start <= (span.get("start_time") or 0) <= end:
                counts[span.get("service") or "unknown"] += 1
        items = [{"service": k, "count": v} for k, v in counts.items()]
        items.sort(key=lambda x: (-x["count"], x["service"]))
        return {"items": items[:size]}

    def get_trace(self, trace_id: str, limit: int = 1000) -> Dict[str, Any]:
        # 线性扫描整个环形缓冲区：memory 后端仅用于冒烟/开发，不做索引优化。
        matched = [s for s in self._spans if s.get("trace_id") == trace_id]
        matched.sort(key=lambda s: (s.get("start_time") or 0, s.get("span_id") or ""))
        truncated = len(matched) > limit
        spans = matched[:limit]
        return {
            "trace_id": trace_id,
            "spans": spans,
            "meta": _trace_meta(spans, truncated),
        }


class EsStorage(Storage):
    """ES 7.x 后端。统计全部用 ES 原生聚合。"""

    def __init__(
        self,
        url: str,
        user: str = "",
        password: str = "",
        index_prefix: str = "paltrace-spans",
        verify_certs: bool = False,
        refresh: bool = True,
    ):
        kwargs: Dict[str, Any] = {"verify_certs": verify_certs}
        if user:
            kwargs["http_auth"] = (user, password)
        self.es = Elasticsearch([url], **kwargs)
        self.prefix = index_prefix
        self.refresh = refresh
        self._ensure_template()

    # ---------- 写入 ----------
    def _ensure_template(self) -> None:
        body = _template_body(self.prefix)
        try:
            self.es.indices.put_index_template(name=self.prefix, body=body)
            logger.info("index template 已就绪: %s", self.prefix)
            return
        except Exception as exc:  # ES < 7.8 无 composable template
            logger.debug("put_index_template 失败，尝试 legacy: %s", exc)
        try:
            legacy = {
                "index_patterns": body["index_patterns"],
                "settings": body["template"]["settings"],
                "mappings": body["template"]["mappings"],
            }
            self.es.indices.put_template(name=self.prefix, body=legacy)
            logger.info("legacy index template 已就绪: %s", self.prefix)
        except Exception as exc:
            logger.warning("index template 创建失败（写入仍可继续，但 mapping 非最优）: %s", exc)

    def _index_name(self) -> str:
        return f"{self.prefix}-{datetime.now(timezone.utc).strftime('%Y.%m.%d')}"

    def index(self, docs: List[Dict[str, Any]]) -> int:
        if not docs:
            return 0
        name = self._index_name()
        ops: List[Dict[str, Any]] = []
        for doc in docs:
            ops.append({"index": {"_index": name}})
            ops.append(doc)
        # 必须检查 bulk 返回的 errors：单条 doc 失败（字段类型冲突、mapping 问题等）
        # 不会影响整体 HTTP 状态码，不检查就会静默丢数据。
        res = self.es.bulk(body=ops, refresh="true" if self.refresh else "false")
        self._log_bulk_errors(res, len(docs))
        return len(docs)

    @staticmethod
    def _log_bulk_errors(res: Dict[str, Any], total: int) -> int:
        """检查 bulk 响应中的逐条错误，返回失败条数。"""
        if not res or not res.get("errors"):
            return 0
        failed = 0
        samples: List[str] = []
        for item in res.get("items") or []:
            action = item.get("index") or item.get("create") or {}
            error = action.get("error")
            if not error:
                continue
            failed += 1
            if len(samples) < 3:
                reason = error.get("reason") if isinstance(error, dict) else error
                samples.append(f"{action.get('_id', '?')}: {error.get('type')} - {str(reason)[:200]}")
        logger.error(
            "ES bulk 写入部分失败：%s/%s 条未入库（数据已丢失）。样例: %s",
            failed, total, " | ".join(samples) or "(无详情)",
        )
        return failed

    # ---------- 查询 ----------
    def _range(self, hours: int) -> Dict[str, Any]:
        return {"range": {"@timestamp": {"gte": f"now-{hours}h", "lte": "now"}}}

    def _search(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self.es.search(index=f"{self.prefix}-*", body=body)

    def overview(self, hours: int) -> Dict[str, Any]:
        body = {
            "size": 0,
            "query": self._range(hours),
            "aggs": {
                # cardinality 默认是近似值，precision_threshold 默认仅 3000；
                # trace 是高基数字段，需显式调高，否则 trace 数明显偏小。
                "traces": {
                    "cardinality": {"field": "trace_id", "precision_threshold": 40000}
                },
                "errors": {"filter": {"term": {"status": "ERROR"}}},
                "ts_from": {"min": {"field": "@timestamp"}},
                "ts_to": {"max": {"field": "@timestamp"}},
            },
        }
        res = self._search(body)
        agg = res.get("aggregations", {})
        spans = res.get("hits", {}).get("total", {}).get("value", 0)
        errors = agg.get("errors", {}).get("doc_count", 0)

        def _ms(node: Dict[str, Any]) -> Optional[int]:
            value = node.get("value")
            return int(value) if value is not None else None

        return {
            "spans": spans,
            "traces": agg.get("traces", {}).get("value", 0),
            "errors": errors,
            "error_rate": round(errors / spans * 100, 2) if spans else 0.0,
            "from": _ms(agg.get("ts_from", {})),
            "to": _ms(agg.get("ts_to", {})),
        }

    def tokens_by_model(self, hours: int, size: int = 20) -> Dict[str, Any]:
        # 只统计真正产生 token 消耗的 span：父 span（如 agent）常带 gen_ai.*.model
        # 却没有 usage 字段，计入会让「调用次数」虚高。
        body = {
            "size": 0,
            "query": {
                "bool": {
                    "filter": [
                        self._range(hours),
                        {
                            "bool": {
                                "should": [
                                    {"range": {"input_tokens": {"gt": 0}}},
                                    {"range": {"output_tokens": {"gt": 0}}},
                                ],
                                "minimum_should_match": 1,
                            }
                        },
                    ]
                }
            },
            "aggs": {
                "by_model": {
                    "terms": {"field": "model", "size": size},
                    "aggs": {
                        "input_tokens": {"sum": {"field": "input_tokens"}},
                        "output_tokens": {"sum": {"field": "output_tokens"}},
                    },
                }
            },
        }
        res = self._search(body)
        buckets = res.get("aggregations", {}).get("by_model", {}).get("buckets", [])
        items = []
        for bucket in buckets:
            in_tok = int(bucket.get("input_tokens", {}).get("value") or 0)
            out_tok = int(bucket.get("output_tokens", {}).get("value") or 0)
            items.append(
                {
                    "model": bucket.get("key"),
                    "calls": bucket.get("doc_count", 0),
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "total_tokens": in_tok + out_tok,
                }
            )
        items.sort(key=lambda x: -x["total_tokens"])
        return {"items": items}

    def latency(self, hours: int, size: int = 20) -> Dict[str, Any]:
        body = {
            "size": 0,
            "query": self._range(hours),
            "aggs": {
                "by_operation": {
                    "terms": {"field": "operation", "size": size},
                    "aggs": {
                        "lat": {
                            "percentiles": {
                                "field": "duration_ms",
                                "percents": [50, 95, 99],
                            }
                        },
                        "max_duration": {"max": {"field": "duration_ms"}},
                    },
                }
            },
        }
        res = self._search(body)
        buckets = res.get("aggregations", {}).get("by_operation", {}).get("buckets", [])
        items = []
        for bucket in buckets:
            values = bucket.get("lat", {}).get("values", {})
            items.append(
                {
                    "operation": bucket.get("key"),
                    "count": bucket.get("doc_count", 0),
                    "p50": round(values.get("50.0") or 0, 3),
                    "p95": round(values.get("95.0") or 0, 3),
                    "p99": round(values.get("99.0") or 0, 3),
                    "max": round(bucket.get("max_duration", {}).get("value") or 0, 3),
                }
            )
        return {"items": items}

    def tool_calls(self, hours: int, size: int = 20) -> Dict[str, Any]:
        body = {
            "size": 0,
            "query": {"bool": {"filter": [self._range(hours), {"exists": {"field": "tool_name"}}]}},
            "aggs": {"by_tool": {"terms": {"field": "tool_name", "size": size}}},
        }
        res = self._search(body)
        buckets = res.get("aggregations", {}).get("by_tool", {}).get("buckets", [])
        return {
            "items": [
                {"tool_name": b.get("key"), "calls": b.get("doc_count", 0)} for b in buckets
            ]
        }

    def recent_traces(
        self,
        hours: int,
        size: int = 20,
        operation: Optional[str] = None,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        start, end = _resolve_range(hours, start_ms, end_ms)
        # 显式毫秒而非 now-{hours}h 相对表达式：可控、可缓存友好，且与 Memory 后端口径一致
        time_filter: Dict[str, Any] = {"range": {"@timestamp": {"gte": start, "lte": end}}}

        # ---- 阶段一：operation 只决定「哪些 trace 入选」，并按最近时间排序（ADR-11）----
        ordered_ids: Optional[List[str]] = None
        if operation:
            # 走 `compound_op` runtime field：与下拉里展示的"操作名"完全对齐，
            # 老数据 (operation 粗粒度) 和新数据 (operation 已复合) 都能命中
            body: Dict[str, Any] = {
                "size": 0,
                "runtime_mappings": _COMPOUND_OP_RUNTIME,
                "query": {"bool": {"filter": [time_filter, {"term": {"compound_op": operation}}]}},
                "aggs": {
                    "by_trace": {
                        "terms": {
                            "field": "trace_id",
                            "size": size,
                            "order": {"last_ts": "desc"},
                            "shard_size": size * 10,
                        },
                        "aggs": {"last_ts": {"max": {"field": "@timestamp"}}},
                    }
                },
            }
            res = self._search(body)
            buckets = res.get("aggregations", {}).get("by_trace", {}).get("buckets", [])
            ordered_ids = [b.get("key") for b in buckets if b.get("key")]
            if not ordered_ids:
                return {"items": []}   # 短路：无命中就不必再查一次

        # ---- 阶段二：统计基于整条 trace 的全部 span ----
        must: List[Dict[str, Any]] = [time_filter]
        if ordered_ids is not None:
            must.append({"terms": {"trace_id": ordered_ids}})
            # 已锁定 trace 集合，无需再排序/扩 shard
            terms_agg: Dict[str, Any] = {"field": "trace_id", "size": len(ordered_ids)}
        else:
            terms_agg = {
                "field": "trace_id",
                "size": size,
                "order": {"last_ts": "desc"},
                # 按子聚合排序时，ES 只在每个分片取 shard_size 个候选，
                # 默认值会让「最近 trace」排序不准，调高以提升精度。
                "shard_size": size * 10,
            }

        body = {
            "size": 0,
            "query": {"bool": {"filter": must}},
            "aggs": {
                "by_trace": {
                    "terms": terms_agg,
                    "aggs": {
                        "last_ts": {"max": {"field": "@timestamp"}},
                        "tokens": {"sum": {"field": "total_tokens"}},
                        "min_start": {"min": {"field": "start_time"}},
                        # 取最早开始的 span 作为 root span（多数情况即为真 root；
                        # 即便异常场景里"先开始的 span"也是用户最想看的入口）
                        "first_span": {
                            "top_hits": {
                                "size": 1,
                                "sort": [{"start_time": {"order": "asc"}}],
                                "_source": ["span_name", "operation", "service", "start_time"],
                            }
                        },
                        "services": {"terms": {"field": "service", "size": 5}},
                        "operations": {"terms": {"field": "operation", "size": 10}},
                    },
                }
            },
        }
        res = self._search(body)
        buckets = res.get("aggregations", {}).get("by_trace", {}).get("buckets", [])

        def _int(v: Any) -> Optional[int]:
            return int(v) if v is not None else None

        by_id: Dict[str, Dict[str, Any]] = {}
        for bucket in buckets:
            services_buckets = bucket.get("services", {}).get("buckets", [])
            ops_buckets = bucket.get("operations", {}).get("buckets", [])
            services = sorted(s for s in (b.get("key") for b in services_buckets) if s)
            service_spans = {b.get("key"): b.get("doc_count", 0) for b in services_buckets if b.get("key")}

            # 从 top_hits 拿 root span（最早开始的 span）
            hits = bucket.get("first_span", {}).get("hits", {}).get("hits", [])
            root_src = hits[0].get("_source", {}) if hits else {}
            # trace_name 用 span_name（"invoke_agent Default Agent"）而非 operation，
            # 与 Jaeger 风格一致——operation 是分类标签，span_name 才是 trace "标题"
            root_name = root_src.get("span_name") or "unknown"
            trace_name = (
                f"{services[0]}: {root_name}" if len(services) == 1
                else f"{','.join(services)}: {root_name}"
            )

            min_start = _int(bucket.get("min_start", {}).get("value"))
            # max end = max(@timestamp)；@timestamp 字段已是 end 的 epoch 毫秒
            max_end = _int(bucket.get("last_ts", {}).get("value"))
            duration_ms = round(max((max_end or 0) - (min_start or 0), 0.0), 3)

            by_id[bucket.get("key")] = {
                "trace_id": bucket.get("key"),
                "spans": bucket.get("doc_count", 0),
                "total_tokens": int(bucket.get("tokens", {}).get("value") or 0),
                "last_ts": int(bucket.get("last_ts", {}).get("value") or 0) or None,
                "service": ", ".join(services),
                "services": services,
                "service_spans": service_spans,
                "operations": sorted(b.get("key") for b in ops_buckets if b.get("key")),
                "duration_ms": duration_ms,
                "start_time": int(min_start) if min_start else 0,
                "trace_name": trace_name,
            }

        # 有过滤时按阶段一的「最近优先」顺序输出；无过滤时桶本身已按 last_ts 降序
        items = (
            [by_id[t] for t in ordered_ids if t in by_id]
            if ordered_ids is not None
            else list(by_id.values())
        )
        return {"items": items}

    def list_operations(
        self,
        hours: int,
        size: int = 50,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
        service: Optional[str] = None,
    ) -> Dict[str, Any]:
        start, end = _resolve_range(hours, start_ms, end_ms)
        filters: List[Dict[str, Any]] = [
            {"range": {"@timestamp": {"gte": start, "lte": end}}}
        ]
        if service:
            filters.append({"term": {"service": service}})
        # 走 `compound_op` runtime field：老数据（operation 还是粗粒度 "chat"）和
        # 新数据（operation 已是复合值 "chat xxx"）都按同一份口径聚合。
        body = {
            "size": 0,
            "runtime_mappings": _COMPOUND_OP_RUNTIME,
            "query": {"bool": {"filter": filters}},
            "aggs": {"by_operation": {"terms": {"field": "compound_op", "size": size}}},
        }
        res = self._search(body)
        buckets = res.get("aggregations", {}).get("by_operation", {}).get("buckets", [])
        return {
            "items": [
                {"operation": b.get("key"), "count": b.get("doc_count", 0)}
                for b in buckets
            ]
        }

    def list_services(
        self,
        hours: int,
        size: int = 50,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        start, end = _resolve_range(hours, start_ms, end_ms)
        body = {
            "size": 0,
            "query": {
                "bool": {
                    "filter": [{"range": {"@timestamp": {"gte": start, "lte": end}}}]
                }
            },
            "aggs": {"by_service": {"terms": {"field": "service", "size": size}}},
        }
        res = self._search(body)
        buckets = res.get("aggregations", {}).get("by_service", {}).get("buckets", [])
        return {
            "items": [
                {"service": b.get("key"), "count": b.get("doc_count", 0)}
                for b in buckets
            ]
        }

    def get_trace(self, trace_id: str, limit: int = 1000) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "size": limit,
            # 让 hits.total 返回真实总数（而非上限 10000 的截断值），用于判断是否截断
            "track_total_hits": True,
            "query": {"bool": {"filter": [{"term": {"trace_id": trace_id}}]}},
            "sort": [
                {"start_time": {"order": "asc"}},
                # 二级排序不可省：同一毫秒内开始的子 span 若只按时间排序，
                # 顺序会在多次查询间抖动，导致瀑布图行序跳变。
                {"span_id": {"order": "asc"}},
            ],
        }
        res = self._search(body)
        hits = res.get("hits", {})
        total = hits.get("total")
        if isinstance(total, dict):  # ES 7 返回 {"value": n, "relation": "eq"}
            total = total.get("value", 0)
        spans = [h.get("_source", {}) for h in hits.get("hits", [])]
        return {
            "trace_id": trace_id,
            "spans": spans,
            "meta": _trace_meta(spans, int(total or 0) > limit),
        }


def build_storage() -> Storage:
    """按配置构建存储后端。ES 不可达时给出明确日志，但不阻断进程启动。"""
    from . import config

    if config.STORAGE_BACKEND == "memory":
        logger.info("存储后端: memory（上限 %s 条，仅用于冒烟/开发）", config.MEMORY_MAX_SPANS)
        return MemoryStorage(max_spans=config.MEMORY_MAX_SPANS)

    logger.info("存储后端: es -> %s (prefix=%s)", config.ES_URL, config.ES_INDEX_PREFIX)
    storage = EsStorage(
        url=config.ES_URL,
        user=config.ES_USER,
        password=config.ES_PASSWORD,
        index_prefix=config.ES_INDEX_PREFIX,
        verify_certs=config.ES_VERIFY_CERTS,
        refresh=config.ES_REFRESH,
    )
    try:
        info = storage.es.info()
        logger.info("ES 连接成功: cluster=%s version=%s", info.get("cluster_name"), info["version"]["number"])
    except Exception as exc:
        logger.warning("ES 连接失败，统计接口将报错直到 ES 可用: %s", exc)
    return storage
