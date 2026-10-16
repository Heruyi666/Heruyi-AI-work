# W5 pg-mcp 评分体系（评委视角）

> 本文回答的是「**以什么方式打分**」，而不是「打了多少分」。
> 评分结果放在最后一节，前面是可复用的评审方法。

## 0. 一句话评分哲学

**不采信作者提供的证据，只采信评委亲手复现的证据。**

作者写的验收脚本、演示报告都属于「自证材料」。它们有用，但只能作为**线索**——
评委的工作是沿着线索去**独立取证**，而不是把作者的结论抄一遍。

---

## 1. 四层证据模型

| 层级 | 名称 | 权重 | 耗时 | 回答的问题 |
|---|---|---:|---:|---|
| L1 | 静态交付审阅 | 20 | ~5 min | 需求项是否都做了？代码卫不卫生？ |
| L2 | 运行时证据 | 30 | ~10 min | 测试是真断言还是走过场？ |
| L3 | 对抗性验证 | **35** | ~15 min | 评委自己动手打，能不能打穿？ |
| L4 | 诚实度与工程判断 | 15 | ~5 min | 有没有把没做的说成做了？ |

对抗性验证权重最高——**因为它是唯一无法靠"写材料"蒙混的一层**。

---

## 2. L1 静态交付审阅（20 分）

### 2.1 需求逐条对照

方法：把 review 文档里每一条「必须/建议」列成表，逐条去代码里找落点。

**关键：不是找"有没有这段代码"，而是找"这段代码有没有被调用"。**
本作业的指控恰恰是「写了但没接线」，所以调用链比代码存在更重要。

检查手法：
```bash
grep -rn "函数名" src/          # 声明处 + 调用处必须都有
```

### 2.2 技术债指纹（量化，5 秒出结果）

这些数字是代码的"体检指标"，比读代码更快也更客观：

```bash
grep -rc "noqa" src --include="*.py"          # 抑制告警数量
grep -rc "type: ignore" src --include="*.py"  # 类型逃逸（最危险）
grep -rn "TODO\|FIXME\|XXX\|HACK" src         # 未完成的承诺
grep -rn "except:\|except Exception" src      # 吞异常
```

**判读标准**（每千行）：

| 指标 | 优秀 | 及格 | 危险 |
|---|---|---|---|
| `type: ignore` | 0 | ≤2 | >5（等于放弃了类型检查） |
| `bare except` | 0 | ≤1 | >2（错误会被静默吞掉） |
| `noqa` | ≤1 | ≤5 | >10（在跟 linter 对抗） |

### 2.3 门禁是否真的生效

`ruff` / `mypy` 通过是及格线，不值得加分；**不通过才扣分**。
真正要看的是：门禁是否覆盖了 `tests/` 和 `scripts/`——只 lint `src/` 是常见偷懒。

---

## 3. L2 运行时证据（30 分）

### 3.1 测试是否真断言（最重要的一条）

**反面模式**：mock 掉了被测对象本身，测的是 mock 而不是代码。

判别方法：看测试里 `patch()` 的目标。如果 patch 的是**被测类自己**，
或者断言的是"某个 mock 被调用了"而不是"输出值对不对"，这条测试基本无效。

**关键路径覆盖优先**：安全判定、多库路由、限流、重试这几条路径的测试价值，
远高于 getter / 序列化的测试。覆盖率数字相同，含金量可以差 10 倍。

### 3.2 A/B 对照（识破"写死返回值"）

对于任何声称「配置驱动」的功能，评委必须做对照实验：

```
配置 A（关闭）→ 行为 X
配置 B（打开）→ 行为 Y
X ≠ Y 才算真的接线；X == Y 说明配置是死的
```

这一招能一次性识破所有"假接线"——这是本作业最容易踩的坑。

### 3.3 被跳过的测试

```bash
uv run pytest -rs        # 看 skip 理由
```

**skip 不可耻，可耻的是没理由的 skip**。
`needs live PostgreSQL (set PG_MCP_RUN_INTEGRATION=1)` 是合格理由；
光秃秃一个 `@pytest.mark.skip` 就是藏雷。

