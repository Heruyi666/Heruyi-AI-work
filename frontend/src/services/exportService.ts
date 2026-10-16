/**
 * Client-side query result export helpers.
 *
 * Formats an in-memory QueryResult into CSV / JSON and triggers a browser
 * download. No backend round-trip is needed when results are already in
 * hand, which avoids re-executing the query.
 *
 * The backend also exposes POST /api/v1/dbs/{name}/export for the one-call
 * "execute + export" automation used by the Claude Code /export-query command.
 */

import { QueryResult } from "../types/query";

/** Serialize a single cell value for CSV output. */
function serializeCell(value: any): string {
  if (value === null || value === undefined) return "";
  if (value instanceof Date) return value.toISOString();
  if (typeof value === "object") return JSON.stringify(value);
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}

/** Escape and quote a CSV field if it contains special characters. */
function escapeCsvField(raw: string): string {
  if (/[",\n\r]/.test(raw)) {
    return `"${raw.replace(/"/g, '""')}"`;
  }
  return raw;
}

/** Convert a QueryResult to a UTF-8 CSV string (BOM-prefixed for Excel). */
export function resultToCsv(result: QueryResult): string {
  const headers = result.columns.map((c) => c.name);
  const lines: string[] = [headers.join(",")];

  for (const row of result.rows) {
    lines.push(
      headers.map((h) => escapeCsvField(serializeCell(row[h]))).join(",")
    );
  }
  // Prepend BOM so Excel opens multibyte content correctly
  return "\uFEFF" + lines.join("\n");
}

/** Convert a QueryResult to a pretty-printed JSON string. */
export function resultToJson(result: QueryResult): string {
  return JSON.stringify(result, null, 2);
}

/** Trigger a browser download of a text blob. */
export function downloadFile(
  content: string,
  filename: string,
  mime: string
): void {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export type ExportFormat = "csv" | "json";

/** Export a QueryResult in the given format, prompting a file download. */
export function exportResult(
  result: QueryResult,
  dbName: string,
  format: ExportFormat
): void {
  const ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);

  if (format === "csv") {
    downloadFile(
      resultToCsv(result),
      `${dbName}_${ts}.csv`,
      "text/csv;charset=utf-8"
    );
  } else {
    downloadFile(
      resultToJson(result),
      `${dbName}_${ts}.json`,
      "application/json"
    );
  }
}
