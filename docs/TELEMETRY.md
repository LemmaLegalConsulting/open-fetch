# Portable telemetry: Azure Monitor and AWS

**HTTP telemetry requires explicit consumer/environment/API-key scope.** Configure [credential scopes](CREDENTIAL_SCOPES.md) and `TELEMETRY_SCOPE_DESTINATIONS` first. The shared destination examples below describe exporter settings for internal/legacy instrumentation; they are not fallback destinations for scoped HTTP requests.

FETCH owns the instrumentation and privacy rules. Export destinations are configuration:

| Signal | Azure | AWS |
| --- | --- | --- |
| Request/provider traces | Azure Monitor trace exporter | OTLP/HTTP to a collector, then SigV4 to CloudWatch/X-Ray |
| Optional request-summary logs | Azure Monitor log exporter | OTLP/HTTP to a collector, then SigV4 to CloudWatch Logs |
| Per-client isolation | Generic destination map | The same destination map |
| Content policy | Explicit client allowlist | The same explicit client allowlist |

AWS's native OTLP endpoints support logs, metrics, and traces. FETCH currently emits traces and optional request-summary logs; it does not claim to emit metrics or automatically forward all Python application logs. Provider latency/token usage are recorded as trace attributes where available. Existing Langfuse attributes remain for compatibility; `fetch.*` correlation attributes are common across destinations.

## Shared destination: Azure

```dotenv
TELEMETRY_ENABLED=true
TELEMETRY_EXPORTER=azure
AZURE_MONITOR_CONNECTION_STRING=<your connection string>
TELEMETRY_LOGS_ENABLED=true
TELEMETRY_SERVICE_NAME=fetch
TELEMETRY_ENVIRONMENT=production
TELEMETRY_CONTENT_CLIENT_IDS=
```

Azure uses the Python Azure Monitor exporters already included in requirements. No AWS credentials or collector are required for this path.

## Shared destination: AWS through OTLP

```dotenv
TELEMETRY_ENABLED=true
TELEMETRY_EXPORTER=otlp
OTEL_EXPORTER_OTLP_ENDPOINT=http://YOUR_PRIVATE_COLLECTOR:4318
TELEMETRY_LOGS_ENABLED=true
TELEMETRY_SERVICE_NAME=fetch
TELEMETRY_ENVIRONMENT=production
TELEMETRY_CONTENT_CLIENT_IDS=
```

`aws` and `adot` are aliases for `otlp`; they do not implement AWS signing in the Python application. For a base endpoint FETCH appends `/v1/traces` and `/v1/logs`. Exact signal endpoints can instead be set with `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` and `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT`. Headers can be supplied through `OTEL_EXPORTER_OTLP_HEADERS` or the signal-specific `..._TRACES_HEADERS` / `..._LOGS_HEADERS` variables, in comma-separated `name=value` form. This exporter uses HTTP/protobuf, not gRPC.

[infra/otel/aws-collector.yaml](../infra/otel/aws-collector.yaml) is an OpenTelemetry Collector Contrib configuration with OTLP receivers, batching, memory limiting, and separate SigV4 exporters for traces and logs. Use a distribution/version containing `sigv4auth` and `otlphttp`; pin the collector image in your deployment. Give the collector AWS credentials/role permissions for the destinations. Create the CloudWatch log group/stream, then set `AWS_REGION`, `AWS_LOG_GROUP`, and `AWS_LOG_STREAM` in the collector environment.

CloudWatch's native OTLP trace endpoint requires Transaction Search to be enabled in the target account/Region. That is an account-wide ingestion setting: review it with the AWS administrator instead of enabling it automatically. The collector requires `xray:PutTraceSegments` and `logs:PutLogEvents` permissions for the selected destinations; follow AWS's setup guide for additional account-level setup permissions. The config does not create resources or retention policies.

Run the collector on the same host or on a private reachable service. On Fly, a private collector Machine can receive telemetry from the app without exposing port 4318 publicly; use an IPv6 listener such as `[::]:4318` for Fly's private network. Do not add the unauthenticated OTLP receiver to a public `http_service`. If traffic crosses a public network, configure TLS and collector authentication, then set the corresponding OTLP headers. AWS signing happens on the collector-to-AWS hop. FETCH must not point its unsigned trace exporter directly at the AWS trace endpoint.

