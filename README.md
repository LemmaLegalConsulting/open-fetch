# open-fetch

FETCH classifies legal problem descriptions using an ensemble of model providers and a keyword classifier. This source-available edition contains the API, Oregon State Bar taxonomy and classification rules, LIST integration, screening protocols, and validation tooling. It excludes private intake datasets and the organization-specific reporting dashboard.

## Research and public evaluation data

See the [FETCH public research repository](https://github.com/nonprofittechy/fetch_public_data) for public/anonymized test data, evaluation resources, and research artifacts.

The paper [*That’s So FETCH: Fashioning Ensemble Techniques for LLM Classification in Civil Legal Intake and Referral*](https://doi.org/10.3233/FAIA251588) describes FETCH’s ensemble classification and follow-up-question approach.

The recommended **Terra + Haiku classification ensemble, with Terra for question merging**, differs from the paper’s model combination. See the [October 2026 performance note](docs/MODEL_PERFORMANCE.md) for the separate public-benchmark evaluation, merge-quality pilot, costs, and limitations.

## Run locally

Use Python 3.12. From this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
ENV=dev OFFLINE_MODE=1 uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs for the interactive API. The optional review dashboard is available at `/dashboard/`; see [dashboard setup](docs/DASHBOARD.md).

```bash
curl http://127.0.0.1:8000/api/classify \
  -H 'Content-Type: application/json' \
  -d '{"text":"I need help establishing paternity.","skip_followups":true,"enable_screening_protocols":false}'
```

Offline mode uses the keyword classifier without model calls. The keyword classifier uses the bundled taxonomy keyword and negative-indicator rules. Supplementary category descriptions and configurable hint examples are included in app/data/. For the full ensemble, set OFFLINE_MODE=0 and configure your own model credentials in .env. Provider configuration and weights are in app/core/config.py. SPOT sends input to its external classification service; other enabled model providers also receive submitted input. No existing cloud account or deployment is included.

For production set ENV=production, configure [API credential scopes](docs/CREDENTIAL_SCOPES.md), and inject environment variables through your deployment. Production does not load .env automatically. Development mode permits anonymous requests only when no API credentials are configured. The example environment and container default to production; explicitly set ENV=dev for local use.

```bash
docker build -t open-fetch .
docker run --rm -p 8080:8080 --env-file .env open-fetch
```

Telemetry is disabled in the example configuration. Application logs omit model responses and exception bodies. Optional response caches and opted-in dashboard/trace content remain sensitive: keep them private and out of Git. Legacy OSB telemetry event contracts remain in the code for compatibility; configure any telemetry explicitly for your own deployment.

## Validation

```bash
source .venv/bin/activate
OFFLINE_MODE=1 TELEMETRY_ENABLED=false pytest -q
```

See [PromptFoo setup](promptfoo/README.md) to run your own evaluations. Only newly written synthetic evaluation examples are bundled. See [screening protocols](docs/SCREENING_PROTOCOLS.md) for the optional stable-ID screening flow.

## License

Required Notice: Copyright 2026 Oregon State Bar

Licensed under [PolyForm Noncommercial 1.0.0](LICENSE). See [NOTICE](NOTICE). Third-party materials retain their own applicable terms.

## Dashboard, Fly.io, and telemetry

A generic activity dashboard is included, with SQLite for local development and DynamoDB for AWS. See [dashboard setup](docs/DASHBOARD.md) and [AWS deployment](docs/AWS_DEPLOYMENT.md). The primary hosting path is [Fly.io](docs/FLY_DEPLOYMENT.md), with a storage-only AWS template for dashboard activity. An ECS template is also available. [Portable OpenTelemetry](docs/TELEMETRY.md) supports Azure Monitor and AWS/OTLP for traces and optional structured request logs.

Use `FETCH_ENABLED_PROVIDERS=compatible,keyword` with `COMPATIBLE_BASE_URL`, `COMPATIBLE_MODEL`, and `COMPATIBLE_API_KEY` for a portable OpenAI-compatible model backend, including OpenAI models hosted by Amazon Bedrock. This configuration routes classification, question merging, and screening extraction to the configured endpoint.

## Consumer and test/production isolation

Use [credential scopes](docs/CREDENTIAL_SCOPES.md) to bind each API key to a consumer, `test` or `production` environment, and nonsecret key ID. Traces and structured logs route only to that exact scope's destination, with no shared fallback. New dashboard records and review access use the same scope. Legacy keys still classify but do not export HTTP telemetry until migrated.

See [Azure Foundry and AWS Bedrock model hosting](docs/MODEL_HOSTING.md) for the Terra + Haiku ensemble with separate credentials and explicit merge/screening model selections.
