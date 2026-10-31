# Separate FETCH telemetry viewer

This Next.js application carries over the original Azure viewer's conversation cards, follow-up threads, classification history, and CSV export. It is deployed independently from the classification API and reads existing OpenTelemetry data. It does not collect activity, create an activity database, or add calls to the classification request path. No historical snapshots, recipient lists, organization identifiers, or real intake data are bundled.

## What is portable

OpenTelemetry standardizes emission, not querying. `lib/backends.ts` implements Azure Log Analytics and AWS CloudWatch Logs Insights adapters; `lib/normalize.ts` maps their span representations into the same UI records. An additional backend needs a read adapter returning the `Span` contract and the same scope checks. This is a FETCH-aware viewer of OpenTelemetry spans, not a universal OTLP query API or general-purpose trace waterfall.

- Azure: queries `AppRequests` in the configured workspace using `DefaultAzureCredential` (managed identity in Azure; developer credentials when run locally).
- AWS: queries CloudWatch Transaction Search span records in `aws/spans` (or a compatible scoped log group). It reads span attributes, not the content-free request-summary logs. Configure Transaction Search and the API's OTLP export separately; the viewer does not enable ingestion or change retention.
- Model hosting is independent: a viewer on AWS can query Azure with suitable Azure credentials, and a viewer on Azure/Fly can query AWS with suitable AWS credentials. The supplied AWS template grants only CloudWatch query access.

## Authentication and isolation

Use a tenant-specific Entra issuer, Cognito user-pool issuer, or compatible OIDC provider with authorization-code flow and PKCE. Register `https://YOUR_VIEWER/api/auth/callback` as the exact redirect URI. Sign-in verifies signature, issuer, audience, expiration, state, and nonce. No Azure identity headers are trusted by this app. HTTPS is required, including local testing through an HTTPS reverse proxy.

Copy `.env.example` to `.env.local` for local development. Set the origin, random session secret (at least 32 bytes), and OIDC client credentials. Production receives settings through the host's secret mechanism. Entra uses `OIDC_CLIENT_AUTH=post`; Cognito clients with a secret use `basic`. Never put credentials in `NEXT_PUBLIC_*` variables. Session cookies are Secure, HttpOnly, SameSite=Lax, signed, and expire after one hour; signing out clears the viewer session, not the upstream SSO session. Rotating the session secret invalidates all sessions.

`VIEWER_CONFIG_JSON` contains only server-owned source definitions and grants. Map the validated OIDC **sub** claim (not an email or Entra object ID) to exact scope IDs:

```json
{
  "scopes": {
    "school/test/key-one": {
      "clientId": "school",
      "environment": "test",
      "keyId": "key-one",
      "allowContent": false,
      "source": {"kind": "aws", "region": "us-east-1", "logGroup": "aws/spans"}
    }
  },
  "viewers": {"OIDC_SUBJECT_FROM_YOUR_IDENTITY_ADMIN": ["school/test/key-one"]}
}
```

For Azure, replace `source` with `{"kind":"azure","workspaceId":"YOUR_WORKSPACE_GUID"}`. Grant the viewer's managed identity query access only to the necessary workspace. A reviewer can select only explicitly granted scopes. Grants are rechecked on every request, including CSV downloads. Query text and backend addresses cannot be supplied by browsers. Results are checked again against all four trusted attributes: `fetch.scope_id`, `fetch.client_id`, `fetch.environment`, and `fetch.api_key_id`. A conversation ID never grants access. Same conversation IDs under different keys/environments do not merge.

The viewer's server-side read identity is privileged: do not give reviewers its credentials or direct access to a shared workspace/log group. Queries enforce application-level access for reviewers, but cannot create physical storage isolation. In particular, AWS Transaction Search's `aws/spans` is account/Region-wide; separate OTLP receiver URLs alone do not isolate that underlying storage. Keep the API's scoped destinations and infrastructure access boundaries in place; use separately isolated accounts/resources or an appropriately secured routing arrangement where required. A viewer deployment should have only the sources its audience needs. The template covers one account/log group; cross-account roles are not automatically provisioned.

