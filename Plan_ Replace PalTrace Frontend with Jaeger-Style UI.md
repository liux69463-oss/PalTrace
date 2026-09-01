# Plan: Replace PalTrace Frontend with Jaeger-Style UI

## Context

The user has provided a Jaeger UI-style design (HTML + CSS) in `design/` and wants the current PalTrace frontend replaced with this new design. The design is a static mockup with no JavaScript — all interactivity must be implemented. The backend needs new endpoints and enhanced data shapes to support the new UI.

## Scope

### Backend Changes

**1. New endpoint: `GET /api/services`**
- Returns service names + span counts in time window
- Used by sidebar "Service (N)" dropdown
- Implement in both `EsStorage` and `MemoryStorage`
- Response: `{ "items": [{ "service": "qwenpaw", "count": 123 }] }`

**2. Enhance `recent_traces` return shape**
Current shape lacks fields the design requires. Add:
- `duration_ms`: trace duration (max end - min start across all spans)
- `start_time`: earliest span start_time (epoch ms)
- `trace_name`: `"service: root_operation"` format (e.g. `"qwenpaw: invoke_agent Default Agent"`)
- `service_spans`: per-service span counts as object `{ "qwenpaw": 7 }` instead of comma-joined string

Changes needed in:
- `app/storage.py`: `MemoryStorage.recent_traces()` and `EsStorage.recent_traces()`
- Both must return identical shapes (project convention)

**3. New route in `app/main.py`**: `GET /api/services` with `hours`, `start`, `end` params

### Frontend Changes

**Replace `app/static/index.html` entirely** with new SPA implementing the Jaeger-style UI.

**Architecture**: Single HTML file with inline CSS + JS (zero CDN dependency, per project convention NFR-1). Hash routing: `#/` → search view, `#/trace/<trace_id>` → trace detail view.

**CSS**: Inline the design's `styles.css` (593 lines) + `trace-detail.css` (431 lines) directly. These are already pure CSS with no external dependencies.

**HTML Structure** (from design):
- **Search page** (`#/`): Header → Sidebar (filters) → Main content (scatter chart + results table)
- **Trace detail** (`#/trace/<id>`): Header → Subheader (trace title + ID) → Stats bar → Mini timeline → Split pane (tree panel + waterfall timeline)

**JavaScript to implement**:

1. **Router**: Hash-based, switch between `#search-view` and `#trace-view`
2. **Search page logic**:
   - Load services → populate sidebar dropdown
   - Load operations → populate operation dropdown
   - Find Traces button → call `/api/traces` with filters → render scatter chart + table
   - Scatter chart: pure SVG/CSS dots positioned by (start_time → x%, duration_ms → y%)
   - Results table: render traces with duration bars, service badges, sortable columns
   - Trace ID search box in header → navigate to `#/trace/<id>`
   - View toggle (List/Table) — Table is the only implemented view (List is placeholder)
3. **Trace detail logic**:
   - Fetch `/api/traces/<trace_id>` → build tree + waterfall
   - Tree panel: recursive rendering with indent levels, collapse/expand toggles
   - Waterfall: compute left% and width% for each span bar relative to trace duration
   - Mini timeline: single bar showing full trace duration
   - Stats bar: Trace Start, Duration, Services, Depth, Total Spans
   - Span click → show detail panel (reuse existing span detail logic from current frontend)
4. **Utility functions**:
   - `formatDuration(ms)`: convert ms → "48.0s", "13.7s", "3.2s" etc.
   - `formatTimestamp(epochMs)`: → "August 31 2026, 18:40:17.845"
   - `serviceColor(service)`: deterministic color from service name hash
   - Tree builder (reuse existing `buildTree` logic from current frontend)

### Files to Modify

| File | Action |
|---|---|
| `app/static/index.html` | **Rewrite** — new SPA with Jaeger UI |
| `app/storage.py` | **Modify** — add `list_services()`, enhance `recent_traces()` return shape |
| `app/main.py` | **Modify** — add `/api/services` route |

### Implementation Order

1. Backend: Add `list_services()` to Storage ABC + both implementations
2. Backend: Enhance `recent_traces()` to include `duration_ms`, `start_time`, `trace_name`, `service_spans`
3. Backend: Add `/api/services` route in `main.py`
4. Frontend: Rewrite `app/static/index.html` with new UI (CSS from design + JS for all interactivity)

### Verification

1. Start server: `ENABLE_GRPC=false STORAGE_BACKEND=es ./.venv/bin/uvicorn app.main:app`
2. Send test data: `python send_test_trace.py`
3. Open browser at `http://localhost:8000`
4. Verify search page: sidebar filters populate, Find Traces returns results, scatter chart renders, table shows traces
5. Click a trace → verify detail page: tree panel, waterfall bars, stats bar, mini timeline
6. Test trace ID search in header
7. Test with `STORAGE_BACKEND=memory` to verify both backends