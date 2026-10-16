# pg-mcp W5 离线演示报告

> 由 `uv run python scripts/demo_offline.py` 自动生成，使用内存替身（无 PostgreSQL / 无 OpenAI Key）跑通真实请求链路。

```text
==============================================================================
pg-mcp (W5 研发岗作业) 离线端到端演示
无需 PostgreSQL / 无需 OpenAI Key —— 使用内存替身跑真实链路
==============================================================================

==============================================================================
[1] 启动：多数据库注册 + 安全策略/限流配置真正注入组件
==============================================================================
  配置的数据库: ['analytics', 'billing']
  Schema 内省（走真实 SchemaIntrospector + 假连接池）:
        - analytics: 3 张表 -> Database: analytics | PostgreSQL Version: PostgreSQL 16.2 (off
        - billing: 3 张表 -> Database: billing | PostgreSQL Version: PostgreSQL 16.2 (offli
  [OK]   每个数据库都创建了独立连接池
  [OK]   每个数据库都有独立 SQLExecutor
  [OK]   blocked_tables/blocked_columns/allow_explain 已注入 SQLValidator  blocked_tables=['payments'], blocked_columns=['users.password_hash']
  [OK]   限流器按配置生效 (queries=10, llm=5)

==============================================================================
[2] 多数据库路由：访问 B 库的请求绝不会落到 A 库
==============================================================================
  question: billing 库一共有多少订单？ (database='billing')
        - success: True
        - generated_sql: SELECT COUNT(*) FROM users;
        - rows: [{'count': 1}]
        - request_id: 382ffe99-4ee8-4199-bf08-e35fb53ede32
        - analytics 池执行过的 SQL: []
        - billing   池执行过的 SQL: ['SELECT COUNT(*) FROM users LIMIT 10000']
  [OK]   SQL 在 billing 池上执行
  [OK]   analytics 池未被触碰（无跨库串扰）
        - database='reporting' 响应: {'code': 'database_error', 'message': "Database 'reporting' not found", 'details': {'requested_database': 'reporting', 'available_databases': ['analytics', 'billing']}}
  [OK]   未知数据库被拒绝且返回可选库列表

==============================================================================
[3] 安全控制（1/3）：配置的敏感表被真实拦截
==============================================================================
        - SQL 访问 payments: success=False code=security_violation reason=blocked_table
  [OK]   payments 被拒绝 (reason=blocked_table)
        - SQL 访问 public.users: success=False code=security_violation reason=blocked_table
  [OK]   public.users 被拒绝 (reason=blocked_table)
        - metrics sql_rejected: {'pg_mcp_sql_rejected_total{reason="blocked_table"}': 2.0}

==============================================================================
[4] 安全控制（2/3）：敏感列拦截（含表限定名 / 通配）
==============================================================================
  [OK]   users.password_hash 被拒绝  {'reason': 'blocked_column', 'column': 'users.password_hash'}
  [OK]   payments.* 通配拦截 SELECT * 被拒绝  {'reason': 'blocked_column', 'column': 'payments.*'}
  [OK]   正常列仍可访问  sql=SELECT id, email FROM users;

==============================================================================
[5] 安全控制（3/3）：EXPLAIN 策略由配置决定
==============================================================================
  [OK]   SECURITY_ALLOW_EXPLAIN=false -> EXPLAIN 被拒绝  {'reason': 'explain_not_allowed'}
  [OK]   SECURITY_ALLOW_EXPLAIN=true -> EXPLAIN 放行  sql=EXPLAIN SELECT id FROM users;

==============================================================================
[6] 安全控制（补充）：写操作与多语句被拒绝
==============================================================================
  [OK]   DELETE 被拒绝  reason=forbidden_statement
  [OK]   多语句被拒绝  reason=multiple_statements
  [OK]   合法 SELECT 仍可执行  sql=SELECT id, email FROM users LIMIT 5;

==============================================================================
[7] 弹性：速率限制真正作用于 LLM 调用
==============================================================================
        - 3 个并发请求（LLM 并发上限=1）结果: ['success', 'rate_limit_exceeded', 'rate_limit_exceeded']
  [OK]   超出上限的请求被限流拒绝  success=1, rejected=2
        - metrics rate_limit_rejections: {'pg_mcp_query_requests_total{database="analytics",status="rate_limit_exceeded"}': 2.0, 'pg_mcp_rate_limit_rejections_total{limiter="llm"}': 2.0}

==============================================================================
[8] 弹性：重试 + 指数退避（LLM 瞬时故障 / DB 连接闪断）
==============================================================================
        - LLM 调用次数（第 3 次成功）: 3
  [OK]   LLM 瞬时错误经退避重试后成功  sql=SELECT COUNT(*) FROM orders;
        - DB 语句尝试次数（前 2 次连接闪断）: 3
        - 重试后返回的行: [{'count': 1}]
  [OK]   DB 瞬时连接故障经退避重试后成功  success=True, attempts=3
        - metrics retry_attempts: {'pg_mcp_retry_attempts_total{operation="llm.generate_sql",outcome="retried"}': 2.0, 'pg_mcp_retry_attempts_total{operation="db.execute",outcome="retried"}': 2.0}

==============================================================================
[9] 校验配置生效：max_question_length / min_confidence_score
==============================================================================
  [OK]   超长问题在调用 LLM 前被拒绝 (question_too_long)  question_len=105
  [OK]   超长问题未触发 LLM 调用
        - confidence: 40
        - warning: Result confidence 40% is below the configured threshold (70%): 列不匹配，仅部分回答了问题。
  [OK]   低置信度不再伪装成 100，且给出 warning

==============================================================================
[10] 可观测性：健康检查 + 指标埋点 + request_id 追踪
==============================================================================
        - billing 故障注入: pending_failures=99
  [OK]   成功请求 request_id 已回传  3a4206e4-70c5-4637-8fff-a35fc718e689
  [OK]   失败请求也回传 request_id（全链路可追踪）  ed5ee70c-7e10-4c20-8821-f6db038ee7b1
  健康检查报告:
        - status: degraded
        - database[analytics]: {'reachable': True, 'schema_cached': True, 'status': 'up'}
        - database[billing]: {'reachable': False, 'schema_cached': True, 'status': 'down'}
        - circuit_breaker: {'state': <CircuitState.CLOSED: 'closed'>, 'failure_count': 1, 'failure_threshold': 5, 'recovery_timeout': 60.0, 'last_failure_time': 1790578216.472716}
        - rate_limiter.queries.total_requests: 1
  [OK]   健康检查覆盖所有库并标记 degraded
  指标快照（节选）:
        - pg_mcp_db_query_duration_seconds_count: 1.0
        - pg_mcp_llm_calls_total{operation="generate_sql"}: 4.0
        - pg_mcp_llm_calls_total{operation="validate_result"}: 1.0
        - pg_mcp_query_duration_seconds_count: 2.0
        - pg_mcp_query_requests_total{database="analytics",status="security_violation"}: 1.0
        - pg_mcp_query_requests_total{database="analytics",status="success"}: 1.0
        - pg_mcp_sql_rejected_total{reason="blocked_table"}: 3.0
  [OK]   指标 pg_mcp_query_requests_total{database="analytics",status="success"} >= 1  actual=1.0
  [OK]   指标 pg_mcp_sql_rejected_total{reason="blocked_table"} >= 1  actual=3.0
  [OK]   指标 pg_mcp_llm_calls_total{operation="generate_sql"} >= 2  actual=4.0
  [OK]   指标 pg_mcp_db_query_duration_seconds_count >= 1  actual=1.0
  [OK]   指标 pg_mcp_health_checks_total{status="degraded"} >= 1  actual=1.0

==============================================================================
[11] 响应/模型缺陷修复：tokens_used 恒存在、ErrorDetail 单一实现
==============================================================================
  [OK]   成功响应包含 tokens_used  tokens_used=42
  [OK]   失败响应也包含 tokens_used  tokens_used=0
  [OK]   ErrorDetail 只剩一个实现（query.ErrorDetail is errors.ErrorDetail）
  [OK]   QueryResponse.to_dict 不再重复定义  definitions=1

==============================================================================
汇总: 35 项通过, 0 项失败
==============================================================================
```