---

## 4. L3 对抗性验证（35 分，权重最高）

评委自己编用例，**不用作者准备的**——作者挑的用例必然是他测过的。

### 4.1 拦截率：自编攻击语句

对安全类代码，我至少手工构造 20 条，覆盖这些绕过手法：

| 绕过手法 | 示例 |
|---|---|
| CTE 包装 | `WITH x AS (SELECT * FROM payments) SELECT * FROM x` |
| 派生表 / 子查询 | `SELECT * FROM (SELECT * FROM payments) t` |
| 大小写变形 | `SELECT * FROM PAYMENTS` |
| 引号标识符 | `SELECT * FROM public."payments"` |
| 别名隐藏 | `SELECT u.password_hash AS ph FROM users u` |
| UNION 拼接 | `SELECT * FROM users UNION ALL SELECT * FROM payments` |
| IN 子查询 | `WHERE id IN (SELECT id FROM payments)` |
| 标量子查询 | `SELECT (SELECT password_hash FROM users LIMIT 1)` |
| ORDER BY / HAVING 藏列 | `ORDER BY password_hash`、`HAVING MAX(password_hash)` |
| 危险函数 | `lo_import('/etc/passwd')`、`pg_sleep()`、`COPY ... TO PROGRAM` |
| 跨库三段式 | `SELECT * FROM otherdb.public.users` |
| 系统 schema | `pg_catalog.pg_shadow`、`information_schema.tables` |

### 4.2 误杀率：**这一条最容易被漏掉，也最能区分水平**

> 把 `SELECT` 全部禁掉，拦截率 100%，但它是废墙。

必须同时验证正常查询**不被误拒**：JOIN / GROUP BY / HAVING / CTE /
窗口函数 / EXTRACT / UNION / 时间区间过滤……

**判读**：拦截率高不算本事，「拦截率 100% + 误杀率 0%」才算。

### 4.3 fail-closed 检查

遇到**无法解析**的语句时，系统是拒绝还是放行？

- 拒绝（fail-closed）→ 安全，但可能误伤
- 放行（fail-open）→ 灾难，等于给攻击者开了后门

这一条往往是最高分差所在：很多人写的安全模块，解析异常分支直接 `return True`。

### 4.4 默认配置是否安全（security by default）

新功能默认开启还是关闭？

- 安全类功能默认**关闭** → 用户不配就没有防护 → 扣分
- 但若为向后兼容而默认关闭，且**文档明确声明** → 可酌情接受

评委要区分「刻意取舍」和「忘了打开」。前者是工程判断，后者是疏漏。

### 4.5 改配置证伪

把安全配置清空 / 重试次数设 0，**对应的验收项必须变成 FAIL**。
如果关掉配置后演示依然全绿，说明演示根本没读配置——直接判自证材料造假。

---

## 5. L4 诚实度与工程判断（15 分）

这一层最主观，但最能拉开差距。

**加分的**：
- 主动声明「我的验收脚本只覆盖 X，覆盖不到 Y」——**边界声明比满分报告更可信**
- 主动披露自己材料的缺陷（本报告就写明了验收脚本自身修过 3 个 bug）
- 对文档建议**有理有据地否决**，并写明理由

**扣分的**：
- 把 `skipped` 说成通过
- 把「写了代码」说成「功能已启用」
- 用覆盖率数字掩盖关键路径没测

**工程判断的识别**：无脑照做文档 vs 有取舍。
看到「建议引入 pglast 替换 SQLGlot」就真的换掉，和「文档自己标 Low 级、现有用例已覆盖，不换」——
后者得分更高。

---

## 6. 红线（一票否决级）

命中任意一条，该层级直接判 0，且总分不超过 60：

1. 测试数据造假（硬编码期望值冒充真实输出）
2. 把被跳过的真实环境测试说成「已通过」
3. 验收脚本里出现恒真断言（`assert True`、断言自己的常量）
4. 关闭 lint/类型检查来让门禁变绿，而非修复问题

