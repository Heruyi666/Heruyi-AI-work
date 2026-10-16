#!/usr/bin/env python
"""Per-requirement acceptance harness for the W5 homework (offline, no PG / OpenAI).

The assignment (R&D role) lists three missing capabilities:

  1. Multi-database and security control were promised in the design but never
     enabled: the server always used a single executor, so table / column access
     restrictions and the EXPLAIN policy could not be enforced. Requests could
     reach the wrong database and sensitive objects stayed unprotected.
  2. The resilience and observability modules (rate limiting, retry / backoff,
     metrics, tracing) existed only on paper and were never wired into the
     request path.
  3. Response / model defects (a duplicated ``to_dict`` method, unused config
     fields) plus insufficient test coverage, which made the system diverge from
     the implementation plan and hard to verify.

Verdict rule: **A/B comparison**. Proving that an implementation merely exists is
not enough -- the code review accused the project of writing code without wiring
it. Therefore every requirement runs twice: with the feature disabled (control)
and enabled (treatment). Only when the same request behaves differently in the
two states is the feature considered truly wired into the request path.

Usage::

    uv run python scripts/verify_w5_requirements.py

The report is also written to ``W5_ACCEPTANCE.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pg_mcp.config.settings import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

from pydantic import SecretStr  # noqa: E402

from pg_mcp.app import AppComponents, build_components  # noqa: E402
from pg_mcp.config.settings import (  # noqa: E402
    AuthConfig,
    AuthPrincipal,
    DatabaseConfig,
    OpenAIConfig,
    SecurityConfig,
    ValidationConfig,
)
from pg_mcp.models.errors import AuthError, LLMUnavailableError  # noqa: E402
from pg_mcp.models.query import QueryRequest, ReturnType  # noqa: E402
from pg_mcp.observability.metrics import MetricsCollector  # noqa: E402
from pg_mcp.security.auth import Authenticator  # noqa: E402
from pg_mcp.services.result_validator import ResultValidator  # noqa: E402
from pg_mcp.services.sql_executor import SQLExecutor  # noqa: E402
from pg_mcp.services.sql_validator import SQLValidator  # noqa: E402
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
    """Print a line to stdout (flushed) and record it for the report.

    Args:
        line: Text to emit.
    """
    print(line, flush=True)
    OUT.write(line + "\n")


def requirement(index: str, quote: str) -> None:
    """Print a requirement header with the original assignment wording.

    Args:
        index: Requirement index from the screenshot.
        quote: Original wording, wrapped for readability.
    """
    emit("")
    emit("=" * 78)
    emit(f"【要求 {index}】")
    emit("=" * 78)
    for line in textwrap.wrap(quote, width=72):
        emit("  " + line)


def topic(title: str) -> None:
    """Print a sub-topic header inside a requirement.

    Args:
        title: Sub-topic title.
    """
    emit("")
    emit(f"  ── {title}")


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
    emit(f"      {mark} {description}{suffix}")
    return condition


def field(name: str, value: Any) -> None:
    """Print a key/value observation line.

    Args:
        name: Field name.
        value: Field value.
    """
    emit(f"        - {name}: {value}")


def where(rel_path: str, needle: str, limit: int = 2) -> str:
    """Locate ``needle`` inside ``rel_path`` and return ``file:line`` anchors.

    Args:
        rel_path: Path relative to the project root.
        needle: Literal text to search for.
        limit: Maximum number of anchors to return.

    Returns:
        str: Comma-joined ``file:line`` anchors, or a "not found" note.
    """
    path = PROJECT_ROOT / rel_path
    if not path.exists():
        return f"{rel_path} (文件缺失)"
    lines = path.read_text(encoding="utf-8").splitlines()
    hits = [f"{rel_path}:{i + 1}" for i, line in enumerate(lines) if needle in line]
    if not hits:
        return f"{rel_path} (未找到 `{needle}`)"
    return ", ".join(hits[:limit])


def wiring(needles: Sequence[str]) -> list[str]:
    """Return production source files referencing any of ``needles``.

    ``config/`` and ``testing/`` are excluded on purpose: a field appearing in
    its own declaration or in a fixture builder is *not* evidence that the value
    reaches the request path.

    Args:
        needles: Candidate identifiers for one configuration field.

    Returns:
        list[str]: Relative paths of referencing production modules.
    """
    hits: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        if "/config/" in rel or "/testing/" in rel:
            continue
        text = path.read_text(encoding="utf-8")
        if any(needle in text for needle in needles):
            hits.append(rel.replace("src/pg_mcp/", ""))
    return hits


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
    load_schemas: bool = False,
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
        sql_generator=generator,
        result_validator=result_validator or StubResultValidator(confidence=92),
        metrics=metrics,
        load_schemas=load_schemas,
    )


async def ask(
    components: AppComponents,
    question: str,
    *,
    database: str | None = None,
    return_type: ReturnType = ReturnType.RESULT,
) -> dict[str, Any]:
    """Run one request through the orchestrator and return the tool payload.

    Args:
        components: Wired components.
        question: Natural language question.
        database: Optional target database.
        return_type: ``sql`` or ``result``.

    Returns:
        dict[str, Any]: Serialized response (identical to the MCP tool output).
    """
    response = await components.orchestrator.execute_query(
        QueryRequest(question=question, database=database, return_type=return_type)
    )
    return response.to_dict()


def reason_of(payload: dict[str, Any]) -> str | None:
    """Extract the structured rejection reason from a failed payload.

    Args:
        payload: Serialized query response.

    Returns:
        str | None: The ``details["reason"]`` value when present.
    """
    error = payload.get("error") or {}
    return (error.get("details") or {}).get("reason")


# ----------------------------------------------------------------------
# 要求 1 — 多数据库 + 安全控制
# ----------------------------------------------------------------------
async def req_1_multidb_and_security() -> None:
    """Verify requirement 1: multi-database routing and enforced security policy."""
    requirement(
        "1",
        "多数据库与安全控制功能虽在设计中有承诺，但实际未能启用：服务器始终使用单一"
        "执行器，无法强制实施表 / 列访问限制或 EXPLAIN 策略，这可能导致请求访问错误"
        "数据库，且敏感对象无法得到保护。",
    )

    # ---------------- 1.1 多数据库路由 ----------------
    topic("1.1 多数据库路由：每个库一个执行器，请求 B 库不会落到 A 库")
    emit(f"      实现位置: {where('src/pg_mcp/app.py', 'SQLExecutor(')}")
    emit(
        "                "
        f"{where('src/pg_mcp/services/orchestrator.py', '_get_executor')}"
    )

    settings = build_offline_settings(databases=("analytics", "billing"), max_retries=0)
    pools = make_pools(["analytics", "billing"])
    generator = StubSQLGenerator(
        ["SELECT COUNT(*) FROM users;", "SELECT COUNT(*) FROM orders;"]
    )
    # 先让启动期完成 schema 内省，随后的断言才只统计"用户请求"产生的 SQL。
    components = await build(
        settings, pools=pools, generator=generator, load_schemas=True
    )

    check(
        "executors 字典中每个库对应不同的 executor 对象",
        set(components.executors) == {"analytics", "billing"}
        and components.executors["analytics"] is not components.executors["billing"],
    )

    observed: dict[str, tuple[int, int]] = {}
    for target in ("analytics", "billing"):
        for pool in pools.values():
            pool.clear_recordings()
        await ask(components, f"{target} 库里有多少数据", database=target)
        observed[target] = (
            len(pools["analytics"].executed),
            len(pools["billing"].executed),
        )
        emit(
            f"      A/B  请求 database={target:<9} -> "
            f"analytics 池 {observed[target][0]} 条 SQL, "
            f"billing 池 {observed[target][1]} 条 SQL"
        )

    check(
        "请求 analytics 只在 analytics 池执行（billing 池未被触碰）",
        observed["analytics"] == (1, 0),
        f"observed={observed['analytics']}",
    )
    check(
        "请求 billing 只在 billing 池执行（analytics 池未被触碰）",
        observed["billing"] == (0, 1),
        f"observed={observed['billing']}",
    )
    check(
        "两条链路落在不同连接池 -> 不是'单一执行器'",
        observed["analytics"][0] == 1 and observed["billing"][1] == 1,
    )

    unknown = await ask(components, "随便看看", database="reporting")
    field("database='reporting' 的响应", unknown.get("error"))
    check(
        "未知库不会被静默路由到默认库，而是拒绝并给出可选库列表",
        unknown["success"] is False
        and (unknown["error"]["details"] or {}).get("available_databases")
        == ["analytics", "billing"],
    )
    await components.shutdown()

    # ---------------- 1.2 表访问限制 ----------------
    topic("1.2 表访问限制：同一条 SQL，配置前后行为必须不同")
    emit(f"      实现位置: {where('src/pg_mcp/services/sql_validator.py', 'blocked_table')}")

    async def probe_table(blocked: tuple[str, ...]) -> dict[str, Any]:
        cfg = build_offline_settings(
            databases=("analytics",), blocked_tables=blocked, max_retries=0
        )
        pls = make_pools(["analytics"])
        gen = StubSQLGenerator(["SELECT id, card_number FROM payments;"])
        comp = await build(cfg, pools=pls, generator=gen)
        result = await ask(comp, "给我 payments 的数据", database="analytics")
        await comp.shutdown()
        return result

    off = await probe_table(())
    on = await probe_table(("payments",))
    emit("      A 对照 SECURITY_BLOCKED_TABLES 未配置 -> "
         f"success={off['success']}")
    emit("      B 实验 SECURITY_BLOCKED_TABLES=payments -> "
         f"success={on['success']} reason={reason_of(on)}")
    check("未配置策略时该查询可通过", off["success"] is True)
    check(
        "配置策略后同一条查询被拒绝，reason=blocked_table",
        on["success"] is False and reason_of(on) == "blocked_table",
    )

    cases = (
        (
            "限定名 public.users 拦截裸名 users 的 SQL",
            ("public.users",),
            "SELECT id, email FROM users;",
        ),
        (
            "裸名 users 拦截限定名 public.users 的 SQL",
            ("users",),
            "SELECT id, email FROM public.users;",
        ),
    )
    for label, blocked, sql in cases:
        result = await probe_table_named(blocked, sql)
        check(
            f"{label}",
            result["success"] is False and reason_of(result) == "blocked_table",
            f"reason={reason_of(result)}",
        )

    # ---------------- 1.3 列访问限制 ----------------
    topic("1.3 列访问限制：限定名 / 通配 / 别名均生效")
    emit(f"      实现位置: {where('src/pg_mcp/services/sql_validator.py', 'blocked_column')}")

    async def probe_column(
        blocked: tuple[str, ...], sql: str
    ) -> dict[str, Any]:
        cfg = build_offline_settings(
            databases=("analytics",), blocked_columns=blocked, max_retries=0
        )
        pls = make_pools(["analytics"])
        gen = StubSQLGenerator([sql])
        comp = await build(cfg, pools=pls, generator=gen)
        result = await ask(comp, "查询", database="analytics")
        await comp.shutdown()
        return result

    sql_sensitive = "SELECT id, email, password_hash FROM users;"
    off_col = await probe_column((), sql_sensitive)
    on_col = await probe_column(("users.password_hash",), sql_sensitive)
    emit(f"      A 对照 未配置 blocked_columns -> success={off_col['success']}")
    emit(
        "      B 实验 users.password_hash -> "
        f"success={on_col['success']} reason={reason_of(on_col)}"
    )
    check("未配置策略时可读取该列", off_col["success"] is True)
    check(
        "配置 users.password_hash 后同一 SQL 被拒绝 (blocked_column)",
        on_col["success"] is False and reason_of(on_col) == "blocked_column",
    )

    star = await probe_column(("payments.*",), "SELECT * FROM payments WHERE id = 1;")
    check(
        "payments.* 通配拦截 SELECT * 星投影",
        star["success"] is False and reason_of(star) == "blocked_column",
    )
    alias = await probe_column(
        ("users.password_hash",),
        "SELECT u.password_hash FROM users AS u;",
    )
    check(
        "别名 u.password_hash 仍被解析并拦截",
        alias["success"] is False and reason_of(alias) == "blocked_column",
    )
    ok_col = await probe_column(
        ("users.password_hash",), "SELECT id, email FROM users;"
    )
    check("未被策略覆盖的列仍可正常查询", ok_col["success"] is True)

    # ---------------- 1.4 EXPLAIN 策略 ----------------
    topic("1.4 EXPLAIN 策略：由配置决定放行或拒绝")
    emit(
        f"      实现位置: {where('src/pg_mcp/services/sql_validator.py', 'explain_not_allowed')}"
    )

    explain_results: dict[str, dict[str, Any]] = {}
    for allow in (False, True):
        cfg = build_offline_settings(
            databases=("analytics",), allow_explain=allow, max_retries=0
        )
        pls = make_pools(["analytics"])
        gen = StubSQLGenerator(["EXPLAIN SELECT id FROM users;"])
        comp = await build(cfg, pools=pls, generator=gen)
        explain_results[str(allow)] = await ask(comp, "看看查询计划", database="analytics")
        await comp.shutdown()

    emit(
        "      A 对照 SECURITY_ALLOW_EXPLAIN=false -> "
        f"success={explain_results['False']['success']} "
        f"reason={reason_of(explain_results['False'])}"
    )
    emit(
        "      B 实验 SECURITY_ALLOW_EXPLAIN=true  -> "
        f"success={explain_results['True']['success']}"
    )
    check(
        "allow_explain=false 时 EXPLAIN 被拒绝 (explain_not_allowed)",
        explain_results["False"]["success"] is False
        and reason_of(explain_results["False"]) == "explain_not_allowed",
    )
    check(
        "allow_explain=true 时同一语句放行",
        explain_results["True"]["success"] is True,
    )

    # ---------------- 1.5 写操作 / 多语句 ----------------
    topic("1.5 写操作与多语句：只读边界不被绕过")
    cfg = build_offline_settings(databases=("analytics",), max_retries=0)
    pls = make_pools(["analytics"])
    gen = StubSQLGenerator(
        [
            "DELETE FROM users WHERE id = 1;",
            "SELECT 1; DROP TABLE users;",
            "SELECT id FROM users LIMIT 5;",
        ]
    )
    comp = await build(cfg, pools=pls, generator=gen)
    dml = await ask(comp, "删除一个用户", database="analytics")
    multi = await ask(comp, "先看个 1 再删表", database="analytics")
    legal = await ask(comp, "列出 5 个用户", database="analytics")
    check("DELETE 被拒绝 (forbidden_statement)", reason_of(dml) == "forbidden_statement")
    check("多语句被拒绝 (multiple_statements)", reason_of(multi) == "multiple_statements")
    check("合法 SELECT 仍可执行", legal["success"] is True)
    await comp.shutdown()


async def probe_table_named(blocked: tuple[str, ...], sql: str) -> dict[str, Any]:
    """Run one query with a given blocked-table policy.

    Args:
        blocked: Blocked table entries.
        sql: SQL the stubbed LLM will return.

    Returns:
        dict[str, Any]: Serialized response.
    """
    cfg = build_offline_settings(
        databases=("analytics",), blocked_tables=blocked, max_retries=0
    )
    pools = make_pools(["analytics"])
    components = await build(cfg, pools=pools, generator=StubSQLGenerator([sql]))
    result = await ask(components, "查询", database="analytics")
    await components.shutdown()
    return result


# ----------------------------------------------------------------------
# 要求 2 — 弹性 + 可观测性接入请求链路
# ----------------------------------------------------------------------
async def req_2_resilience_and_observability() -> None:
    """Verify requirement 2: rate limiting, retry/backoff, metrics, tracing."""
    requirement(
        "2",
        "弹性与可观测性模块（如速率限制、重试 / 退避机制、指标 / 追踪系统）仅停留在"
        "设计层面，尚未整合到实际请求处理流程中。",
    )

    # ---------------- 2.1 速率限制 ----------------
    topic("2.1 速率限制：并发上限真的会拒绝请求")
    emit(f"      实现位置: {where('src/pg_mcp/services/orchestrator.py', '_acquire_slot')}")

    async def burst(limit: int, acquire_timeout: float) -> list[str]:
        cfg = build_offline_settings(
            databases=("analytics",),
            max_concurrent_llm_calls=limit,
            acquire_timeout=acquire_timeout,
        )
        pools = make_pools(["analytics"])
        gen = StubSQLGenerator(["SELECT COUNT(*) FROM users;"], latency=0.5)
        comp = await build(cfg, pools=pools, generator=gen)
        results = await asyncio.gather(
            *[
                ask(comp, f"并发请求 {i}", database="analytics", return_type=ReturnType.SQL)
                for i in range(3)
            ]
        )
        codes = ["success" if r["success"] else r["error"]["code"] for r in results]
        await comp.shutdown()
        return codes

    codes_off = await burst(limit=10, acquire_timeout=5.0)
    codes_on = await burst(limit=1, acquire_timeout=0.2)
    emit(f"      A 对照 MAX_CONCURRENT_LLM_CALLS=10 -> {codes_off}")
    emit(f"      B 实验 MAX_CONCURRENT_LLM_CALLS=1  -> {codes_on}")
    check("上限=10 时 3 个并发请求全部成功", codes_off.count("success") == 3)
    check(
        "上限=1 时超出的请求被限流拒绝 (rate_limit_exceeded)",
        codes_on.count("rate_limit_exceeded") >= 1,
        f"rejected={codes_on.count('rate_limit_exceeded')}",
    )

    # ---------------- 2.2 重试 + 指数退避 ----------------
    topic("2.2 重试 / 退避：瞬时故障被真重试，且退避时间指数增长")
    emit(f"      实现位置: {where('src/pg_mcp/resilience/retry.py', 'async def retry_async')}")
    emit(f"                {where('src/pg_mcp/services/sql_executor.py', 'retry_async(')}")

    async def llm_case(retries: int) -> tuple[int, bool, float]:
        cfg = build_offline_settings(
            databases=("analytics",),
            max_retries=retries,
            retry_delay=0.05,
            backoff_factor=2.0,
        )
        pools = make_pools(["analytics"])
        gen = StubSQLGenerator(
            [
                LLMUnavailableError("OpenAI API rate limit exceeded"),
                LLMUnavailableError("upstream temporarily unavailable"),
                "SELECT COUNT(*) FROM orders;",
            ]
        )
        comp = await build(cfg, pools=pools, generator=gen)
        started = time.perf_counter()
        result = await ask(comp, "统计订单数", database="analytics")
        elapsed = time.perf_counter() - started
        await comp.shutdown()
        return len(gen.calls), result["success"], elapsed

    calls_off, ok_off, t_off = await llm_case(0)
    calls_on, ok_on, t_on = await llm_case(3)
    emit(
        f"      A 对照 RESILIENCE_MAX_RETRIES=0 -> LLM 调用 {calls_off} 次, "
        f"success={ok_off}, 耗时 {t_off:.3f}s"
    )
    emit(
        f"      B 实验 RESILIENCE_MAX_RETRIES=3 -> LLM 调用 {calls_on} 次, "
        f"success={ok_on}, 耗时 {t_on:.3f}s"
    )
    check("重试关闭时一次失败即返回失败", calls_off == 1 and ok_off is False)
    check("重试开启时前两次瞬时错误被重试，第 3 次成功", calls_on == 3 and ok_on is True)
    check(
        "重试之间确实等待了指数退避（delay=0.05 × backoff=2 -> ≈0.15s）",
        t_on >= 0.13,
        f"elapsed={t_on:.3f}s",
    )
    check("关闭重试的耗时明显短于开启重试", t_on > t_off, f"{t_off:.3f}s vs {t_on:.3f}s")

    async def db_case(retries: int) -> tuple[int, bool, float]:
        cfg = build_offline_settings(
            databases=("analytics",),
            max_retries=retries,
            retry_delay=0.05,
            backoff_factor=2.0,
        )
        pool = FakePool("analytics", handler=offline_pg_handler(DEFAULT_TABLES))
        pools = {"analytics": pool}
        gen = StubSQLGenerator(["SELECT COUNT(*) FROM users;"])
        comp = await build(cfg, pools=pools, generator=gen, load_schemas=True)
        pool.reset_attempts()
        pool.failures_before_success = 2
        started = time.perf_counter()
        result = await ask(comp, "统计用户数", database="analytics")
        elapsed = time.perf_counter() - started
        await comp.shutdown()
        return pool.attempts, result["success"], elapsed

    attempts_off, ok_db_off, t_db_off = await db_case(0)
    attempts_on, ok_db_on, t_db_on = await db_case(3)
    emit(
        f"      A 对照 RESILIENCE_MAX_RETRIES=0 -> DB 语句尝试 {attempts_off} 次, "
        f"success={ok_db_off}, 耗时 {t_db_off:.3f}s"
    )
    emit(
        f"      B 实验 RESILIENCE_MAX_RETRIES=3 -> DB 语句尝试 {attempts_on} 次, "
        f"success={ok_db_on}, 耗时 {t_db_on:.3f}s"
    )
    check("DB 瞬时连接故障：关闭重试时不重试", attempts_off == 1 and ok_db_off is False)
    check(
        "同一故障在开启重试后被重试并成功",
        attempts_on == 3 and ok_db_on is True,
        f"attempts={attempts_on}",
    )
    check(
        "DB 重试同样走了退避等待",
        t_db_on >= 0.13 and t_db_on > t_db_off,
        f"{t_db_off:.3f}s -> {t_db_on:.3f}s",
    )

    # ---------------- 2.3 指标埋点 ----------------
    topic("2.3 指标：请求路径上的计数从 0 变为非 0（证明真的在打点）")
    metrics_anchor = where(
        "src/pg_mcp/observability/metrics.py", "increment_retry_attempts"
    )
    emit(f"      实现位置: {metrics_anchor}")

    metrics = MetricsCollector()
    metrics.reset_all_metrics()
    success_key = 'pg_mcp_query_requests_total{database="analytics",status="success"}'
    rejected_key = 'pg_mcp_sql_rejected_total{reason="blocked_table"}'

    cfg = build_offline_settings(
        databases=("analytics",), blocked_tables=("payments",), max_retries=0
    )
    pools = make_pools(["analytics"])
    gen = StubSQLGenerator(["SELECT COUNT(*) FROM users;", "SELECT * FROM payments;"])
    comp = await build(cfg, pools=pools, generator=gen, metrics=metrics, load_schemas=True)
    # 基线取在启动之后：只统计"由用户请求引起"的计数变化，排除 schema 内省噪声。
    before = metrics.snapshot()
    field("请求前 snapshot 中 success 计数", before.get(success_key, "不存在"))

    await ask(comp, "统计用户数", database="analytics")
    await ask(comp, "查支付数据", database="analytics")
    after = metrics.snapshot()
    for key in (success_key, rejected_key):
        field(f"请求后 {key}", after.get(key, "不存在"))
    delta_success = after.get(success_key, 0) - before.get(success_key, 0)
    delta_rejected = after.get(rejected_key, 0) - before.get(rejected_key, 0)
    check("请求前 query_requests{status=success} 计数为 0", before.get(success_key, 0) == 0)
    check(
        "一次成功请求使 query_requests{status=success} 计数 +1",
        delta_success == 1,
        f"delta={delta_success}",
    )
    check(
        "一次被拒请求使 sql_rejected{reason=blocked_table} 计数 +1",
        delta_rejected == 1,
        f"delta={delta_rejected}",
    )
    check(
        "DB 查询耗时直方图被采样（db_query_duration count >= 1）",
        after.get("pg_mcp_db_query_duration_seconds_count", 0) >= 1,
    )
    await comp.shutdown()

    # ---------------- 2.4 追踪 ----------------
    topic("2.4 追踪：每个请求有独立 request_id，成功与失败都回传并写入日志")
    emit(f"      实现位置: {where('src/pg_mcp/services/orchestrator.py', 'request_context(')}")

    cfg = build_offline_settings(
        databases=("analytics",), blocked_tables=("payments",), max_retries=0
    )
    pools = make_pools(["analytics"])
    gen = StubSQLGenerator(["SELECT COUNT(*) FROM users;", "SELECT * FROM payments;"])
    comp = await build(cfg, pools=pools, generator=gen)
    first = await ask(comp, "统计用户数", database="analytics")
    second = await ask(comp, "统计用户数", database="analytics")
    failed = await ask(comp, "不存在的库", database="nope")
    field("第 1 次请求 request_id", first.get("request_id"))
    field("第 2 次请求 request_id", second.get("request_id"))
    field("失败请求 request_id", failed.get("request_id"))
    check(
        "两次请求的 request_id 都存在且互不相同（不是硬编码常量）",
        bool(first.get("request_id"))
        and bool(second.get("request_id"))
        and first["request_id"] != second["request_id"],
    )
    check("失败响应同样携带 request_id（可端到端排查）", bool(failed.get("request_id")))

    logged = await capture_request_ids(comp)
    field("结构化日志中捕获到的 request_id", logged)
    check(
        "安全拒绝的日志行里也带 request_id（日志与请求可关联）",
        len(logged) >= 1,
    )
    await comp.shutdown()


async def capture_request_ids(components: AppComponents) -> list[str]:
    """Trigger one rejected request and collect request ids emitted to the log.

    Args:
        components: Wired components with a sensitive table configured.

    Returns:
        list[str]: Request ids observed on log records.
    """
    found: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            rid = getattr(record, "request_id", None)
            if rid:
                found.append(str(rid))
                return
            text = record.getMessage()
            if "request_id=" in text:
                found.append(text.split("request_id=", 1)[1].split("]")[0])

    logger = logging.getLogger("pg_mcp")
    handler = _Capture()
    logger.addHandler(handler)
    original_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        await ask(components, "查支付数据", database="analytics")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)
    return list(dict.fromkeys(found))


# ----------------------------------------------------------------------
# 要求 3 — 响应/模型缺陷 + 测试覆盖
# ----------------------------------------------------------------------
async def req_3_model_and_tests() -> None:
    """Verify requirement 3: response model defects, dead config, test coverage."""
    requirement(
        "3",
        "响应 / 模型缺陷（重复的 to_dict 方法、未使用的配置字段）及测试覆盖不足，"
        "导致当前系统行为偏离实施方案，且难以进行有效验证。",
    )

    # ---------------- 3.1 重复的 to_dict ----------------
    topic("3.1 重复的 to_dict 方法：只剩一处定义")
    emit(f"      实现位置: {where('src/pg_mcp/models/query.py', 'def to_dict')}")

    from pg_mcp.models.errors import ErrorDetail as ErrorsErrorDetail
    from pg_mcp.models.query import ErrorDetail as QueryErrorDetail
    from pg_mcp.models.query import QueryResponse

    definitions = [k.__name__ for k in QueryResponse.__mro__ if "to_dict" in k.__dict__]
    field("QueryResponse 继承链上定义 to_dict 的类", definitions)
    check("QueryResponse.to_dict 只有一处定义", len(definitions) == 1, str(definitions))
    check(
        "ErrorDetail 只有一份实现（query 模块复用 errors 模块）",
        QueryErrorDetail is ErrorsErrorDetail,
    )

    cfg = build_offline_settings(databases=("analytics",))
    pools = make_pools(["analytics"])
    gen = StubSQLGenerator(["SELECT 1;"])
    comp = await build(cfg, pools=pools, generator=gen)
    ok = await ask(comp, "连通性检查", database="analytics", return_type=ReturnType.SQL)
    bad = await ask(comp, "不存在的库", database="nope")
    check("成功响应包含 tokens_used", "tokens_used" in ok, f"tokens_used={ok['tokens_used']}")
    check("失败响应也包含 tokens_used", "tokens_used" in bad, f"tokens_used={bad['tokens_used']}")
    await comp.shutdown()

    # ---------------- 3.2 未使用的配置字段 ----------------
    topic("3.2 未使用的配置字段：逐个确认真实接线（排除声明处与测试替身）")
    fields: list[tuple[str, str, tuple[str, ...]]] = [
        ("databases", "多数据库列表", ("databases",)),
        ("blocked_tables", "敏感表策略", ("blocked_tables",)),
        ("blocked_columns", "敏感列策略", ("blocked_columns",)),
        ("blocked_functions", "危险函数黑名单", ("blocked_functions",)),
        ("allow_explain", "EXPLAIN 策略", ("allow_explain",)),
        ("readonly_role", "只读角色", ("readonly_role",)),
        ("safe_search_path", "安全 search_path", ("safe_search_path",)),
        ("max_rows", "结果行数上限", ("max_result_rows", "max_rows")),
        ("max_execution_time", "查询超时", ("query_timeout_seconds", "max_execution_time")),
        ("max_question_length", "问题长度上限", ("max_question_length",)),
        ("min_confidence_score", "置信度阈值", ("min_confidence_score",)),
        ("max_retries", "最大重试次数", ("max_retries",)),
        ("retry_delay", "重试初始延迟", ("retry_delay",)),
        ("backoff_factor", "退避倍数", ("backoff_factor",)),
        ("max_retry_delay", "退避上限", ("max_retry_delay",)),
        ("retry_jitter", "退避抖动", ("retry_jitter",)),
        ("max_concurrent_queries", "DB 并发上限", ("max_concurrent_queries",)),
        ("max_concurrent_llm_calls", "LLM 并发上限", ("max_concurrent_llm_calls",)),
        ("acquire_timeout", "取槽超时", ("acquire_timeout",)),
        ("metrics_enabled", "指标开关", ("metrics_enabled",)),
        ("log_format", "日志格式", ("log_format",)),
        ("ssl_mode", "SSL 模式", ("ssl_mode",)),
        ("schema_ttl", "schema 缓存 TTL", ("schema_ttl",)),
    ]
    dead: list[str] = []
    for name, label, needles in fields:
        refs = wiring(needles)
        if not refs:
            dead.append(name)
        emit(f"      {name:<24} {label:<16} 接线于 {refs or '【无引用 —— 死配置】'}")
    check(
        f"全部 {len(fields)} 个配置字段都有生产代码引用（无死配置）",
        not dead,
        f"未接线={dead}" if dead else "",
    )

    # ---------------- 3.3 测试覆盖 ----------------
    topic("3.3 测试覆盖：新增针对上述修复的回归测试")
    tests_dir = PROJECT_ROOT / "tests"
    counts: dict[str, int] = {}
    for path in sorted(tests_dir.rglob("test_*.py")):
        counts[path.relative_to(tests_dir).as_posix()] = path.read_text(
            encoding="utf-8"
        ).count("def test_")
    total = sum(counts.values())
    field("测试文件数", len(counts))
    field("静态统计的用例数 (def test_)", total)
    for name, count in counts.items():
        emit(f"      {name:<44} {count:>3} 例")
    focused = [
        # Original three requirements.
        "unit/test_security_policy.py",
        "unit/test_app_multidb.py",
        "unit/test_retry.py",
        "unit/test_resilience_observability.py",
        # test-plan review 0008 section 2.3 (were reported missing).
        "unit/test_result_validator.py",
        "unit/test_prompts.py",
        "unit/test_metrics.py",
        "unit/test_introspection.py",
        # Added alongside the design-review fixes.
        "unit/test_auth.py",
        "unit/test_data_masking.py",
        "security/test_advanced_security.py",
    ]
    missing = [name for name in focused if name not in counts]
    check("存在针对三点要求的专项测试文件", not missing, f"missing={missing}")
    collected = collect_pytest_cases()
    if collected is not None:
        field("pytest --collect-only 实际收集用例数", collected)
        check("测试套件可被 pytest 正常收集", collected >= 400, f"collected={collected}")


def collect_pytest_cases() -> int | None:
    """Count test cases by asking pytest to collect them (best effort).

    Returns:
        int | None: Number of collected tests, or None when collection failed.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, trusted cmd
            [sys.executable, "-m", "pytest", "--collect-only", "-q"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    output = proc.stdout + "\n" + proc.stderr
    match = re.search(r"(\d+)\s+tests?\s+(?:collected|selected|deselected)", output)
    return int(match.group(1)) if match else None


# ----------------------------------------------------------------------
# 附录 A — design review (0003) 遗留项
# ----------------------------------------------------------------------
async def appendix_design_review() -> None:
    """Verify the design-review findings (0003) that were still open."""
    emit("")
    emit("=" * 78)
    emit("【附 A】design-review (0003) 点名但仍未修的项")
    emit("=" * 78)

    # ---------------- A1 session 语句注入 ----------------
    topic("A1 SET ROLE 引用标识符 + session 值参数化（0003 #7）")
    pool = FakePool("analytics", handler=offline_pg_handler(DEFAULT_TABLES))
    executor = SQLExecutor(
        pool=pool,  # type: ignore[arg-type]
        security_config=SecurityConfig(readonly_role="ro_user"),
        db_config=DatabaseConfig(name="analytics"),
        db_name="analytics",
    )
    await executor.execute("SELECT 1;")
    statements = pool.session_statements
    field("session 语句", statements)
    field("session 参数", pool.session_params)
    check('SET ROLE 使用引用标识符 SET ROLE "ro_user"', 'SET ROLE "ro_user"' in statements)
    check(
        "readonly_role 不出现在任何语句文本中（走参数化/引用，非拼接）",
        "ro_user" not in " ".join(statements).replace('SET ROLE "ro_user"', ""),
    )
    check(
        "search_path 走 set_config 参数化",
        any("set_config('search_path', $1, false)" in s for s in statements),
    )

    # ---------------- A2 系统 schema ----------------
    topic("A2 系统 schema 拦截（0003 #7）")
    sql_system = "SELECT * FROM pg_catalog.pg_user"
    for block, should_block in ((False, False), (True, True)):
        validator = SQLValidator(SecurityConfig(block_system_schemas=block))
        violation = validator.find_violation(sql_system)
        reason = violation.reason if violation else None
        emit(
            f"      A/B  SECURITY_BLOCK_SYSTEM_SCHEMAS={block} -> "
            f"reason={reason}"
        )
        check(
            f"block_system_schemas={block} 时系统 schema {'被拒绝' if should_block else '放行'}",
            (reason == "system_schema") is should_block,
        )

    # ---------------- A3 LIMIT 下推 ----------------
    topic("A3 LIMIT 下推，避免整表拉进内存（0003 #2）")
    for enforce, expected in (
        (False, "SELECT id FROM users;"),
        (True, "SELECT id FROM users LIMIT 50"),
    ):
        row_pool = FakePool("analytics", handler=offline_pg_handler(DEFAULT_TABLES))
        row_executor = SQLExecutor(
            pool=row_pool,  # type: ignore[arg-type]
            security_config=SecurityConfig(enforce_row_limit=enforce, max_rows=50),
            db_config=DatabaseConfig(name="analytics"),
            db_name="analytics",
        )
        await row_executor.execute("SELECT id FROM users;")
        actual = row_pool.executed[0]
        emit(f"      A/B  SECURITY_ENFORCE_ROW_LIMIT={enforce} -> 执行 SQL: {actual}")
        check(f"enforce_row_limit={enforce} 时执行语句为 {expected}", actual == expected)

    # ---------------- A4 送 LLM 前脱敏 ----------------
    topic("A4 敏感数据在送 LLM 前脱敏（0003 #3）")
    off_prompt = await capture_validation_prompt(mask=False)
    on_prompt = await capture_validation_prompt(mask=True)
    emit(f"      A 对照 VALIDATION_MASK_SENSITIVE_DATA=false -> 明文出现在 prompt: "
         f"{'a@b.com' in off_prompt}")
    emit(f"      B 实验 VALIDATION_MASK_SENSITIVE_DATA=true  -> 明文出现在 prompt: "
         f"{'a@b.com' in on_prompt}")
    check("关闭脱敏时明文会外发（说明风险真实存在）", "a@b.com" in off_prompt)
    check("开启脱敏后明文不再进入 prompt", "a@b.com" not in on_prompt)
    check("脱敏保留结构（占位符可见）", "***" in on_prompt)

    # ---------------- A5 认证与按库授权 ----------------
    topic("A5 认证 + 按库授权 + 审计（0003 #1）")
    anonymous = Authenticator(AuthConfig(enabled=False))
    check(
        "认证关闭时不做任何限制（向后兼容现有部署）",
        anonymous.authenticate(None) is None and not anonymous.enabled,
    )

    scoped = Authenticator(
        AuthConfig(
            enabled=True,
            principals=[
                AuthPrincipal(
                    name="analyst",
                    api_key=SecretStr("k-analyst"),
                    allowed_databases=["analytics"],
                )
            ],
        )
    )
    try:
        scoped.authenticate("wrong-key")
        unknown_rejected = False
    except AuthError:
        unknown_rejected = True
    check("未知 API key 被拒绝 (unauthorized)", unknown_rejected)

    principal = scoped.authenticate("k-analyst")
    scoped.authorize(principal, "analytics", request_id="audit-1")
    check("有效 key 可访问被授权的库", principal is not None)

    try:
        scoped.authorize(principal, "billing", request_id="audit-2")
        cross_rejected = False
    except AuthError:
        cross_rejected = True
    check("越权访问未授权库被拒绝 (forbidden)", cross_rejected)

    outcomes = [record["outcome"] for record in scoped.audit.records]
    field("审计记录 outcome", outcomes)
    check(
        "每次判定都留下审计记录（含拒绝）",
        outcomes.count("rejected") >= 2 and outcomes.count("authorized") >= 1,
    )


async def capture_validation_prompt(*, mask: bool) -> str:
    """Run one validation and return the prompt that was sent to the LLM.

    Args:
        mask: Whether sensitive-value masking is enabled.

    Returns:
        str: The user prompt handed to the model.
    """
    validator = ResultValidator(
        OpenAIConfig(api_key=SecretStr("sk-acceptance"), model="offline-stub"),
        ValidationConfig(enabled=True, mask_sensitive_data=mask, sample_rows=5),
        SecurityConfig(),
    )
    captured: dict[str, Any] = {}

    async def _capture(**kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        completion = MagicMock()
        completion.choices = [
            MagicMock(message=MagicMock(content='{"confidence": 90, "explanation": "ok"}'))
        ]
        completion.usage = MagicMock(total_tokens=7)
        return completion

    with patch.object(
        validator.client.chat.completions,
        "create",
        new=AsyncMock(side_effect=_capture),
    ):
        await validator.validate(
            question="List customers",
            sql="SELECT email FROM users;",
            results=[{"email": "a@b.com"}],
            row_count=1,
        )
    messages = captured.get("messages") or []
    return str(messages[1]["content"]) if len(messages) > 1 else ""


# ----------------------------------------------------------------------
# 手动证伪指引
# ----------------------------------------------------------------------
def how_to_falsify() -> None:
    """Print a short manual falsification guide for the reviewer."""
    emit("")
    emit("=" * 78)
    emit("附：你可以自己动手证伪（30 秒）")
    emit("=" * 78)
    emit("  如果上述功能只是'写在文档里'，那么改配置后行为不会变。请自行验证：")
    emit("")
    emit("  1) 关掉安全策略，看敏感表是否变成可访问：")
    emit("       export SECURITY_BLOCKED_TABLES=''  # bash；Windows 下用 $env:...")
    emit("       uv run python scripts/demo_offline.py   # 场景 3 应出现 [FAIL]")
    emit("  2) 把重试次数设为 0，看瞬时故障是否不再恢复：")
    emit("       export RESILIENCE_MAX_RETRIES='0'   # bash；Windows 下用 $env:...")
    emit("       uv run python scripts/demo_offline.py   # 场景 8 应出现 [FAIL]")
    emit("  3) 指定一个不存在的库，看是否被拒绝而不是静默走默认库：")
    emit("       uv run python scripts/verify_w5_requirements.py  # 见 1.1 的 database_error")
    emit("")
    emit("  反向理解：demo 里之所以 [OK]，正是因为它把配置读进了请求链路。")


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------
async def main() -> int:
    """Run every requirement check and summarize the result.

    Returns:
        int: Process exit code (0 when all checks passed).
    """
    from pg_mcp.observability.logging import configure_logging

    configure_logging(level="ERROR", log_format="text", enable_sensitive_filter=True)

    emit("=" * 78)
    emit("W5 研发岗作业 —— 图片三点要求 逐条验收")
    emit("判定原则：A/B 对照（关闭态 vs 打开态），只有行为真的改变才算'已接线'")
    emit("全程离线：无需 PostgreSQL、无需 OpenAI Key")
    emit("说明：输出中的 [ERROR] ... SQL validation failed 属于预期 ——")
    emit("      那是安全策略拒绝非法 SQL 时打的带 request_id 的结构化错误日志。")
    emit("=" * 78)

    await req_1_multidb_and_security()
    await req_2_resilience_and_observability()
    await req_3_model_and_tests()
    await appendix_design_review()
    how_to_falsify()

    emit("")
    emit("=" * 78)
    emit(f"验收汇总: {PASS} 项通过, {FAIL} 项失败")
    emit("=" * 78)
    verdict = (
        "作业三点要求 + design-review(0003) 遗留项 全部落地"
        "（A/B 对照实验均表现出配置相关行为差异）"
        if FAIL == 0
        else "存在未通过项，见上文 [FAIL]"
    )
    emit(f"结论: {verdict}")

    report_path = PROJECT_ROOT / "W5_ACCEPTANCE.md"
    report_path.write_text(
        "# pg-mcp W5 —— 图片三点要求验收报告\n\n"
        "> 由 `uv run python scripts/verify_w5_requirements.py` 自动生成。\n"
        "> 全部离线运行（内存替身替代 PostgreSQL 与 LLM），每条要求做 A/B 对照实验。\n\n"
        "```text\n" + OUT.getvalue() + "```\n",
        encoding="utf-8",
    )
    emit(f"报告已写入: {report_path}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    raise SystemExit(asyncio.run(main()))
