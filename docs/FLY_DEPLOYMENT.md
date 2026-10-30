# Primary deployment: Fly.io

Run the FETCH API and review dashboard in one Docker-based Fly app. Model hosting and telemetry hosting are independent choices: the app can call an OpenAI-compatible endpoint and export telemetry to Azure Monitor or AWS through OTLP.

The supplied `fly.toml` uses one shared CPU, 1 GB RAM, HTTPS, `/health` checks, and automatic stop/start with zero machines kept warm. This reduces idle compute usage but allows cold starts; set `min_machines_running=1` if responsiveness matters more. Fly storage, IPs, networking, and external services can still incur charges while machines are stopped. No fixed cost estimate is implied.

## Setup

1. Create a Fly app in the host's organization, using your chosen app name. The checked-in configuration has no account-specific app name; pass `--app YOUR_APP` to Fly commands.
2. Choose the model backend and set `COMPATIBLE_BASE_URL`, `COMPATIBLE_MODEL`, and `COMPATIBLE_API_KEY` with Fly secrets. These may target OpenAI, Bedrock, or another compatible host. `FETCH_ENABLED_PROVIDERS=compatible,keyword` restricts model calls to that endpoint. Bedrock is optional.
3. Configure `API_CREDENTIALS` with separate test/production keys and inject its referenced token variables through Fly secrets. Map separate review credentials to the full consumer/environment/key scopes in `DASHBOARD_TOKEN_CLIENT_IDS`. See [credential scopes](CREDENTIAL_SCOPES.md); do not reuse test and production credentials.
4. Provision persistent dashboard storage. The default uses DynamoDB: deploy `infra/aws/activity-storage.json` and set `DASHBOARD_TABLE` plus the AWS Region. Grant the app only the table/index actions in `infra/aws/activity-policy.example.json`, replacing its placeholders. This storage-only stack creates no ALB, ECS service, model resources, or cloud credentials.
5. Supply AWS credentials using the host's preferred credential mechanism. Boto3 uses its standard credential chain. A scoped access key can be injected via Fly secrets (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_SESSION_TOKEN` if applicable). Temporary tokens must be refreshed by the host; do not paste short-lived credentials and assume automatic renewal. Fly also supports [AWS role federation](https://fly.io/blog/oidc-cloud-roles/); integrate that with your approved credential process if avoiding stored access keys. Federation is not automatically configured by this repository.
6. Configure exact `TELEMETRY_SCOPE_DESTINATIONS` for each credential scope, using [Azure or AWS OpenTelemetry](TELEMETRY.md). Leave telemetry disabled until the destination is configured. On Fly, AWS can serve only the telemetry/storage functions; no AWS application hosting is required.
7. Review `fly.toml`, then deploy with `fly deploy --app YOUR_APP --ha=false`. Open `https://YOUR_APP.fly.dev/dashboard/` and exercise a synthetic classification with a mapped API token. These commands are instructions for the operator; no deployment has been performed.

Inject secrets with the Fly CLI or the host's secret-management workflow. Avoid placing real credentials in `fly.toml`, shell scripts, or committed examples. `ENV=production` means the application consumes environment variables, not a local .env file.

## Storage choices

The container filesystem is ephemeral; do not use the default SQLite path for production on Fly. DynamoDB lets multiple Machines share activity and survive redeployments. If you want no AWS activity database, use a persistent Fly volume, set `DASHBOARD_STORAGE=sqlite` and `DASHBOARD_SQLITE_PATH=/data/activity.sqlite3`, mount that volume at `/data`, and constrain deployment to one Machine. Volumes are not shared databases; multi-Machine SQLite needs a separate replication design. Backups and retention remain the host's responsibility. You can also disable the dashboard entirely with `DASHBOARD_ENABLED=false`.

Application stdout/stderr remains available through Fly logs. Portable request traces and structured request-summary logs can be exported separately via OpenTelemetry; they do not require moving the app to AWS. See [telemetry](TELEMETRY.md) for collector configuration and the distinction between operational telemetry and dashboard records.

The all-AWS [ECS template](AWS_DEPLOYMENT.md) remains an alternative for hosts that want it.

Configuration reference: [Fly app configuration](https://docs.fly.io/reference/configuration).
