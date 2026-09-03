# PalTrace 运行时数据采集与统计平台（Trace Hub）
## 需求分析与设计文档（MVP）

| 项 | 内容 |
|---|---|
| 版本 | v0.1.0（MVP） + v0.2.0（trace 树视图） |
| 定位 | 接收 LoongSuite 采集的 PalTrace OTLP trace，落 ES 7.x，提供运行时统计看板与调用链展开 |
| 技术栈 | Python 3.11 + FastAPI + elasticsearch-py 7.17 + ES 7.x，看板零外部依赖 |
| 范围 | 「采集 → 存储 → 统计 → 展示」闭环 + trace 树视图（v0.2.0 补齐，设计见 `trace-tree-design.md`） |

---

## 1. 背景与目标

### 1.1 背景
PalTrace（基于 AgentScope 的个人助手）通过 **LoongSuite** 做无侵入采集，产出标准 **OTLP trace**，span 上携带 `gen_ai.*` 语义属性（模型、token、工具调用）。

公司已有 **ES 7.x**。此前评估过三条路：

| 方案 | 结论 |
|---|---|
| Jaeger v2.20 源码构建 | 可行但重：需 `make create-baseimg`→`make build-jaeger`→`make docker`；实测 `make build-jaeger` 因 `rebuild-ui.sh` 强行走 `git fetch` 内网 gitee（无凭据）而失败 |
| SkyWalking 10.4 | 本地 Docker 能起，但 OTLP 数据落不到 segment（OTLP handler 依赖默认关闭的 receiver-zipkin），排查成本高 |
| A2E | 定位是「框架内驱动型评测引擎」（自己构造 langgraph agent + 注入工具），而 PalTrace 是应用级黑盒，契约不匹配 |

### 1.2 目标（MVP）
用最小成本拿到**可运行的统计能力**：PalTrace 运行时被采集 → 数据入库 → 看板展示 token 消耗、耗时分位、工具调用、错误率。

**不是目标**：复刻 Jaeger 的服务拓扑、告警、多租户。这些明确推迟。

> v0.2.0 更新：trace 树视图（调用链展开 + span 详情）已实现，设计见 `trace-tree-design.md`；
> 服务拓扑、火焰图聚合仍在推迟范围。

---

## 2. 需求

### 2.1 功能性需求

| ID | 需求 | 优先级 | 说明 |
|---|---|---|---|
| FR-1 | 接收 OTLP trace | P0 | 支持 **gRPC :4317**（LoongSuite 默认）与 **HTTP/JSON**（复用 FastAPI 端口，默认 `:8000/v1/traces`）两种协议，避免强制改采集端配置 |
| FR-2 | 解析并归一化 span | P0 | 提取 trace/span/parent、service、name、kind、起止时间、status |
| FR-3 | 提取 `gen_ai.*` 指标 | P0 | operation、model、input/output tokens、tool name |
| FR-4 | 持久化到 ES 7.x | P0 | **按月**索引（`ES_INDEX_GRANULARITY` 可切回按天），带 index template 固定 mapping |
| FR-5 | 统计接口 | P0 | 概览、token 消耗（按模型）、耗时分位、工具调用 TOP、错误率、近期 trace 列表 |
| FR-6 | 看板展示 | P0 | 单页看板，自动刷新，可读即可 |

### 2.2 非功能性需求

| ID | 需求 | 设计对策 |
|---|---|---|
| NFR-1 | **内网可用**（公司网络无外网） | 看板禁用任何 CDN，纯 CSS 图表 + 原生 JS；运行时零外部请求 |
| NFR-2 | 隐私/合规 | 默认**丢弃** `gen_ai.prompt` / `gen_ai.completion`（可能含用户输入），不入库 |
| NFR-3 | 语义约定版本兼容 | `gen_ai` 有 stable 与 experimental 两套命名，取数需双版本兜底 |
| NFR-4 | 易部署/易验证 | 提供 `memory` 后端，无 ES 也能一键冒烟；`docker-compose.yml` 含本地 ES 7.17 |
| NFR-5 | 配置外置 | ES 地址/账号/索引前缀/索引粒度/分片数全部走环境变量 |
| NFR-6 | 接收端并发 | 同步落库经 `run_in_threadpool` 丢线程池（避免阻塞事件循环）；ES 后端无状态，支持 uvicorn 多 worker 与多实例水平扩容 |

