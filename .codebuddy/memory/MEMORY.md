# MEMORY（PalTrace 长期记忆）

## 项目定位

- **PalTrace** = OTLP trace 采集 + 统计 + 调用链展示平台。接收 QwenPaw（AgentScope 应用，经 LoongSuite 采集）上报的 OTLP trace。
- 仓库：`/Users/liuxin/Documents/GitHub/PalTrace`
- **QwenPaw 在另一个仓库**：`/Users/liuxin/Documents/GitHub/QwenPal`。本仓库只是接收端，不要以为能在这里启动 QwenPaw。

## 架构与硬约定

- 单端口 FastAPI：`:8000` 同时提供 `POST /v1/traces`（OTLP/HTTP，JSON + protobuf 双编码）、`GET /api/*`、看板 `/`；另有 gRPC `:4317`。
- **gRPC 与 HTTP 共用一套解析**：`MessageToDict(proto)` 产出结构与 OTLP/HTTP JSON 同构，所以只有一份 `flatten_payload`。不要拆成两套解析。
- `Storage` ABC 双实现：`EsStorage`（ES 原生聚合，正确）/ `MemoryStorage`（Python 聚合，仅冒烟）。**任何新增查询都要两个后端各实现一遍，且返回结构一致**。
- **ES 必须用 7.x 客户端**（`elasticsearch==7.17.9`）。8.x 的 `bulk()` 参数由 `body=` 改名 `operations=`，混用直接报错。
- **看板零外部依赖**（公司内网无外网）：不引任何 CDN / 图表库，纯 CSS + 原生 JS，单页 `app/static/index.html`。新增功能务必保持 0 外部请求。
- 索引按天 `paltrace-spans-YYYY.MM.DD`；`attributes` 设 `dynamic: false`（不建索引但保留 `_source`，防 field explosion，且可供后续取用）。
- 隐私：默认丢弃 `gen_ai.prompt` / `gen_ai.completion`。

## 索引前缀

- 索引前缀默认 `paltrace-spans`（已从 `qwenpaw-*` 改名）。**再改前缀会导致历史索引查不到**，需用户确认弃数据后才能动。

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
