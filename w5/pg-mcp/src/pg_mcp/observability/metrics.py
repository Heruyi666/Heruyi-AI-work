"""Prometheus metrics collector for PostgreSQL MCP Server.

This module implements comprehensive metrics collection using prometheus_client,
tracking query requests, LLM calls, database operations, resilience events and
system health. The collector is now wired into the live request path (orchestrator,
executor, server) instead of only existing as dead code.
"""

import contextlib

from prometheus_client import REGISTRY, Counter, Gauge, Histogram, start_http_server
from prometheus_client.metrics import MetricWrapperBase

# Metric attribute names owned by the collector (used for clean re-creation).
_METRIC_ATTRS: tuple[str, ...] = (
    "query_requests",
    "query_duration",
    "llm_calls",
    "llm_latency",
    "llm_tokens_used",
    "sql_rejected",
    "db_connections_active",
    "db_query_duration",
    "schema_cache_age",
    "retry_attempts",
    "rate_limit_rejections",
    "health_checks",
)


class MetricsCollector:
    """Centralized metrics collector using Prometheus client.

    This class provides singleton access to all application metrics,
    implementing the metrics specified in the implementation plan.

    Metrics Categories:
    - Query metrics: Request counts and durations
    - LLM metrics: API calls, latency, and token usage
    - Database metrics: Connection pool and query performance
    - Security metrics: Rejected queries
    - Resilience metrics: Retries and rate-limit rejections
    - Cache metrics: Schema cache age

    Example:
        >>> metrics = MetricsCollector()
        >>> metrics.increment_query_request(status="success", database="mydb")
        >>> with metrics.query_duration.time():
        ...     await execute_query()
    """

    _instance: "MetricsCollector | None" = None

    def __new__(cls) -> "MetricsCollector":
        """Ensure singleton instance."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialize_metrics()
        return cls._instance

    def _initialize_metrics(self) -> None:
        """Initialize all Prometheus metrics.

        Creates counters, histograms, and gauges for tracking various
        aspects of the MCP server operation. Any previously registered
        metric with the same attribute is unregistered first so the
        collector can be safely re-created (used by tests).
        """
        for attr in _METRIC_ATTRS:
            existing = getattr(self, attr, None)
            if isinstance(existing, MetricWrapperBase):
                with contextlib.suppress(Exception):
                    REGISTRY.unregister(existing)

        # Query Metrics
        self.query_requests: Counter = Counter(
            "pg_mcp_query_requests_total",
            "Total number of query requests processed",
            labelnames=["status", "database"],
        )

        self.query_duration: Histogram = Histogram(
            "pg_mcp_query_duration_seconds",
            "Query request processing duration in seconds",
            buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
        )

        # LLM Metrics
        self.llm_calls: Counter = Counter(
            "pg_mcp_llm_calls_total",
            "Total number of LLM API calls",
            labelnames=["operation"],
        )

        self.llm_latency: Histogram = Histogram(
            "pg_mcp_llm_latency_seconds",
            "LLM API call latency in seconds",
            labelnames=["operation"],
            buckets=(0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0),
        )

        self.llm_tokens_used: Counter = Counter(
            "pg_mcp_llm_tokens_used",
            "Total number of tokens used by LLM",
            labelnames=["operation"],
        )

        # Security Metrics
        self.sql_rejected: Counter = Counter(
            "pg_mcp_sql_rejected_total",
            "Total number of SQL queries rejected by security checks",
            labelnames=["reason"],
        )

        # Database Metrics
        self.db_connections_active: Gauge = Gauge(
            "pg_mcp_db_connections_active",
            "Number of active database connections",
            labelnames=["database"],
        )

        self.db_query_duration: Histogram = Histogram(
            "pg_mcp_db_query_duration_seconds",
            "Database query execution duration in seconds",
            buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0),
        )

        # Cache Metrics
        self.schema_cache_age: Gauge = Gauge(
            "pg_mcp_schema_cache_age_seconds",
            "Age of the schema cache in seconds",
            labelnames=["database"],
        )

        # Resilience Metrics
        self.retry_attempts: Counter = Counter(
            "pg_mcp_retry_attempts_total",
            "Total number of retry attempts by operation and outcome",
            labelnames=["operation", "outcome"],
        )

        self.rate_limit_rejections: Counter = Counter(
            "pg_mcp_rate_limit_rejections_total",
            "Total number of requests rejected by a rate limiter",
            labelnames=["limiter"],
        )

        # Health Metrics
        self.health_checks: Counter = Counter(
            "pg_mcp_health_checks_total",
            "Total number of health checks by resulting status",
            labelnames=["status"],
        )

    def start_metrics_server(self, port: int) -> None:
        """Start the Prometheus metrics HTTP server.

        Args:
            port: Port number to listen on for metrics scraping.

        Example:
            >>> metrics = MetricsCollector()
            >>> metrics.start_metrics_server(9090)
            # Metrics available at http://localhost:9090/metrics
        """
        start_http_server(port)

    def increment_query_request(self, status: str, database: str) -> None:
        """Increment query request counter.

        Args:
            status: Query status (success, error, validation_failed, etc.)
            database: Target database name.
        """
        self.query_requests.labels(status=status, database=database).inc()

    def increment_llm_call(self, operation: str) -> None:
        """Increment LLM call counter.

        Args:
            operation: Type of LLM operation (generate_sql, validate_result, etc.)
        """
        self.llm_calls.labels(operation=operation).inc()

    def observe_llm_latency(self, operation: str, duration: float) -> None:
        """Record LLM call latency.

        Args:
            operation: Type of LLM operation.
            duration: Duration in seconds.
        """
        self.llm_latency.labels(operation=operation).observe(duration)

    def increment_llm_tokens(self, operation: str, tokens: int) -> None:
        """Increment LLM token usage counter.

        Args:
            operation: Type of LLM operation.
            tokens: Number of tokens used.
        """
        self.llm_tokens_used.labels(operation=operation).inc(tokens)

    def increment_sql_rejected(self, reason: str) -> None:
        """Increment SQL rejection counter.

        Args:
            reason: Reason for rejection (blocked_table, blocked_column, ...)
        """
        self.sql_rejected.labels(reason=reason).inc()

    def set_db_connections_active(self, database: str, count: int) -> None:
        """Set active database connection count.

        Args:
            database: Database name.
            count: Number of active connections.
        """
        self.db_connections_active.labels(database=database).set(count)

    def observe_db_query_duration(self, duration: float) -> None:
        """Record database query duration.

        Args:
            duration: Duration in seconds.
        """
        self.db_query_duration.observe(duration)

    def observe_query_duration(self, duration: float) -> None:
        """Record end-to-end query request duration.

        Args:
            duration: Duration in seconds.
        """
        self.query_duration.observe(duration)

    def set_schema_cache_age(self, database: str, age_seconds: float) -> None:
        """Set schema cache age.

        Args:
            database: Database name.
            age_seconds: Cache age in seconds.
        """
        self.schema_cache_age.labels(database=database).set(age_seconds)

    def increment_retry_attempts(self, operation: str, outcome: str) -> None:
        """Increment retry counter.

        Args:
            operation: Logical operation name (e.g. ``llm.generate_sql``).
            outcome: One of ``retried``, ``exhausted`` or ``fatal``.
        """
        self.retry_attempts.labels(operation=operation, outcome=outcome).inc()

    def increment_rate_limit_rejection(self, limiter: str) -> None:
        """Increment rate limiter rejection counter.

        Args:
            limiter: Limiter name (``queries`` or ``llm``).
        """
        self.rate_limit_rejections.labels(limiter=limiter).inc()

    def record_health_check(self, status: str) -> None:
        """Increment health check counter.

        Args:
            status: Overall health status (healthy, degraded, unhealthy).
        """
        self.health_checks.labels(status=status).inc()

    def snapshot(self) -> dict[str, float]:
        """Return a plain dictionary with the current values of all metrics.

        This is convenient for diagnostics, demos and tests: it avoids having
        to scrape the Prometheus HTTP endpoint.

        Returns:
            dict[str, float]: Mapping of metric sample key to current value.
            Histogram ``_bucket``/``_created`` samples are omitted.
        """
        snapshot: dict[str, float] = {}
        for attr in _METRIC_ATTRS:
            metric = getattr(self, attr, None)
            if not isinstance(metric, MetricWrapperBase):
                continue
            for collected in metric.collect():
                for sample in collected.samples:
                    if sample.name.endswith(("_bucket", "_created")):
                        continue
                    key = sample.name
                    if sample.labels:
                        labels = ",".join(f'{k}="{v}"' for k, v in sorted(sample.labels.items()))
                        key = f"{key}{{{labels}}}"
                    snapshot[key] = sample.value
        return snapshot

    def reset_all_metrics(self) -> None:
        """Reset all metrics to their initial state.

        Metrics are unregistered from the default registry and re-created,
        which makes this safe to call from tests.
        """
        self._initialize_metrics()


# Singleton instance
metrics = MetricsCollector()
