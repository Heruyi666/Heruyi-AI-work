"""Query orchestrator for coordinating the complete query flow.

This module provides the QueryOrchestrator class that coordinates all components
of the query processing pipeline: SQL generation, validation, execution, and result
validation.

It implements:
- Per-database executor routing (a request for database "B" never runs on "A")
- Rate limiting around LLM and database calls
- Retry with exponential backoff for transient LLM/DB failures
- Input length validation and result confidence thresholds
- Metrics instrumentation and request_id tracing across the whole pipeline
"""

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from asyncpg import Pool

from pg_mcp.cache.schema_cache import SchemaCache
from pg_mcp.config.settings import ResilienceConfig, ValidationConfig
from pg_mcp.models.errors import (
    DatabaseError,
    ErrorCode,
    LLMError,
    PgMcpError,
    RateLimitExceededError,
    SchemaLoadError,
    SecurityViolationError,
    SQLParseError,
)
from pg_mcp.models.query import (
    ErrorDetail,
    QueryRequest,
    QueryResponse,
    QueryResult,
    ReturnType,
    ValidationResult,
)
from pg_mcp.observability.metrics import MetricsCollector
from pg_mcp.observability.tracing import get_request_id, request_context
from pg_mcp.resilience.circuit_breaker import CircuitBreaker
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.resilience.retry import RetryPolicy, is_retryable_llm_error, retry_async
from pg_mcp.services.result_validator import ResultValidator
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_generator import SQLGenerator
from pg_mcp.services.sql_validator import SQLValidator

logger = logging.getLogger(__name__)

LimiterKind = Literal["llm", "queries"]


