# PalTrace Trace 树视图（调用链展开）设计文档

| 项 | 内容 |
|---|---|
| 版本 | v0.2.0 |
| 定位 | 在 MVP 统计看板之上，补齐「单条 trace 的调用链展开」 |
| 前置 | MVP v0.1.0（见 `requirements-and-design.md`） |
| 范围 | 后端按 `trace_id` 取全量 span + 前端树形瀑布图 + span 属性详情 |
| 技术栈 | 沿用现有技术栈，**不新增任何依赖**（详见 ADR-10） |

---

## 1. 背景与目标

### 1.1 现状与缺口

MVP 已交付「采集 → 存储 → 统计 → 展示」闭环，但看板**只有聚合统计和 trace 列表**：

- 能看到：token 总量、耗时分位、工具调用次数、错误率、近期有哪些 trace。
- **看不到**：一次请求内部发生了什么——agent 调了几次 LLM、中间插了哪些工具调用、谁是谁的子调用、哪一步最慢、哪一步报错。

排障时只能看到数字，看不到链路，这是 MVP 明确记录的最大缺口：

> `requirements-and-design.md` §2.3：`trace 树 / 火焰图 / span 详情钻取（Jaeger 式视图）` → Out of Scope
> `requirements-and-design.md` §5：后续优化优先级建议 **1. trace 树视图（补最大缺口）**

本文档把该缺口从 Out of Scope 转为**本期实现**。

### 1.2 目标

点击列表里任意一条 trace → 展开成 Jaeger 式的 span 树，能够回答：

1. **结构**：agent → LLM chat → 工具调用之间的父子嵌套关系。
2. **时序**：每一步相对整条 trace 的开始时间与耗时，同层可横向对比。
3. **定位**：哪一步自身耗时长（self time）、哪一步报错。
4. **细节**：任意 span 上携带的 `gen_ai.*` 原始属性。

### 1.3 本期不做

- 火焰图聚合视图、服务拓扑 / 依赖图
- 多条 trace 对比、span 检索（按属性任意查询）
- 采样、写入侧改造、鉴权与多租户

---

## 2. 可行性结论（关键）

> **现有数据模型已完全支撑树视图，无需改写入链路，无需改 ES mapping。**

`otlp.py::flatten_span` 落库的字段中，树视图所需的全部信息已齐全：

| 树视图需要 | 现有字段 | 说明 |
|---|---|---|
| 父子关系 | `span_id` / `parent_span_id` | keyword，精确匹配 |
| 定位起点 | `start_time` | epoch 毫秒（`start_ns // 1e6`） |
| 条宽 | `duration_ms` | 与 `start_time` **同为毫秒**，单位一致，可直接运算 |
| 错误标记 | `status` | `OK` / `ERROR` / `UNSET` |
| 分组配色 | `service` | resource 的 `service.name` |
| 行文案 | `span_name` / `operation` / `model` / `tool_name` | 已归一化 |
| 详情面板 | `attributes` | 全部原始属性 |

**`attributes` 的可用性**：index template 中 `attributes` 设为 `{"type": "object", "dynamic": False}`——不建索引，但**完整保留在 `_source`** 中，可原样取出做详情展示。这正是 MVP 预留的"供后续扩展"能力，本期直接兑现。

---

## 3. 需求

### 3.1 功能性需求

| ID | 需求 | 优先级 | 说明 |
|---|---|---|---|
| FR-1 | 按 `trace_id` 拉取该 trace 的全部 span | P0 | **不带时间窗**，理由见 §4.1 |
| FR-2 | 返回扁平 span 列表 + trace 级元信息 | P0 | span 数、服务集合、起止时间、总 token、错误数 |
| FR-3 | 前端按 `parent_span_id` 构建树 | P0 | 含孤儿节点处理 |
| FR-4 | 瀑布图：相对开始时间定位、时长定宽 | P0 | 同层可横向对比 |
| FR-5 | 展开 / 折叠子树 | P0 | 默认全部展开 |
| FR-6 | span 详情：基本字段 + `attributes` 全量表 | P0 | 点开即渲染 |
| FR-7 | 错误 span 高亮 + trace 级错误数提示 | P0 | 沿用现有红色语义 |
| FR-8 | 从列表进入、可返回；URL 可分享 | P1 | hash 路由 `#/trace/<id>` |
| FR-9 | 超大 trace 保护：span 数上限 + 截断提示 | P0 | 防浏览器卡死 |
| FR-10 | 自身耗时（self time） | P1 | 定位"耗在自己身上还是等子调用" |

