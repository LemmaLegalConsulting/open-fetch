import type { Scope } from "./config";
import type { ReportRow } from "./sessions";
export type Span = {
  timestamp: string;
  id: string;
  attributes: Record<string, unknown>;
  error?: boolean;
};
export function object(value: unknown): Record<string, unknown> {
  if (typeof value === "string") {
    try {
      return object(JSON.parse(value));
    } catch {
      return {};
    }
  }
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}
export function flatten(
  value: Record<string, unknown>,
  prefix = "",
): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(value)) {
    const key = prefix + k;
    if (v && typeof v === "object" && !Array.isArray(v))
      Object.assign(result, flatten(v as Record<string, unknown>, key + "."));
    else result[key] = v;
  }
  return result;
}
const text = (x: unknown) => (typeof x === "string" ? x : "");
function chunks(a: Record<string, unknown>, prefix: string) {
  return Array.from({ length: 16 }, (_, i) =>
    text(a[`${prefix}.chunk_${String(i + 1).padStart(2, "0")}`]),
  ).join("");
}
export function normalize(span: Span, scope: Scope): ReportRow | null {
  const a = span.attributes;
  const id = `${scope.clientId}/${scope.environment}/${scope.keyId}`;
  // Recheck backend results. Session IDs, user input, and UI filters never establish ownership.
  if (
    a["fetch.scope_id"] !== id ||
    a["fetch.client_id"] !== scope.clientId ||
    a["fetch.environment"] !== scope.environment ||
    a["fetch.api_key_id"] !== scope.keyId
  )
    return null;
  if (
    !a["langfuse.trace.name"] ||
    a["langfuse.observation.type"] === "generation"
  )
    return null;
  const content =
    scope.allowContent === true &&
    (a["fetch.content_captured"] === true ||
      a["fetch.content_captured"] === "true");
  const input = content
    ? object(chunks(a, "fetch.event") || a["langfuse.trace.input"])
    : {};
  const output = content ? object(a["langfuse.trace.output"]) : {};
  const outcomes: Record<string, string> = {
    referred: "Referral completed",
    no_match: "Completed — no matching referral",
    no_legal_problem: "Completed — no legal problem",
    error: "Error — completion unconfirmed",
  };
  return {
    timestamp: span.timestamp,
    client_id: scope.clientId,
    environment: scope.environment,
    api_key_id: scope.keyId,
    scope_id: id,
    conversation_id:
      text(a["fetch.conversation_id"]) || text(a["langfuse.session.id"]),
    source_request_id: text(a["fetch.request_id"]) || span.id,
    activity_type: scope.environment === "test" ? "TEST" : "PRODUCTION",
    completion_status: span.error
      ? "Request error"
      : outcomes[text(input.outcome)] || "No completion recorded",
    query: content
      ? chunks(a, "fetch.user_query") ||
        text(input.problem_description) ||
        text(input.query)
      : "",
    raw_input: JSON.stringify(input),
    raw_output: JSON.stringify(output),
    content_captured: content,
    details_truncated:
      a["fetch.event.is_truncated"] === true ||
      a["fetch.event.is_truncated"] === "true" ||
      a["fetch.user_query.is_truncated"] === true ||
      a["fetch.user_query.is_truncated"] === "true",
  };
}
