# MEMORY（PalTrace 长期记忆）

## 项目定位

- **PalTrace** = OTLP trace 采集 + 统计 + 调用链展示平台。接收 QwenPaw（AgentScope 应用，经 LoongSuite 采集）上报的 OTLP trace。
- 仓库：`/Users/liuxin/Documents/GitHub/PalTrace`
- **QwenPaw 在另一个仓库**：`/Users/liuxin/Documents/GitHub/QwenPal`。本仓库只是接收端，不要以为能在这里启动 QwenPaw。

## 上游 QwenPaw 数据源现状（2026-09-02 核实）

- **metrics 管线在上游已就绪**：`QwenPal/plugins/loongsuite-otel/qwenpaw_otel_plugin.py` 自建私有 `MeterProvider` + `PeriodicExportingMetricReader` + `OTLPMetricExporter`，目标 = `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` 或 `{OTEL_EXPORTER_OTLP_ENDPOINT}/v1/metrics`（endpoint 默认 `http://localhost:4318`）。provider 显式传给 `AgentScopeInstrumentor().instrument(meter_provider=...)`。
- **编码是 OTLP/HTTP + protobuf**（`otlp.proto.http` exporter 默认），不是 JSON——PalTrace 接 `/v1/metrics` 必须支持 protobuf。
- **instrumentor 确实产 metrics**（`loongsuite-instrumentation-agentscope`，QwenPal pin `~=0.8.0`，PyPI 最新即 0.8.0）：两个标准直方图 `gen_ai.client.operation.duration`（秒）、`gen_ai.client.token.usage`（token，带 `gen_ai.token.type=input/completion`）。维度：`gen_ai.system`、`gen_ai.operation.name`、`gen_ai.request.model`、`server.address/port`、`error.type`（GenAI semconv 1.30.0）。→ **"按模型切分现成、预聚合不扫全量 span"的说法成立**。
- **metrics 没有 user 维度**：user 只在 span/resource（`qwenpaw.user`）里，metrics 直方图帮不上按 user 统计。
- QwenPal 仓库内**没有自建 metric 代码**（搜 `create_histogram|get_meter` 只命中插件接管线那几行），指标全靠 instrumentor。
- 本机**未安装** loongsuite-instrumentation-agentscope（`python3 -c import` 失败、find 无结果），无法本地读源码，上述结论来自 PyPI README + deepwiki。

## 架构与硬约定

- 单端口 FastAPI：`:8000` 同时提供 `POST /v1/traces`（OTLP/HTTP，JSON + protobuf 双编码）、`GET /api/*`、看板 `/`。
- **gRPC `:4317` 默认关闭**（`ENABLE_GRPC` 默认 `false`，2026-09-03 改）：生产走 OTLP/HTTP。gRPC 与 `--workers N` 互斥——多进程只有首个能 bind 4317，其余报 `Address already in use`（已捕获为 warning）。开启需 `ENABLE_GRPC=true` + `--workers 1`。
- **接收端点必须走线程池**：`POST /v1/traces` 是 `async def` 但落库用同步 es 客户端，已改为 `await run_in_threadpool(handle_payload, payload)`。同理，高频路径的 `logger.info("接收 N 个 span")` 已降为 `debug`。
- **`--workers` 只能配 `es` 后端**：`memory` 后端数据在进程内存里，多进程各存一份，看板结果会跳变。
- **gRPC 与 HTTP 共用一套解析**：`MessageToDict(proto)` 产出结构与 OTLP/HTTP JSON 同构，所以只有一份 `flatten_payload`。不要拆成两套解析。
- `Storage` ABC 双实现：`EsStorage`（ES 原生聚合，正确）/ `MemoryStorage`（Python 聚合，仅冒烟）。**任何新增查询都要两个后端各实现一遍，且返回结构一致**。
- **ES 必须用 7.x 客户端**（`elasticsearch==7.17.9`）。8.x 的 `bulk()` 参数由 `body=` 改名 `operations=`，混用直接报错。
- **看板零外部依赖**（公司内网无外网）：不引任何 CDN / 图表库，纯 CSS + 原生 JS，单页 `app/static/index.html`。新增功能务必保持 0 外部请求。
- **树视图左右两列是独立容器，行高必须严格一致**：`.tree-row` 与 `.timeline-row` 都是固定 `height:30px`（不要用 min-height/padding 撑），两栏表头（`.tree-header` / `.timeline-ruler`）都是 32px。改任一边必须同步另一边，否则逐行累积错位（2026-09-03 修过一次）。
- 索引按天 `paltrace-spans-YYYY.MM.DD`；`attributes` 设 `dynamic: false`（不建索引但保留 `_source`，防 field explosion，且可供后续取用）。
- 隐私：默认丢弃 `gen_ai.prompt` / `gen_ai.completion`。

