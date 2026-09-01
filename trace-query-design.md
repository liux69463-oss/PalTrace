# PalTrace Trace 查询增强设计文档

| 项 | 内容 |
|---|---|
| 版本 | v0.3.0 |
| 前置 | v0.2.0（trace 树视图，见 `trace-tree-design.md`） |
| 目标 | trace 列表支持 **operation 过滤** + **时间范围查询（含预设）** + **两者组合** |
| 范围 | 存储层能力扩展 + 2 个接口 + 看板过滤器 UI |
| 依赖 | 不新增任何依赖（延续 NFR-1 零外部依赖） |

---

## 1. 背景与现状

`GET /api/traces` 目前只能按 `hours`（默认 24）取最近 N 条 trace，无法：

- 按操作类型筛选（如"只看含 `web_search` 的 trace"）
- 查询细粒度时间范围（`hours` 最小粒度是 1 小时，无法查最近 5 分钟）

排障时这两类需求很常见：**"最近 10 分钟有没有报错的工具调用"**、**"只看 chat 类的 trace"**。

当前实现（`Storage.recent_traces(hours, size)`）：

| 后端 | 实现 |
|---|---|
| ES | 按 `@timestamp` 范围过滤 → `terms` 聚合 `trace_id`（按 `max(@timestamp)` 降序，`shard_size=size*10`）→ 子聚合 `tokens` / `services` |
| Memory | Python 侧按窗口过滤后聚合 |

返回：`{trace_id, spans, total_tokens, last_ts, service}`。

---

## 2. 需求

| ID | 需求 | 优先级 | 说明 |
|---|---|---|---|
| FR-1 | 按 `operation` 过滤 trace | P0 | 返回**包含**该 operation span 的 trace |
| FR-2 | 时间范围查询（显式起止） | P0 | 传 `start` / `end`（epoch 毫秒） |
| FR-3 | 时间预设：最近 5 分钟 / 10 分钟 … 至 2 天 | P0 | 前端按预设换算成 `start`/`end` |
| FR-4 | operation + 时间范围**组合查询** | P0 | 两个条件同时生效（AND） |
| FR-5 | 可选出 operation 列表供下拉框 | P0 | 新增 `/api/operations` |
| FR-6 | 每条 trace 展示其包含的 operations | P1 | 便于理解为何命中过滤 |

---

## 3. 设计

### 3.1 接口

| 方法 | 路径 | 参数 | 说明 |
|---|---|---|---|
| GET | `/api/traces` | `hours` `size` `operation` `start` `end` | 扩展现有接口（兼容旧参数） |
| GET | `/api/operations` | `hours` `start` `end` `size` | 该时间窗内出现过的 operation 列表 |

参数细节：

| 参数 | 类型 | 默认 | 约束 |
|---|---|---|---|
| `hours` | int | `DEFAULT_HOURS`（24） | 1..720（30 天）；**仅当未传 `start`/`end` 时生效** |
| `start` / `end` | int（epoch ms） | — | 必须**成对**传；`start < end`；跨度 ≤ 30 天 |
| `operation` | str | — | 精确匹配 `operation` 字段（keyword） |
| `size` | int | 20 | 1..200 |

**优先级**：显式 `start`/`end` > `hours`。二者可以共存，但 `start`/`end` 优先。

### 3.2 时间范围语义

- 统一以 **`@timestamp`（= span 的 `end_time`，epoch 毫秒）** 为准，与现有 `recent_traces` 口径一致。
- 前端预设换算成 `start = now - N`、`end = now`，**在查询发起时计算**，保证刷新时窗口随之前移。

预设列表（下拉框）：

| 预设 | 跨度 |
|---|---|
| 最近 5 分钟 | 5 min |
| 最近 10 分钟 | 10 min |
| 最近 30 分钟 | 30 min |
| 最近 1 小时 | 1 h |
| 最近 6 小时 | 6 h |
| 最近 24 小时 | 24 h（默认） |
| 最近 2 天 | 48 h |

另提供**自定义**起止（`datetime-local` 输入）以满足任意区间。

### 3.3 关键决策：operation 过滤要保证 trace 级统计不被"污染"