### 2.3 明确不做（Out of Scope，后续优化）

- 火焰图聚合视图、服务拓扑、依赖图
  （trace 树 + span 详情钻取已于 **v0.2.0 实现**，见 `trace-tree-design.md`）
- 采样、背压、限流
- 鉴权与多租户
- metrics / logs 接入
- ILM rollover（先用按月索引 + 按索引删除清理）
- 告警

---

## 3. 设计

### 3.1 总体架构

```
PalTrace ──LoongSuite(OTLP)──► Trace Hub (FastAPI)
                                 ├─ gRPC :4317  (TraceService/Export)
                                 ├─ HTTP :8000  POST /v1/traces
                                 │
                                 ├─ otlp.py     解析 + gen_ai 提取 + 隐私字段丢弃
                                 ├─ storage.py  EsStorage / MemoryStorage
                                 └─ main.py     /api/* 统计接口 + 静态看板 /
                                        │
                                        ▼
                                    ES 7.x  paltrace-spans-YYYY.MM（可切回 -YYYY.MM.DD）
```

**关键设计：gRPC 与 HTTP 共用一套解析逻辑。**
`MessageToDict(proto_request)`（默认 camelCase）产出的结构与 OTLP/HTTP JSON body **完全一致**，因此两条协议可复用同一个 `flatten()` 函数，避免两套解析代码分叉。

### 3.2 数据模型（span → ES doc）

| ES 字段 | 类型 | 来源 |
|---|---|---|
| `trace_id` / `span_id` / `parent_span_id` | keyword | span 原始字段（十六进制字符串） |
| `service` | keyword | resource 属性 `service.name` |
| `span_name` | keyword | span.name |
| `kind` | keyword | span.kind（数值转名称：INTERNAL/SERVER/CLIENT/...） |
| `operation` | keyword | `gen_ai.operation.name`，缺失时推断 |
| `model` | keyword | `gen_ai.response.model` ‖ `gen_ai.request.model` |
| `input_tokens` / `output_tokens` / `total_tokens` | long | `gen_ai.usage.*` |
| `tool_name` | keyword | `gen_ai.tool.call.name` ‖ `gen_ai.tool.name` |
| `duration_ms` | double | `(end - start) / 1e6` |
| `status` | keyword | span.status.code → OK / ERROR / UNSET |
| `@timestamp` | date | end_time（毫秒） |
| `attributes` | object | 除隐私字段外的全部原始属性，供后续扩展 |

### 3.3 `gen_ai` 字段映射与版本兼容

LoongSuite 启用了 `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`，但为稳妥做双版本兜底：

| 语义 | experimental（优先） | stable（兜底） |
|---|---|---|
| 操作类型 | `gen_ai.operation.name` | `gen_ai.request.type` |
| 模型 | `gen_ai.response.model` | `gen_ai.request.model` |
| 输入 token | `gen_ai.usage.input_tokens` | `gen_ai.usage.prompt_tokens` |
| 输出 token | `gen_ai.usage.output_tokens` | `gen_ai.usage.completion_tokens` |
| 工具名 | `gen_ai.tool.call.name` | `gen_ai.tool.name` |

**细节**：OTLP/HTTP JSON 中 int64 按 proto3 JSON 规范编码为**字符串**，故所有数值字段统一用 `int()`/`float()` 转换，兼容 `"120"` 与 `120` 两种形态。

若 `operation` 缺失，按启发式推断：有 token 字段 → `chat`；有 tool_name → `tool`；否则 `internal`。

