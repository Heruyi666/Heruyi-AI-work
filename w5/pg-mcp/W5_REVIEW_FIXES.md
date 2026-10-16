# W5 pg-mcp：按 `specs/w5/0006-pg-mcp-code-review.md` 的修复清单

> 环境事实：本机**无 docker / 无 PostgreSQL / 无 psql / 无 OpenAI Key**。
> 因此"能在本地运行并直接看到结果"通过**离线替身**实现：跑的是真实请求链路，
> 只有 PostgreSQL 与 LLM 两处外部依赖被内存替身替换。

## 一键验证

```bash
cd D:\zuoye\第二期作业\w5\pg-mcp
uv sync --extra dev

uv run python scripts/verify_w5_requirements.py  # 57 项断言：作业三点 + 0003 遗留项，逐条 A/B 对照
uv run python scripts/demo_offline.py            # 11 场景 / 35 项断言，报告写入 W5_DEMO_REPORT.md
uv run python scripts/bench_offline.py           # 14 项：并发限流守约 / 无泄漏 / 重试预算 / 延迟分布
uv run pytest                                    # 488 passed / 1 skipped（全程离线）
uv run pytest --cov=pg_mcp                       # 覆盖率 89%
uv run ruff check src tests scripts              # All checks passed
uv run mypy src/pg_mcp                           # Success: no issues found in 39 source files
```

> `verify_w5_requirements.py` 对应作业图片里的三点要求，判定方式是 **A/B 对照**：
> 同一条请求在"配置关闭 / 打开"两种状态下行为必须不同，才说明该功能真的接入了
> 请求链路（code review 的指控恰恰是"写了但没接线"）。报告见 `W5_ACCEPTANCE.md`。
> 报告末尾的【附 A】覆盖本文档上一节列出的 0003 / 0008 遗留项。

---

## Priority 1 — 多数据库未实现（请求 B 库却跑在 A 库）

| 问题 | 修复 | 位置 |
| --- | --- | --- |
| `Settings` 没有 `databases` | 新增 `databases: list[DatabaseConfig]`，空时回退到传统 `database`；库名唯一性校验；`get_database(name)` | `config/settings.py` |
| 只有一个 executor | 每库一个 `SQLExecutor`，`orchestrator._get_executor()` 按解析出的库名路由 | `app.py`、`services/orchestrator.py` |
| server 里硬编码组件装配 | 新增组合根 `build_components()`，server / 测试 / 演示共用同一对象图 | `app.py`、`server.py` |
| 未知库名无提示 | 拒绝并返回 `available_databases` | `services/orchestrator.py::_resolve_database` |

验证：演示场景 1、2；`tests/unit/test_app_multidb.py`。

## Priority 2 — 安全配置未接线

| 问题 | 修复 |
| --- | --- |
| `blocked_tables` / `blocked_columns` 不存在 | `SecurityConfig` 新增字段（支持 CSV 与 JSON 环境变量；`NoDecode` 避免 CSV 被当成 JSON 解析） |
| validator 传 `None` | `build_components` 从 `settings.security` 注入 |
| EXPLAIN 无策略 | `allow_explain`，未显式传参时跟随配置 |
| 拒绝原因不可观测 | 新增 `Violation` dataclass，异常 `details["reason"]` 结构化打标签 |

支持的拦截形式：

- 表：`payments`、`public.users`（限定名同时拦截裸名）
- 列：`password_hash`（裸名）、`users.password_hash`（限定名）、`payments.*`（通配 + 别名解析 + `SELECT *`）
- 语句：DML/DDL（`forbidden_statement`）、多语句（`multiple_statements`）、危险函数（`blocked_function`）、非 SELECT 命令（`disallowed_command`）

验证：演示场景 3–6；`tests/unit/test_security_policy.py`。

## Priority 3 — 弹性配置为死代码

| 问题 | 修复 |
| --- | --- |
| 缺 `resilience/retry.py`，`retry_delay`/`backoff_factor` 未使用 | 新增 `RetryPolicy` + `retry_async`（指数退避 + jitter + 可重试判定） |
| 限流器创建后未使用 | `orchestrator._acquire_slot("llm"\|"queries")`，超时抛 `RateLimitExceededError` 并计指标 |
| 哪些错误可重试无定义 | `is_retryable_db_error`（SQLSTATE 白名单 + 连接类异常）、`is_retryable_llm_error` |

验证：演示场景 7、8；`tests/unit/test_retry.py`、`tests/unit/test_resilience_observability.py`。