**问题**：一条 trace 含多种 operation。若直接在查询层加 `term: {operation: "chat"}`，
ES 的 `doc_count`（即 `spans`）与 `tokens` 会**只统计匹配的那部分 span**，
导致列表显示 `Span=3`，点进树视图却是 13 个 span —— 语义割裂、用户困惑。

**决策（ADR-11）**：采用**两阶段查询**，让过滤只决定"哪些 trace 入选"，
统计仍基于整条 trace：

```
阶段一（仅当传了 operation 时执行）
  query: [时间范围] AND [operation = X]
  aggs : terms(trace_id)，按 max(@timestamp) 降序，size = size
  → 得到按最近时间排好序的、命中的 trace_id 列表

阶段二（总是执行）
  query: [时间范围] AND [trace_id IN (阶段一结果)]     # 未传 operation 时无 trace_id 过滤
  aggs : terms(trace_id) → doc_count(真实 span 数) / sum(total_tokens) / terms(services) / terms(operations)
  → 得到每条 trace 的**完整**统计

最后按阶段一的顺序输出（保证"最近优先"的排序不被阶段二打乱）
```

代价：多一次 ES 往返。收益：`spans` / `total_tokens` / `service` 始终是**整条 trace 的真实值**，与树视图一致。

> 备选方案「单查询 + `bucket_selector` 剪枝」被否决：terms 聚合先取 top-N 再剪枝，
> 当大量 trace 不含该 operation 时会返回**少于 size 条甚至空**，结果不可预期。

### 3.4 ES 实现要点

- 时间过滤统一走 `range: {"@timestamp": {"gte": start_ms, "lte": end_ms}}`，替换原来的 `now-{hours}h` 相对表达式（相对时间在 ES 里有缓存与精度问题，显式毫秒更可控）。
- `operation` 是 keyword，用 `term` 精确匹配。
- 阶段二用 `terms: {"trace_id": [...]}`（`terms` 查询，非聚合）。
- `services` / `operations` 子聚合 `size` 分别取 5 / 10，避免长尾。
- 阶段一的 terms 聚合保留 `shard_size = size * 10`（按子聚合排序时的精度补偿）。

### 3.5 Memory 实现要点

线性扫描环形缓冲区，用同一套语义：

```python
matched = [s for s in spans if start <= s["@timestamp"] <= end]
if operation:
    hit_ids = {s["trace_id"] for s in matched if s.get("operation") == operation}
else:
    hit_ids = {s["trace_id"] for s in matched}
# 统计时仍基于该 trace 的**全部** span（不因 operation 过滤而少算）
```

排序按 `last_ts` 降序，取前 `size` 条。**与 ES 口径严格一致**（含 `operations` 字段）。

### 3.6 响应结构

```json
{
  "items": [
    {
      "trace_id": "fZ2y5BEnAm2kMPUkUSH0zw==",
      "spans": 13,
      "total_tokens": 167680,
      "last_ts": 1788225379082,
      "service": "qwenpaw",
      "operations": ["chat", "execute_tool", "invoke_agent", "react"]
    }
  ]
}
```

新增 `operations` 字段（该 trace 内出现过的 operation，升序）。**不改变既有字段含义**，前端与旧逻辑兼容。

`/api/operations`：

```json
{"items": [{"operation": "chat", "count": 13}, {"operation": "execute_tool", "count": 6}]}
```

---

## 4. 前端设计

### 4.1 过滤器 UI

在「近期 Trace」面板标题行下方加一行过滤条：

```
[operation ▾ 全部]  [时间 ▾ 最近24小时]  [自定义: 起 __ 止 __]  [查询] [重置]
```

- **operation 下拉**：`全部` + `/api/operations` 返回的列表（显示 `operation (count)`）。
- **时间预设下拉**：3.2 的预设列表；选「自定义」时启用两个 `datetime-local` 输入。
- **查询按钮**：立即用当前条件重新拉取 `/api/traces`；也可改为**改动即查**（更顺手，本方案采用改动即查）。
- **重置**：恢复 `operation=全部`、`时间=最近24小时`。

### 4.2 状态与自动刷新

- 过滤条件保存在模块级变量 `filters = {operation: "", preset: "24h", start: null, end: null}`。
- 10s 自动刷新沿用现有 `load()`，**但只刷新统计卡片**；trace 列表单独用 `loadTraces()`，携带当前 `filters`，避免刷新后用户选的过滤条件被冲掉。
- 当前生效条件展示在列表上方（如 `已筛选：operation=chat · 最近10分钟`），避免用户困惑。