### 3.4 存储设计

- **索引**：按月 `paltrace-spans-YYYY.MM`（前缀可配；`ES_INDEX_GRANULARITY=day` 可切回 `-YYYY.MM.DD`）。
  两种命名都匹配 `<prefix>-*`，可共存——切换后历史索引照常可查，**无需迁移或 reindex**
- **Index Template**：`PUT _index_template/paltrace-spans`，固定 keyword/long/date 类型，避免动态映射把 `model` 映射成 text 导致无法聚合。
  分片/副本数可配（`ES_SHARDS` / `ES_REPLICAS`），但**只对新建索引生效**
- **写入**：`bulk`，每个 OTLP 请求一个 batch。默认**不**强制 refresh（`ES_REFRESH=false`），
  交由 ES 的 `refresh_interval` 兜底——强制 refresh 会让每次 bulk 都生成新 segment，吞吐差一个数量级
- **客户端**：`elasticsearch==7.17.9`。**必须用 7.x 客户端**——8.x 客户端面向 ES 8 API，且 `bulk()` 参数由 `body=` 改名为 `operations=`，混用会直接报错
- **并发**：客户端是**同步**的，而接收端点是 `async def`，故落库必须经 `run_in_threadpool` 丢进线程池
  ——否则同步 IO 会阻塞事件循环，把所有请求（含统计 API）串行化
- **Memory 后端**：环形缓冲区（默认上限 20000 条），用于无 ES 时的冒烟测试与本地开发。
  数据在进程内存里，**不能多 worker / 多实例**（各进程各存一份，看板每次命中的进程不同、结果会跳变）

### 3.5 统计口径

| 指标 | 口径 |
|---|---|
| 概览 | span 总数、trace 数（cardinality）、错误 span 数、错误率、时间范围 |
| Token 消耗 | 按 `model` 分组，sum(input)、sum(output)、合计、调用次数 |
| 耗时分位 | 按 `operation` 分组，percentiles(duration_ms, 50/95/99) |
| 工具调用 | 过滤 `tool_name` 存在，按工具名 terms 计数，降序 TOP N |
| 错误率 | `status=ERROR` 的 span 数 / span 总数 |
| 近期 trace | 按 `trace_id` 分组，以桶内最新时间降序，返回 TOP 20 |

**时间窗口**：默认最近 24 小时，接口支持 `?hours=N`。

**统计执行位置**：
- ES 后端 → 原生 ES 聚合（正确、可伸缩）
- Memory 后端 → Python 侧聚合（仅用于冒烟）

两者通过统一 `Storage` 接口暴露同名方法，上层无感切换。

### 3.6 接口设计

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/traces` | OTLP/HTTP 接收端点 |
| GET | `/api/overview` | 概览 |
| GET | `/api/tokens` | token 消耗（按模型） |
| GET | `/api/latency` | 耗时分位（按操作） |
| GET | `/api/tools` | 工具调用 TOP |
| GET | `/api/traces` | 近期 trace 列表，支持 `operation` / `service` / `user` 过滤 + `start`/`end` 时间范围 + 任意组合（设计见 `trace-query-design.md`） |
| GET | `/api/operations` | 时间窗内出现过的 operation 列表（供过滤下拉框，支持 `service` 级联） |
| GET | `/api/users` | 时间窗内出现过的 user 列表及 span 数（供 User 过滤下拉框） |
| GET | `/api/traces/{trace_id}` | 单条 trace 全量 span + 元信息（树视图用；**不带时间窗**，见 `trace-tree-design.md` §4.1） |
| GET | `/` | 看板页面 |
| GET | `/healthz` | 健康检查 |

### 3.7 展示设计

单页 `static/index.html`：
- **零外部依赖**：不引 Chart.js / 任何 CDN，用 CSS 宽度百分比画条形图 + 表格
- 自动刷新（默认 10s，可暂停）
- 分区：概览卡片 / Token 消耗 / 耗时分位 / 工具调用 / 近期 trace
- **trace 树视图**（v0.2.0）：hash 路由 `#/trace/<id>`，span 树 + 时间瀑布 + span 属性详情，
  不引任何 CDN，纯 CSS + 原生 JS（设计见 `trace-tree-design.md`）

