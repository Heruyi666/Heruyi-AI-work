"""Unit tests for the metrics collector (test-plan review 0008, section 2.3)."""

import pytest

from pg_mcp.observability.metrics import MetricsCollector


@pytest.fixture
def metrics() -> MetricsCollector:
    """A collector with a clean registry."""
    collector = MetricsCollector()
    collector.reset_all_metrics()
    return collector


class TestCounters:
    """Counters accumulate and are readable through snapshot()."""

    def test_query_requests_counter(self, metrics: MetricsCollector) -> None:
        """Requests are counted per database and status."""
        metrics.increment_query_request("success", "analytics")
        metrics.increment_query_request("success", "analytics")
        metrics.increment_query_request("security_violation", "analytics")

        snapshot = metrics.snapshot()
        key = 'pg_mcp_query_requests_total{database="analytics",status="success"}'
        assert snapshot[key] == 2.0
        rejected = 'pg_mcp_query_requests_total{database="analytics",status="security_violation"}'
        assert snapshot[rejected] == 1.0

    def test_llm_tokens_counter(self, metrics: MetricsCollector) -> None:
        """Token usage accumulates per operation."""
        metrics.increment_llm_tokens("generate_sql", 100)
        metrics.increment_llm_tokens("generate_sql", 50)

        snapshot = metrics.snapshot()
        # prometheus_client appends `_total` to Counter sample names.
        assert snapshot['pg_mcp_llm_tokens_used_total{operation="generate_sql"}'] == 150.0

    def test_sql_rejected_counter_carries_reason(self, metrics: MetricsCollector) -> None:
        """Rejections are labelled with the structured reason."""
        metrics.increment_sql_rejected("blocked_table")

        snapshot = metrics.snapshot()
        assert snapshot['pg_mcp_sql_rejected_total{reason="blocked_table"}'] == 1.0

    def test_retry_and_rate_limit_counters(self, metrics: MetricsCollector) -> None:
        """Resilience events are observable."""
        metrics.increment_retry_attempts("db.execute", "retried")
        metrics.increment_rate_limit_rejection("llm")

        snapshot = metrics.snapshot()
        retried = 'pg_mcp_retry_attempts_total{operation="db.execute",outcome="retried"}'
        limited = 'pg_mcp_rate_limit_rejections_total{limiter="llm"}'
        assert snapshot[retried] == 1.0
        assert snapshot[limited] == 1.0


class TestHistogramsAndGauges:
    """Histograms expose counts, gauges expose the last value."""

    def test_query_duration_histogram(self, metrics: MetricsCollector) -> None:
        """Durations are sampled and counted."""
        metrics.observe_query_duration(0.1)
        metrics.observe_query_duration(0.5)

        snapshot = metrics.snapshot()
        assert snapshot["pg_mcp_query_duration_seconds_count"] == 2.0

    def test_db_query_duration_histogram(self, metrics: MetricsCollector) -> None:
        """Database durations are tracked separately from end-to-end latency."""
        metrics.observe_db_query_duration(0.2)

        snapshot = metrics.snapshot()
        assert snapshot["pg_mcp_db_query_duration_seconds_count"] == 1.0

    def test_connections_gauge(self, metrics: MetricsCollector) -> None:
        """The active-connection gauge keeps the latest value."""
        metrics.set_db_connections_active("analytics", 3)
        metrics.set_db_connections_active("analytics", 5)

        snapshot = metrics.snapshot()
        assert snapshot['pg_mcp_db_connections_active{database="analytics"}'] == 5.0


class TestReset:
    """reset_all_metrics() must fully unregister, not just zero out."""

    def test_reset_clears_every_series(self, metrics: MetricsCollector) -> None:
        """After a reset no counter value survives.

        Histograms keep an empty bucket series (``_count``/``_sum`` are always
        exported by prometheus_client), so the assertion is on the values.
        """
        metrics.increment_query_request("success", "analytics")
        metrics.reset_all_metrics()

        snapshot = metrics.snapshot()
        assert all(value == 0.0 for value in snapshot.values())
        assert 'pg_mcp_query_requests_total{database="analytics",status="success"}' not in (
            snapshot
        )

    def test_reset_can_be_repeated(self, metrics: MetricsCollector) -> None:
        """Repeated resets must not raise a duplicate-timeseries error."""
        metrics.increment_query_request("success", "analytics")
        metrics.reset_all_metrics()
        metrics.reset_all_metrics()
        metrics.increment_query_request("success", "analytics")

        assert metrics.snapshot() != {}
