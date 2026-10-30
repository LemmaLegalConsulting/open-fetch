# Azure Foundry and AWS Bedrock model hosting

FETCH supports a named ensemble of GPT, Claude Haiku, and open-weight models. Select deployments available in your Azure/AWS account; provider names are stable FETCH identifiers, not fixed model versions. Gemini and Mistral remain legacy options for existing installations and are not needed for this configuration.

## Named ensemble

Set this JSON as one environment variable (single-line JSON is convenient for dotenv files):

```dotenv
OFFLINE_MODE=0
FETCH_ENABLED_PROVIDERS=gpt,haiku,open_weight
FETCH_AUXILIARY_MODEL=haiku
FETCH_MODEL_ENDPOINTS={"gpt":{"protocol":"openai","model_env":"GPT_MODEL","base_url_env":"GPT_BASE_URL","api_key_env":"GPT_API_KEY"},"haiku":{"protocol":"anthropic","model_env":"HAIKU_MODEL","base_url_env":"HAIKU_BASE_URL","api_key_env":"HAIKU_API_KEY","max_tokens":4096},"open_weight":{"protocol":"openai","model_env":"OPEN_WEIGHT_MODEL","base_url_env":"OPEN_WEIGHT_BASE_URL","api_key_env":"OPEN_WEIGHT_API_KEY"}}
```

Inject the nine referenced environment values using your host's secret/configuration mechanism. Each model has its own URL and credential; no credential falls back to another vendor. Environment references are required; do not put keys directly in the JSON. URLs must use HTTPS and must not contain embedded credentials, query parameters or fragments. Duplicate/reserved model names and missing configuration fail startup.

`FETCH_AUXILIARY_MODEL` selects the enabled portable endpoint used for question merging and narrative screening. If omitted, the first enabled portable endpoint is used. Follow-up answers reach every ensemble member. This configuration does not instantiate legacy Gemini/Mistral/OpenAI clients or require a direct OpenAI account. Named members have equal voting weights (1.0); keyword can be added explicitly. Screening as likely non-legal requires a strict majority of attempted provider weight. Failed members do not contribute false votes: partial failures set `degraded=true` and list nonsecret `failed_providers`; total failure returns HTTP 503. Individual label confidence contributions are capped at 1.0. Requests may select only deployment-allowed members.

## Endpoint contracts

| Protocol | Azure Foundry base URL | AWS Bedrock base URL | Authentication |
| --- | --- | --- | --- |
| `openai` | `https://RESOURCE.services.ai.azure.com/openai/v1` | `https://bedrock-runtime.REGION.amazonaws.com/openai/v1` | Bearer API key via OpenAI SDK |
| `anthropic` | `https://RESOURCE.services.ai.azure.com/anthropic` | `https://bedrock-runtime.REGION.amazonaws.com/anthropic` | `x-api-key` plus `anthropic-version: 2023-06-01` |

Supply the exact supported endpoint from your model deployment. Existing Azure `cognitiveservices.azure.com/openai/v1` URLs can also be supplied. For supported Bedrock Mantle models, use the documented `/v1` OpenAI base or `/anthropic` Messages base on `bedrock-mantle.REGION.api.aws`. Do not append `/chat/completions` or `/v1/messages`; FETCH adds the operation path.

For Azure, model values are deployment names. For Bedrock, use the model or inference-profile identifier supported by that endpoint and Region. Availability in a catalog does not imply support for every API. GPT and the open-weight member must support Chat Completions; Haiku uses Messages. This release does not implement SigV4 signing, automatic credential refresh, or Entra token acquisition. Use API-key authentication, rotate keys through the hosting platform and restart workers. A cloud IAM access-key ID is not a Bedrock model API key.

Both protocols preserve conversation messages and reject truncated, refused, tool-only or empty completions. Classification JSON is parsed locally. Claude's output token budget defaults to 4096 and can be configured per endpoint. These adapters deliberately avoid optional model-specific temperature, reasoning or structured-output parameters.

## Hosting

The container can run on Fly.io while using either model cloud, independently of DynamoDB storage and scoped telemetry. On Fly, replace `FETCH_ENABLED_PROVIDERS` in `fly.toml` and inject the registry, auxiliary name and referenced settings with Fly secrets/configuration. The optional ECS template is a single-compatible-model example: for the ensemble, replace that container's model environment entries and add all three model secrets to its Secrets Manager JSON and `Secrets` list. Do not merely set the registry without injecting its references. Azure container hosts use the same image and environment settings; no Azure application-hosting template is bundled.

The original `COMPATIBLE_MODEL`, `COMPATIBLE_BASE_URL`, `COMPATIBLE_API_KEY` configuration remains supported for a single Chat Completions endpoint.

## Verification and rollout

Offline HTTP contract tests cover Azure and Bedrock request URLs, authentication headers, three model members, multi-turn input, auxiliary calls, incomplete responses, credential separation and cleanup. No live model or cloud account has been called. Before rollout, deploy approved model versions in each account and run your own synthetic validations through a `test` credential. Check classification quality, latency, quotas, billing, model/API support and the expected isolated trace destination before switching production. Unit tests do not establish classification equivalence between replacements.

Official references checked for this implementation:

- [Azure Claude Messages endpoints and API-key headers](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/use-foundry-models-claude)
- [Azure SDK and endpoint overview](https://learn.microsoft.com/en-us/azure/foundry/how-to/develop/sdk-overview)
- [Bedrock Chat Completions](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-chat-completions.html)
- [Bedrock Messages API](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-messages-api.html)
- [Bedrock model/API compatibility](https://docs.aws.amazon.com/bedrock/latest/userguide/models-api-compatibility.html)