### 4.3 时间预设换算

```js
const PRESETS = [
  {label: '最近 5 分钟',  ms: 5 * 60_000},
  {label: '最近 10 分钟', ms: 10 * 60_000},
  {label: '最近 30 分钟', ms: 30 * 60_000},
  {label: '最近 1 小时',  ms: 60 * 60_000},
  {label: '最近 6 小时',  ms: 6 * 60 * 60_000},
  {label: '最近 24 小时', ms: 24 * 60 * 60_000},
  {label: '最近 2 天',    ms: 48 * 60 * 60_000},
];
```

每次查询时 `start = Date.now() - preset.ms`、`end = Date.now()`（**不缓存时间戳**，否则窗口不会随刷新前移）。

---

## 5. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 两阶段查询多一次 ES 往返 | 延迟增加 | 仅当传 `operation` 时才走第二阶段过滤；数据量小时可忽略 |
| 阶段一返回 0 个 trace_id | 阶段二 `terms` 传空数组 | 提前短路返回空列表，不发起第二次查询 |
| 时间跨度过大 | 扫描量大 | 跨度上限 30 天（与现有 `hours ≤ 720` 一致） |
| `start`/`end` 只传一个 | 语义不清 | 校验：必须成对，否则 400 |
| 时区问题（`datetime-local`） | 自定义范围偏差 | 前端用本地时间构造，转成 epoch ms 传给后端；后端只认毫秒，无时区概念 |
| Memory 后端 deque 淘汰 | 久远数据查不到 | 既有局限，memory 仅冒烟用 |

---

## 6. 验收标准

1. `?operation=chat` 只返回**含 chat span** 的 trace，且每条的 `spans` / `total_tokens` 是**整条 trace** 的真实值（与树视图一致）。
2. `?start=&end=` 能查任意区间；预设 5 分钟 / 10 分钟 / 2 天均生效。
3. `?operation=execute_tool&start=&end=` 组合查询两条件同时生效（AND）。
4. **ES 与 Memory 两个后端返回结构一致**（含新增的 `operations` 字段）。
5. `/api/operations` 返回该窗口内真实出现过的 operation 及计数。
6. 参数校验：只传 `start` 不传 `end` → 400；`start >= end` → 400。
7. 前端：切换 operation / 时间预设立即生效；重置按钮恢复默认；显示当前生效条件。
8. 回归：过滤条件在 10s 自动刷新后**不被重置**；树视图与既有统计面板不受影响；页面 0 外部请求。

---

## 7. 实施步骤

| # | 任务 | 涉及文件 |
|---|---|---|
| 1 | `Storage` ABC 扩展 `recent_traces(..., operation, start_ms, end_ms)`；新增 `list_operations` | `app/storage.py` |
| 2 | `EsStorage` 两阶段实现 + `list_operations` | `app/storage.py` |
| 3 | `MemoryStorage` 同口径实现 + `list_operations` | `app/storage.py` |
| 4 | `/api/traces` 扩展参数与校验；新增 `/api/operations` | `app/main.py` |
| 5 | 过滤条 UI + 预设换算 + 独立 `loadTraces()` + 生效条件提示 | `app/static/index.html` |
| 6 | 冒烟：单条件、组合、边界校验、双后端一致性 | — |
| 7 | 更新 `requirements-and-design.md` 接口表 | `requirements-and-design.md` |

---

## 8. ADR

| # | 决策 | 备选 | 理由 |
|---|---|---|---|
| ADR-11 | operation 过滤走**两阶段查询**，统计基于整条 trace | 单查询在 query 层过滤 / `bucket_selector` 剪枝 | 避免"列表 Span=3、点进去 13"的语义割裂；且结果数量可预期（单查询剪枝会返回少于 size 条） |
| ADR-12 | 时间范围用**显式 epoch 毫秒**，前端换算预设 | 后端接收 `minutes` / 相对表达式 | 后端无时区与相对时间缓存问题；前端换算简单，窗口随刷新自然前移 |
| ADR-13 | 扩展现有 `/api/traces`，不新增路径 | 新增 `/api/traces/search` | 保持单一入口，旧参数 `hours` 继续兼容，前端改动最小 |