### 3.2 非功能性需求

| ID | 需求 | 设计对策 |
|---|---|---|
| NFR-1 | **零外部依赖**（延续 ADR-5） | 不引任何 CDN / 图表库，纯 CSS + 原生 JS；树与瀑布全部手写 |
| NFR-2 | 两个存储后端行为一致 | `Storage` ABC 新增同名方法，ES / Memory 双实现，返回结构一致 |
| NFR-3 | 大 trace 不拖垮页面 | 后端 `limit` 兜底 + 前端可折叠；后续可加虚拟滚动 |
| NFR-4 | 列表与详情口径一致 | 明细查询不使用统计窗口（见 §4.1） |

---

## 4. 后端设计

### 4.1 设计要点：明细查询不带时间窗

其余统计接口统一走 `?hours=N` 时间窗，**但 `get_trace` 是唯一例外**。

| | 时间窗查询 | 按 `trace_id` 查询（本方案） |
|---|---|---|
| 行为 | 受 `hours` 限制 | 不限时间，查全量 |
| 风险 | 列表里点一条稍旧的 trace → 详情查不到，体验割裂 | 无 |

跨天问题由 `paltrace-spans-*` 通配索引天然覆盖（trace 跨零点落在不同天索引也能查全）。

代价：无法利用时间范围裁剪分片。但 `trace_id` 是 keyword 精确匹配，单 trace 命中量小（百级），可接受。

### 4.2 存储层新增方法

`Storage` ABC 增加：

```python
@abstractmethod
def get_trace(self, trace_id: str, limit: int = 1000) -> Dict[str, Any]:
    """返回该 trace 的全量 span（按时间升序）+ trace 级元信息。"""
```

**后端只负责「取全量 + 排序 + 算元信息」，不建树**——树构建交给前端（见 ADR-8）。

**统一返回结构**：

```json
{
  "trace_id": "3f2a…",
  "spans": [
    {
      "trace_id": "3f2a…", "span_id": "a1…", "parent_span_id": "",
      "service": "paltrace", "span_name": "PalTraceAgent.reply",
      "kind": "INTERNAL", "operation": "agent", "model": "qwen-max",
      "tool_name": null, "input_tokens": 0, "output_tokens": 0,
      "total_tokens": 0, "duration_ms": 3600.0, "status": "OK",
      "start_time": 1690000000000, "@timestamp": 1690000003600,
      "attributes": { "gen_ai.operation.name": "agent" }
    }
  ],
  "meta": {
    "span_count": 5,
    "truncated": false,
    "services": ["paltrace"],
    "start": 1690000000000,
    "end": 1690000003600,
    "duration_ms": 3600.0,
    "total_tokens": 2140,
    "errors": 1
  }
}
```

### 4.3 `EsStorage.get_trace`

```python
body = {
    "size": limit,
    "track_total_hits": True,          # 让 hits.total 真实，用于判断截断
    "query": {"bool": {"filter": [{"term": {"trace_id": trace_id}}]}},
    "sort": [
        {"start_time": {"order": "asc"}},
        {"span_id":    {"order": "asc"}},   # 二级排序：见下方说明
    ],
}
```

- **不带 `range` 过滤**（§4.1）。`trace_id` 是 keyword，用 `term` 精确匹配。
- **二级排序 `span_id` 不可省**：同一毫秒内开始的子 span（如并行的工具调用）若只按 `start_time` 排序，顺序在多次查询间会抖动，瀑布图行序会跳变。加 `span_id` 保证稳定序。
- **`limit` 上限 5000**：ES `index.max_result_window` 默认 10000，留足余量避免深分页报错。
- **`truncated`**：`hits.total.value > limit` 时置 `True`，前端据此提示"仅展示前 N 条"。

### 4.4 `MemoryStorage.get_trace`

线性扫描环形缓冲区，筛 `trace_id` 相等 → 按 `(start_time, span_id)` 排序 → 取前 `limit` → 用与 ES 一致的口径算元信息。

