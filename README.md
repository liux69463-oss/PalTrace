# PalTrace（v0.3.0）

接收 **LoongSuite** 采集的 PalTrace **OTLP trace**，落 **Elasticsearch 7.x**，
提供运行时统计看板与**调用链（trace 树）展开**。

```
PalTrace ──LoongSuite(OTLP)──► Trace Hub ──► ES 7.x ──► 统计看板 + 调用链树视图
                                 ├─ gRPC  :4317（可关闭，见「快速开始」）
                                 └─ HTTP  :8000/v1/traces
```

设计文档：

| 文档                                                              | 内容                                    |
| --------------------------------------------------------------- | ------------------------------------- |
| [`requirements-and-design.md`](requirements-and-design.md)       | 需求分析与总体设计（MVP）                         |
| [`trace-tree-design.md`](trace-tree-design.md)                   | 调用链树视图：span 树 + 时间瀑布 + 属性详情（v0.2.0）    |
| [`trace-query-design.md`](trace-query-design.md)                 | trace 查询增强：operation 过滤 + 时间范围预设（v0.3.0） |

---

## 一、快速开始

### 0. 准备（两种方式公共步骤）

```bash
cd /path/to/PalTrace
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
```

### 方式 A：内存存储（`memory`）

最快，用于验证链路是否打通。**重启即清空**，不适合长期使用。

```bash
STORAGE_BACKEND=memory \
ENABLE_GRPC=false \
./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### 方式 B：ES 存储（`es`）· 推荐

数据持久化，重启不丢。

```bash
# 1) 起 ES 容器 —— 只起 elasticsearch 这一个服务。
#    不要直接 docker compose up -d：compose 里的 hub 服务会占用 :8000，
#    与下面本地启动的 PalTrace 抢端口。
source ~/.zprofile          # 若报 docker: command not found，先执行这行
docker compose up -d elasticsearch

# 2) 确认 ES 就绪（约 10 秒，能返回版本号即可）
curl -s http://localhost:9200

# 3) 启动 PalTrace（es 后端）
ENABLE_GRPC=false \
STORAGE_BACKEND=es \
ES_URL=http://localhost:9200 \
ES_INDEX_PREFIX=paltrace-spans \
ES_REFRESH=true \
./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

> 公司环境请**不要**用 compose 里的 elasticsearch，改为把 `ES_URL` 指向公司既有 ES 7.x。

**关于 `ENABLE_GRPC=false`**（可选，但同机跑 Jaeger 时强烈建议）：

Jaeger v2 自带 OTel 自埋点，其 OTLP exporter **默认指向 `localhost:4317`**，与 PalTrace 的 gRPC 端口相同，
会把 Jaeger 的自描述 trace（如 `/api/v3/operations`，每条约 1 分钟一条）灌进 PalTrace 污染统计。
关掉 gRPC 即可切断。QwenPaw 走 HTTP 上报（`:8000`）时不受影响，所以关掉没有副作用。

### 验证启动结果

```bash
# 看当前后端与 gRPC 开关
curl -s localhost:8000/healthz
#   es     期望: {"status":"ok","backend":"es",    "grpc_enabled":false,...}
#   memory 期望: {"status":"ok","backend":"memory","grpc_enabled":false,...}

# 确认后端确实加载了新代码（能匹配到 trace 明细路由）
curl -s localhost:8000/openapi.json | grep trace_id
```

> 看板页面是每次请求从磁盘读取的，所以**后端进程没重启时，页面是新的、接口却是旧的**。
> 判断后端有没有加载新代码，要看 `/openapi.json` 里的路由，而不是看页面长什么样。

### 发送合成数据冒烟

```bash
./.venv/bin/python send_test_trace.py --mode http --traces 2
```

> 用 `--mode http` 而非 `both`：`both` 会同时发 gRPC 到 `:4317`，而上面关闭了 gRPC，那一半会失败。

打开 <http://localhost:8000> 看板，应立刻看到 token / 耗时 / 工具调用统计；
点「近期 Trace」表里的 Trace ID 可展开调用链（span 树 + 时间瀑布 + 属性详情）。

---

**两种存储后端对比**：

| | `es` | `memory` |
|---|---|---|
| 数据持久化 | ✅ 重启不丢 | ❌ 重启全清 |
| 需要 Docker | ✅ 要起 `paltrace-es` | ❌ 不需要 |
| 容量 | 受磁盘限制 | 环形缓冲，上限 `MEMORY_MAX_SPANS`（默认 20000），写满淘汰旧的 |
| 适用 | 日常使用、排查真实 trace | 快速验证链路是否打通 |

---

## 二、PalTrace 对接（LoongSuite 侧配置）

### 走 gRPC（推荐，LoongSuite 默认协议，端口即标准 4317）

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://<hub-host>:4317
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc
export OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental
export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT

