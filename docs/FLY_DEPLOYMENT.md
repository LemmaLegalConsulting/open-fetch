# Deploy the API on Fly.io

The Fly app hosts only the FETCH API. Models and telemetry may be hosted separately in AWS or Azure. There is no activity database, persistent volume requirement, or dashboard UI on this host.

1. Create a Fly app and supply its name with `--app YOUR_APP`.
2. Configure the [Terra + Haiku endpoints and merge role](MODEL_HOSTING.md). Override the single-provider example in `fly.toml` and inject endpoint credentials through Fly secrets.
3. Configure [API credential scopes](CREDENTIAL_SCOPES.md) with distinct test/production credentials for every consumer/key.
4. Configure exact scoped OpenTelemetry destinations using the [telemetry guide](TELEMETRY.md). Keep telemetry disabled until destinations and referenced secrets are ready. AWS can host only the collector/telemetry backend; it need not host the API or a database.
5. Review `fly.toml`, deploy with `fly deploy --app YOUR_APP --ha=false`, and exercise `/health` and synthetic `/api/classify` calls. Inspect the external telemetry backend for correctly isolated traces.

The sample uses a shared CPU, 1 GB RAM, HTTPS, health checks, and automatic stop/start with zero warm Machines. This permits cold starts. Set `min_machines_running=1` if latency and less frequent exporter shutdown matter more than idle cost. Batch export is best-effort; graceful shutdown flushes buffered telemetry, but abrupt termination can lose it.

Inject secrets through the hosting platform; production does not load `.env`. Application stdout/stderr remains available through Fly logs. The included [telemetry viewer](../viewer/README.md) deploys as a separate app (on Fly, Azure, or Lambda) and queries the telemetry backend with scoped read authorization. No cloud deployment is performed by these instructions.

Configure [API rate limiting](RATE_LIMITING.md) before scaling beyond one process: inject `FETCH_RATE_LIMIT_BACKEND=redis` and `FETCH_RATE_REDIS_URL` through the host secret mechanism for shared quotas. Memory limits reset on restart and multiply across replicas, including deployment overlap.

## Stable DNS and migration

The app's `YOUR_APP.fly.dev` hostname remains stable across releases. Allocate ingress addresses with `fly ips allocate-v6 --app YOUR_APP` and `fly ips allocate-v4 --shared --app YOUR_APP`. Add your custom hostname with `fly certs add YOUR_HOSTNAME --app YOUR_APP`, then follow `fly certs setup YOUR_HOSTNAME --app YOUR_APP` for the exact DNS validation records. In Cloudflare, a DNS-only CNAME to the app's assigned CNAME target is straightforward; replace any conflicting A/AAAA records for that hostname. If you enable Cloudflare proxying, use Full (strict) TLS and follow Fly's ownership/certificate validation instructions.

When moving an existing hostname, first validate the new app through its `.fly.dev` address, including authentication and a synthetic live model request. Coordinate certificate registration and DNS changes so the old app remains available during the transition. Preserve the old DNS records for rollback. Changing DNS alone does not register a hostname with Fly.

Fly's managed Upstash integration has its own plans; do not assume a free allowance from a direct Upstash account also applies through Fly. Disable eviction for a rate-limit database and use a dedicated database. Set `min_machines_running=1` if avoiding cold starts is required; the example's zero minimum prioritizes idle cost.