## Priority 4 — 模型 / 校验 / 可观测性缺陷

| 问题 | 修复 |
| --- | --- |
| `QueryResponse.to_dict` 重复定义 | 只保留一份；`tokens_used` 恒存在（未知时为 0） |
| `ErrorDetail` 两套实现 | 统一为 Pydantic 模型（`models/errors.py`），`models/query.py` 直接复用 |
| `max_question_length` 未强制 | 调 LLM 之前拒绝，返回 `question_too_long` |
| `min_confidence_score` 被忽略（失败默认 100） | 低置信度给 `warning`；校验失败返回 **0** 而不是 100 |
| 无健康检查 | 新增 `HealthChecker` + MCP 工具 `health_check()`，状态 `healthy/degraded/unhealthy` |
| 指标只在文档里 | 指标真正埋进请求路径，新增 `retry_attempts`、`rate_limit_rejections`、`health_checks`、`snapshot()` |
| 无 request_id 追踪 | contextvars 传播，成功/失败响应都回传 `request_id` |
| 集成/E2E 依赖真实服务 | 新增 `pg_mcp/testing/fakes.py`，测试与演示默认离线确定性运行 |

---

## 附：design-review (0003) 与 test-plan-review (0008) 的遗留项

> 上一轮只按 `0006` 修复，漏了 `0003`（架构评审）和 `0008`（测试计划评审）里
> 点名的问题。本轮补齐，验收脚本的**【附 A】**章节逐条做 A/B 对照。

| 出处 | 问题 | 修复 | 位置 |
| --- | --- | --- | --- |
| 0003 #7 | `SET ROLE {role}` 未加引号，可注入 | 改为引用标识符 `SET ROLE "role"`；`search_path` / `statement_timeout` 改为 `set_config($1)` 参数化，值不再进入语句文本 | `services/sql_executor.py::_set_session_params` |
| 0003 #7 | 未阻止系统 schema | 新增 `block_system_schemas`（默认开），拦截 `pg_catalog` / `information_schema` / `pg_toast` / `pg_temp`，reason=`system_schema` | `services/sql_validator.py::_check_system_schemas` |
| 0003 #2 | 先 fetch 全部行再切片，DoS | 新增 `enforce_row_limit`（默认开），把 `max_rows` 作为 `LIMIT` 下推进 SQL；Python 侧切片保留为二次保险 | `services/sql_executor.py::_apply_row_limit` |
| 0003 #3 | 查询结果含 PII 直接发往 OpenAI | 新增脱敏模块：`blocked_columns` + 列名启发式（password/token/card/email/...），保留结构替换值；`mask_sensitive_data` 默认开 | `services/data_masking.py`、`services/result_validator.py` |
| 0003 #1 | 无认证/授权、无审计 | 新增 `Authenticator`（API key → Principal → 按库授权）+ `AuditLog`；`AUTH_ENABLED` 默认 false 以向后兼容，开启后 `query` 工具接受 `api_key` 参数 | `security/auth.py`、`server.py` |
| 0008 #2.5 | 缺跨库访问防护 | 三段式名 `otherdb.public.users` 拒绝，reason=`cross_database_reference` | `services/sql_validator.py::_check_cross_database` |
| 0008 #2.3 | ResultValidator / Prompts / Metrics / Introspection 无测试 | 新增 4 个测试模块（共 40+ 用例） | `tests/unit/test_{result_validator,prompts,metrics,introspection}.py` |
| 0008 #2.5 | 安全测试深度不足 | 新增 `tests/security/test_advanced_security.py`：search_path 注入、角色提权、跨库引用、系统 schema、错误信息泄露 | `tests/security/` |

### 有意未采纳的建议（及理由）

| 建议 | 决定 | 理由 |
| --- | --- | --- |
| 0003 #3「默认禁用结果验证」 | 未采纳 | `ValidationConfig.enabled` 是本次作业要求的 `min_confidence_score` 置信度功能的载体，禁用会使其失效；改为默认开启**脱敏**来保护数据 |
| 0003 #7「补充 pglast 原生解析器」 | 未采纳 | 属 Low/Info 级选型建议，引入新依赖且 SQLGlot 已覆盖现有用例 |
| 0003 #10「多租户配额、K8s 清单、灾备」 | 未采纳 | 超出本次作业范围，属于生产化阶段工作 |

## 第三轮：从 87 → 92 的补强（真实驱动验证 + 并发压测 + 安全自检）