> ⚠️ 已知局限（memory 后端固有，不影响 ES）：`deque(maxlen=20000)` 会淘汰旧 span，可能出现"列表里有、点开缺 span"。memory 后端定位是冒烟/开发，文档标注即可。

### 4.5 元信息计算口径

| 字段 | 口径 |
|---|---|
| `span_count` | 返回的 span 条数（截断后） |
| `truncated` | 实际总数 > `limit` |
| `services` | `service` 去重后排序 |
| `start` | `min(start_time)`（跳过 0/空） |
| `end` | `max(start_time + duration_ms)` |
| `duration_ms` | `end - start` |
| `total_tokens` | `sum(total_tokens)` |
| `errors` | `count(status == "ERROR")` |

### 4.6 接口

| 方法 | 路径 | 参数 | 说明 |
|---|---|---|---|
| GET | `/api/traces/{trace_id}` | `limit`（可选，1..5000，默认 1000） | 单条 trace 全量 span + 元信息 |

- 与已有 `/api/traces` **不冲突**（后者无路径参数）。
- 路由顺序：明细路由定义在列表路由之后仍可正常工作（FastAPI 按精确路径匹配，不会把 `{trace_id}` 误匹配到列表）。
- **404 语义**：查不到任何 span → `404 {"error": "trace not found"}`。前端据此区分"没有"与"失败"，避免把"不存在"显示成故障。
- 其他异常沿用现有 `_safe()` → 503。

---

## 5. 前端设计

### 5.1 入口与路由

沿用单页 `static/index.html`（NFR-1，无构建步骤）。

- 「近期 Trace」表的 Trace ID 列改为 `<a href="#/trace/<id>">`。
- `hashchange` 路由：`#/trace/<trace_id>` → 详情视图；空 hash → 回统计视图。
- 收益：URL 可复制分享，刷新不丢上下文（FR-8）。

### 5.2 树构建算法（前端）

```js
const ids = new Set(spans.map(s => s.span_id));
const byParent = groupBy(spans, s => s.parent_span_id || "");
const roots = spans.filter(s => !s.parent_span_id || !ids.has(s.parent_span_id));
function childrenOf(id){ return (byParent.get(id) || []).sort(byStartThenId); }
```

三个必须处理的健壮性问题：

| 问题 | 后果 | 对策 |
|---|---|---|
| **孤儿 span**（父节点不在结果集：被截断 / 跨天丢失） | 整棵子树静默消失，看起来"少了一半链路" | 升级为根，并在行尾标记「父节点缺失」 |
| **脏数据成环** | 递归死循环，页面卡死 | `visited` 集合 + `MAX_DEPTH` 兜底 |
| **同毫秒开始的子节点** | 行序抖动 | 按 `(start_time, span_id)` 稳定排序 |

### 5.3 瀑布图布局

时间基准：`t0 = meta.start`，`total = meta.duration_ms || 1`

```js
leftPct  = (span.start_time - t0) / total * 100
widthPct = Math.max(span.duration_ms / total * 100, 0.3)   // 极短 span 给最小可见宽度
```

行结构（左右两栏）：

| 栏 | 宽度 | 内容 |
|---|---|---|
| 左 | 40% | 缩进（每层 14px）+ 折叠三角 + service 色块徽标 + `span_name` + `operation` 标签 |
| 右 | 60% | 相对轨迹条：整行是时间轴，条块按 `leftPct/widthPct` 定位 |

- **配色**：按 `service` 做稳定哈希取色（同服务同色，一眼分清来源），沿用现有 CSS 变量色板。
- **错误**：`status == "ERROR"` → 强制红色 + 左侧红色边框。
- **self time**（自身耗时）= `duration_ms - sum(直接子节点 duration_ms)`，`Math.max(0, …)` 后显示在时长列。用于区分"耗在自己身上"还是"等子调用"。

### 5.4 交互

| 交互 | 行为 |
|---|---|
| 点折叠三角 | 折叠 / 展开该子树（默认全展开） |
| 点行 | 展开详情区：基本字段表 + `attributes` 全量表 |
| 悬停 | `title` 显示完整 `span_name` / 时长 |
| 返回按钮 / 清空 hash | 回统计视图 |
| 复制按钮 | 复制 `trace_id` / `span_id` |

- **XSS**：所有插值（含 `attributes` 的 key/value）一律走已有 `esc()`。
- `attributes` 中长文本（如 `gen_ai.tool.call.arguments`）允许换行展示。

