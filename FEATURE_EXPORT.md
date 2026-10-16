# FEATURE_EXPORT.md — 数据导出功能设计文档

> 项目：智能数据库查询工具（`w2/db_query`）
> 新增功能：查询结果数据导出（CSV / JSON）+ 一键自动化
> 涉及能力：Claude Code 的 **agents / commands / skills**

---

## 一、设计思路

### 1.1 目标

在已有的"智能数据库查询工具"上，新增**数据导出模块**，让用户能把查询结果导出为 **CSV** 与 **JSON** 两种格式，并通过自然语言 / 界面操作 / 一条命令触发"执行查询 + 导出结果"的全流程。

### 1.2 三条触发路径

本功能设计了三条互补的触发路径，覆盖"人"和"AI"两类使用者：

| 路径 | 触发方式 | 适用场景 | 实现 |
|------|----------|----------|------|
| **A. 前端界面** | 查询成功后弹窗主动询问 / 点击 EXPORT 按钮 | 人在浏览器里操作 | `exportService.ts` 客户端导出 |
| **B. 后端一键 API** | `POST /api/v1/dbs/{name}/export` | 脚本 / AI Agent 单次调用 | `export_service.py` 三步管线 |
| **C. Claude Code 命令** | `/export-query <db> <sql> [format]` | 在 Claude Code 里一句话导出 | `.claude/commands/export-query.md` |

### 1.3 核心设计决策

1. **复用既有安全链，不旁路校验**：导出端点复用 `execute_query_with_service → sql_validator`，强制 SELECT-only，且每次导出都记入查询历史。导出 = 一次可审计的只读查询。
2. **任务显式分解为三步**：把"导出数据"拆成 `获取查询结果 → 格式化数据 → 创建文件` 三个独立可测的子任务，让 AI Agent 能逐步推理、协调处理（呼应任务要求里的"Agent 任务分解"）。
3. **前端客户端导出 + 后端一键导出并存**：前端已有结果时直接客户端格式化下载（即时、不重复查询）；后端一键端点面向自动化（执行+导出合一）。
4. **切入点选择**：`show.tsx` 原本就有 `EXPORT CSV` / `EXPORT JSON` 两个**占位按钮但未接线**——这是最小侵入的接入点，无需改动路由或新增页面。

---

## 二、架构：改动前 vs 改动后

### 2.1 原有架构（理解结论）

工具采用 **Adapter + Factory + Facade** 模式：

```
前端 (React + Ant Design + Refine)
   │  POST /api/v1/dbs/{name}/query  { sql }
   ▼
API 层 (FastAPI routes — 无业务逻辑)        queries.py
   ▼
Service 层 (Facade)                         database_service.py
   ├─ SQL 校验   sql_validator.py (sqlglot, SELECT-only)
   ├─ 查询执行   adapter.execute_query()
   └─ 查询历史   query.py
   ▼
Adapter 层 (ABC + Registry 工厂)            base.py / registry.py
   ├─ PostgreSQLAdapter
   └─ MySQLAdapter
   ▼
QueryResult { columns, rows, rowCount, executionTimeMs, sql }
```

### 2.2 新增导出模块（改动后）

```
                        ┌──────────────── 新增 ────────────────┐
前端                     │ exportService.ts (客户端 CSV/JSON 下载)│
  show.tsx ──按钮/弹窗──►│ exportResult() → Blob 下载            │
                         └──────────────────────────────────────┘
                         ┌──────────────── 新增 ────────────────┐
后端 API                 │ POST /api/v1/dbs/{name}/export        │
  queries.py ──────────► │  { sql, format } → StreamingResponse  │
                         └───────────────┬──────────────────────┘
                                         ▼
                         ┌──────────────── 新增 ────────────────┐
Service 层               │ export_service.py (三步管线)           │
                         │  ① fetch_query_result()  执行查询     │
                         │  ② format_as_csv/json()  格式化        │
                         │  ③ create_export_response() 生成文件  │
                         │  export_query_result()  ← 编排器       │
                         └───────────────┬──────────────────────┘
                                         ▼ (复用)
                         execute_query_with_service → sql_validator → adapter
```

**关键点**：导出模块是**纯增量**，没有修改任何既有适配器、校验器或查询逻辑，只新增了一个 service、一个 schema 字段、一个端点，以及前端的接线。符合原有架构的 Open-Closed 原则。

---

## 三、新增/修改文件清单

### 后端（Python / FastAPI）

| 文件 | 类型 | 说明 |
|------|------|------|
| `w2/db_query/backend/app/services/export_service.py` | **新增** | 导出服务，三步任务分解 + 编排器 |
| `w2/db_query/backend/app/models/schemas.py` | 修改 | 新增 `ExportInput` schema（`sql` + `format`） |
| `w2/db_query/backend/app/api/v1/queries.py` | 修改 | 新增 `POST /{name}/export` 端点 |
| `w2/db_query/backend/tests/unit/test_export.py` | **新增** | 18 个单元测试，全通过 |