针对评分体系 `W5_RUBRIC.md` 里的扣分点逐条补强：

| 扣分点 | 补强 | 文件 |
|---|---|---|
| 弹性只在替身下验证 | 用**真实 asyncpg 异常类**验证重试判定（11 类，含 `SerializationError` / `QueryCanceledError`），并断言「可重试类的 SQLSTATE 必须都在策略白名单里」 | `tests/unit/test_retry_real_driver.py` |
| 真连库才炸的参数错误 | 用 `inspect.signature` 校验 `create_pool` 的每个 kwarg 都是 `asyncpg.connect` 真实接受的；校验 `ssl_mode` 值域与 asyncpg 文档集合**双向一致** | `tests/unit/test_driver_contract.py` |
| 认证默认关闭 = "忘了打开" | 新增安全态势自检：多库且认证关闭 → `risk`；单库 → `warn`；启动时 WARNING 打点；`health_check` 返回 `security_posture` | `src/pg_mcp/security/posture.py` |
| 无并发/资源证据 | 新增压测：120 并发下 DB 峰值=上限、60 并发过载下显式拒绝 54 个、指标序列数稳定、全程无连接泄漏 | `scripts/bench_offline.py` |
| pool.py 覆盖率仅 46% | 补多库创建 / 失败回滚 / 超时 terminate / 异常 terminate | `tests/unit/test_pool.py` |

### 压测挖出的真 bug：schema 缓存惊群（cache stampede）

```
orchestrator.py:230  冷缓存 → 内省（acquire 连接、打数据库）
orchestrator.py:291  才进入限流槽 _acquire_slot("queries")
```

并发 60 个请求打到冷缓存时，**60 个请求各自内省一次**，且发生在限流之前——
限流的 DoS 防护被完全绕过。单测一次只发一个请求，永远碰不到。

修复：`SchemaCache.load` 加 per-database 双重检查锁（`asyncio.Lock` + double-check），
`refresh()` 走 `force=True` 保留强制刷新语义。修复后并发峰值 **60 → 1**，
已加 5 个并发防回归测试（`tests/unit/test_cache_stampede.py`）。

### 本轮踩坑

- `monkeypatch.setattr(pool_module.asyncpg, "create_pool", fake)` 会污染**全局** asyncpg 模块，
  之后再 `inspect.signature(asyncpg.create_pool)` 拿到的是 fake 的签名 → 必须在 patch **之前**取签名。
- asyncpg 真实类名是 `SerializationError` / `PostgresSyntaxError`，不是 `SerializationFailureError` / `SyntaxError`。
- `ResilienceConfig.acquire_timeout` 有 `ge=0.1` 下限，压测想模拟"立即拒绝"只能用 0.1。
- `SchemaCache.load` 缓存命中即返回后，`refresh()` 会失去强制刷新能力 → 必须加 `force` 参数。

## 新增/重写文件

| 文件 | 作用 |
| --- | --- |
| `src/pg_mcp/app.py` | 组合根：多库 executor、安全策略、限流、熔断、指标的统一下线 |
| `src/pg_mcp/services/health.py` | 健康检查探针（每库 `SELECT 1` + schema 缓存覆盖） |
| `src/pg_mcp/resilience/retry.py` | 重试策略与可重试错误判定 |
| `src/pg_mcp/testing/fakes.py` | 离线替身（假连接池 / 内省 handler / 脚本化 LLM） |
| `scripts/demo_offline.py` | 离线端到端演示与自检报告 |
| `tests/unit/test_{retry,app_multidb,security_policy,resilience_observability}.py` | 针对上述修复的回归测试 |

## 环境变量（新增）

```bash
DATABASES=[{"name":"analytics","host":"..."},{"name":"billing","host":"..."}]  # 或 DATABASES__0__NAME=analytics
SECURITY_BLOCKED_TABLES=payments,public.secrets      # CSV 或 JSON 数组
SECURITY_BLOCKED_COLUMNS=users.password_hash,payments.*
SECURITY_ALLOW_EXPLAIN=true
RESILIENCE_MAX_RETRIES=3
RESILIENCE_RETRY_DELAY=0.5
RESILIENCE_BACKOFF_FACTOR=2.0
RESILIENCE_MAX_RETRY_DELAY=30
RESILIENCE_RETRY_JITTER=0.1
RESILIENCE_MAX_CONCURRENT_QUERIES=10
RESILIENCE_MAX_CONCURRENT_LLM_CALLS=5
RESILIENCE_ACQUIRE_TIMEOUT=30
```
