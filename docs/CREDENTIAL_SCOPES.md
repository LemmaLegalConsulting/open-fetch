# Isolation by consumer, environment, and API key

Every API key that exports telemetry must have a server-configured identity:

```
consumer / environment / nonsecret-key-ID
```

A production key and a test key for the same consumer are different scopes. Two production keys for that consumer are also different scopes. A conversation ID never determines scope. The environment must be `test` or `production`; it is independent of the host process's `ENV` and `TELEMETRY_ENVIRONMENT` settings.

## Credential registry

Set `API_CREDENTIALS` to JSON in server configuration. Values reference environment variables containing actual secrets:

```json
{
  "school-prod-one": {
    "client_id": "school",
    "environment": "production",
    "token_env": "SCHOOL_PROD_TOKEN",
    "capture_content": false
  },
  "school-test-one": {
    "client_id": "school",
    "environment": "test",
    "token_env": "SCHOOL_TEST_TOKEN",
    "capture_content": false
  }
}
```

Inject independent, high-entropy values for `SCHOOL_PROD_TOKEN` and `SCHOOL_TEST_TOKEN` through Fly secrets, AWS Secrets Manager, or your host's secret-management system. Never put actual secrets in this file. A caller sends its secret as `Authorization: Bearer ...`. It cannot change consumer, environment, key ID, scope ID, or capture policy through request JSON or headers.

`client_id` and the registry's key IDs use letters, digits, periods, hyphens, and underscores; `/` is reserved as the scope separator. IDs must start with a letter/digit and be at most 128 characters. Missing secret variables, duplicate secret values, malformed configuration, and a secret assigned in both the structured and legacy client maps produce a generic 503 authentication error; secrets are never included in the error. Unknown credentials receive 401.

`capture_content` defaults to false and is a **per-key** permission. A consumer-wide legacy content allowlist cannot broaden it. Request-summary logs never include content regardless of this permission. Use test credentials for tests: a request made with a production credential remains production even if the body claims `is_test=true`.

## Exact destinations

Set `TELEMETRY_SCOPE_DESTINATIONS` with the complete scope IDs:

```json
{
  "school/production/school-prod-one": {
    "exporter": "azure",
    "connection_string_env": "SCHOOL_PROD_AZURE_CONNECTION"
  },
  "school/test/school-test-one": {
    "exporter": "otlp",
    "endpoint_env": "SCHOOL_TEST_COLLECTOR_ENDPOINT",
    "headers_env": "SCHOOL_TEST_COLLECTOR_HEADERS"
  }
}
```

Both scopes may use Azure, both may use AWS collectors, or they may use different vendors. Configure distinct destinations for each scope. A missing destination, invalid configuration, or exporter failure suppresses that scope's export; there is **no fallback** to a consumer destination, process-wide environment destination, or shared exporter. Classification can still succeed while telemetry is unavailable.

Traces and structured logs follow the same scope. Root/child spans and summary logs contain `fetch.client_id`, `fetch.environment`, `fetch.api_key_id`, and `fetch.scope_id`. Exporter resources use the authenticated environment. The raw API secret and its hash are never emitted. Providers are cached separately for each scope, and request context is reset after success or failure; concurrent requests cannot switch each other's routes.

The application rejects identical resolved destinations configured for multiple scopes, independently for traces and logs. A shared collector must expose distinct authenticated routes/headers and enforce separate downstream resources. FETCH cannot prove that two different URLs or connection strings don't ultimately reach the same storage account: the operator must configure separate Azure resources or AWS destinations/access policies. Attribute filtering alone is not a privacy boundary. AWS collector examples should be instantiated/configured per destination, or extended with authenticated routing by the operator.

Restart after changing routing or credentials, because exporters are cached. For a distinct new key, assign a new key ID and destination. Rotating a secret under the same key ID intentionally retains that logical scope. Historical records are not automatically migrated.

## Dashboard records

Structured credentials also partition new dashboard records by the full scope ID. Review tokens use that scope as the left side of `DASHBOARD_TOKEN_CLIENT_IDS`:

```dotenv
DASHBOARD_TOKEN_CLIENT_IDS=school/production/school-prod-one=<production-review-secret>,school/test/school-test-one=<test-review-secret>
```

This is read authorization, not a UI filter: a test reviewer cannot read production records, even with the same conversation ID or a forged query parameter. One review token reads one exact partition. Legacy records retain their old consumer partition and are not merged with new scoped records. For scoped keys, dashboard text capture requires both `DASHBOARD_CAPTURE_CONTENT=true` and that key’s `capture_content=true`; a global flag cannot override a key’s denial. Legacy mapped keys retain the global dashboard opt-in behavior.

## Migration and development

Legacy `API_TOKENS` and `API_TOKEN_CLIENT_IDS` still authenticate for classification, but authenticated HTTP requests using those credentials do **not** export telemetry until migrated to `API_CREDENTIALS`. Anonymous dev calls also never export. This prevents an unknown environment/key identity from slipping into a shared production destination.

Legacy client/shared exporter settings remain available for existing internal instrumentation when no structured registry exists. Once `API_CREDENTIALS` is configured, unscoped internal/evaluation calls are also suppressed; explicitly bind an authenticated scope in trusted integration code or call the API using a test credential. Supplied PromptFoo configurations disable telemetry by default.

To migrate: inject the secret references, define the registry and exact destinations, assign separate review credentials to the new partitions, restart, then exercise synthetic test and production requests. Inspect both destination resources to verify separation before using real data. No cloud resources or keys are provisioned by this configuration.