class QueryOrchestrator:
    """Orchestrates the complete query processing pipeline.

    This class coordinates SQL generation, validation, execution, and result
    validation. It implements retry logic with error feedback, circuit breaker
    pattern for fault tolerance, rate limiting, and comprehensive error handling.

    Example:
        >>> orchestrator = QueryOrchestrator(
        ...     sql_generator=generator,
        ...     sql_validator=validator,
        ...     executors={"mydb": executor},
        ...     result_validator=result_validator,
        ...     schema_cache=cache,
        ...     pools={"mydb": pool},
        ...     resilience_config=resilience_config,
        ...     validation_config=validation_config,
        ... )
        >>> response = await orchestrator.execute_query(QueryRequest(
        ...     question="How many users?",
        ...     database="mydb"
        ... ))
    """

    def __init__(
        self,
        sql_generator: SQLGenerator,
        sql_validator: SQLValidator,
        sql_executor: SQLExecutor | None = None,
        result_validator: ResultValidator | None = None,
        schema_cache: SchemaCache | None = None,
        pools: dict[str, Pool] | None = None,
        resilience_config: ResilienceConfig | None = None,
        validation_config: ValidationConfig | None = None,
        *,
        executors: dict[str, SQLExecutor] | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        rate_limiter: MultiRateLimiter | None = None,
        metrics: MetricsCollector | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        """Initialize query orchestrator.

        Args:
            sql_generator: SQL generation service.
            sql_validator: SQL validation service.
            sql_executor: SQL execution service. Legacy single-database
                argument; when given (and ``executors`` is empty) it is reused
                for every configured pool.
            result_validator: Result validation service.
            schema_cache: Schema cache instance.
            pools: Dictionary mapping database names to connection pools.
            resilience_config: Resilience configuration for retries and circuit breaker.
            validation_config: Validation configuration including thresholds.
            executors: Mapping of database name to SQL executor. When provided,
                requests are routed to the executor matching the resolved
                database name.
            circuit_breaker: Shared circuit breaker instance. A new one is
                created when omitted.
            rate_limiter: Rate limiter applied around LLM and DB calls.
            metrics: Metrics collector for instrumentation.
            retry_policy: Retry policy for transient LLM failures. Defaults to
                the values in ``resilience_config``.
        """
        self.sql_generator = sql_generator
        self.sql_validator = sql_validator
        self.result_validator = result_validator
        self.schema_cache = schema_cache
        self.pools = pools or {}
        self.resilience_config = resilience_config or ResilienceConfig()
        self.validation_config = validation_config or ValidationConfig()
        self.rate_limiter = rate_limiter
        self.metrics = metrics

        # Build the per-database executor map (multi-database routing).
        self.executors: dict[str, SQLExecutor] = dict(executors or {})
        if not self.executors and sql_executor is not None:
            pool_names = list(self.pools.keys())
            if not pool_names:
                # Only trust a real (string) database name; a Mock/stub must not
                # silently invent a database that was never configured.
                fallback = getattr(sql_executor, "db_name", None)
                pool_names = [fallback] if isinstance(fallback, str) else []
            self.executors = dict.fromkeys(pool_names, sql_executor)

        # Backwards-compatible accessor for the "primary" executor.
        self.sql_executor = sql_executor or next(iter(self.executors.values()), None)
        if self.sql_executor is None:
            raise ValueError("Either 'executors' or 'sql_executor' must be provided")

        # Circuit breaker for LLM calls (shared, single instance).
        self.circuit_breaker = circuit_breaker or CircuitBreaker(
            failure_threshold=self.resilience_config.circuit_breaker_threshold,
            recovery_timeout=self.resilience_config.circuit_breaker_timeout,
        )

        self.retry_policy = retry_policy or RetryPolicy.from_config(self.resilience_config)
        self._acquire_timeout = self.resilience_config.acquire_timeout

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute_query(self, request: QueryRequest) -> QueryResponse:
        """Execute complete query flow from question to results.

        This method orchestrates the entire pipeline:
        1. Generate request_id for tracking (propagated via contextvars)
        2. Validate the question length against ``max_question_length``
        3. Resolve the target database and select its executor
        4. Load schema from cache
        5. Generate and validate SQL (rate limited, with retry/backoff)
        6. Execute SQL on the selected database (rate limited)
        7. Validate results and apply the confidence threshold
        8. Return structured response

        Args:
            request: Query request containing question and parameters.

        Returns:
            QueryResponse: Complete response with SQL, results, or error information.

        Example:
            >>> response = await orchestrator.execute_query(
            ...     QueryRequest(question="Count all users", return_type="result")
            ... )
            >>> if response.success:
            ...     print(f"Found {response.data.row_count} rows")
        """
        async with request_context(get_request_id()) as request_id:
            return await self._execute_query(request, request_id)

    async def _execute_query(self, request: QueryRequest, request_id: str) -> QueryResponse:
        """Run the pipeline inside an established request context.

        Args:
            request: Query request containing question and parameters.
            request_id: Tracing identifier for this request.

        Returns:
            QueryResponse: Complete response with SQL, results, or error information.
        """
        started = time.perf_counter()
        status = "error"
        database_label = request.database or "default"

        logger.info(
            "Starting query execution",
            extra={"request_id": request_id, "question": request.question[:100]},
        )

        try:
            # Step 0: Enforce the input length guard before any LLM call.
            self._check_question_length(request.question, request_id)

            # Step 1: Resolve database name
            database_name = self._resolve_database(request.database)
            database_label = database_name
            logger.debug(
                "Resolved database",
                extra={"request_id": request_id, "database": database_name},
            )

            # Step 1b: Select the executor bound to that database.
            executor = self._get_executor(database_name)

            # Step 2: Get schema from cache
            if self.schema_cache is None:
                raise SchemaLoadError(
                    message="No schema cache configured for this server",
                    details={"database": database_name},
                )
            schema = self.schema_cache.get(database_name)
            if schema is None:
                # Schema not in cache, load it
                pool = self.pools.get(database_name)
                if pool is None:
                    raise DatabaseError(
                        message=f"No connection pool available for database '{database_name}'",
                        details={"database": database_name},
                    )
                try:
                    schema = await self.schema_cache.load(database_name, pool)
                except Exception as e:
                    raise SchemaLoadError(
                        message=f"Failed to load schema for database '{database_name}': {e!s}",
                        details={"database": database_name, "error": str(e)},
                    ) from e
            else:
                self._record_cache_age(database_name)

            logger.debug(
                "Schema loaded",
                extra={
                    "request_id": request_id,
                    "database": database_name,
                    "tables": len(schema.tables),
                },
            )

            # Step 3: Generate and validate SQL with retry logic
            generated_sql, validation_result, tokens_used = await self._generate_sql_with_retry(
                question=request.question,
                schema=schema,
                request_id=request_id,
            )

            # Step 4: If return_type is SQL, return early
            if request.return_type == ReturnType.SQL:
                logger.info(
                    "Returning SQL only",
                    extra={"request_id": request_id, "sql_length": len(generated_sql)},
                )
                status = "success"
                return QueryResponse(
                    success=True,
                    generated_sql=generated_sql,
                    validation=validation_result,
                    data=None,
                    error=None,
                    confidence=100,
                    tokens_used=tokens_used,
                    request_id=request_id,
                    warning=None,
                )

            # Step 5: Execute SQL on the database-specific executor
            logger.debug(
                "Executing SQL",
                extra={"request_id": request_id, "database": database_name},
            )
            start_time = self._get_current_time_ms()

            async with self._acquire_slot("queries", request_id=request_id):
                results, total_count = await executor.execute(generated_sql)

            execution_time_ms = self._get_current_time_ms() - start_time
            logger.info(
                "SQL executed successfully",
                extra={
                    "request_id": request_id,
                    "database": database_name,
                    "row_count": total_count,
                    "execution_time_ms": execution_time_ms,
                },
            )

            # Step 6: Validate results (non-blocking, failures don't fail the request)
            result_confidence, warning = await self._validate_results_safely(
                question=request.question,
                sql=generated_sql,
                results=results,
                row_count=total_count,
                request_id=request_id,
            )

            # Step 7: Build successful response
            query_result = QueryResult(
                columns=list(results[0].keys()) if results else [],
                rows=results,
                row_count=len(results),  # Limited row count (after max_rows applied)
                execution_time_ms=execution_time_ms,
            )

            status = "success" if warning is None else "low_confidence"
            return QueryResponse(
                success=True,
                generated_sql=generated_sql,
                validation=validation_result,
                data=query_result,
                error=None,
                confidence=result_confidence,
                tokens_used=tokens_used,
                request_id=request_id,
                warning=warning,
            )

        except PgMcpError as e:
            # Handle known application errors
            status = e.code.value
            logger.warning(
                "Query execution failed with known error",
                extra={
                    "request_id": request_id,
                    "error_code": e.code,
                    "error_message": str(e),
                },
            )
            return QueryResponse(
                success=False,
                generated_sql=None,
                validation=None,
                data=None,
                error=ErrorDetail(
                    code=e.code.value,
                    message=e.message,
                    details=e.details,
                ),
                confidence=0,
                tokens_used=None,
                request_id=request_id,
                warning=None,
            )
        except Exception as e:
            # Handle unexpected errors
            status = "internal_error"
            logger.exception(
                "Query execution failed with unexpected error",
                extra={"request_id": request_id},
            )
            return QueryResponse(
                success=False,
                generated_sql=None,
                validation=None,
                data=None,
                error=ErrorDetail(
                    code=ErrorCode.INTERNAL_ERROR.value,
                    message=f"Internal server error: {e!s}",
                    details={"error_type": type(e).__name__},
                ),
                confidence=0,
                tokens_used=None,
                request_id=request_id,
                warning=None,
            )
        finally:
            duration = time.perf_counter() - started
            if self.metrics is not None:
                self.metrics.observe_query_duration(duration)
                self.metrics.increment_query_request(status=status, database=database_label)

    # ------------------------------------------------------------------
    # Pipeline helpers
    # ------------------------------------------------------------------

    def _check_question_length(self, question: str, request_id: str) -> None:
        """Reject questions longer than the configured limit.

        Args:
            question: Natural language question from the client.
            request_id: Request ID used for logging.

        Raises:
            PgMcpError: With code ``question_too_long`` when the limit is exceeded.
            ValidationError: Kept as a ``PgMcpError`` subclass for compatibility.
        """
        max_length = self.validation_config.max_question_length
        if len(question) > max_length:
            logger.warning(
                "Question rejected: exceeds max_question_length",
                extra={
                    "request_id": request_id,
                    "length": len(question),
                    "max_question_length": max_length,
                },
            )
            raise PgMcpError(
                message=(
                    f"Question is too long: {len(question)} characters "
                    f"(max {max_length})"
                ),
                code=ErrorCode.QUESTION_TOO_LONG,
                details={"length": len(question), "max_question_length": max_length},
            )

    def _record_cache_age(self, database_name: str) -> None:
        """Publish the schema cache age metric for a database.

        Args:
            database_name: Database whose cache was hit.
        """
        if self.metrics is None or self.schema_cache is None:
            return
        age = self.schema_cache.get_cache_age(database_name)
        if age is not None:
            self.metrics.set_schema_cache_age(database_name, age)

    @asynccontextmanager
    async def _acquire_slot(
        self,
        kind: LimiterKind,
        *,
        request_id: str,
    ) -> AsyncIterator[None]:
        """Reserve a rate limiter slot for an LLM or database operation.

        Args:
            kind: Which limiter to use (``llm`` or ``queries``).
            request_id: Request ID used for logging.

        Yields:
            None

        Raises:
            RateLimitExceededError: If no slot becomes available in time.
        """
        if self.rate_limiter is None:
            yield
            return

        limiter = (
            self.rate_limiter.llm_limiter if kind == "llm" else self.rate_limiter.query_limiter
        )
        acquired = await limiter.acquire(timeout=self._acquire_timeout)
        if not acquired:
            if self.metrics is not None:
                self.metrics.increment_rate_limit_rejection(kind)
            logger.warning(
                "Request rejected by rate limiter",
                extra={
                    "request_id": request_id,
                    "limiter": kind,
                    "timeout_seconds": self._acquire_timeout,
                },
            )
            raise RateLimitExceededError(
                message=(
                    f"Rate limit exceeded for '{kind}' operations: "
                    f"no slot available within {self._acquire_timeout}s"
                ),
                details={
                    "limiter": kind,
                    "timeout_seconds": self._acquire_timeout,
                    "active": limiter.active_count,
                    "max_concurrent": limiter.max_concurrent,
                },
            )

        try:
            yield
        finally:
            limiter.release()

    def _get_executor(self, database_name: str) -> SQLExecutor:
        """Return the executor bound to the given database.

        Args:
            database_name: Resolved database name.

        Returns:
            SQLExecutor: Executor that talks to that database.

        Raises:
            DatabaseError: When no executor is configured for the database.
        """
        executor = self.executors.get(database_name)
        if executor is None:
            raise DatabaseError(
                message=f"No SQL executor configured for database '{database_name}'",
                details={
                    "requested_database": database_name,
                    "available_databases": sorted(self.executors.keys()),
                },
            )
        return executor

    def _respect_rate_limit(self) -> bool:
        """Whether a rate limiter is configured for this orchestrator.

        Returns:
            bool: True when rate limiting is active.
        """
        return self.rate_limiter is not None

    def _resolve_database(self, database: str | None) -> str:
        """Resolve database name from request or auto-select.

        If database is specified, validate it exists.
        If not specified and only one database available, auto-select it.

        Args:
            database: Database name from request (optional).

        Returns:
            str: Resolved database name.

        Raises:
            DatabaseError: If database is invalid or cannot be auto-selected.

        Example:
            >>> name = orchestrator._resolve_database("mydb")  # Validates "mydb" exists
            >>> name = orchestrator._resolve_database(None)  # Auto-selects if only one DB
        """
        available = self._available_databases()

        if database is not None:
            # Validate specified database exists
            if database not in available:
                raise DatabaseError(
                    message=f"Database '{database}' not found",
                    details={
                        "requested_database": database,
                        "available_databases": available,
                    },
                )
            return database

        # Auto-select if only one database available
        if len(available) == 0:
            raise DatabaseError(
                message="No databases configured",
                details={},
            )
        if len(available) == 1:
            return available[0]

        # Multiple databases, must specify
        raise DatabaseError(
            message="Multiple databases available, please specify which to query",
            details={"available_databases": available},
        )

    def _available_databases(self) -> list[str]:
        """List the databases this orchestrator can serve.

        Returns:
            list[str]: Sorted database names (executors win over pools).
        """
        names = set(self.executors) | set(self.pools)
        return sorted(names)

    async def _generate_sql_with_retry(
        self,
        question: str,
        schema: Any,
        request_id: str,
    ) -> tuple[str, ValidationResult, int | None]:
        """Generate and validate SQL with retry logic on validation failures.

        This method implements a retry loop that:
        1. Checks circuit breaker state
        2. Acquires an LLM rate limiter slot
        3. Generates SQL using the LLM (with retry/backoff on transient errors)
        4. Validates the generated SQL
        5. On validation failure, retries with error feedback
        6. Records success/failure to the circuit breaker and metrics

        Args:
            question: User's natural language question.
            schema: Database schema for context.
            request_id: Request ID for tracking.

        Returns:
            tuple: (generated_sql, validation_result, tokens_used)

        Raises:
            LLMError: If circuit breaker is open or generation fails.
            SecurityViolationError: If SQL fails validation after all retries.
            SQLParseError: If SQL cannot be parsed.

        Example:
            >>> sql, validation, tokens = await orchestrator._generate_sql_with_retry(
            ...     question="Count users",
            ...     schema=db_schema,
            ...     request_id="123",
            ... )
        """
        # Check circuit breaker
        if not self.circuit_breaker.allow_request():
            raise LLMError(
                message="SQL generation service is temporarily unavailable (circuit breaker open)",
                details={
                    "circuit_state": self.circuit_breaker.state,
                    "failure_count": self.circuit_breaker.failure_count,
                },
            )

        previous_sql: str | None = None
        error_feedback: str | None = None
        max_retries = self.resilience_config.max_retries
        tokens_used: int | None = None

        for attempt in range(max_retries + 1):
            try:
                logger.debug(
                    "Generating SQL",
                    extra={
                        "request_id": request_id,
                        "attempt": attempt + 1,
                        "max_retries": max_retries + 1,
                    },
                )

                # Generate SQL (rate limited + transient-failure retry)
                generated_sql = await self._generate_once(
                    question=question,
                    schema=schema,
                    previous_sql=previous_sql,
                    error_feedback=error_feedback,
                    request_id=request_id,
                )
                tokens_used = self._current_tokens_used()

                logger.debug(
                    "SQL generated",
                    extra={
                        "request_id": request_id,
                        "sql_length": len(generated_sql),
                    },
                )

                # Validate SQL
                try:
                    self.sql_validator.validate_or_raise(generated_sql)
                except (SecurityViolationError, SQLParseError) as validation_error:
                    self._record_sql_rejection(validation_error, request_id)
                    if attempt < max_retries:
                        # Record as failure and retry with feedback
                        logger.warning(
                            "SQL validation failed, retrying with feedback",
                            extra={
                                "request_id": request_id,
                                "attempt": attempt + 1,
                                "error": str(validation_error),
                            },
                        )
                        previous_sql = generated_sql
                        error_feedback = str(validation_error)
                        continue
                    # Out of retries, record failure and raise
                    self.circuit_breaker.record_failure()
                    logger.error(
                        "SQL validation failed after all retries",
                        extra={
                            "request_id": request_id,
                            "attempts": attempt + 1,
                            "error": str(validation_error),
                        },
                    )
                    raise

                # Validation successful
                self.circuit_breaker.record_success()
                logger.info(
                    "SQL generated and validated successfully",
                    extra={
                        "request_id": request_id,
                        "attempts": attempt + 1,
                    },
                )

                return generated_sql, self._build_validation_result(generated_sql), tokens_used

            except (LLMError, SecurityViolationError, SQLParseError):
                # Re-raise known errors
                raise
            except PgMcpError:
                # Rate limit / boundary errors must not be masked as LLM errors
                raise
            except Exception as e:
                # Unexpected error during generation
                self.circuit_breaker.record_failure()
                logger.exception(
                    "Unexpected error during SQL generation",
                    extra={"request_id": request_id},
                )
                raise LLMError(
                    message=f"SQL generation failed unexpectedly: {e!s}",
                    details={"error_type": type(e).__name__},
                ) from e

        # Should not reach here, but just in case
        self.circuit_breaker.record_failure()
        raise LLMError(
            message="SQL generation failed after all retry attempts",
            details={"max_retries": max_retries},
        )

    async def _generate_once(
        self,
        *,
        question: str,
        schema: Any,
        previous_sql: str | None,
        error_feedback: str | None,
        request_id: str,
    ) -> str:
        """Call the SQL generator once, with rate limiting, metrics and retry.

        Args:
            question: User's natural language question.
            schema: Database schema for context.
            previous_sql: Previously generated SQL that failed validation.
            error_feedback: Validation error message used as retry feedback.
            request_id: Request ID for logging.

        Returns:
            str: Generated SQL statement.

        Raises:
            RateLimitExceededError: If no LLM slot is available.
            LLMError: If generation fails after the retry policy is exhausted.
        """
        started = time.perf_counter()
        if self.metrics is not None:
            self.metrics.increment_llm_call("generate_sql")

        try:
            async with self._acquire_slot("llm", request_id=request_id):
                return await retry_async(
                    lambda: self.sql_generator.generate(
                        question=question,
                        schema=schema,
                        previous_attempt=previous_sql,
                        error_feedback=error_feedback,
                    ),
                    policy=self.retry_policy,
                    is_retryable=is_retryable_llm_error,
                    operation_name="llm.generate_sql",
                    metrics=self.metrics,
                    on_retry=lambda attempt, error, delay: logger.warning(
                        "Retrying SQL generation after transient error",
                        extra={
                            "request_id": request_id,
                            "attempt": attempt + 1,
                            "delay_seconds": round(delay, 3),
                            "error": str(error),
                        },
                    ),
                )
        finally:
            if self.metrics is not None:
                self.metrics.observe_llm_latency("generate_sql", time.perf_counter() - started)

    def _current_tokens_used(self) -> int | None:
        """Read the token usage reported by the last LLM call.

        Returns:
            int | None: Token count when the provider reported it.
        """
        tokens = getattr(self.sql_generator, "last_tokens_used", None)
        if isinstance(tokens, int) and tokens > 0:
            if self.metrics is not None:
                self.metrics.increment_llm_tokens("generate_sql", tokens)
            return tokens
        return None

    def _build_validation_result(self, sql: str) -> ValidationResult:
        """Build the observable validation outcome for a validated SQL statement.

        Args:
            sql: SQL statement that already passed ``validate_or_raise``.

        Returns:
            ValidationResult: Real validation details when the validator can
            provide them, otherwise a permissive default.
        """
        builder = getattr(self.sql_validator, "build_validation_result", None)
        if callable(builder):
            try:
                candidate = builder(sql)
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("Validator could not build validation result: %s", e)
            else:
                if isinstance(candidate, ValidationResult):
                    return candidate

        return ValidationResult(
            is_valid=True,
            is_select=True,
            allows_data_modification=False,
            uses_blocked_functions=[],
            error_message=None,
        )

    def _record_sql_rejection(self, error: BaseException, request_id: str) -> None:
        """Record a security/parse rejection in metrics.

        Args:
            error: The validation exception that was raised.
            request_id: Request ID used for logging context.
        """
        details = getattr(error, "details", None) or {}
        reason = details.get("reason") if isinstance(details, dict) else None
        if not reason:
            reason = "parse_error" if isinstance(error, SQLParseError) else "security_violation"
        if self.metrics is not None:
            self.metrics.increment_sql_rejected(str(reason))
        logger.debug(
            "SQL rejected by validator",
            extra={"request_id": request_id, "reason": reason},
        )

    async def _validate_results_safely(
        self,
        question: str,
        sql: str,
        results: list[dict[str, Any]],
        row_count: int,
        request_id: str,
    ) -> tuple[int, str | None]:
        """Validate query results with error handling (non-blocking).

        This method attempts to validate results using LLM, but failures
        don't cause the overall query to fail. The configured
        ``min_confidence_score`` is applied to produce an advisory warning
        instead of silently reporting perfect confidence.

        Args:
            question: User's original question.
            sql: Generated SQL query.
            results: Query results.
            row_count: Total row count.
            request_id: Request ID for tracking.

        Returns:
            tuple[int, str | None]: Confidence score (0-100) and an optional
            warning when the score is below ``min_confidence_score``.

        Example:
            >>> confidence, warning = await orchestrator._validate_results_safely(
            ...     question="Count users",
            ...     sql="SELECT COUNT(*) FROM users",
            ...     results=[{"count": 42}],
            ...     row_count=1,
            ...     request_id="123",
            ... )
        """
        if not self.validation_config.enabled:
            return 100, None
        if self.result_validator is None:
            return 100, "Result validation is not configured; confidence unverified."

        try:
            logger.debug(
                "Validating results",
                extra={"request_id": request_id},
            )

            if self.metrics is not None:
                self.metrics.increment_llm_call("validate_result")

            started = time.perf_counter()
            async with self._acquire_slot("llm", request_id=request_id):
                validation_result = await self.result_validator.validate(
                    question=question,
                    sql=sql,
                    results=results,
                    row_count=row_count,
                )
            if self.metrics is not None:
                self.metrics.observe_llm_latency("validate_result", time.perf_counter() - started)
                tokens = getattr(self.result_validator, "last_tokens_used", None)
                if isinstance(tokens, int) and tokens > 0:
                    self.metrics.increment_llm_tokens("validate_result", tokens)

            logger.info(
                "Result validation completed",
                extra={
                    "request_id": request_id,
                    "confidence": validation_result.confidence,
                    "is_acceptable": validation_result.is_acceptable,
                },
            )

            warning: str | None = None
            threshold = self.validation_config.min_confidence_score
            if validation_result.confidence < threshold:
                warning = (
                    f"Result confidence {validation_result.confidence}% is below the "
                    f"configured threshold ({threshold}%): {validation_result.explanation}"
                )
                if validation_result.suggestion:
                    warning += f" Suggestion: {validation_result.suggestion}"

            return validation_result.confidence, warning

        except RateLimitExceededError:
            # A saturated LLM limiter should not discard a valid result set.
            logger.warning(
                "Result validation skipped: LLM rate limit reached",
                extra={"request_id": request_id},
            )
            return 100, "Result validation was skipped because the LLM rate limit was reached."
        except Exception as e:
            # Log but don't fail the query. Confidence is reported as 0 (unknown)
            # instead of 100 so that unverified results are not silently trusted.
            logger.warning(
                "Result validation failed, reporting unverified confidence",
                extra={
                    "request_id": request_id,
                    "error": str(e),
                },
            )
            return 0, f"Result validation unavailable ({type(e).__name__}); confidence unverified."

    def _get_current_time_ms(self) -> float:
        """Get current time in milliseconds.

        Returns:
            float: Current time in milliseconds since epoch.
        """
        return time.time() * 1000
