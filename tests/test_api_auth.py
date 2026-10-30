import pytest
from fastapi.testclient import TestClient
from app.main import app
import os

client = TestClient(app)


@pytest.fixture(autouse=True)
def set_api_tokens_env():
    os.environ["API_TOKENS"] = "test_token_1,test_token_2"
    # Ensure we're NOT in dev mode for auth tests
    os.environ.pop("ENV", None)
    yield
    del os.environ["API_TOKENS"]


def test_classify_unauthorized():
    response = client.post(
        "/api/classify", json={"text": "test", "enabled_models": ["gemini"]}
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "Not authenticated"}


def test_classify_invalid_token():
    headers = {"Authorization": "Bearer invalid_token"}
    response = client.post(
        "/api/classify",
        headers=headers,
        json={"text": "test", "enabled_models": ["gemini"]},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid authentication credentials"}


def test_classify_valid_token():
    headers = {"Authorization": "Bearer test_token_1"}
    response = client.post(
        "/api/classify",
        headers=headers,
        json={"text": "test", "enabled_models": ["gemini"]},
    )
    # Assuming a successful response for a valid token, even if the classification service itself is mocked or returns an empty result
    # The actual classification logic is tested in test_classification_service.py
    assert (
        response.status_code == 200 or response.status_code == 422
    )  # 422 if validation error for request body, but 200 if it proceeds to service
    # Further assertions can be added here based on the expected response structure after successful authentication


def test_wisconsin_classification_correlation_and_openapi(monkeypatch):
    from app.services.classification_service import ClassificationService

    monkeypatch.setenv("API_TOKENS", "wisc-test")
    monkeypatch.setenv("API_TOKEN_CLIENT_IDS", "wisconsin=wisc-test")
    monkeypatch.setenv("OFFLINE_MODE", "1")
    service = ClassificationService(enabled_providers_override=[])
    monkeypatch.setattr("app.main.classification_service", service)
    response = client.post(
        "/api/classify",
        headers={"Authorization": "Bearer wisc-test"},
        json={
            "text": "Example",
            "client_id": "osbar-referral",
            "conversation_id": "s1",
            "client_request_id": "c1",
            "user_id": "u1",
            "skip_semantic_merge": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["client_id"] == "wisconsin"
    assert response.json()["request_id"]
    schema = client.get("/openapi.json").json()
    request_fields = schema["components"]["schemas"]["ClassificationRequest"][
        "properties"
    ]
    assert {"conversation_id", "client_request_id", "user_id"} <= request_fields.keys()
    assert "Organization and end-to-end tracking" in schema["info"]["description"]