loongsuite-instrument --traces_exporter otlp --service_name paltrace \
  python app.py
```

> **前提**：PalTrace 侧必须开着 gRPC 接收（`ENABLE_GRPC=true`，`config.py` 的默认值即是）。
> 若你直接照 [`.env.example`](.env.example) 抄的配置，那里默认写的是 `false`
> （为避开同机 Jaeger 的自描述 trace 串入），用 gRPC 上报前需改回 `true`。
> 同机跑 Jaeger 时，更推荐改用下面的 HTTP 方案。
>
> 快速判断是否已被 Jaeger 污染：`curl -s 'localhost:8000/api/traces?hours=24' | grep jaeger`
> 有结果就说明串进来了。

### 走 HTTP（protobuf）

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://<hub-host>:8000
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental
export OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT
```

> exporter 会自动在 endpoint 后追加 `/v1/traces`，因此 endpoint **不要**带路径。

> ⚠️ `OTEL_EXPORTER_OTLP_PROTOCOL` **不能写 `http`**。该 OTel SDK 只认 `http/protobuf` 与 `grpc`，
> 写 `http` 会直接报 `unsupported oltp protocol 'http' is configured`。

### 收窄自动埋点范围（可选，但强烈建议）

QwenPaw 这类应用自带大量库埋点，会产生成千上万条无 `gen_ai` 属性的 `internal` span 刷屏看板。用该变量关掉噪声源：

```bash
export OTEL_PYTHON_DISABLED_INSTRUMENTATIONS="fastapi, starlette, requests, urllib3, httpx, aiohttp-client, sqlite3, sqlalchemy, urllib, grpc_client, grpc_server, grpc_aio_client, grpc_aio_server, asyncio, threading, botocore, jinja2, tornado, system_metrics"
```

> **`fastapi` 和 `starlette` 必须带上**：实测 QwenPaw 一次短任务会产生 **2471 个**
> `opentelemetry.instrumentation.fastapi` 的 span（来自其 Web 后端的控制台轮询接口，
> 如 `POST /api/console/chat` 被调用 1424 次）。只禁用 `requests` / `httpx` 等**不会**减少噪声，
> 因为真正的刷屏源头是 fastapi。加上这两项后，span 数从 2479 降到约 8 个真实 span。

---

## 三、接口一览

| 方法   | 路径              | 说明                        |
| ---- | --------------- | ------------------------- |
| POST | `/v1/traces`    | OTLP/HTTP 接收端点            |
| GET  | `/api/overview` | 概览：span 数、trace 数、错误数、错误率 |
| GET  | `/api/tokens`   | Token 消耗（按模型聚合）           |
| GET  | `/api/latency`  | 耗时分位 p50/p95/p99（按操作类型）   |
| GET  | `/api/tools`    | 工具调用 TOP                  |
| GET  | `/api/traces`   | 近期 trace 列表（支持 operation 过滤 + 时间范围） |
| GET  | `/api/operations` | 时间窗内出现过的 operation 列表（供过滤下拉框） |
| GET  | `/api/traces/{trace_id}` | 单条 trace 的全量 span + 元信息（调用链树视图用） |
| GET  | `/healthz`      | 健康检查                      |
| GET  | `/`             | 看板页面                      |

统计接口均支持 `?hours=N`（默认 24，取自 `DEFAULT_HOURS`）与 `?size=N`。

**`/api/traces` 的查询参数**（可组合）：

| 参数 | 说明 |
|---|---|
| `operation` | 只返回**包含**该 operation span 的 trace。返回的 `spans` / `total_tokens` 仍是整条 trace 的真实统计 |
| `start` / `end` | 显式时间范围（epoch 毫秒），**必须成对**，`start < end`，跨度 ≤ 30 天。优先级高于 `hours` |

**`/api/traces/{trace_id}`** 不带时间窗——否则点开一条稍旧的 trace 会查不到、与列表口径割裂。
参数 `limit` 默认 1000、上限 5000；trace 不存在时返回 **404**。

---

## 四、配置

全部走环境变量，见 [`.env.example`](.env.example)。常用项：

| 变量                          | 默认                                | 说明                               |
| --------------------------- | --------------------------------- | -------------------------------- |
| `STORAGE_BACKEND`           | `es`                              | `es` 或 `memory`                  |
| `ES_URL`                    | `http://localhost:9200`           | ES 地址                            |
| `ES_USER` / `ES_PASSWORD`   | 空                                 | ES 账号（无认证时留空）                    |
| `ES_INDEX_PREFIX`           | `paltrace-spans`                   | 索引前缀，实际索引为 `<prefix>-YYYY.MM.DD` |
| `ES_REFRESH`                | `true`                            | 写入后立即刷新；生产高吞吐建议 `false`          |
| `HTTP_PORT`                 | `8000`                            | 看板 + API + OTLP/HTTP             |
| `GRPC_PORT` / `ENABLE_GRPC` | `4317` / `true`                   | OTLP/gRPC                        |
| `DROP_ATTRIBUTES`           | `gen_ai.prompt,gen_ai.completion` | 不入库的属性（隐私）                       |
| `MEMORY_MAX_SPANS`          | `20000`                           | memory 后端环形缓冲上限                  |