> 选 CSS 图表而非 Chart.js 的核心理由就是 NFR-1：公司内网无外网，CDN 必然加载失败，看板会白屏。

### 3.8 部署形态

- **本地/测试**：`docker-compose up`（receiver + ES 7.17 单节点）
- **公司 K8s**：receiver 为无状态服务，多副本 + Service；ES 指向公司既有 7.x；镜像走行内流水线构建

---

## 4. 关键技术决策与权衡（ADR 摘要）

| # | 决策 | 备选 | 理由 |
|---|---|---|---|
| ADR-1 | 选 Python 而非 Java | Spring Boot | MVP 求快；Python 侧 OTLP 解析约 50 行，Java 需 proto 生成 + 较重脚手架 |
| ADR-2 | 双协议接收（gRPC + HTTP） | 仅 HTTP/JSON | LoongSuite 默认 gRPC 4317，仅支持 HTTP 会逼用户改采集端配置 |
| ADR-3 | ES 7.x 专用客户端 | 8.x 客户端 | API 与参数名不兼容（`body=` vs `operations=`），必须匹配服务端主版本 |
| ADR-4 | ES 后端用 ES 聚合 | 全走 Python 聚合 | Python 侧聚合需限制扫描量，数据量大时口径不准；ES 聚合原生正确 |
| ADR-5 | 看板零 CDN | Chart.js | 内网无外网，CDN 必失败 |
| ADR-6 | 默认丢弃 prompt/completion | 全量入库 | 可能含用户隐私输入；且已设 `NO_CONTENT` 时本就无此字段 |
| ADR-7 | 默认走 ES 后端，`memory` 为可选项 | 只支持 ES | 保证无 ES 环境也能一键验证，降低上手成本 |

---

## 5. 风险与后续优化路径

| 风险 | 影响 | 缓解 / 后续动作 |
|---|---|---|
| LoongSuite 实际发出的 `gen_ai` 版本与假设不符 | 字段取不到，统计为空 | 已做双版本兜底；上线前用真实 trace 核对一次属性清单 |
| ES 聚合基数爆炸（高基数字段做 terms） | 内存/性能问题 | 已限制 `size`；后续对高基数字段改用 composite agg |
| 单条请求 span 数过多 | 写入抖动 | 后续加批量缓冲（攒批/定时 flush） |
| 无鉴权，端点裸奔 | 内网可用但可被任意写入 | 后续加 token 校验或网络策略限制来源 |
| 按月索引无清理 | 磁盘增长 | 清理动作本身很简单：`DELETE paltrace-spans-YYYY.MM`；量大到需要自动化时再上 ILM |

**后续优化优先级建议**：
1. 火焰图聚合视图 / span 属性检索（~~trace 树视图~~ 已于 v0.2.0 完成）
2. 写入攒批 + 背压
3. ES 聚合升级为 composite（高基数）
4. 鉴权与租户隔离
5. ILM rollover

---

## 6. 验证方案

1. **冒烟（无 ES）**：`STORAGE_BACKEND=memory` 启动 → 发送合成 OTLP 数据 → 校验统计接口与看板
2. **端到端（ES）**：`docker-compose up` 起 ES 7.17 → `STORAGE_BACKEND=es` → 同样发送 →
   校验 ES 中确实生成 **`paltrace-spans-YYYY.MM`** 索引且统计一致
3. **真机对接**：采集端设 `OTEL_EXPORTER_OTLP_ENDPOINT=http://<hub-host>:8000` +
   `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`（gRPC 默认已关闭），启动后看板应有数据