Sources: [AWS OTLP endpoints](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-OTLPEndpoint.html), [AWS OpenTelemetry quickstart](https://docs.aws.amazon.com/en_en/AmazonCloudWatch/latest/monitoring/GettingSetup.html).

## Legacy per-client destinations

For HTTP requests use the exact scoped destinations in [CREDENTIAL_SCOPES.md](CREDENTIAL_SCOPES.md). The map below remains for legacy internal integrations; it does not override or provide fallback for scoped requests.

Set `TELEMETRY_CLIENT_DESTINATIONS` to a JSON object in secret configuration:

```json
{
  "client-a": {
    "exporter": "azure",
    "connection_string_env": "CLIENT_A_AZURE_CONNECTION"
  },
  "client-b": {
    "exporter": "otlp",
    "endpoint_env": "CLIENT_B_OTLP_ENDPOINT",
    "headers_env": "CLIENT_B_OTLP_HEADERS"
  }
}
```

Each entry can use a base `endpoint` or exact `traces_endpoint` and `logs_endpoint`; each string field also supports an `_env` reference. Headers can be a JSON object or a `headers_env` reference. Direct values are accepted but references make secret handling simpler. Include both signal endpoints if only signal-specific URLs are supplied and logs are enabled.

The legacy `TELEMETRY_AZURE_CLIENT_CONNECTION_STRINGS` map is still accepted. Generic entries override legacy entries for the same client. `TELEMETRY_ISOLATED_CLIENT_IDS` can reserve clients before their destinations exist. Missing credentials, malformed maps, unsupported exporters, and initialization errors fail closed: they do not fall back to shared telemetry. Client identity comes from server-side API-token mapping. Child spans stay on the root span's destination; structured logs use the same routing configuration. Restart the process after destination/credential changes because providers are cached.

## Privacy and logs

For scoped HTTP requests, `capture_content` in the credential registry is the only trace-content permission; the consumer-wide allowlist below cannot broaden it.

No organization receives automatic content permission in this public edition. Isolation alone also does not grant content permission. An empty `TELEMETRY_CONTENT_CLIENT_IDS` retains only correlation/status on request spans, and does not emit provider-content spans. To opt a client into content traces, add its ID explicitly. `TELEMETRY_CAPTURE_FULL_QUERY=false` disables separate full-query chunk attributes; it is **not** a general redaction switch when content is allowed, because prompts/answers may still occur in input/output attributes.

Request-summary logs are enabled independently with `TELEMETRY_LOGS_ENABLED=true` (default false), while `TELEMETRY_ENABLED=true` enables the overall pathway. They contain only the event name, permitted correlation IDs, completion/error status, and error class. They never contain raw exception messages, prompts, answers, or arbitrary Python log messages. IDs themselves must be opaque and free of personal information. Structured summaries follow successful trace initialization; a missing trace destination suppresses both signals. Export is best-effort and is not an audit ledger.

## Capture and viewing

OpenTelemetry is the activity capture pathway. The API does not write a separate SQLite/DynamoDB activity database or host a review UI. Use Azure Monitor or your AWS telemetry backend for viewing, or deploy the included [standalone viewer](../viewer/README.md) against those backends. Named reviewers receive server-side scope grants for both the conversation UI and CSV downloads. Backend read permissions must enforce the same consumer/environment/API-key isolation; UI filters alone are insufficient.

Trace and log exporters batch in the background. Classification and referral-event requests do not force-flush or wait for export. Graceful API shutdown performs a bounded best-effort flush. Abrupt termination can lose buffered telemetry; this is not a durable audit ledger. Allow shutdown time and consider keeping a Fly Machine warm if frequent stops impair trace delivery.

Ordinary application logs use stdout and omit model output/exception bodies. The legacy referral event schema can contain contact and attorney details when a scope permits content. `TELEMETRY_CAPTURE_FULL_QUERY=false` affects query text, not every content field. Exact scoped destinations and per-key content permissions remain required.
