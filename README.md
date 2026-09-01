# PalTrace（MVP v0.1.0）

接收 **LoongSuite** 采集的 PalTrace **OTLP trace**，落 **Elasticsearch 7.x**，提供运行时统计看板。

```
PalTrace ──LoongSuite(OTLP)──► Trace Hub ──► ES 7.x ──► 统计看板
                                 ├─ gRPC  :4317
                                 └─ HTTP  :8000/v1/traces
```

完整的需求分析与设计见 [`requirements-and-design.md`](requirements-and-design.md)。

---

## 一、快速开始

### 方式 A：无 ES 冒烟（最快，30 秒验证链路）

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt

STORAGE_BACKEND=memory ./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

另开终端发送合成数据：

```bash
./.venv/bin/python send_test_trace.py --mode both --traces 2
```

打开 <http://localhost:8000> 看板，应立刻看到 token / 耗时 / 工具调用统计。

### 方式 B：完整端到端（本地起 ES 7.17）

```bash
docker compose up -d
./.venv/bin/python send_test_trace.py --mode both --traces 2
# 看板 http://localhost:8000
```

> 公司环境请**不要**用 compose 里的 elasticsearch，改为把 `ES_URL` 指向公司既有 ES 7.x。

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

### 走 HTTP/JSON

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://<hub-host>:8000
export OTEL_EXPORTER_OTLP_PROTOCOL=http
```

> exporter 会自动在 endpoint 后追加 `/v1/traces`，因此 endpoint **不要**带路径。
```bash
export OTEL_PYTHON_DISABLED_INSTRUMENTATIONS="requests, urllib3, httpx, aiohttp-client, sqlite3, sqlalchemy, urllib, grpc_client, grpc_server, grpc_aio_client, grpc_aio_server, asyncio, threading, botocore, jinja2, tornado, system_metrics"

---

## 三、接口一览

| 方法   | 路径              | 说明                        |
| ---- | --------------- | ------------------------- |
| POST | `/v1/traces`    | OTLP/HTTP 接收端点            |
| GET  | `/api/overview` | 概览：span 数、trace 数、错误数、错误率 |
| GET  | `/api/tokens`   | Token 消耗（按模型聚合）           |
| GET  | `/api/latency`  | 耗时分位 p50/p95/p99（按操作类型）   |
| GET  | `/api/tools`    | 工具调用 TOP                  |
| GET  | `/api/traces`   | 近期 trace 列表               |
| GET  | `/healthz`      | 健康检查                      |
| GET  | `/`             | 看板页面                      |

统计接口均支持 `?hours=N`（默认 24，取自 `DEFAULT_HOURS`）与 `?size=N`。

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
├── requirements-and-design.md   # 需求分析与设计文档
├── app/
│   ├── config.py                # 环境变量配置
│   ├── otlp.py                  # OTLP 解析 + gen_ai 提取（HTTP/gRPC 共用）
│   ├── storage.py               # EsStorage / MemoryStorage + 统计聚合
│   ├── grpc_server.py           # OTLP/gRPC 接收端
│   ├── main.py                  # FastAPI：接收端点 + 统计 API + 静态看板
│   └── static/index.html        # 看板（零外部依赖）
├── send_test_trace.py           # 合成 OTLP 数据，用于冒烟验证
├── docker-compose.yml           # hub + ES 7.17（本地验证用）
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

- 无 trace 树 / 火焰图视图（当前只有聚合统计与 trace 列表）
- 写入未攒批，每条 OTLP 请求一次 bulk
- 无鉴权、无租户隔离
- 无采样与背压
- 按天索引，无 ILM 清理
- 仅接入 traces，未接 metrics / logs

---

## 八、排错

| 现象            | 原因 / 处理                                                        |
| ------------- | -------------------------------------------------------------- |
| 看板有数据但全为 0 或空 | 检查时间戳单位。看服务日志是否有 `时间戳 … 量级异常` 告警                               |
| 统计接口返回 503    | ES 不可达，确认 `ES_URL` 与网络；可临时切 `STORAGE_BACKEND=memory` 验证其余链路    |
| gRPC 端口未监听    | 看日志是否有 `gRPC 接收端启动失败`；HTTP 路径不受影响                              |
| ES 中字段无法聚合    | index template 未生效时会被动态映射成 text，确认 `paltrace-spans` template 存在 |