### 5.5 加载与异常态

| 状态 | 展示 |
|---|---|
| 加载中 | 「加载中…」 |
| 404 | 「该 trace 不存在或已被清理」 |
| 5xx | 复用现有 `.banner` 样式 |
| `truncated == true` | 顶部黄色提示「span 数超过上限，仅展示前 N 条」 |

---

## 6. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| `attributes` 体积大（长工具参数） | 单 trace 响应过大 | 后端 `limit` 兜底；详情按需渲染（点开才生成 DOM）；后续可加属性裁剪 |
| 超大 trace（数千 span） | 浏览器卡顿 | `limit` 默认 1000；树可折叠；后续可加虚拟滚动 |
| 同毫秒排序抖动 | 瀑布行序跳变 | 二级排序 `span_id`（§4.3） |
| 孤儿 span | 子树消失 | 升级为根 + UI 标记（§5.2） |
| memory 后端 deque 淘汰 | 点开缺 span | 文档标注，仅冒烟用（§4.4） |
| ES `max_result_window` | 深分页报错 | `limit` 上限 5000 ≪ 10000 |
| **合成数据父子时序不嵌套** | 瀑布图出现"孩子比父亲长"的畸形嵌套，验收失真 | 修 `send_test_trace.py`（§8 步骤 4） |

---

## 7. 验收标准

1. `GET /api/traces/{trace_id}` 对已知 trace 返回全部 span，条数与列表中该 trace 的 span 数一致。
2. **es 与 memory 两个后端返回结构一致**（字段名、类型一致）。
3. 列表点击 → 树形展开，父子缩进正确；瀑布条位置/宽度与时间成比例，子 span 完全落在父 span 时间范围内。
4. 点击 span 能看到 `attributes`（含 `gen_ai.*`）全量键值。
5. ERROR span 红色高亮；trace 级错误数正确。
6. 不存在的 `trace_id` → 404 友好提示，不白屏、不报故障。
7. 直接访问 / 刷新 `#/trace/<id>` 可用。
8. **回归**：页面无任何外部网络请求（NFR-1）；原有统计面板功能不受影响。

---

## 8. 实施步骤

| # | 任务 | 涉及文件 |
|---|---|---|
| 1 | `Storage` ABC 新增 `get_trace`；实现 `EsStorage` / `MemoryStorage` | `app/storage.py` |
| 2 | 新增 `/api/traces/{trace_id}` 路由（含 404 语义） | `app/main.py` |
| 3 | hash 路由 + 详情视图 + 树构建 + 瀑布渲染 + 详情面板 | `app/static/index.html` |
| 4 | 修 `send_test_trace.py` 的父子时序，保证子 span 严格嵌套在父 span 内 | `send_test_trace.py` |
| 5 | 冒烟验收（memory 后端 + 合成数据） | — |
| 6 | 更新 `requirements-and-design.md`：把 trace 树从 Out of Scope 划入已实现 | `requirements-and-design.md` |

### 步骤 4 说明（合成数据修复）

现状问题：root span 时长随机 `3000–4200ms`，而 4 个子 span 顺序累加（含 `GAP_MS=50` 间隔）累计可达约 `3800ms`，**子 span 可能超出父 span 的结束时间**，瀑布图会出现"孩子比父亲长"的非法嵌套。

修法：先生成并累计子 span 的总时长，再令 `root.duration = 子span总时长 + 间隔总和 + 余量`，保证严格嵌套：

```
root_start = t0
root_end   = t0 + (Σ child_duration + Σ gap + margin)
```

---

## 9. ADR 补充

| # | 决策 | 备选 | 理由 |
|---|---|---|---|
| ADR-8 | 后端返回**扁平列表**，树在前端构建 | 后端建树返回嵌套 JSON | 前端构建可复用于排序 / 折叠 / 过滤；ES 天然返回扁平文档，后端建树需额外递归且不便扩展 |
| ADR-9 | 明细查询**不带时间窗** | 沿用 `hours` 窗口 | 保证"点开就能看到"，避免列表与详情口径不一致（§4.1） |
| ADR-10 | 沿用单页 `index.html` + hash 路由 | 新建 `trace.html` / 引入前端框架 | 延续 NFR-1 零依赖；无构建步骤，改动最小，不破坏现有看板 |
