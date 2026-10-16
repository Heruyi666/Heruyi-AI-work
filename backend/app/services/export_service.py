"""Data export service — formats query results as CSV/JSON files.

Implements an explicit three-step task decomposition so the export
workflow can be coordinated by an AI Agent:

  Step 1  fetch_query_result      — resolve the connection + execute SQL → QueryResult
  Step 2  format_as_csv / _json   — format QueryResult into the target representation
  Step 3  create_export_response  — wrap the formatted content into a downloadable response

The orchestrator `export_query_result` chains the three steps together so a
single call performs "execute query + export result" in one shot.

Security: export reuses the existing query pipeline (execute_query_with_service
→ sql_validator), so only SELECT statements are allowed and every exported
query is recorded in the query history.
"""

import csv
import io
import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select

from app.models.database import DatabaseConnection
from app.models.schemas import QueryResult
from app.services.query_wrapper import execute_query_with_service
from app.services.sql_validator import SqlValidationError

logger = logging.getLogger(__name__)

#: Formats supported by the exporter.
SUPPORTED_FORMATS = ("csv", "json")

#: MIME types keyed by format.
MEDIA_TYPES = {
    "csv": "text/csv",
    "json": "application/json",
}


class ExportError(Exception):
    """Raised for export-level failures (bad format, etc).

    Query-execution failures surface as SqlValidationError / Exception and are
    handled by the API layer the same way as the normal query endpoint.
    """

    pass


# ---------------------------------------------------------------------------
# Step 1 — fetch the query result
# ---------------------------------------------------------------------------
async def fetch_query_result(
    session: Session,
    database_name: str,
    sql: str,
) -> QueryResult:
    """Resolve the database connection and execute the SQL.

    Reuses `execute_query_with_service`, which validates the SQL (SELECT only),
    executes it via the adapter, and records the result in query history.

    Args:
        session: SQLite session.
        database_name: Registered connection name.
        sql: SQL SELECT query to execute and export.

    Returns:
        QueryResult with columns + rows.

    Raises:
        HTTPException 404: connection not found.
        SqlValidationError: non-SELECT / malformed SQL.
        Exception: query execution failure.
    """
    statement = select(DatabaseConnection).where(
        DatabaseConnection.name == database_name
    )
    connection = session.exec(statement).first()
    if not connection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Database connection '{database_name}' not found",
        )

    result = await execute_query_with_service(
        session,
        database_name,
        connection.db_type,
        connection.url,
        sql,
    )
    return result


# ---------------------------------------------------------------------------
# Step 2 — format the query result
# ---------------------------------------------------------------------------
def _serialize_cell(value: Any) -> str:
    """Serialize a single cell value for CSV output."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def format_as_csv(result: QueryResult) -> str:
    """Format a QueryResult into a UTF-8 CSV string.

    The column order follows `result.columns`. None → empty cell, nested
    objects/lists are JSON-encoded, datetimes use ISO format.
    """
    output = io.StringIO()
    writer = csv.writer(output)
    headers = [col.name for col in result.columns]
    writer.writerow(headers)
    for row in result.rows:
        writer.writerow([_serialize_cell(row.get(h)) for h in headers])
    return output.getvalue()


def format_as_json(result: QueryResult) -> str:
    """Format a QueryResult into a pretty-printed JSON string.

    Exports the full result object (columns + rows + metadata) so the
    consumer can reconstruct the query context.
    """
    payload = result.model_dump(by_alias=True)
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


# ---------------------------------------------------------------------------
# Step 3 — build the downloadable response
# ---------------------------------------------------------------------------
def create_export_response(
    content: str,
    database_name: str,
    fmt: str,
) -> StreamingResponse:
    """Wrap formatted content into a downloadable StreamingResponse.

    A UTF-8 BOM is prepended for CSV so Excel opens multibyte content
    correctly. The filename embeds the connection name + timestamp.
    """
    encoded = content.encode("utf-8")
    if fmt == "csv":
        encoded = b"\xef\xbb\xbf" + encoded  # UTF-8 BOM for Excel
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{database_name}_{timestamp}.{fmt}"
    return StreamingResponse(
        iter([encoded]),
        media_type=MEDIA_TYPES[fmt],
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


# ---------------------------------------------------------------------------
# Orchestrator — full pipeline
# ---------------------------------------------------------------------------
async def export_query_result(
    session: Session,
    database_name: str,
    sql: str,
    fmt: str,
) -> StreamingResponse:
    """Run the full export pipeline: fetch → format → respond.

    This is the one-call "execute query + export result" entry point used by
    the Claude Code `/export-query` command and the `data-export` agent.

    Args:
        session: SQLite session.
        database_name: Registered connection name.
        sql: SQL SELECT query to execute and export.
        fmt: Export format — one of SUPPORTED_FORMATS.

    Returns:
        StreamingResponse delivering the formatted file.

    Raises:
        ExportError: unsupported format.
        SqlValidationError: non-SELECT SQL.
        HTTPException 404: connection not found.
        Exception: query execution failure.
    """
    fmt = (fmt or "").lower().strip()
    if fmt not in SUPPORTED_FORMATS:
        raise ExportError(
            f"Unsupported export format: '{fmt}'. Supported: {', '.join(SUPPORTED_FORMATS)}"
        )

    # Step 1 — fetch (executes SQL, records history)
    result = await fetch_query_result(session, database_name, sql)
    logger.info(
        "Export step 1 (fetch): %d rows from '%s' in %dms",
        result.row_count,
        database_name,
        result.execution_time_ms,
    )

    # Step 2 — format
    if fmt == "csv":
        content = format_as_csv(result)
    else:
        content = format_as_json(result)
    logger.info("Export step 2 (format): %s, %d chars", fmt, len(content))

    # Step 3 — respond
    response = create_export_response(content, database_name, fmt)
    logger.info("Export step 3 (respond): %s ready", fmt)
    return response