---

## 7. 本次实测数据（评委亲手采集）

采集时间：2026-09-28，独立于作者提供的验收脚本。

### L1 静态

| 指标 | 实测 | 判定 |
|---|---:|---|
| 源文件 / 行数 | 38 / 8548 | — |
| `ruff` (src+tests+scripts) | All checks passed | 及格 |
| `mypy` | 38 files, 0 issues | 及格 |
| `type: ignore` | **0** | 优秀 |
| `bare except` | **0** | 优秀 |
| `TODO/FIXME` | **0** | 优秀 |
| `noqa` | 34（约 4‰） | 及格偏优 |

### L2 运行时

| 指标 | 实测 |
|---|---:|
| 测试文件 / 用例定义 | 32 / 474 |
| `pytest` 结果 | 488 passed / 1 skipped |
| 覆盖率 | **89%**（关键路径：retry 100%、circuit_breaker 100%、posture 100%、introspection 100%、rate_limiter 98%、data_masking 97%、pool 96%） |
| skip 理由 | `needs live PostgreSQL + OpenAI (set PG_MCP_RUN_INTEGRATION=1)` — 理由成立 |
| 真实驱动验证 | 用**真实 asyncpg 异常类**验证重试判定（11 类）+ 用 `inspect.signature` 校验连接池调用契约（25 项） |
| 并发/资源压测 | `scripts/bench_offline.py` 14/14：120 并发下 DB 峰值=8（等于上限）、60 并发过载下拒绝 54 个、指标序列数稳定在 15 |

### L3 对抗性（本次现场执行）

| 项目 | 结果 |
|---|---|
| 自编攻击语句 21 条 | **21/21 拦截**（CTE、派生表、大小写、引号标识符、别名、UNION、IN 子查询、标量子查询、ORDER BY/HAVING 藏列、`lo_import`、`pg_sleep`、`COPY TO PROGRAM`、跨库三段式、系统 schema 全部命中） |
| 正常查询误杀率 | **0 / 6**（JOIN、GROUP BY/HAVING、CTE、EXTRACT、UNION、时间过滤全部放行） |
| fail-closed | 多语句 / 无法解析 → 拒绝 |
| 默认安全 | `block_system_schemas` / `enforce_row_limit` / 脱敏 默认开；**认证默认关**（已声明） |

### L4 诚实度

- 已声明未覆盖范围（RBAC 默认关闭、无真实 PG 回归）
- 已披露验收脚本自身修过的 bug
- 对 3 条文档建议给出否决理由
- 主动压测并**自己找出一个真 bug**（见下），而不是只跑绿灯

### 压测挖出的真 bug：schema 缓存惊群

单测永远发现不了，因为一次只发一个请求：

```
并发 60 个请求 + 冷缓存 → 60 个请求各自内省一次
内省发生在限流槽之前 → 并发限流被完全绕过 → DoS 防护失效
```

修复：`SchemaCache.load` 加 per-database 双重检查锁（`force=True` 留给 refresh）。
修复后并发峰值 **60 → 1**。已加 5 个并发防回归测试守住。

**这一条本身就是 L2/L3 的证据**：能自己把 bug 挖出来并封住，比多写 50 个绿灯测试有价值得多。

---

## 8. 评分结果

| 层级 | 满分 | 得分 | 主要依据 |
|---|---:|---:|---|
| L1 静态交付 | 20 | **20** | 门禁全绿、技术债指纹优秀；真实 PG 回归套件已入库 |
| L2 运行时证据 | 30 | **29** | 488 用例 + **90%** 覆盖率 + 真实驱动契约/异常验证 + 并发压测 + **17 项真实 PostgreSQL 回归** |
| L3 对抗性验证 | 35 | **33** | 21/21 拦截 + 0/6 误杀 + fail-closed + 压测挖出并修复惊群 + **真实库上验证会话加固**；扣 2：认证默认关闭 |
| L4 诚实度 | 15 | **15** | 边界声明完整、主动披露缺陷、否决建议有理据、主动压测自查 |
| **合计** | **100** | **97** | |

