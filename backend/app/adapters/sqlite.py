"""SQLite database adapter."""

import asyncio
import sqlite3
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.adapters.base import (
    DatabaseAdapter,
    ConnectionConfig,
    QueryResult,
    MetadataResult,
)


class SQLiteAdapter(DatabaseAdapter):
    """SQLite database adapter using built-in sqlite3 + asyncio thread pool."""

    def __init__(self, config: ConnectionConfig):
        super().__init__(config)
        # Parse file path from URL: sqlite:///path/to/file.db
        self._db_path = self._parse_path(config.url)

    @staticmethod
    def _parse_path(url: str) -> str:
        """Extract file path from SQLite URL."""
        if url.startswith("sqlite:///"):
            raw = url[len("sqlite:///"):]
            # On Windows, path may be like /D:/path/to/file.db
            if len(raw) > 2 and raw[0] == "/" and raw[2] == ":" and raw[1].isalpha():
                raw = raw[1:]  # strip leading slash for Windows drive letter
            return raw
        return url

    def _get_conn(self) -> sqlite3.Connection:
        """Create or reuse a thread-local connection."""
        if self._pool is None:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._pool = conn
        return self._pool

    async def test_connection(self) -> Tuple[bool, Optional[str]]:
        """Test SQLite connection by checking file existence."""
        try:
            if not os.path.exists(self._db_path):
                return False, f"Database file not found: {self._db_path}"
            await asyncio.to_thread(
                lambda: sqlite3.connect(self._db_path).close()
            )
            return True, None
        except Exception as e:
            return False, str(e)

    async def get_connection_pool(self) -> Any:
        """SQLite uses a single connection (no pool)."""
        return await asyncio.to_thread(self._get_conn)

    async def close_connection_pool(self) -> None:
        """Close the SQLite connection."""
        if self._pool is not None:
            await asyncio.to_thread(self._pool.close)
            self._pool = None

    async def extract_metadata(self) -> MetadataResult:
        """Extract SQLite metadata from sqlite_master and PRAGMA."""

        def _do_extract() -> Tuple[List[Dict], List[Dict]]:
            conn = self._get_conn()
            tables: List[Dict[str, Any]] = []
            views: List[Dict[str, Any]] = []

            # Get all user tables
            table_rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()

            for row in table_rows:
                table_name = row["name"]
                columns = self._get_columns_sync(conn, table_name)
                row_count = conn.execute(
                    f'SELECT COUNT(*) FROM "{table_name}"'
                ).fetchone()[0]

                tables.append({
                    "name": table_name,
                    "schemaName": "main",
                    "type": "table",
                    "columns": columns,
                    "rowCount": row_count,
                })

            # Get views
            view_rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
            ).fetchall()

            for row in view_rows:
                view_name = row["name"]
                columns = self._get_columns_sync(conn, view_name)
                views.append({
                    "name": view_name,
                    "schemaName": "main",
                    "type": "view",
                    "columns": columns,
                })

            return tables, views

        tables, views = await asyncio.to_thread(_do_extract)
        return MetadataResult(tables=tables, views=views)

    def _get_columns_sync(self, conn: sqlite3.Connection, table_name: str) -> List[Dict[str, Any]]:
        """Get column metadata using PRAGMA table_info (sync)."""
        cols = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        pk_cols = {c["name"] for c in cols if c["pk"] > 0}

        columns: List[Dict[str, Any]] = []
        for col in cols:
            column_meta = {
                "name": col["name"],
                "dataType": col["type"] if col["type"] else "TEXT",
                "nullable": not col["notnull"],
                "primaryKey": col["name"] in pk_cols,
                "unique": False,
                "defaultValue": col["dflt_value"],
            }
            columns.append(column_meta)
        return columns

    async def execute_query(self, sql: str) -> QueryResult:
        """Execute query against SQLite using thread pool."""

        def _do_query() -> Tuple[List[Dict[str, str]], List[Dict[str, Any]], int]:
            conn = self._get_conn()
            cursor = conn.execute(sql)
            rows = cursor.fetchall()

            columns: List[Dict[str, str]] = []
            result_rows: List[Dict[str, Any]] = []

            if rows:
                # Column headers from cursor.description
                for desc in cursor.description:
                    col_name = desc[0]
                    columns.append({"name": col_name, "dataType": "TEXT"})

                for row in rows:
                    result_rows.append(dict(row))

            return columns, result_rows, len(result_rows)

        columns, rows, count = await asyncio.to_thread(_do_query)
        return QueryResult(columns=columns, rows=rows, row_count=count)

    def get_dialect_name(self) -> str:
        return "sqlite"

    def get_identifier_quote_char(self) -> str:
        return '"'
