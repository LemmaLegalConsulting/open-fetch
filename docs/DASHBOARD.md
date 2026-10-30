# Generic activity dashboard

For test/production and API-key isolation, use [credential scopes](CREDENTIAL_SCOPES.md). New scoped credentials partition activity by consumer/environment/key; reviewer credentials must name that full scope. The simple legacy examples below remain useful for local demonstrations but do not export HTTP telemetry.

Open `/dashboard/` to sign in with a dedicated review token. The dashboard shows recent conversations, classification labels, follow-up history when captured, and host-reported outcomes. It provides 7/30/90-day views, filtering of loaded conversations, pagination, and CSV export. Counts describe loaded records, not an implied full-population statistic. Expanding a conversation fetches its history for the selected period independently of the activity page.

## Local setup

From the repository root, activate `.venv`, install requirements, and configure `.env`:

```dotenv
ENV=dev
OFFLINE_MODE=1
DASHBOARD_ENABLED=true
DASHBOARD_STORAGE=sqlite
DASHBOARD_TOKEN_CLIENT_IDS=demo=<review-token>
API_TOKEN_CLIENT_IDS=demo=<classification-token>
DASHBOARD_CAPTURE_CONTENT=false
```

Generate distinct tokens locally (for example `python -c 'import secrets; print(secrets.token_urlsafe(32))'`). Start `uvicorn app.main:app --reload`. Sign in at http://127.0.0.1:8000/dashboard/ using your review token. SQLite creates an ignored `private-data/activity.sqlite3` file. Use a separate test client ID rather than copying a real activity database into the repository.

Development mode normally bypasses API auth; when you supply an Authorization header, it is validated so activity can be attributed to the mapped client. Calls without mapped credentials are never recorded in the dashboard, even if a body claims a client_id. Send the classification token in `Authorization: Bearer ...`, plus a stable opaque `conversation_id` in each `/api/classify` request. Missing conversation IDs are represented as separate request sessions.

For production set ENV=production and use HTTPS. Dashboard authentication never bypasses credentials in dev mode. Login stores the review token in an HttpOnly, SameSite=Strict cookie, Secure outside development, with an eight-hour browser lifetime. Tokens remain valid until removed from configuration; they are not per-user SSO sessions. Sign-out removes the cookie. Do not reuse classification tokens as review tokens. Browser storage contains no model credentials.

## Capture and outcomes

`DASHBOARD_CAPTURE_CONTENT=false` stores IDs, timestamps, labels, and the no-legal-problem flag. Set it to true only when the operator intends to retain submitted descriptions, follow-up answers, and generated questions. This affects new records, not existing retained records. No names, contact details, or attorney lists are explicitly collected by this generic schema. Submitted text may still contain such information when content capture is enabled.

A host application can submit a separate completion outcome:

```http
POST /api/activity/outcome
Authorization: Bearer <classification-token>
Content-Type: application/json

{"conversation_id":"synthetic-example","outcome":"referred"}
```

Allowed outcomes: referred, no_match, no_legal_problem, error, in_progress. Outcomes are caller-reported; classification alone never implies a referral. The legacy OSB telemetry route is separate and is not automatically imported. No historical archive is bundled.

`DASHBOARD_RETENTION_DAYS` defaults to 30. SQLite removes expired records on subsequent writes and filters them on reads. DynamoDB uses TTL plus read-time expiry filtering; actual deletion can lag expiry. Retained data and exported CSVs may be sensitive even with content capture off. Application logs and model services are separate from dashboard capture settings.

Exports include all retained records in the selected interval up to 10,000 records (or 100 DynamoDB query pages); larger reports return an error rather than silently exporting a partial report. CSV cells protect against spreadsheet formula interpretation. Downloaded data is outside server retention controls.

See [AWS deployment](AWS_DEPLOYMENT.md) for the DynamoDB schema, IAM configuration, and deployment template.

For structured credentials, raw dashboard text requires both the global capture flag and the key’s `capture_content` permission. Duplicate review secrets and reuse of API secrets are rejected. Review cookies contain the review credential: the browser expires its copy after eight hours, but the credential itself remains valid until rotated. Use high-entropy review tokens, TLS, and hosting-layer rate limits; this is not an SSO/session-management system. Mounted SQLite directories must be writable by container UID 10001. SQLite uses secure deletion for subsequently deleted rows; backup copies have separate retention obligations.