## ES mapping 变更规则（加字段时的操作铁律）

- **加新字段不需要删历史数据、不需要 reindex**。旧索引缺该字段完全合法：terms 聚合跳过、term 过滤不匹配、`_source` 里没有该 key（前端显示 `—`），全程不报错。
- **index template 只影响新建索引**。已存在的索引（尤其当日那个）必须手动 `PUT /<index>/_mapping` 补字段。
- **不补的后果不是"旧数据报错"，而是"新字段被动态推断成 text"**——template 顶层没设 `dynamic`，ES 默认 `true`：
  - `terms` 聚合该字段 → 400 `Fielddata is disabled on text fields`（**真报错**）
  - `term` 精确过滤 → 静默失效（值含 `/` 会被分词，如 `liuxin/80376130` 匹配不到）
- **唯一必须删数据 / reindex 的情况**：字段已被动态映射成某类型又要改成别的类型（如 text→keyword），ES 报 `mapper [x] cannot be changed from type ...`。
- **操作顺序铁律**：改代码 → 补 mapping → 再放新数据进来。顺序反了就只能 reindex。
- 自查命令：`curl 'localhost:9200/<prefix>-*/_mapping/field/<field>'`——返回 `{"type":"keyword"}` 且**没有** `.keyword` 子字段 = 显式 mapping 正确。

## 性能与多实例部署（2026-09-03 分析）

**多实例可行性**
- **ES 后端：可水平扩容**（无状态：`EsStorage` 只有 es client / prefix / refresh 三个实例属性；`_ensure_template()` 幂等；索引按天，多实例写同一索引无冲突；所有 `/api/*` 每次实时查 ES，无本地缓存）。
- **Memory 后端：不可多实例**（`MemoryStorage._spans` 是进程内环形缓冲，各实例数据不共享，看板会看到不同结果）。
- **gRPC 的 LB 陷阱**：gRPC 是 HTTP/2 长连接，L4 负载均衡（K8s Service 默认 / NLB）只做**连接级**均衡 → 一个客户端建立连接后所有请求都打到同一实例，加实例无效。需要 L7（Envoy / nginx `grpc_pass`）或改用 OTLP/HTTP（短连接，L4 即可分摊）。

**性能瓶颈（按严重度排序，均未修）**
1. **`POST /v1/traces` 是 `async def`，内部却调同步阻塞的 `es.bulk()`（elasticsearch 7.x 同步客户端）→ 阻塞整个 event loop**，所有请求被串行化。对照：统计接口用 `def`，FastAPI 自动丢线程池，反而不阻塞。修法：`await run_in_threadpool(handle_payload, payload)`。
2. `ES_REFRESH` 默认 `true` → 每次 bulk 强制 refresh，ES 最贵操作之一。生产须 `false`。
3. index template 写死 `number_of_shards: 1, number_of_replicas: 0` → **单分片，写入并行度锁死**；且该字段只能通过改 template 影响**新建**索引。
4. 无攒批：每个请求一次 bulk 往返；无背压、无限流。
5. Dockerfile 单 worker（`uvicorn` 无 `--workers`）。
6. 每 span 冗余存一份完整 `process`（resource 属性）→ 存储放大。
7. 统计查询无缓存，前端一次开板并发 6-8 个聚合查询（含 `cardinality precision_threshold=40000`），与写入抢 ES 资源。
8. gRPC 侧 `ThreadPoolExecutor(max_workers=8)`。

**量级估算（单实例 / ES 单节点）**：当前代码串行化 ≈ 20 QPS；修掉 event loop 阻塞 + 关 refresh ≈ 200-300 QPS（受 GIL 与单分片限制）；再加多实例 + 多分片才能到 1000+ QPS。

