"""Unit tests for the data export feature (CSV/JSON)."""

import pytest
import json as jsonlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from app.main import app
from app.database import get_session
from app.models.database import DatabaseConnection, ConnectionStatus
from app.models.schemas import QueryResult, QueryColumn
from app.services.sql_validator import SqlValidationError
from app.services.export_service import (
    format_as_csv,
    format_as_json,
    create_export_response,
    export_query_result,
    ExportError,
    SUPPORTED_FORMATS,
)


# ---------------------------------------------------------------------------
# Fixtures (mirrors test_api_queries.py patterns)
# ---------------------------------------------------------------------------
@pytest.fixture
def test_session():
    """Create an in-memory SQLite session for testing."""
    from app.models.database import DatabaseConnection
    from app.models.metadata import DatabaseMetadata
    from app.models.query import QueryHistory

    engine = create_engine(
        "sqlite:///file:test_export_db?mode=memory&cache=shared&uri=true",
        connect_args={"check_same_thread": False, "uri": True},
    )
    SQLModel.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def client(test_session):
    """Create TestClient with test database session."""

    def get_test_session():
        return test_session

    app.dependency_overrides[get_session] = get_test_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def sample_connection(test_session):
    """Create a sample database connection."""
    conn = DatabaseConnection(
        name="test_db",
        url="postgresql://user:pass@localhost/testdb",
        description="Test database",
        status=ConnectionStatus.ACTIVE,
        last_connected_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    test_session.add(conn)
    test_session.commit()
    test_session.refresh(conn)
    return conn


def _make_result() -> QueryResult:
    """Build a sample QueryResult for formatter tests."""
    return QueryResult(
        columns=[
            QueryColumn(name="id", dataType="integer"),
            QueryColumn(name="name", dataType="text"),
        ],
        rows=[
            {"id": 1, "name": "Alice"},
            {"id": 2, "name": None},
        ],
        rowCount=2,
        executionTimeMs=12,
        sql="SELECT * FROM users LIMIT 100",
    )


# ---------------------------------------------------------------------------
# Step 2 — pure formatter unit tests
# ---------------------------------------------------------------------------
class TestFormatters:
    """Test the CSV/JSON formatting functions in isolation."""

    def test_format_as_csv_basic(self):
        csv = format_as_csv(_make_result())
        lines = csv.splitlines()
        assert lines[0] == "id,name"
        assert lines[1] == "1,Alice"
        # None → empty cell
        assert lines[2] == "2,"

    def test_format_as_csv_comma_escaping(self):
        result = QueryResult(
            columns=[QueryColumn(name="msg", dataType="text")],
            rows=[{"msg": "hello, world"}],
            rowCount=1,
            executionTimeMs=1,
            sql="SELECT 1",
        )
        csv = format_as_csv(result)
        # csv module quotes values containing commas
        assert '"hello, world"' in csv

    def test_format_as_csv_datetime_and_object(self):
        dt = datetime(2026, 1, 2, 3, 4, 5)
        result = QueryResult(
            columns=[
                QueryColumn(name="ts", dataType="timestamp"),
                QueryColumn(name="payload", dataType="json"),
            ],
            rows=[{"ts": dt, "payload": {"k": "v"}}],
            rowCount=1,
            executionTimeMs=1,
            sql="SELECT 1",
        )
        csv = format_as_csv(result)
        # datetime → ISO format string
        assert dt.isoformat() in csv
        # nested object is JSON-encoded (CSV may double-quote, so check the value)
        assert "v" in csv

    def test_format_as_json_structure(self):
        result = _make_result()
        text = format_as_json(result)
        data = jsonlib.loads(text)
        # camelCase aliases preserved via by_alias
        assert data["rowCount"] == 2
        assert data["executionTimeMs"] == 12
        assert len(data["columns"]) == 2
        assert data["rows"][0]["name"] == "Alice"

    def test_format_as_json_is_serializable(self):
        text = format_as_json(_make_result())
        # Must round-trip through json.loads without error
        jsonlib.loads(text)


# ---------------------------------------------------------------------------
# Step 3 — response builder
# ---------------------------------------------------------------------------
class TestCreateExportResponse:
    """Test the downloadable response builder."""

    def test_csv_response_headers(self):
        resp = create_export_response("a,b\n1,2\n", "demo", "csv")
        assert resp.media_type == "text/csv"
        cd = resp.headers["content-disposition"]
        assert "attachment" in cd
        assert ".csv" in cd
        assert "demo" in cd

    def test_json_response_headers(self):
        resp = create_export_response('{"x":1}', "demo", "json")
        assert resp.media_type == "application/json"
        assert ".json" in resp.headers["content-disposition"]

    def test_csv_response_media_type(self):
        resp = create_export_response("a,b\n1,2\n", "demo", "csv")
        assert resp.media_type == "text/csv"


# ---------------------------------------------------------------------------
# Orchestrator + API endpoint
# ---------------------------------------------------------------------------
class TestExportEndpoint:
    """Test the POST /api/v1/dbs/{name}/export endpoint."""

    @patch("app.services.export_service.execute_query_with_service")
    def test_export_csv_success(self, mock_exec, client, sample_connection):
        mock_exec.return_value = _make_result()

        response = client.post(
            "/api/v1/dbs/test_db/export",
            json={"sql": "SELECT * FROM users", "format": "csv"},
        )

        assert response.status_code == 200
        assert "text/csv" in response.headers.get("content-type", "")
        assert ".csv" in response.headers.get("content-disposition", "")
        # UTF-8 BOM prepended for Excel compatibility
        assert response.content.startswith(b"\xef\xbb\xbf")
        body = response.text
        assert "id,name" in body
        assert "Alice" in body
        mock_exec.assert_called_once()

    @patch("app.services.export_service.execute_query_with_service")
    def test_export_json_success(self, mock_exec, client, sample_connection):
        mock_exec.return_value = _make_result()

        response = client.post(
            "/api/v1/dbs/test_db/export",
            json={"sql": "SELECT * FROM users", "format": "json"},
        )

        assert response.status_code == 200
        assert "application/json" in response.headers.get("content-type", "")
        data = response.json()
        assert data["rowCount"] == 2
        assert data["rows"][0]["name"] == "Alice"

    @patch("app.services.export_service.execute_query_with_service")
    def test_export_default_format_is_csv(self, mock_exec, client, sample_connection):
        mock_exec.return_value = _make_result()
        # Omit format → defaults to csv
        response = client.post(
            "/api/v1/dbs/test_db/export",
            json={"sql": "SELECT * FROM users"},
        )
        assert response.status_code == 200
        assert "text/csv" in response.headers.get("content-type", "")

    def test_export_database_not_found(self, client):
        response = client.post(
            "/api/v1/dbs/nonexistent/export",
            json={"sql": "SELECT 1", "format": "csv"},
        )
        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    @patch("app.services.export_service.execute_query_with_service")
    def test_export_sql_validation_error(self, mock_exec, client, sample_connection):
        mock_exec.side_effect = SqlValidationError("Only SELECT queries are allowed")
        response = client.post(
            "/api/v1/dbs/test_db/export",
            json={"sql": "DELETE FROM users", "format": "csv"},
        )
        assert response.status_code == 400
        assert "Only SELECT" in response.json()["detail"]

    @patch("app.services.export_service.execute_query_with_service")
    def test_export_execution_error(self, mock_exec, client, sample_connection):
        mock_exec.side_effect = Exception("connection refused")
        response = client.post(
            "/api/v1/dbs/test_db/export",
            json={"sql": "SELECT 1", "format": "csv"},
        )
        assert response.status_code == 500
        assert "Export failed" in response.json()["detail"]

    def test_export_unsupported_format(self, client, sample_connection):
        # The body passes validation (format is a free string), but the
        # exporter rejects it with ExportError → 400.
        response = client.post(
            "/api/v1/dbs/test_db/export",
            json={"sql": "SELECT 1", "format": "xml"},
        )
        assert response.status_code == 400
        assert "Unsupported export format" in response.json()["detail"]

    def test_export_missing_sql(self, client, sample_connection):
        response = client.post(
            "/api/v1/dbs/test_db/export",
            json={"format": "csv"},
        )
        assert response.status_code == 422  # pydantic validation

    @pytest.mark.asyncio
    async def test_export_query_result_unsupported_format_raises(self, test_session, sample_connection):
        with pytest.raises(ExportError):
            await export_query_result(test_session, "test_db", "SELECT 1", "xml")


# ---------------------------------------------------------------------------
# Sanity: supported formats contract
# ---------------------------------------------------------------------------
class TestSupportedFormats:
    def test_supported_formats_contains_csv_and_json(self):
        assert "csv" in SUPPORTED_FORMATS
        assert "json" in SUPPORTED_FORMATS
        assert len(SUPPORTED_FORMATS) == 2
