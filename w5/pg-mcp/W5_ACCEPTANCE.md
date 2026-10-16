# pg-mcp W5 —— 图片三点要求验收报告

> 由 `uv run python scripts/verify_w5_requirements.py` 自动生成。
> 全部离线运行（内存替身替代 PostgreSQL 与 LLM），每条要求做 A/B 对照实验。

```text
==============================================================================
W5 研发岗作业 —— 图片三点要求 逐条验收
判定原则：A/B 对照（关闭态 vs 打开态），只有行为真的改变才算'已接线'
全程离线：无需 PostgreSQL、无需 OpenAI Key
说明：输出中的 [ERROR] ... SQL validation failed 属于预期 ——
      那是安全策略拒绝非法 SQL 时打的带 request_id 的结构化错误日志。
==============================================================================

==============================================================================
【要求 1】
==============================================================================
  多数据库与安全控制功能虽在设计中有承诺，但实际未能启用：服务器始终使用单一执行器，无法强制实施表 / 列访问限制或 EXPLAIN
  策略，这可能导致请求访问错误数据库，且敏感对象无法得到保护。

  ── 1.1 多数据库路由：每个库一个执行器，请求 B 库不会落到 A 库
      实现位置: src/pg_mcp/app.py:198
                src/pg_mcp/services/orchestrator.py:222, src/pg_mcp/services/orchestrator.py:491
      [OK]   executors 字典中每个库对应不同的 executor 对象
      A/B  请求 database=analytics -> analytics 池 1 条 SQL, billing 池 0 条 SQL
      A/B  请求 database=billing   -> analytics 池 0 条 SQL, billing 池 1 条 SQL
      [OK]   请求 analytics 只在 analytics 池执行（billing 池未被触碰）  observed=(1, 0)
      [OK]   请求 billing 只在 billing 池执行（analytics 池未被触碰）  observed=(0, 1)
      [OK]   两条链路落在不同连接池 -> 不是'单一执行器'
        - database='reporting' 的响应: {'code': 'database_error', 'message': "Database 'reporting' not found", 'details': {'requested_database': 'reporting', 'available_databases': ['analytics', 'billing']}}
      [OK]   未知库不会被静默路由到默认库，而是拒绝并给出可选库列表

  ── 1.2 表访问限制：同一条 SQL，配置前后行为必须不同
      实现位置: src/pg_mcp/services/sql_validator.py:8, src/pg_mcp/services/sql_validator.py:109
      A 对照 SECURITY_BLOCKED_TABLES 未配置 -> success=True
      B 实验 SECURITY_BLOCKED_TABLES=payments -> success=False reason=blocked_table
      [OK]   未配置策略时该查询可通过
      [OK]   配置策略后同一条查询被拒绝，reason=blocked_table
      [OK]   限定名 public.users 拦截裸名 users 的 SQL  reason=blocked_table
      [OK]   裸名 users 拦截限定名 public.users 的 SQL  reason=blocked_table

  ── 1.3 列访问限制：限定名 / 通配 / 别名均生效
      实现位置: src/pg_mcp/services/sql_validator.py:8, src/pg_mcp/services/sql_validator.py:110
      A 对照 未配置 blocked_columns -> success=True
      B 实验 users.password_hash -> success=False reason=blocked_column
      [OK]   未配置策略时可读取该列
      [OK]   配置 users.password_hash 后同一 SQL 被拒绝 (blocked_column)
      [OK]   payments.* 通配拦截 SELECT * 星投影
      [OK]   别名 u.password_hash 仍被解析并拦截
      [OK]   未被策略覆盖的列仍可正常查询

  ── 1.4 EXPLAIN 策略：由配置决定放行或拒绝
      实现位置: src/pg_mcp/services/sql_validator.py:351
      A 对照 SECURITY_ALLOW_EXPLAIN=false -> success=False reason=explain_not_allowed
      B 实验 SECURITY_ALLOW_EXPLAIN=true  -> success=True
      [OK]   allow_explain=false 时 EXPLAIN 被拒绝 (explain_not_allowed)
      [OK]   allow_explain=true 时同一语句放行

  ── 1.5 写操作与多语句：只读边界不被绕过
      [OK]   DELETE 被拒绝 (forbidden_statement)
      [OK]   多语句被拒绝 (multiple_statements)
      [OK]   合法 SELECT 仍可执行

==============================================================================
【要求 2】
==============================================================================
  弹性与可观测性模块（如速率限制、重试 / 退避机制、指标 / 追踪系统）仅停留在设计层面，尚未整合到实际请求处理流程中。

  ── 2.1 速率限制：并发上限真的会拒绝请求
      实现位置: src/pg_mcp/services/orchestrator.py:291, src/pg_mcp/services/orchestrator.py:436
      A 对照 MAX_CONCURRENT_LLM_CALLS=10 -> ['success', 'success', 'success']
      B 实验 MAX_CONCURRENT_LLM_CALLS=1  -> ['success', 'rate_limit_exceeded', 'rate_limit_exceeded']
      [OK]   上限=10 时 3 个并发请求全部成功
      [OK]   上限=1 时超出的请求被限流拒绝 (rate_limit_exceeded)  rejected=2

  ── 2.2 重试 / 退避：瞬时故障被真重试，且退避时间指数增长
      实现位置: src/pg_mcp/resilience/retry.py:130
                src/pg_mcp/services/sql_executor.py:123
      A 对照 RESILIENCE_MAX_RETRIES=0 -> LLM 调用 1 次, success=False, 耗时 0.001s
      B 实验 RESILIENCE_MAX_RETRIES=3 -> LLM 调用 3 次, success=True, 耗时 0.185s
      [OK]   重试关闭时一次失败即返回失败
      [OK]   重试开启时前两次瞬时错误被重试，第 3 次成功
      [OK]   重试之间确实等待了指数退避（delay=0.05 × backoff=2 -> ≈0.15s）  elapsed=0.185s
      [OK]   关闭重试的耗时明显短于开启重试  0.001s vs 0.185s
      A 对照 RESILIENCE_MAX_RETRIES=0 -> DB 语句尝试 1 次, success=False, 耗时 0.001s
      B 实验 RESILIENCE_MAX_RETRIES=3 -> DB 语句尝试 3 次, success=True, 耗时 0.183s
      [OK]   DB 瞬时连接故障：关闭重试时不重试
      [OK]   同一故障在开启重试后被重试并成功  attempts=3
      [OK]   DB 重试同样走了退避等待  0.001s -> 0.183s

  ── 2.3 指标：请求路径上的计数从 0 变为非 0（证明真的在打点）
      实现位置: src/pg_mcp/observability/metrics.py:245
        - 请求前 snapshot 中 success 计数: 不存在
        - 请求后 pg_mcp_query_requests_total{database="analytics",status="success"}: 1.0
        - 请求后 pg_mcp_sql_rejected_total{reason="blocked_table"}: 1.0
      [OK]   请求前 query_requests{status=success} 计数为 0
      [OK]   一次成功请求使 query_requests{status=success} 计数 +1  delta=1.0
      [OK]   一次被拒请求使 sql_rejected{reason=blocked_table} 计数 +1  delta=1.0
      [OK]   DB 查询耗时直方图被采样（db_query_duration count >= 1）

  ── 2.4 追踪：每个请求有独立 request_id，成功与失败都回传并写入日志
      实现位置: src/pg_mcp/services/orchestrator.py:187
        - 第 1 次请求 request_id: 261cd74e-e9e7-4c46-a0a3-3bcaad91b1e0
        - 第 2 次请求 request_id: c7a1d4e8-9a62-4423-96c8-6c3e0f2ab571
        - 失败请求 request_id: 086ae563-ed1b-4d85-9c8f-9c2bdf942d8e
      [OK]   两次请求的 request_id 都存在且互不相同（不是硬编码常量）
      [OK]   失败响应同样携带 request_id（可端到端排查）
        - 结构化日志中捕获到的 request_id: ['8e71fe2a-9ad3-4e9b-a465-11bab76e8cec']
      [OK]   安全拒绝的日志行里也带 request_id（日志与请求可关联）

==============================================================================
【要求 3】
==============================================================================
  响应 / 模型缺陷（重复的 to_dict 方法、未使用的配置字段）及测试覆盖不足，导致当前系统行为偏离实施方案，且难以进行有效验证。

  ── 3.1 重复的 to_dict 方法：只剩一处定义
      实现位置: src/pg_mcp/models/query.py:142, src/pg_mcp/models/query.py:172
        - QueryResponse 继承链上定义 to_dict 的类: ['QueryResponse']
      [OK]   QueryResponse.to_dict 只有一处定义  ['QueryResponse']
      [OK]   ErrorDetail 只有一份实现（query 模块复用 errors 模块）
      [OK]   成功响应包含 tokens_used  tokens_used=42
      [OK]   失败响应也包含 tokens_used  tokens_used=0

  ── 3.2 未使用的配置字段：逐个确认真实接线（排除声明处与测试替身）
      databases                多数据库列表           接线于 ['__init__.py', 'app.py', 'cache/schema_cache.py', 'db/pool.py', 'security/auth.py', 'security/posture.py', 'server.py', 'services/health.py', 'services/orchestrator.py']
      blocked_tables           敏感表策略            接线于 ['app.py', 'security/posture.py', 'server.py', 'services/sql_validator.py']
      blocked_columns          敏感列策略            接线于 ['app.py', 'security/posture.py', 'server.py', 'services/result_validator.py', 'services/sql_validator.py']
      blocked_functions        危险函数黑名单          接线于 ['models/query.py', 'services/orchestrator.py', 'services/sql_validator.py']
      allow_explain            EXPLAIN 策略       接线于 ['app.py', 'security/posture.py', 'server.py', 'services/sql_validator.py']
      readonly_role            只读角色             接线于 ['security/posture.py', 'services/sql_executor.py']
      safe_search_path         安全 search_path   接线于 ['services/sql_executor.py']
      max_rows                 结果行数上限           接线于 ['security/posture.py', 'services/orchestrator.py', 'services/sql_executor.py']
      max_execution_time       查询超时             接线于 ['services/sql_executor.py']
      max_question_length      问题长度上限           接线于 ['services/orchestrator.py']
      min_confidence_score     置信度阈值            接线于 ['models/query.py', 'services/orchestrator.py']
      max_retries              最大重试次数           接线于 ['resilience/retry.py', 'services/orchestrator.py']
      retry_delay              重试初始延迟           接线于 ['resilience/retry.py']
      backoff_factor           退避倍数             接线于 ['resilience/retry.py']
      max_retry_delay          退避上限             接线于 ['resilience/retry.py']
      retry_jitter             退避抖动             接线于 ['resilience/retry.py']
      max_concurrent_queries   DB 并发上限          接线于 ['app.py', 'server.py']
      max_concurrent_llm_calls LLM 并发上限         接线于 ['app.py', 'server.py']
      acquire_timeout          取槽超时             接线于 ['services/orchestrator.py']
      metrics_enabled          指标开关             接线于 ['server.py']
      log_format               日志格式             接线于 ['observability/__init__.py', 'observability/logging.py', 'server.py']
      ssl_mode                 SSL 模式           接线于 ['db/pool.py']
      schema_ttl               schema 缓存 TTL    接线于 ['cache/schema_cache.py']
      [OK]   全部 23 个配置字段都有生产代码引用（无死配置）

  ── 3.3 测试覆盖：新增针对上述修复的回归测试
        - 测试文件数: 26
        - 静态统计的用例数 (def test_): 446
      e2e/test_mcp.py                               14 例
      integration/test_full_flow.py                 18 例
      security/test_advanced_security.py            12 例
      unit/test_app_multidb.py                       9 例
      unit/test_auth.py                             12 例
      unit/test_cache_stampede.py                    5 例
      unit/test_config.py                           51 例
      unit/test_data_masking.py                     10 例
      unit/test_driver_contract.py                   7 例
      unit/test_introspection.py                     5 例
      unit/test_metrics.py                           9 例
      unit/test_models.py                           34 例
      unit/test_orchestrator.py                     21 例
      unit/test_pool.py                              7 例
      unit/test_prompts.py                           7 例
      unit/test_resilience.py                       36 例
      unit/test_resilience_observability.py         14 例
      unit/test_result_validator.py                 10 例
      unit/test_retry.py                            14 例
      unit/test_retry_real_driver.py                 6 例
      unit/test_schema_cache.py                     20 例
      unit/test_security_policy.py                  15 例
      unit/test_security_posture.py                 12 例
      unit/test_sql_executor.py                     19 例
      unit/test_sql_generator.py                    23 例
      unit/test_sql_validator.py                    56 例
      [OK]   存在针对三点要求的专项测试文件  missing=[]
        - pytest --collect-only 实际收集用例数: 489
      [OK]   测试套件可被 pytest 正常收集  collected=489

==============================================================================
【附 A】design-review (0003) 点名但仍未修的项
==============================================================================

  ── A1 SET ROLE 引用标识符 + session 值参数化（0003 #7）
        - session 语句: ["SELECT set_config('statement_timeout', $1, false)", "SELECT set_config('search_path', $1, false)", 'SET ROLE "ro_user"']
        - session 参数: [("SELECT set_config('statement_timeout', $1, false)", ('30000',)), ("SELECT set_config('search_path', $1, false)", ('public',)), ('SET ROLE "ro_user"', ())]
      [OK]   SET ROLE 使用引用标识符 SET ROLE "ro_user"
      [OK]   readonly_role 不出现在任何语句文本中（走参数化/引用，非拼接）
      [OK]   search_path 走 set_config 参数化

  ── A2 系统 schema 拦截（0003 #7）
      A/B  SECURITY_BLOCK_SYSTEM_SCHEMAS=False -> reason=None
      [OK]   block_system_schemas=False 时系统 schema 放行
      A/B  SECURITY_BLOCK_SYSTEM_SCHEMAS=True -> reason=system_schema
      [OK]   block_system_schemas=True 时系统 schema 被拒绝

  ── A3 LIMIT 下推，避免整表拉进内存（0003 #2）
      A/B  SECURITY_ENFORCE_ROW_LIMIT=False -> 执行 SQL: SELECT id FROM users;
      [OK]   enforce_row_limit=False 时执行语句为 SELECT id FROM users;
      A/B  SECURITY_ENFORCE_ROW_LIMIT=True -> 执行 SQL: SELECT id FROM users LIMIT 50
      [OK]   enforce_row_limit=True 时执行语句为 SELECT id FROM users LIMIT 50

  ── A4 敏感数据在送 LLM 前脱敏（0003 #3）
      A 对照 VALIDATION_MASK_SENSITIVE_DATA=false -> 明文出现在 prompt: True
      B 实验 VALIDATION_MASK_SENSITIVE_DATA=true  -> 明文出现在 prompt: False
      [OK]   关闭脱敏时明文会外发（说明风险真实存在）
      [OK]   开启脱敏后明文不再进入 prompt
      [OK]   脱敏保留结构（占位符可见）

  ── A5 认证 + 按库授权 + 审计（0003 #1）
      [OK]   认证关闭时不做任何限制（向后兼容现有部署）
      [OK]   未知 API key 被拒绝 (unauthorized)
      [OK]   有效 key 可访问被授权的库
      [OK]   越权访问未授权库被拒绝 (forbidden)
        - 审计记录 outcome: ['rejected', 'authorized', 'rejected']
      [OK]   每次判定都留下审计记录（含拒绝）

==============================================================================
附：你可以自己动手证伪（30 秒）
==============================================================================
  如果上述功能只是'写在文档里'，那么改配置后行为不会变。请自行验证：

  1) 关掉安全策略，看敏感表是否变成可访问：
       export SECURITY_BLOCKED_TABLES=''  # bash；Windows 下用 $env:...
       uv run python scripts/demo_offline.py   # 场景 3 应出现 [FAIL]
  2) 把重试次数设为 0，看瞬时故障是否不再恢复：
       export RESILIENCE_MAX_RETRIES='0'   # bash；Windows 下用 $env:...
       uv run python scripts/demo_offline.py   # 场景 8 应出现 [FAIL]
  3) 指定一个不存在的库，看是否被拒绝而不是静默走默认库：
       uv run python scripts/verify_w5_requirements.py  # 见 1.1 的 database_error

  反向理解：demo 里之所以 [OK]，正是因为它把配置读进了请求链路。

==============================================================================
验收汇总: 57 项通过, 0 项失败
==============================================================================
结论: 作业三点要求 + design-review(0003) 遗留项 全部落地（A/B 对照实验均表现出配置相关行为差异）
```