### 前端（React / TypeScript）

| 文件 | 类型 | 说明 |
|------|------|------|
| `w2/db_query/frontend/src/services/exportService.ts` | **新增** | 客户端 CSV/JSON 格式化 + 浏览器下载 |
| `w2/db_query/frontend/src/pages/databases/show.tsx` | 修改 | 接线 EXPORT 按钮 + 查询后主动询问弹窗 |

### Claude Code 自动化（agents / commands / skills）

| 文件 | 类型 | 说明 |
|------|------|------|
| `.claude/agents/data-export.md` | **新增** | 导出子 Agent，负责任务分解与协调 |
| `.claude/commands/export-query.md` | **新增** | `/export-query` 一键导出命令 |
| `.claude/skills/data-export/SKILL.md` | **新增** | 导出技能文档（触发词、用法、排障） |

---

## 四、Claude Code 三件套：agents / commands / skills

本功能完整运用了 Claude Code 的三类自动化配置，三者各司其职：

### 4.1 Agent — `.claude/agents/data-export.md`

**职责**：一个专门处理"导出数据"工作流的子 Agent。它内置了**任务分解**心智模型，把每次导出请求拆成三步并逐步推进：

1. **获取查询结果** — 校验 SQL 是 SELECT（前置 sanity check），调用后端导出端点
2. **格式化数据** — 选择 `csv`/`json`（后端负责实际格式化）
3. **创建文件** — 把流式响应保存到 `w2/db_query/exports/` 下，文件名带时间戳

它还内置了错误映射（404/400/500 各自的处理）和"何时不应行动"的边界（只查询不导出时让位给普通查询流程）。

**调用方式**：在 Claude Code 中通过 `data-export` 这个 agent 名称唤起，或由 `/export-query` 命令 handoff 触发。

### 4.2 Command — `.claude/commands/export-query.md`

**职责**：一条斜杠命令 `/export-query <database_name> <sql> [format]`，把"执行查询 + 导出结果"压缩成**一键触发**。

**执行流程**（命令文档里定义的 5 个阶段）：
1. **Phase 1 解析参数** — 拆出库名 / SQL / 格式（默认 csv）
2. **Phase 2 前置检查** — 非 SELECT 直接拒绝；格式必须是 csv/json
3. **Phase 3 触发一键导出** — 用 curl 调 `POST /export`，流式保存到 `exports/`
4. **Phase 4 验证回报** — 检查文件大小、行数，HTTP 错误则原文回报
5. **Phase 5 跟进 handoff** — 提供"换格式重导出"的交接按钮

**示例**：
```
/export-query mydb "SELECT * FROM users LIMIT 50"
/export-query shop_db "SELECT id, name FROM products WHERE active = true" json
```

### 4.3 Skill — `.claude/skills/data-export/SKILL.md`

**职责**：导出功能的"知识包"。当用户提到"导出/下载/保存查询结果为文件"时被触发，文档化：
- 支持的两种格式（CSV 带 UTF-8 BOM 适配 Excel；JSON 保留完整结果对象）
- 两条导出路径（A 客户端即时导出 / B 后端一键自动化）及其取舍
- 三步任务分解架构图
- 安全规则（SELECT-only、可审计、行数上限 1000）
- 错误恢复表（404/400/500 各自原因与修复）
- 本地运行命令

三者关系：**Skill** 提供知识背景与触发，**Command** 是一键触发的入口，**Agent** 是能自主协调多步的执行体。命令执行完还可 handoff 给 Agent 做"换格式重导出"。

---

## 五、核心练习点对照

| 练习要求 | 本项目如何落地 |
|----------|----------------|
| **代码库理解与扩展** | 用 AI 快速读通 w2/db_query 的 Adapter+Facade 架构，定位到 `show.tsx` 已有的占位 EXPORT 按钮作为切入点，新增 `export_service.py` 而不改动任何既有适配器 |
| **AI Agent 任务分解** | `export_service.py` 把"导出数据"显式拆成 `fetch_query_result`(获取) → `format_as_csv/json`(格式化) → `create_export_response`(创建文件) 三步；`data-export` agent 沿用同一分解逐步协调 |
| **工具链整合** | **Cursor** 用于快速迭代生成/接线前端按钮与导出服务（所见即所得）；**Claude Code** 的命令/agent 负责多步骤自动化（执行+导出+存盘+验证），二者互补 |

---

## 六、本地运行方式

### 6.1 前置

