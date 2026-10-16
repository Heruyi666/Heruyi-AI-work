#!/usr/bin/env python
"""Offline end-to-end demonstration of the pg-mcp fixes (W5 homework).

Runs the *real* pipeline — settings, security validator, per-database executors,
rate limiter, retry/backoff, metrics, tracing, health checker — against in-memory
doubles for PostgreSQL and the LLM, so no database server and no OpenAI key are
required.

Usage::

    uv run python scripts/demo_offline.py

The same report is written to ``W5_DEMO_REPORT.md`` next to the project root.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pg_mcp.config.settings import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from pg_mcp.app import AppComponents, build_components  # noqa: E402
from pg_mcp.models.errors import LLMUnavailableError  # noqa: E402
from pg_mcp.models.query import QueryRequest, ReturnType  # noqa: E402
from pg_mcp.observability.metrics import MetricsCollector  # noqa: E402
from pg_mcp.testing.fakes import (  # noqa: E402
    DEFAULT_TABLES,
    FakePool,
    StubResultValidator,
    StubSQLGenerator,
    build_offline_settings,
    make_pool_factory,
    offline_pg_handler,
)

OUT: io.StringIO = io.StringIO()
PASS = 0
FAIL = 0


# ----------------------------------------------------------------------
# Reporting helpers
# ----------------------------------------------------------------------
def emit(line: str = "") -> None:
    """Print a line to stdout (flushing immediately) and record it.

    Args:
        line: Text to emit.
    """
    print(line, flush=True)
    OUT.write(line + "\n")


def section(title: str, index: str) -> None:
    """Print a section header.

    Args:
        title: Section title.
        index: Section number label (e.g. ``"3"``).
    """
    emit("")
    emit("=" * 78)
    emit(f"[{index}] {title}")
    emit("=" * 78)


def check(description: str, condition: bool, detail: str = "") -> bool:
    """Record and print an assertion-style check.

    Args:
        description: What is being verified.
        condition: Result of the verification.
        detail: Extra information printed alongside.

    Returns:
        bool: The condition, for optional chaining.
    """
    global PASS, FAIL
    if condition:
        PASS += 1
        mark = "[OK]  "
    else:
        FAIL += 1
        mark = "[FAIL]"
    suffix = f"  {detail}" if detail else ""
    emit(f"  {mark} {description}{suffix}")
    return condition


def field(name: str, value: Any) -> None:
    """Print a key/value observation line.

    Args:
        name: Field name.
        value: Field value.
    """
    emit(f"        - {name}: {value}")


# ----------------------------------------------------------------------
# Fixture helpers
# ----------------------------------------------------------------------
def make_pools(names: Sequence[str], **kwargs: Any) -> dict[str, FakePool]:
    """Create one fake pool per database name sharing introspection fixtures.

    Args:
        names: Database names.
        **kwargs: Extra arguments forwarded to :class:`FakePool`.

    Returns:
        dict[str, FakePool]: Fake pools keyed by database name.
    """
    handler = offline_pg_handler(DEFAULT_TABLES)
    return {name: FakePool(name, handler=handler, **kwargs) for name in names}


async def build(
    settings: Settings,
    *,
    pools: dict[str, FakePool],
    generator: StubSQLGenerator,
    result_validator: StubResultValidator | None = None,
    metrics: MetricsCollector | None = None,
    load_schemas: bool = True,
) -> AppComponents:
    """Build a component graph wired to offline doubles.

    Args:
        settings: Settings to use.
        pools: Fake pools keyed by database name.
        generator: Scripted SQL generator.
        result_validator: Optional scripted result validator.
        metrics: Optional metrics collector.
        load_schemas: Whether to introspect schemas at startup.

    Returns:
        AppComponents: The wired object graph.
    """
    return await build_components(
        settings,
        pool_factory=make_pool_factory(pools),
        sql_generator=generator,  # type: ignore[arg-type]
        result_validator=result_validator or StubResultValidator(confidence=92),  # type: ignore[arg-type]
        metrics=metrics,
        load_schemas=load_schemas,
    )


async def run_query(
    components: AppComponents,
    question: str,
    *,
    database: str | None = None,
    return_type: ReturnType = ReturnType.RESULT,
) -> dict[str, Any]:
    """Execute a query through the orchestrator and return the tool payload.

    Args:
        components: Wired components.
        question: Natural language question.
        database: Optional target database.
        return_type: ``sql`` or ``result``.

    Returns:
        dict[str, Any]: Serialized response (the same dict the MCP tool returns).
    """
    response = await components.orchestrator.execute_query(
        QueryRequest(question=question, database=database, return_type=return_type)
    )
    return response.to_dict()


# ----------------------------------------------------------------------
# Scenarios
# ----------------------------------------------------------------------
async def scenario_1_startup() -> AppComponents:
    """Demonstrate startup with several databases and wired security policies.

    Returns:
        AppComponents: Reusable graph for the next scenarios.
    """
    section("启动：多数据库注册 + 安全策略/限流配置真正注入组件", "1")

    settings = build_offline_settings(
        databases=("analytics", "billing"),
        blocked_tables=("payments",),
        blocked_columns=("users.password_hash",),
    )
    pools = make_pools(["analytics", "billing"])
    generator = StubSQLGenerator(["SELECT COUNT(*) FROM users;"])
    metrics = MetricsCollector()
    metrics.reset_all_metrics()

    components = await build(
        settings, pools=pools, generator=generator, metrics=metrics, load_schemas=True
    )

    emit(f"  配置的数据库: {[db.name for db in settings.databases]}")
    emit("  Schema 内省（走真实 SchemaIntrospector + 假连接池）:")
    for name in pools:
        schema = components.schema_cache.get(name)
        tables = len(schema.tables) if schema else 0
        preview = (
            schema.to_prompt_context()[:60].replace(chr(10), " | ") if schema else ""
        )
        field(name, f"{tables} 张表 -> {preview}")
    check("每个数据库都创建了独立连接池", set(pools) == set(components.pools))
    check("每个数据库都有独立 SQLExecutor", set(components.executors) == {"analytics", "billing"})

    validator = components.sql_validator
    check(
        "blocked_tables/blocked_columns/allow_explain 已注入 SQLValidator",
        validator.blocked_tables == {"payments"}
        and validator.blocked_columns == {"users.password_hash"}
        and validator.allow_explain is False,
        f"blocked_tables={sorted(validator.blocked_tables)}, "
        f"blocked_columns={sorted(validator.blocked_columns)}",
    )
    check(
        "限流器按配置生效 (queries=10, llm=5)",
        components.rate_limiter.query_limiter.max_concurrent == 10
        and components.rate_limiter.llm_limiter.max_concurrent == 5,
    )
    return components


async def scenario_2_routing(components: AppComponents, pools: dict[str, FakePool]) -> None:
    """Demonstrate that the request hits the executor of the requested database.

    Args:
        components: Wired components.
        pools: Fake pools used to observe execution.
    """
    section("多数据库路由：访问 B 库的请求绝不会落到 A 库", "2")

    for pool in pools.values():
        pool.executed.clear()

    result = await run_query(
        components,
        "billing 库一共有多少订单？",
        database="billing",
    )
    emit("  question: billing 库一共有多少订单？ (database='billing')")
    field("success", result["success"])
    field("generated_sql", result.get("generated_sql"))
    field("rows", result.get("data", {}).get("rows"))
    field("request_id", result.get("request_id"))
    field("analytics 池执行过的 SQL", pools["analytics"].executed)
    field("billing   池执行过的 SQL", pools["billing"].executed)
    check("SQL 在 billing 池上执行", len(pools["billing"].executed) == 1)
    check("analytics 池未被触碰（无跨库串扰）", pools["analytics"].executed == [])

    # Unknown database must fail fast and list the alternatives.
    bad = await run_query(components, "查看用户", database="reporting")
    field("database='reporting' 响应", bad.get("error"))
    check(
        "未知数据库被拒绝且返回可选库列表",
        bad["success"] is False
        and bad["error"]["code"] == "database_error"
        and bad["error"]["details"]["available_databases"] == ["analytics", "billing"],
    )


async def scenario_3_blocked_table() -> None:
    """Demonstrate sensitive-table blocking coming from configuration."""
    section("安全控制（1/3）：配置的敏感表被真实拦截", "3")

    settings = build_offline_settings(
        databases=("analytics", "billing"),
        blocked_tables=("payments", "public.users"),
        max_retries=0,
    )
    pools = make_pools(["analytics", "billing"])
    generator = StubSQLGenerator(
        [
            "SELECT id, card_number FROM payments;",  # blocked table
            "SELECT id, email FROM public.users;",  # schema-qualified block
        ]
    )
    components = await build(settings, pools=pools, generator=generator, load_schemas=False)

    for label in ("payments", "public.users"):
        result = await run_query(components, f"给我 {label} 的数据", database="billing")
        err = result["error"]
        reason = err["details"].get("reason")
        field(
            f"SQL 访问 {label}",
            f"success={result['success']} code={err['code']} reason={reason}",
        )
        check(
            f"{label} 被拒绝 (reason=blocked_table)",
            err["details"].get("reason") == "blocked_table",
        )

    rejected = {
        k: v
        for k, v in components.metrics.snapshot().items()
        if k.startswith("pg_mcp_sql_rejected")
    }
    field("metrics sql_rejected", rejected)
    await components.shutdown()


async def scenario_4_blocked_column() -> None:
    """Demonstrate sensitive-column blocking (qualified and bare names)."""
    section("安全控制（2/3）：敏感列拦截（含表限定名 / 通配）", "4")

    settings = build_offline_settings(
        databases=("analytics",),
        blocked_columns=("users.password_hash", "payments.*"),
        max_retries=0,
    )
    pools = make_pools(["analytics"])
    generator = StubSQLGenerator(
        [
            "SELECT id, email, password_hash FROM users;",
            "SELECT * FROM payments WHERE id = 1;",
            "SELECT id, email FROM users;",
        ]
    )
    components = await build(settings, pools=pools, generator=generator, load_schemas=False)

    r1 = await run_query(components, "列出用户密码哈希", database="analytics")
    check(
        "users.password_hash 被拒绝",
        r1["error"]["details"].get("reason") == "blocked_column",
        str(r1["error"]["details"]),
    )
    r2 = await run_query(components, "查支付明细", database="analytics")
    check(
        "payments.* 通配拦截 SELECT * 被拒绝",
        r2["error"]["details"].get("reason") == "blocked_column",
        str(r2["error"]["details"]),
    )
    r3 = await run_query(components, "列出用户邮箱", database="analytics")
    check("正常列仍可访问", r3["success"] is True, f"sql={r3.get('generated_sql')}")
    await components.shutdown()


async def scenario_5_explain_policy() -> None:
    """Demonstrate the EXPLAIN policy toggled by configuration."""
    section("安全控制（3/3）：EXPLAIN 策略由配置决定", "5")

    for allow in (False, True):
        settings = build_offline_settings(
            databases=("analytics",), allow_explain=allow, max_retries=0
        )
        pools = make_pools(["analytics"])
        generator = StubSQLGenerator(["EXPLAIN SELECT id FROM users;"])
        components = await build(settings, pools=pools, generator=generator, load_schemas=False)
        result = await run_query(components, "看看查询计划", database="analytics")
        if allow:
            check(
                "SECURITY_ALLOW_EXPLAIN=true -> EXPLAIN 放行",
                result["success"] is True,
                f"sql={result.get('generated_sql')}",
            )
        else:
            check(
                "SECURITY_ALLOW_EXPLAIN=false -> EXPLAIN 被拒绝",
                result["success"] is False
                and result["error"]["code"] == "security_violation"
                and result["error"]["details"].get("reason") == "explain_not_allowed",
                str(result["error"]["details"]),
            )
        await components.shutdown()


async def scenario_6_dml() -> None:
    """Demonstrate DML/multi-statement rejection."""
    section("安全控制（补充）：写操作与多语句被拒绝", "6")

    settings = build_offline_settings(databases=("analytics",), max_retries=0)
    pools = make_pools(["analytics"])
    generator = StubSQLGenerator(
        [
            "DELETE FROM users WHERE id = 1;",
            "SELECT 1; DROP TABLE users;",
            "SELECT id, email FROM users LIMIT 5;",
        ]
    )
    components = await build(settings, pools=pools, generator=generator, load_schemas=False)

    r1 = await run_query(components, "删除一个用户", database="analytics")
    check(
        "DELETE 被拒绝",
        r1["success"] is False and r1["error"]["details"].get("reason") == "forbidden_statement",
        f"reason={r1['error']['details'].get('reason')}",
    )
    r2 = await run_query(components, "先看个 1 再删表", database="analytics")
    check(
        "多语句被拒绝",
        r2["success"] is False and r2["error"]["details"].get("reason") == "multiple_statements",
        f"reason={r2['error']['details'].get('reason')}",
    )
    r3 = await run_query(components, "列出 5 个用户", database="analytics")
    check("合法 SELECT 仍可执行", r3["success"] is True, f"sql={r3.get('generated_sql')}")
    await components.shutdown()


async def scenario_7_rate_limit() -> None:
    """Demonstrate that configured rate limits are enforced on the request path."""
    section("弹性：速率限制真正作用于 LLM 调用", "7")

    settings = build_offline_settings(
        databases=("analytics",),
        max_concurrent_llm_calls=1,
        acquire_timeout=0.2,
    )
    pools = make_pools(["analytics"])
    generator = StubSQLGenerator(["SELECT COUNT(*) FROM users;"], latency=0.5)
    components = await build(settings, pools=pools, generator=generator, load_schemas=False)

    results = await asyncio.gather(
        *[
            run_query(components, f"并发请求 {i}", database="analytics", return_type=ReturnType.SQL)
            for i in range(3)
        ]
    )
    codes = ["success" if r["success"] else r["error"]["code"] for r in results]
    field("3 个并发请求（LLM 并发上限=1）结果", codes)
    check(
        "超出上限的请求被限流拒绝",
        codes.count("rate_limit_exceeded") >= 1
        and codes.count("success") >= 1,
        f"success={codes.count('success')}, rejected={codes.count('rate_limit_exceeded')}",
    )
    field(
        "metrics rate_limit_rejections",
        {k: v for k, v in components.metrics.snapshot().items() if "rate_limit" in k},
    )
    await components.shutdown()


async def scenario_8_retry() -> None:
    """Demonstrate retry/backoff for transient LLM and database failures."""
    section("弹性：重试 + 指数退避（LLM 瞬时故障 / DB 连接闪断）", "8")

    # --- LLM transient failure then success ---
    settings = build_offline_settings(
        databases=("analytics",), max_retries=3, retry_delay=0.05, backoff_factor=2.0
    )
    pools = make_pools(["analytics"])
    generator = StubSQLGenerator(
        [
            LLMUnavailableError("OpenAI API rate limit exceeded"),
            LLMUnavailableError("upstream temporarily unavailable"),
            "SELECT COUNT(*) FROM orders;",
        ]
    )
    components = await build(settings, pools=pools, generator=generator, load_schemas=False)
    result = await run_query(components, "统计订单数", database="analytics")
    field("LLM 调用次数（第 3 次成功）", len(generator.calls))
    check(
        "LLM 瞬时错误经退避重试后成功",
        result["success"] is True and len(generator.calls) == 3,
        f"sql={result.get('generated_sql')}",
    )
    await components.shutdown()

    # --- Database transient failure then success ---
    settings = build_offline_settings(
        databases=("analytics",), max_retries=3, retry_delay=0.05, backoff_factor=2.0
    )
    pool = FakePool("analytics", handler=offline_pg_handler(DEFAULT_TABLES))
    pools = {"analytics": pool}
    generator = StubSQLGenerator(["SELECT COUNT(*) FROM users;"])
    # Schema is introspected first (pool healthy) so the retries below are spent
    # on the user query itself, not on schema loading.
    components = await build(settings, pools=pools, generator=generator, load_schemas=True)
    pool.reset_attempts()
    pool.failures_before_success = 2  # 连接闪断 2 次后恢复
    result = await run_query(components, "统计用户数", database="analytics")
    field("DB 语句尝试次数（前 2 次连接闪断）", pool.attempts)
    field("重试后返回的行", result.get("data", {}).get("rows") if result["success"] else None)
    check(
        "DB 瞬时连接故障经退避重试后成功",
        result["success"] is True and pool.attempts == 3,
        f"success={result['success']}, attempts={pool.attempts}",
    )
    field(
        "metrics retry_attempts",
        {k: v for k, v in components.metrics.snapshot().items() if "retry" in k},
    )
    await components.shutdown()


async def scenario_9_guards() -> None:
    """Demonstrate the input length guard and confidence threshold."""
    section("校验配置生效：max_question_length / min_confidence_score", "9")

    settings = build_offline_settings(
        databases=("analytics",), max_question_length=64, min_confidence_score=70
    )
    pools = make_pools(["analytics"])
    generator = StubSQLGenerator(["SELECT COUNT(*) FROM users;"])
    validator = StubResultValidator(confidence=40, explanation="列不匹配，仅部分回答了问题。")
    components = await build(
        settings, pools=pools, generator=generator, result_validator=validator, load_schemas=False
    )

    long_question = "请帮我统计" + "非常长的自然语言问题" * 10
    r1 = await run_query(components, long_question, database="analytics")
    check(
        "超长问题在调用 LLM 前被拒绝 (question_too_long)",
        r1["success"] is False and r1["error"]["code"] == "question_too_long",
        f"question_len={len(long_question)}",
    )
    check("超长问题未触发 LLM 调用", generator.calls == [])

    r2 = await run_query(components, "统计用户数", database="analytics")
    field("confidence", r2.get("confidence"))
    field("warning", r2.get("warning"))
    check(
        "低置信度不再伪装成 100，且给出 warning",
        r2["success"] is True
        and r2["confidence"] == 40
        and "below the configured threshold" in (r2.get("warning") or ""),
    )
    await components.shutdown()


async def scenario_10_health_and_metrics() -> None:
    """Demonstrate the health check endpoint and metric instrumentation."""
    section("可观测性：健康检查 + 指标埋点 + request_id 追踪", "10")

    settings = build_offline_settings(
        databases=("analytics", "billing"),
        blocked_tables=("payments",),
    )
    pools = make_pools(["analytics", "billing"])
    generator = StubSQLGenerator(
        [
            "SELECT COUNT(*) FROM users;",
            "SELECT * FROM payments;",
        ]
    )
    metrics = MetricsCollector()
    metrics.reset_all_metrics()
    components = await build(
        settings, pools=pools, generator=generator, metrics=metrics, load_schemas=False
    )
    # 预置 schema，便于健康检查显示 schema_cached
    for name in ("analytics", "billing"):
        await components.schema_cache.load(name, pools[name])
    # billing 随后故意不可用，用于演示 degraded 状态
    pools["billing"].failures_before_success = 99
    field("billing 故障注入", f"pending_failures={pools['billing'].failures_remaining}")

    ok = await run_query(components, "统计用户数", database="analytics")
    denied = await run_query(components, "查支付数据", database="analytics")
    check("成功请求 request_id 已回传", bool(ok.get("request_id")), str(ok.get("request_id")))
    check(
        "失败请求也回传 request_id（全链路可追踪）",
        bool(denied.get("request_id")),
        str(denied.get("request_id")),
    )

    report = await components.health_checker.check()
    emit("  健康检查报告:")
    field("status", report["status"])
    for name, info in report["databases"].items():
        field(f"database[{name}]", info)
    field("circuit_breaker", report["components"]["circuit_breaker"])
    limiter_stats = report["components"]["rate_limiter"]["queries"]
    field("rate_limiter.queries.total_requests", limiter_stats["total_requests"])
    check("健康检查覆盖所有库并标记 degraded", report["status"] == "degraded")

    snapshot = metrics.snapshot()
    emit("  指标快照（节选）:")
    keys_of_interest = (
        "pg_mcp_query_requests_total",
        "pg_mcp_sql_rejected_total",
        "pg_mcp_llm_calls_total",
        "pg_mcp_db_query_duration_seconds_count",
        "pg_mcp_query_duration_seconds_count",
    )
    for key in sorted(snapshot):
        if key.startswith(keys_of_interest):
            field(key, snapshot[key])
    # 注意：snapshot() 的 label 按字典序输出，所以是 database=... 在 status=... 之前。
    expected = {
        'pg_mcp_query_requests_total{database="analytics",status="success"}': 1,
        'pg_mcp_sql_rejected_total{reason="blocked_table"}': 1,
        'pg_mcp_llm_calls_total{operation="generate_sql"}': 2,
        "pg_mcp_db_query_duration_seconds_count": 1,
        'pg_mcp_health_checks_total{status="degraded"}': 1,
    }
    for key, minimum in expected.items():
        actual = snapshot.get(key, 0)
        check(f"指标 {key} >= {minimum}", actual >= minimum, f"actual={actual}")
    await components.shutdown()


async def scenario_11_response_model() -> None:
    """Demonstrate the fixed response model (tokens_used always present)."""
    section("响应/模型缺陷修复：tokens_used 恒存在、ErrorDetail 单一实现", "11")

    from pg_mcp.models.errors import ErrorDetail as ErrorsErrorDetail
    from pg_mcp.models.query import ErrorDetail as QueryErrorDetail
    from pg_mcp.models.query import QueryResponse

    settings = build_offline_settings(databases=("analytics",))
    pools = make_pools(["analytics"])
    generator = StubSQLGenerator(["SELECT 1;"])
    components = await build(settings, pools=pools, generator=generator, load_schemas=False)

    ok = await run_query(components, "连通性检查", database="analytics", return_type=ReturnType.SQL)
    bad = await run_query(components, "不存在的库", database="nope")

    check("成功响应包含 tokens_used", "tokens_used" in ok, f"tokens_used={ok['tokens_used']}")
    check("失败响应也包含 tokens_used", "tokens_used" in bad, f"tokens_used={bad['tokens_used']}")
    check(
        "ErrorDetail 只剩一个实现（query.ErrorDetail is errors.ErrorDetail）",
        QueryErrorDetail is ErrorsErrorDetail,
    )
    to_dict_definitions = sum(1 for klass in QueryResponse.__mro__ if "to_dict" in klass.__dict__)
    check(
        "QueryResponse.to_dict 不再重复定义",
        to_dict_definitions == 1,
        f"definitions={to_dict_definitions}",
    )
    await components.shutdown()


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------
async def main() -> int:
    """Run every scenario and summarize the result.

    Returns:
        int: Process exit code (0 when all checks passed).
    """
    from pg_mcp.observability.logging import configure_logging

    # Keep the console focused on the demo output; metrics/tracing are printed explicitly.
    configure_logging(level="ERROR", log_format="text", enable_sensitive_filter=True)

    emit("=" * 78)
    emit("pg-mcp (W5 研发岗作业) 离线端到端演示")
    emit("无需 PostgreSQL / 无需 OpenAI Key —— 使用内存替身跑真实链路")
    emit("=" * 78)

    components = await scenario_1_startup()
    pools = {name: components.pools[name] for name in components.pools}
    await scenario_2_routing(components, pools)  # type: ignore[arg-type]
    await scenario_3_blocked_table()
    await scenario_4_blocked_column()
    await scenario_5_explain_policy()
    await scenario_6_dml()
    await scenario_7_rate_limit()
    await scenario_8_retry()
    await scenario_9_guards()
    await scenario_10_health_and_metrics()
    await scenario_11_response_model()
    await components.shutdown()

    emit("")
    emit("=" * 78)
    emit(f"汇总: {PASS} 项通过, {FAIL} 项失败")
    emit("=" * 78)

    report_path = PROJECT_ROOT / "W5_DEMO_REPORT.md"
    report_path.write_text(
        "# pg-mcp W5 离线演示报告\n\n"
        "> 由 `uv run python scripts/demo_offline.py` 自动生成，"
        "使用内存替身（无 PostgreSQL / 无 OpenAI Key）跑通真实请求链路。\n\n"
        "```text\n" + OUT.getvalue() + "```\n",
        encoding="utf-8",
    )
    emit(f"报告已写入: {report_path}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    raise SystemExit(asyncio.run(main()))
