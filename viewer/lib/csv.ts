import type { ReportRow } from "./sessions";
export function csvCell(value: unknown) {
  const text =
    value == null
      ? ""
      : typeof value === "string"
        ? value
        : JSON.stringify(value);
  const safe = /^[\s]*[=+@-]/.test(text) ? "'" + text : text;
  return '"' + safe.replaceAll('"', '""') + '"';
}
export function csv(rows: ReportRow[]) {
  const columns = [
    "timestamp",
    "scope_id",
    "conversation_id",
    "source_request_id",
    "completion_status",
    "content_captured",
    "details_truncated",
    "query",
    "raw_input",
    "raw_output",
  ];
  return (
    "\uFEFF" +
    [columns, ...rows.map((r) => columns.map((c) => r[c]))]
      .map((row) => row.map(csvCell).join(","))
      .join("\r\n") +
    "\r\n"
  );
}