- Python ≥ 3.12、Node.js ≥ 18、（可选）uv
- 一个可连接的 PostgreSQL 或 MySQL 数据库（用于真实查询）；测试不需要真实数据库

### 6.2 安装

```bash
cd w2/db_query
make install          # 安装前后端依赖（= cd backend && uv sync --extra dev; cd frontend && npm install）
```

### 6.3 配置

```bash
cp backend/.env.example backend/.env
# 编辑 backend/.env，填入 OPENAI_API_KEY（自然语言转 SQL 用；纯导出功能不需要真实 key，留任意值即可启动）
```

### 6.4 启动

```bash
make dev              # 同时启动后端 :8000 和前端 :5173
```

### 6.5 体验导出功能

**界面操作**：
1. 浏览器打开 `http://localhost:5173`
2. 创建一个数据库连接 → 进入详情页 → 在 SQL 编辑器执行查询
3. 查询成功后会**弹窗主动询问**"Need to export this result?"，点 Export CSV / Export JSON
4. 或直接点结果区右上角的 `EXPORT CSV` / `EXPORT JSON` 按钮

**后端一键 API**（脚本/AI 用）：
```bash
mkdir -p exports
curl -sS -X POST "http://localhost:8000/api/v1/dbs/<连接名>/export" \
  -H "Content-Type: application/json" \
  -d '{"sql":"SELECT * FROM users LIMIT 10","format":"csv"}' \
  -o "exports/result_$(date +%Y%m%d_%H%M%S).csv"
```

**Claude Code 命令**（在仓库根目录用 Claude Code）：
```
/export-query <连接名> "SELECT * FROM users LIMIT 50" csv
```

### 6.6 运行测试

```bash
cd w2/db_query
make test-backend      # 跑全部后端测试
# 或只跑导出测试：
cd backend && OPENAI_API_KEY=dummy uv run pytest tests/unit/test_export.py -v
```

> 导出模块的 18 个单元测试全部通过（覆盖：CSV/JSON 格式化、逗号转义、日期/对象序列化、响应头、BOM、端点成功/404/400/500/缺参/不支持格式）。

---

## 七、自动化流程演示（一键"执行查询 + 导出结果"）

以 `/export-query` 命令为例，展示 Agent 如何协调三步子任务：

```
用户: /export-query mydb "SELECT id, name FROM users LIMIT 50" csv
   │
   ▼  [命令 Phase 2: 前置检查] SELECT ✓，格式 csv ✓
   ▼  [命令 Phase 3: 触发后端一键端点]
   │   POST /api/v1/dbs/mydb/export  { "sql": "...", "format": "csv" }
   │
   ▼  ===== 后端 export_query_result() 三步管线 =====
   │  ① fetch_query_result()   → sql_validator 校验 SELECT-only
   │                             → adapter.execute_query() 执行
   │                             → 记入 QueryHistory
   │  ② format_as_csv()        → 列顺序按 columns，None→空，日期→ISO
   │  ③ create_export_response()→ UTF-8 BOM + StreamingResponse
   │  ===============================================
   ▼  [命令 Phase 4: 验证]
   │   保存到 exports/mydb_20260810_104832.csv
   │   报告: ✅ 50 行，CSV，~1.8 KB
   ▼  [命令 Phase 5: handoff] → 可交接 data-export agent 换 JSON 重导出
```

**用户交互（自然语言触发）**：在前端，查询成功后 AI 助手以弹窗形式**主动询问**"Need to export this result as a file?"，用户一键选择 CSV 或 JSON，无需输入任何命令。

---

## 八、安全与已知限制

- **SELECT-only**：导出复用 `sql_validator`（sqlglot 解析），任何写操作（INSERT/UPDATE/DELETE/DROP…）都会被拒绝并返回 400。
- **可审计**：每次导出都经过正常查询管线，记入 `QueryHistory`，source = manual。
- **行数上限**：后端默认 `LIMIT 1000`（可配置 `query_default_limit`）。
- **文件名**：`<连接名>_<UTC时间戳>.<ext>`，同一查询多次导出会产生多个文件。
- **前端导出**为客户端格式化（不重复查询）；**后端导出**会重新执行一次查询（结果可能因数据变化而不同），自动化场景应明确这一点。

---

## 九、验证结果

- 后端语法检查：✅ 通过
- 导出单元测试：✅ 18/18 通过
- 既有测试套件中 `test_api_queries` 等的失败为**仓库既有问题**（测试 patch 了不存在的 `app.api.v1.queries.execute_query` 属性，实际导入名为 `execute_query_with_service`），与本次导出改动无关。
- 前端：`show.tsx` 接线完成，本地 `QueryResult` 接口与 `types/query` 导出类型结构兼容（TypeScript 结构化类型）。