---

## 五、目录结构

```
paltrace/
├── requirements-and-design.md   # 需求分析与总体设计（MVP）
├── trace-tree-design.md         # 调用链树视图设计（v0.2.0）
├── trace-query-design.md        # trace 查询增强设计（v0.3.0）
├── app/
│   ├── config.py                # 环境变量配置（ES 相关项见「配置」一节）
│   ├── otlp.py                  # OTLP 解析 + gen_ai 提取（HTTP/gRPC 共用）
│   ├── storage.py               # EsStorage / MemoryStorage：统计聚合 + trace 明细
│   ├── grpc_server.py           # OTLP/gRPC 接收端（ENABLE_GRPC=false 时不启动）
│   ├── main.py                  # FastAPI：接收端点 + 统计 API + 静态看板
│   └── static/index.html        # 看板（零外部依赖：统计 + 过滤器 + 调用链树）
├── send_test_trace.py           # 合成 OTLP 数据，用于冒烟验证
├── docker-compose.yml           # elasticsearch + hub（本地用，通常只起 elasticsearch）
├── Dockerfile
└── .env.example
```

---

## 六、设计要点（为什么这么做）

- **双协议接收**：LoongSuite 默认发 gRPC 4317，只做 HTTP 会逼用户改采集端配置。  
  gRPC 侧用 `MessageToDict()` 转成与 HTTP JSON **同构**的 dict，因此两条路径复用同一套解析逻辑。
- **看板零 CDN**：公司内网无外网，引 Chart.js 必然白屏，故图表用纯 CSS 实现。
- **gen_ai 双版本兜底**：`experimental`（`gen_ai.usage.input_tokens`）与 `stable`（`gen_ai.usage.prompt_tokens`）命名不同，取数时按优先级回退。
- **ES 7.x 必须用 7.x 客户端**：8.x 客户端面向 ES 8 API，且 `bulk()` 参数由 `body=` 改名为 `operations=`，混用直接报错。
- **隐私默认收敛**：`gen_ai.prompt` / `gen_ai.completion` 可能含用户输入，默认不入库。
- **时间戳严格按纳秒**：OTLP 规范要求纳秒。若发送端误用毫秒/微秒，服务会打印 WARNING 告警（数据仍入库，但会落在统计窗口之外不可见）。

---

## 七、已知限制（后续优化）

- 无火焰图聚合视图、无 span 属性检索（trace 树与 span 详情已实现）
- 写入未攒批，每条 OTLP 请求一次 bulk
- 无鉴权、无租户隔离
- 无采样与背压
- 按天索引，无 ILM 清理
- 仅接入 traces，未接 metrics / logs
- 超大 trace（数千 span）前端未做虚拟滚动，靠 `limit` 上限 + 折叠兜底

---

## 八、排错

| 现象            | 原因 / 处理                                                        |
| ------------- | -------------------------------------------------------------- |
| 看板有数据但全为 0 或空 | 检查时间戳单位。看服务日志是否有 `时间戳 … 量级异常` 告警                               |
| 统计接口返回 503    | ES 不可达，确认 `ES_URL` 与网络；可临时切 `STORAGE_BACKEND=memory` 验证其余链路    |
| gRPC 端口未监听    | 看日志是否有 `gRPC 接收端启动失败`；HTTP 路径不受影响                              |
| ES 中字段无法聚合    | index template 未生效时会被动态映射成 text，确认 `paltrace-spans` template 存在 |
| 看板出现 `service=jaeger` 的噪声 trace | 同机 Jaeger 的自描述 trace 串入。Jaeger v2 的 OTLP exporter 默认指向 `localhost:4317`，与 PalTrace gRPC 端口相同。用 `ENABLE_GRPC=false` 重启；已入库的用 ES `delete_by_query` 按 `service=jaeger` 清除 |
| 页面是新样式，接口却 404 | 后端进程没重启。`index.html` 每次请求从磁盘读（所以页面是新的），但路由只在启动时注册。用 `curl -s localhost:8000/openapi.json \| grep trace_id` 确认 |
| trace 详情提示「不存在」 | 先看 `/api/traces` 列表里能否查到该 trace。若列表有、详情 404，通常是后端旧进程没加载新路由（同上一条）；若两边都没有，才是数据真的不在窗口内 |
