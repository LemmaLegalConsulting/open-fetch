import { DefaultAzureCredential } from "@azure/identity";
import {
  CloudWatchLogsClient,
  StartQueryCommand,
  GetQueryResultsCommand,
  StopQueryCommand,
} from "@aws-sdk/client-cloudwatch-logs";
import type { Scope } from "./config";
import { flatten, normalize, object, type Span } from "./normalize";
import type { ReportRow } from "./sessions";
export const LIMIT = 500;
export type Report = {
  rows: ReportRow[];
  limited: boolean;
  start: string;
  end: string;
};
const scopeId = (s: Scope) => `${s.clientId}/${s.environment}/${s.keyId}`;
// All query values come from validated server config, never query text submitted by a browser.
export function azureQuery(scope: Scope) {
  return `AppRequests
| where tostring(Properties['fetch.scope_id']) == '${scopeId(scope)}'
| where tostring(Properties['fetch.client_id']) == '${scope.clientId}' and tostring(Properties['fetch.environment']) == '${scope.environment}' and tostring(Properties['fetch.api_key_id']) == '${scope.keyId}'
| order by TimeGenerated desc
| take ${LIMIT}
| project timestamp=TimeGenerated, id=Id, properties=Properties, success=Success`;
}
export function awsQuery(scope: Scope) {
  return `fields @timestamp, @message
| filter \`attributes.fetch.scope_id\` = '${scopeId(scope)}' and \`attributes.fetch.client_id\` = '${scope.clientId}' and \`attributes.fetch.environment\` = '${scope.environment}' and \`attributes.fetch.api_key_id\` = '${scope.keyId}'
| filter ispresent(\`attributes.langfuse.trace.name\`)
| sort @timestamp desc
| limit ${LIMIT}`;
}
export function azureSpans(payload: unknown): Span[] {
  const d = payload as {
    tables?: { columns: { name: string }[]; rows: unknown[][] }[];
    error?: unknown;
  };
  if (d.error || !d.tables?.[0]) throw new Error("Incomplete Azure result");
  const table = d.tables[0];
  return table.rows.map((row) => {
    const r = Object.fromEntries(table.columns.map((c, i) => [c.name, row[i]]));
    return {
      timestamp: String(r.timestamp),
      id: String(r.id),
      attributes: object(r.properties),
      error: r.success === false || r.success === "False",
    };
  });
}
export function awsSpans(rows: { field?: string; value?: string }[][]): Span[] {
  return rows.map((row) => {
    const r = Object.fromEntries(row.map((x) => [x.field, x.value]));
    const data = object(r["@message"]);
    return {
      timestamp: String(r["@timestamp"] || ""),
      id: String(data.spanId || ""),
      attributes: flatten(object(data.attributes)),
      error:
        object(data.status).code === 2 ||
        object(data.status).code === "ERROR" ||
        data.statusCode === "ERROR" ||
        data.statusCode === 2 ||
        object(data.status).code === "STATUS_CODE_ERROR",
    };
  });
}
const azureCredential = new DefaultAzureCredential();
export async function loadReport(
  scope: Scope,
  days: number,
  now = Date.now(),
  dependencies?: { azureToken: () => Promise<string> },
): Promise<Report> {
  if (![1, 7, 30].includes(days)) throw new Error("Invalid period");
  const end = new Date(now).toISOString();
  const start = new Date(now - days * 86400000).toISOString();
  let spans: Span[];
  if (scope.source.kind === "azure") {
    const token = dependencies
      ? await dependencies.azureToken()
      : (
          await azureCredential.getToken(
            "https://api.loganalytics.io/.default",
            { abortSignal: AbortSignal.timeout(8000) },
          )
        ).token;
    const r = await fetch(
      `https://api.loganalytics.io/v1/workspaces/${scope.source.workspaceId}/query`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          query: azureQuery(scope),
          timespan: `${start}/${end}`,
        }),
        signal: AbortSignal.timeout(20000),
        redirect: "error",
      },
    );
    if (!r.ok) throw new Error("Azure query failed");
    spans = azureSpans(await r.json());
  } else {
    const client = new CloudWatchLogsClient({
      region: scope.source.region,
      maxAttempts: 2,
    });
    let queryId: string | undefined;
    let complete = false;
    const signal = AbortSignal.timeout(22000);
    try {
      const q = await client.send(
        new StartQueryCommand({
          logGroupName: scope.source.logGroup,
          startTime: Math.floor(Date.parse(start) / 1000),
          endTime: Math.floor(now / 1000),
          queryString: awsQuery(scope),
          limit: LIMIT,
        }),
        { abortSignal: signal },
      );
      queryId = q.queryId;
      if (!queryId) throw new Error("Query not started");
      while (!signal.aborted) {
        const r = await client.send(new GetQueryResultsCommand({ queryId }), {
          abortSignal: signal,
        });
        if (r.status === "Complete") {
          spans = awsSpans(r.results || []);
          complete = true;
          break;
        }
        if (r.status !== "Running" && r.status !== "Scheduled")
          throw new Error("Query failed");
        await new Promise((resolve) => setTimeout(resolve, 500));
      }
      if (!complete) throw new Error("Query timed out");
    } finally {
      if (queryId && !complete) {
        try {
          await client.send(new StopQueryCommand({ queryId }), {
            abortSignal: AbortSignal.timeout(2000),
          });
        } catch {
          /* best effort cancellation */
        }
      }
      client.destroy();
    }
  }
  const normalized = spans!
    .map((s) => normalize(s, scope))
    .filter((r): r is ReportRow => r !== null);
  const rows: ReportRow[] = [];
  let bytes = 0;
  for (const row of normalized) {
    bytes += Buffer.byteLength(JSON.stringify(row));
    if (bytes > 1500000) break;
    rows.push(row);
  }
  return {
    rows,
    limited: spans!.length >= LIMIT || rows.length < normalized.length,
    start,
    end,
  };
}