### 四轮分数变化：82 → 87 → 92 → 97

不是标准变松，是每一轮都补了实打实的证据：

| 轮次 | 变化 |
|---|---|
| 82 → 87 | 按 0003/0008 补齐 6 项修复与 7 个测试文件；并首次做对抗性抽查（21 条攻击 + 6 条误杀） |
| 87 → 92 | 补真实驱动契约/异常验证、并发压测（并挖出惊群 bug）、安全态势自检、pool 覆盖率 46%→96% |
| 92 → 97 | **真实 PostgreSQL 16.2 落地并跑通 17 项回归**（见下），覆盖率 89%→90% |

### 距离满分还差的 3 分

| 差距 | 补法 |
|---|---|
| -2 认证默认关闭 | 已在自检里告警、基线已文档化，但默认值仍是 false（刻意取舍：单用户 stdio 无真实多租户需求） |
| -1 工程打磨 | 真实环境的长跑 / 内存增长数据（需 24h+ soak） |

### 「真实 PG」已落地（第四轮）

前几轮"本机跑不了真实 PostgreSQL"的**根因已查明并解决**，不是代码问题：

1. 网络不是"没有"，而是**只有国内镜像可达**：`mirrors.aliyun.com` 593 KB/s 可用，
   而 `ftp.postgresql.org`、`registry-1.docker.io`、`pypi.org` 全部超时；
   且机器未安装 docker、`wsl.exe` 被禁用。
2. `pgserver` 装不上是 **ABI 问题而非网络**：项目要求 Python ≥3.14，而 pgserver 的
   Windows wheel 最高只到 `cp312` → 无可用 wheel → 退回源码编译失败。

**解法**：绕开 pip，直接从阿里云镜像取 `pgserver` 的 wheel（12.8 MB / 20 s）当 zip 解压，
得到完整 PostgreSQL 16.2 二进制。已封装为 `scripts/local_postgres.py`
（`up` / `status` / `psql` / `stop` / `reset`），无需 docker、无需安装数据库、无需 Python 3.12。

**真实回归覆盖（17 项，`PG_MCP_RUN_INTEGRATION=1`）**：

| 验证点 | 真实证据 |
|---|---|
| 连接池 + 查询 | 真实 `create_pools` → `SELECT count(*) FROM users` 返回真实行数 |
| 内省正确性 | 内省结果与 `information_schema` **完全相等**（10 张表/视图，含视图） |
| 多库隔离 | `blog_small` 有 users、`analytics` 有 events；跨库查 users 报 `DatabaseError` |
| 池创建失败 | 不存在的库抛 `InvalidCatalogNameError`，不静默跳过 |
| `search_path` | `current_setting('search_path')` = `public` |
| 超时 | `pg_sleep(3)` + `timeout=0.5` 被真实中止 |
| `SET ROLE` | `current_user` = `readonly_user` |
| **引号修复（0003 #6）** | 混合大小写角色 `ReadOnly_User` 生效——不加引号会被折叠成小写而报"角色不存在" |
| 只读事务 | `DELETE FROM users` 被拒，前后行数不变 |
| 行数下推 | `max_rows=3` 时服务端只产 3 行，`row_count == len(rows)`（无虚假总数） |
| 安全前置 | `DROP TABLE users` / `pg_catalog.pg_authid` / 跨库三段式被拦，且真实表仍在 |
| 端到端 | 真实池 + 真实验证 + 真实执行跑通 `query` 工具；多库未指定时返回明确错误而非静默选库 |

真实 PG 还暴露了一个文档缺陷并已修复：`SQLExecutor.execute` 的 docstring 仍宣称返回
"限制前的总行数"，但 `LIMIT` 下推后该值等于返回行数（用户可见的 `row_count` 一直是对的）。

---

## 9. 一句话复述这个方法

> **作者的报告当线索，自己的手当尺子：**
> 静态看指纹，运行看断言真伪，安全看「拦得住 + 不误杀」两头，
> 最后看作者有没有把没做的事如实说出来。
