# Optional all-AWS deployment and compatible model endpoints

For the primary Fly-hosted setup, see [Fly deployment](FLY_DEPLOYMENT.md). AWS telemetry alone is covered in [portable telemetry](TELEMETRY.md); it does not require the ECS stack below.

The FastAPI container serves only the API. The included CloudFormation template deploys ECS Fargate behind an HTTPS Application Load Balancer, with CloudWatch application logs. There is no Azure dependency for this deployment. No cloud resources have been created by preparing this repository.

## Model configuration

Use the standard OpenAI SDK through FETCH's `compatible` provider. For Bedrock Runtime, for example:

```dotenv
OFFLINE_MODE=0
FETCH_ENABLED_PROVIDERS=compatible,keyword
COMPATIBLE_BASE_URL=https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1
COMPATIBLE_MODEL=openai.gpt-oss-120b-1:0
COMPATIBLE_API_KEY=<your Bedrock API key>
```

This example uses an OpenAI model hosted by AWS. The credential is a **Bedrock API key**, not an OpenAI API key or an AWS access-key ID. Choose an endpoint/model or inference-profile ID that supports Chat Completions in your AWS Region and account. Bedrock Mantle endpoints can also be used: supply the exact `/v1` or `/openai/v1` URL given on the model card. Do not append `/chat/completions` to the configured base URL.

The same configuration works with other OpenAI-compatible hosts by changing URL, model, and credential. All three operations—classification, semantic merging, and optional screening extraction—use that endpoint/model. The adapter does not force model-specific reasoning, temperature, JSON-mode, or Responses API parameters. JSON instructions remain in the prompts; incomplete or invalid model responses fail rather than being accepted as classifications. Existing ensemble providers remain available when explicitly configured.

`FETCH_ENABLED_PROVIDERS` is the deployment allowlist. In the configuration above, requests cannot enable Gemini, SPOT, or another external provider. Offline mode still disables model calls. This adapter uses bearer authentication through the standard OpenAI SDK; it does not implement IAM SigV4 signing or automatic Bedrock key refresh. The ECS task role does not grant model inference access. Rotate the Bedrock key and redeploy tasks when needed.

AWS documentation: [Chat Completions and SDK example](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-chat-completions.html), [endpoint compatibility](https://docs.aws.amazon.com/bedrock/latest/userguide/endpoints.html), and [model/API compatibility](https://docs.aws.amazon.com/bedrock/latest/userguide/models-api-compatibility.html). Availability and permissions must be verified in the target account before rollout.

## Infrastructure prerequisites

Supply an existing VPC, two public subnets for the ALB, private subnets for Fargate, an ACM certificate, and a private ECR image URI. Private subnets need NAT or VPC endpoints/routing for ECR, Secrets Manager, CloudWatch and the model endpoint. The template does not create network infrastructure or DNS. Build the image for the Fargate CPU architecture (default x86_64).

Create a Secrets Manager secret in the deployment Region containing these four JSON string values. Generate distinct high-entropy tokens; these names are placeholders, not usable credentials:

```json
{
  "API_CREDENTIALS": "{\"host-prod\":{\"client_id\":\"host\",\"environment\":\"production\",\"token_env\":\"API_KEY_PRODUCTION\",\"capture_content\":false},\"host-test\":{\"client_id\":\"host\",\"environment\":\"test\",\"token_env\":\"API_KEY_TEST\",\"capture_content\":false}}",
  "API_KEY_PRODUCTION": "<production-api-secret>",
  "API_KEY_TEST": "<test-api-secret>",
  "COMPATIBLE_API_KEY": "<Bedrock API key>"
}
```

The structured registry assigns each key a consumer and test/production environment. See [credential scopes](CREDENTIAL_SCOPES.md). Add corresponding task secret bindings if your registry references additional environment variables. Telemetry remains disabled in this template until exact scoped destinations and their secret references are configured. The template assumes the secret's default AWS-managed encryption key; a customer-managed KMS key also requires an explicit decrypt grant on the execution role and key policy.

Build and push the Docker image to your own ECR repository, then deploy `infra/aws/template.json` using your account's approved CloudFormation process. The template requires `CAPABILITY_IAM`. Parameters include VPC/subnets, certificate, image, secret ARN, compatible model/base URL, access CIDR. The default model endpoint is us-east-1; adjust it when deploying elsewhere. DNS must point your certificate's hostname to the output ALB DNS name. Use `/health` and `/docs` to inspect the API.

No activity table or database access policy is created. The execution role pulls the image, writes CloudWatch logs, and reads the supplied secret. No account IDs, domains, real tokens, or Suffolk/OSB infrastructure identifiers are embedded. Secrets are injected into tasks at startup; changing the secret requires replacing/restarting tasks.

The template starts one task, enables deployment rollback, and uses `/health` for liveness. A healthy process does not prove Bedrock permissions, quotas, or model quality. Before serving real requests, use synthetic classification flows against the deployed service. Increase task count or add autoscaling as required by the host's operations team.

## Telemetry and viewing

CloudWatch application-log retention is 30 days. Configure scoped [OpenTelemetry export](TELEMETRY.md) for traces and structured summaries; no activity database or UI is deployed. View traces in the backend or the [separate Lambda-capable viewer](../viewer/README.md). Model hosting may have its own logging outside this repository.

For the recommended Terra + Haiku ensemble, see [model hosting](MODEL_HOSTING.md), including the additional ECS environment/secret wiring. The base ECS template remains a single-model example.

Configure [API rate limiting](RATE_LIMITING.md) before scaling beyond one process: inject `FETCH_RATE_LIMIT_BACKEND=redis` and `FETCH_RATE_REDIS_URL` through the host secret mechanism for shared quotas. Memory limits reset on restart and multiply across replicas, including deployment overlap.
