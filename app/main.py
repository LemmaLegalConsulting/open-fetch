from contextlib import asynccontextmanager
import hmac
import os

if os.getenv("ENV") != "production":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

from fastapi import FastAPI, Depends, HTTPException, status, Request
from app.core.config import TAXONOMY_MAPPING
from app.core.rate_limit import RateLimiter
from app.core.identity import (
    current_scope,
    current_client,
    http_scope_enforced,
    credential_registry,
    legacy_credentials,
)
from typing import Optional
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.models.api_models import (
    ClassificationRequest,
    ClassificationResponse,
    ReferralTelemetryEvent,
)
from app.services.classification_service import (
    ClassificationService,
    ClassificationUnavailable,
)
from app.services.screening_protocols import validate_answers


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.rate_limiter = RateLimiter()
    try:
        yield
    finally:
        from app.telemetry import flush_telemetry_async

        await app.state.rate_limiter.close()
        await flush_telemetry_async()


app = FastAPI(
    lifespan=lifespan,
    title="Legal Problem Classification API",
    description="FETCH legal problem classification API. See README.md for setup and /docs for schemas. Model providers process submitted inputs when enabled.\n\n## Organization and end-to-end tracking\nAuthenticated credentials determine client_id. Send opaque conversation_id and client_request_id values; keep personal information out of identifiers.",
    version="1.0.0",
)


@app.middleware("http")
async def request_context_and_headers(request: Request, call_next):
    scope_token = current_scope.set(None)
    client_token = current_client.set(None)
    enforce_token = http_scope_enforced.set(True)
    try:
        response = await call_next(request)
    finally:
        current_scope.reset(scope_token)
        current_client.reset(client_token)
        http_scope_enforced.reset(enforce_token)
    return response


@app.get("/health")
def health():
    return {"status": "ok"}


classification_service = ClassificationService()

security = HTTPBearer(auto_error=False)
# Also initialized for ASGI callers that do not drive lifespan (e.g. unit tests).
app.state.rate_limiter = RateLimiter()


async def verify_token(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """Authenticate once and bind trusted identity for the whole async request."""
    try:
        registry = credential_registry()
        legacy = legacy_credentials()
    except ValueError:
        raise HTTPException(503, "API credential configuration is invalid") from None
    limiter = request.app.state.rate_limiter
    peer = request.client.host if request.client else "unknown"
    scope, client = None, None
    token = credentials.credentials if credentials else None
    if token is None:
        if os.getenv("ENV") != "dev" or registry or legacy or os.getenv("API_TOKENS"):
            await limiter.failed_auth(peer)
            raise HTTPException(403, "Not authenticated")
    else:
        for secret, candidate in registry:
            if hmac.compare_digest(secret.encode(), token.encode()):
                scope, client = candidate, candidate.client_id
                break
        if scope is None:
            for secret, candidate in legacy:
                if hmac.compare_digest(secret.encode(), token.encode()):
                    client = candidate
                    break
        tokens = [
            t.strip() for t in os.getenv("API_TOKENS", "").split(",") if t.strip()
        ]
        if client is None and not any(
            hmac.compare_digest(secret.encode(), token.encode()) for secret in tokens
        ):
            await limiter.failed_auth(peer)
            raise HTTPException(
                401,
                "Invalid authentication credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
    await limiter.admitted(scope, client, token, peer, request.url.path)
    scope_token = current_scope.set(scope)
    client_token = current_client.set(client)
    enforce_token = http_scope_enforced.set(True)
    try:
        yield token
    finally:
        current_scope.reset(scope_token)
        current_client.reset(client_token)
        http_scope_enforced.reset(enforce_token)


@app.post("/api/classify", response_model=ClassificationResponse)
async def classify(payload: ClassificationRequest, token: str = Depends(verify_token)):
    """Classify a legal problem description and return relevant categories and questions.

    This endpoint analyzes the provided problem description using multiple classifier providers
    (e.g., keyword matching, LLMs like Gemini/Mistral, and taxonomy APIs). Results are aggregated
    with weighted voting to determine the most relevant legal categories and follow-up questions.

    The response includes:
    - **labels**: Top legal categories with confidence scores
    - **follow_up_questions**: Clarifying questions to refine classification
    - **likely_no_legal_problem**: Consensus analysis with weighted result and disagreement score

    Args:
      payload: Request containing problem description, taxonomy options, and debug settings.
      token: Bearer token for authentication (bypassed in dev mode).

    Returns:
      ClassificationResponse with aggregated results from all enabled providers.

    Raises:
      HTTPException: For authentication failures or invalid requests.
    """
    if payload.taxonomy_name not in TAXONOMY_MAPPING:
        raise HTTPException(422, "Unknown taxonomy")
    # Reject unknown/mismatched stable IDs as a client contract error. Displayed
    # question/answer strings are intentionally not used for rule evaluation.
    try:
        validate_answers(payload.screening_answers or [])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Token is already validated by the verify_token dependency above
    if (
        classification_service.allowed_providers is not None
        and payload.enabled_models is not None
    ):
        if set(payload.enabled_models) - set(classification_service.allowed_providers):
            raise HTTPException(
                422, "Requested models are not enabled by this deployment"
            )
    try:
        result = await classification_service.classify(
            payload,
            enabled_models=payload.enabled_models,
            client_id=current_client.get(),
        )
    except ClassificationUnavailable:
        raise HTTPException(503, "Classification is temporarily unavailable") from None
    return result


@app.post("/api/telemetry/referral-completed", status_code=202)
async def referral_completed_telemetry(
    payload: ReferralTelemetryEvent,
    token: str = Depends(verify_token),
):
    """Accept a completed OSB interaction for the shared telemetry stream."""
    from app.telemetry import record_telemetry_event

    client_id = current_client.get()
    if client_id not in {"osbar-referral", "osbar-referral-dev"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This token is not configured for OSB referral telemetry.",
        )

    event_payload = payload.model_dump(mode="json")
    # A dev credential can never claim to be production, regardless of payload.
    if client_id == "osbar-referral-dev":
        event_payload.update(environment="development", is_test=True)
        event_payload["test_reason"] = payload.test_reason or "development_site"
    elif payload.environment != "production" or payload.is_test:
        event_payload["is_test"] = (
            True if payload.environment == "development" else payload.is_test
        )
    # Old clients had no environment marker; do not invent production attribution.
    if payload.schema_version < 2 and client_id != "osbar-referral-dev":
        event_payload["environment"] = "unknown"
    scope = current_scope.get()
    if scope is not None:
        event_payload.update(
            environment=scope.environment, is_test=scope.environment == "test"
        )
    record_telemetry_event(
        name=payload.event_type,
        event_payload=event_payload,
        client_id=client_id,
        full_query_text=payload.query,
    )
    return {"accepted": True}