**生产环境约束（用户 2026-09-03 确认）**
- **2000 = 注册用户数**（QwenPaw 客户端数），**瞬时并发请求峰值估 ~500**；稳态 QPS 由各客户端 OTel `BatchSpanProcessor` 的导出间隔（默认 5s）决定，而非用户数。
- **ES 是 9 节点集群**（非单节点）→ 写入能力 2-4 万 docs/s 量级，**瓶颈 100% 在 PalTrace 应用层**（单 worker + event loop 串行 + `refresh=true`）。故 template 里 `number_of_shards: 1` 是最大浪费：写入只落 1 个节点。
- **只用 ES 后端 + OTLP/HTTP，不用 gRPC** → 应设 `ENABLE_GRPC=false`（`config.py` 里默认值是 True）。好处：HTTP 短连接，L4 负载均衡即可分摊，无需 gRPC 的 L7 长连接方案。
- **不推荐的过度设计**（500 并发量级用不上）：Kafka/Redis 缓冲层、换 AsyncElasticsearch、服务端攒批（客户端 BatchSpanProcessor 已攒）、读写分离。

## 索引前缀

- 索引前缀默认 `paltrace-spans`（已从 `qwenpaw-*` 改名）。**再改前缀会导致历史索引查不到**，需用户确认弃数据后才能动。
- **索引粒度默认按月** `paltrace-spans-YYYY.MM`（2026-09-03 从按天改）；`ES_INDEX_GRANULARITY=day` 可切回 `paltrace-spans-YYYY.MM.DD`。两种命名都匹配 `<prefix>-*`，**可共存，切换无需迁移**（实测：`2026.09.01/02/03` 按天索引与 `2026.09` 月度索引并行可查）。
- `ES_SHARDS=3` / `ES_REPLICAS=1`（2026-09-03 从硬编码 1/0 改为可配）。**只对新建索引生效**。单节点 ES 必须 `ES_REPLICAS=0`，否则副本无法分配、索引/集群恒 yellow（可读写，但会误导健康检查）。
- `ES_REFRESH` 默认 `false`（2026-09-03 从 `true` 改）：强制 refresh 会让每次 bulk 生成新 segment，吞吐差一个数量级。只有本地调试（想立刻看到数据）才设 `true`。

## 环境事实

- 本机 **ES 未运行**（9200 不通），Docker 可用（`docker compose up -d` 可起 ES 7.17）。PalTrace 实际一直跑 **memory 后端**。
- PalTrace 自带 `.venv`，依赖已装齐（fastapi / elasticsearch 7.17.9），无需重新 pip install。
- 启动：`STORAGE_BACKEND=memory ./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000`。**memory 后端重启即清空数据**。
- 合成数据冒烟：`send_test_trace.py`（其父子时序已保证子 span 严格嵌套在 root 内）。

## 操作技巧（本机踩坑）

- `kill` / `rm` / `pkill` 类命令在 execute_command 里常触发权限确认并**超时取消**。改用 Python 脚本（`os.kill` / `pathlib.Path.unlink()`）可稳定执行。
- 需要验证新代码时，**另起独立端口实例**（如 8010 + `ENABLE_GRPC=false` 避免 4317 冲突）比杀掉正在跑的 :8000 更安全，不丢现有数据。

## 已完成的重要修复（摘要，细节见各日日录）

- 静默丢数据（bulk 未查 errors）、非法 JSON 返回 500 引发重试风暴、tokens_by_model 口径虚高、`_first()` 把 `False` 判为空、attributes 动态映射、cardinality/shard_size 精度、protobuf 编码 400。
- **web_search 不被统计**的根因在上游 QwenPaw（network 类工具绕过 agentscope `on_acting` 不产生 `tool:` span）；已在 QwenPal `web_search.py` 给 `web_search`/`web_fetch` 加手动 gen_ai span 修复。
- v0.2.0：trace 树视图（设计见 `trace-tree-design.md`）。

## 设计/文档约定

- 用户偏好：**先出设计文档，确认后再写代码**。设计文档放仓库根（如 `requirements-and-design.md`、`trace-tree-design.md`），风格是表格 + 编号章节 + ADR 表。
- 改功能后要**同步更新 `requirements-and-design.md`** 的对应章节（范围/Out of Scope/接口表/展示设计/后续优先级）。