Content needs both `allowContent: true` in the viewer scope and `fetch.content_captured=true` on the original span. This cannot recover text that was never captured. Existing pre-scope Azure traces and the private historical archive are deliberately not imported. The old deployment can remain in place during migration. New generic viewer access requires newly scoped traces; do not invent API-key ownership for legacy records.

## Queries, completeness, and costs

The UI reads 1, 7, or 30 days, with a 500-root-span limit and a bounded response size. It groups records before paginating conversations. Limit warnings mean histories/counts may be incomplete; CSV export refuses limited results. Individual source fields may also have been truncated by ingestion. There is no auto-refresh, persistent cache, or activity copy. Pagination/CSV perform a fresh bounded query; timestamps can shift between requests.

CloudWatch queries are stopped on timeout when possible. Azure queries use a client timeout. Query errors return generic messages and are not logged with content, credentials, or backend error bodies. API rate/scan quotas, cloud retention, and ingestion loss still apply. A recorded referral means a match was returned, not that a lawyer was contacted or retained.

## Build and validate

```bash
npm ci
npm test
npm run typecheck
npm run build
# Local HTTPS proxy should forward to this server:
npm run start
# Portable container, separate host/app from FETCH:
docker build --target runtime -t fetch-viewer .
```

Tests use synthetic spans and mocked auth/query responses. A successful build does not verify live OIDC, Azure RBAC, AWS account access, or your stored span schema. Before production, run a synthetic test/production intake, sign in with separate scoped viewers, verify cross-scope denial and CSV content, and confirm content-disabled traces reveal no input/output. Keep private exports out of Git.

## Low-idle-cost AWS deployment

The [CloudFormation template](infra/aws-lambda.yaml) runs a separate container on ordinary Lambda with the [AWS Lambda Web Adapter](https://github.com/aws/aws-lambda-web-adapter). It has a Function URL, no ALB, NAT gateway, database, VPC attachment, or provisioned concurrency. It scales to zero. At low review traffic this avoids the fixed cost of an always-running server; [Lambda requests and duration](https://aws.amazon.com/lambda/pricing/), CloudWatch scanning/ingestion/storage, ECR, Secrets Manager, identity-provider usage, and transfer still cost money. No fixed monthly-price promise is implied. Cold starts and query polling add viewer latency, not classification latency.

1. Build the `lambda` target for `linux/amd64`, push it to an ECR repository in your account, and use an immutable image digest.
2. Create the OIDC app and a Secrets Manager JSON containing all seven settings named by the template. Use a temporary HTTPS placeholder for `VIEWER_ORIGIN` on the first deployment; it denies useful sign-in until replaced with the real URL.
3. Deploy the template with your image URI, config secret ARN, and exact span log group ARN. The CloudFormation execution identity must read the config secret. The Lambda role has bounded `StartQuery` access; `GetQueryResults`/`StopQuery` require wildcard resources, and query IDs never leave the server. Query and export routes require signed viewer sessions.
4. Set the secret's `VIEWER_ORIGIN` to the resulting Function URL origin (without trailing slash) and register its callback with the OIDC app. Update the function configuration or the stack using a new secret version/reference so dynamic references refresh. Changing a secret alone does not update environment variables on an existing function.
5. Validate authentication, scopes, and synthetic spans before sharing the URL. The public Function URL uses `AuthType: NONE` because OIDC is enforced inside the app. Unauthenticated invocations still consume Lambda capacity; the template limits concurrency to five. Add edge rate controls if needed for your exposure level.

AWS docs: [Transaction Search span storage](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-Transaction-Search.html), [span queries](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-Transaction-Search-search-analyze-spans.html), [query results](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_GetQueryResults.html).

## Azure or Fly deployment

Build the `runtime` container target and deploy it as its own Azure App Service/container app, with a managed identity allowed to query the configured workspace. Configure the OIDC application and viewer settings as above. Existing App Service Entra authentication can remain as an outer gate, but this app still verifies its own OIDC session. No header-only bypass is included. Use `/api/health` as the health probe and port 8080. See [Azure AppRequests schema](https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/apprequests).

For Fly, use [infra/fly.toml](infra/fly.toml) with a separate app name, inject viewer/auth/backend credentials as secrets, and choose the `runtime` target. It needs no persistent volume. Do not mount this UI in the classification API or share its API credentials.
