import json
import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.dashboard.store import SQLiteStore, DynamoStore, event, session_key


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("DASHBOARD_ENABLED", "true")
    monkeypatch.setenv("DASHBOARD_STORAGE", "sqlite")
    monkeypatch.setenv("DASHBOARD_SQLITE_PATH", str(tmp_path / "events.db"))
    monkeypatch.setenv(
        "DASHBOARD_TOKEN_CLIENT_IDS", "alpha=review-alpha,beta=review-beta"
    )
    monkeypatch.setenv("API_TOKEN_CLIENT_IDS", "alpha=api-alpha,beta=api-beta")
    monkeypatch.setenv("DASHBOARD_CAPTURE_CONTENT", "false")
    monkeypatch.delenv("API_TOKENS", raising=False)
    with TestClient(app, base_url="https://testserver") as c:
        yield c


def login(client, token="review-alpha"):
    return client.post("/dashboard/login", json={"token": token})


def test_dashboard_auth_scope_cookie_and_export(client):
    for tenant in ["alpha", "beta"]:
        SQLiteStore().put(
            event(tenant, "=SYNTHETIC()", "outcome", {"outcome": "referred"})
        )
    assert client.get("/dashboard/api/activity").status_code == 401
    assert login(client, "api-alpha").status_code == 401
    response = login(client)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
    page = client.get("/dashboard/api/activity?client_id=beta").json()
    assert len(page["events"]) == 1 and page["events"][0]["client_id"] == "alpha"
    response = client.get("/dashboard/api/export")
    assert "'=SYNTHETIC()" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors" in response.headers["content-security-policy"]
    assert client.post("/dashboard/logout").status_code == 200
    assert client.get("/dashboard/api/activity").status_code == 401


def test_cross_origin_and_disabled_dashboard(client, monkeypatch):
    assert (
        client.post(
            "/dashboard/login",
            json={"token": "review-alpha"},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    monkeypatch.setenv("DASHBOARD_ENABLED", "false")
    assert client.get("/dashboard/").status_code == 404
    assert login(client).status_code == 404


def test_classification_capture_and_outcomes(client, monkeypatch):
    payload = {
        "text": "I need help with paternity.",
        "conversation_id": "synthetic-session",
        "client_id": "beta",
        "skip_followups": True,
        "enable_screening_protocols": False,
    }
    headers = {"Authorization": "Bearer api-alpha"}
    assert (
        client.post("/api/classify", json=payload, headers=headers).status_code == 200
    )
    assert (
        client.post(
            "/api/activity/outcome",
            json={"conversation_id": "synthetic-session", "outcome": "referred"},
            headers=headers,
        ).status_code
        == 202
    )
    login(client)
    rows = client.get("/dashboard/api/activity").json()["events"]
    assert len(rows) == 2 and {x["client_id"] for x in rows} == {"alpha"}
    classification = next(x for x in rows if x["kind"] == "classification")
    assert "query" not in classification["details"]
    monkeypatch.setenv("DASHBOARD_CAPTURE_CONTENT", "true")
    assert (
        client.post("/api/classify", json=payload, headers=headers).status_code == 200
    )
    rows = client.get("/dashboard/api/activity?session=synthetic-session").json()[
        "events"
    ]
    assert rows[0]["details"]["query"] == payload["text"]
    assert (
        client.get("/dashboard/api/activity?session=other-session").json()["events"]
        == []
    )


def test_storage_failure_does_not_break_classification(client, monkeypatch):
    def fail(*args):
        raise RuntimeError("Synthetic failure")

    monkeypatch.setattr("app.main.capture_classification", fail)
    response = client.post(
        "/api/classify",
        headers={"Authorization": "Bearer api-alpha"},
        json={
            "text": "Paternity",
            "skip_followups": True,
            "enable_screening_protocols": False,
        },
    )
    assert response.status_code == 200


def test_local_pagination_and_expiry(client):
    db = SQLiteStore()
    for i in range(4):
        db.put(event("alpha", "same-session", "outcome", {"outcome": "in_progress"}))
    old = event("alpha", "expired", "outcome", {})
    old["expires_at"] = int(time.time()) - 1
    db.put(old)
    first, cursor = db.query("alpha", "", limit=2)
    second, end = db.query("alpha", "", cursor=cursor, limit=2)
    assert len(first) == len(second) == 2 and end is None
    assert not ({x["sort_key"] for x in first} & {x["sort_key"] for x in second})
    assert len(db.query("alpha", "", session="same-session")[0]) == 4


def test_dynamo_serialization_and_scoped_pagination(monkeypatch):
    table = MagicMock()
    resource = MagicMock()
    resource.Table.return_value = table
    monkeypatch.setattr(
        "app.dashboard.store.boto3.session.Session",
        lambda: type("Session", (), {"resource": lambda *args, **kwargs: resource})(),
    )
    monkeypatch.setenv("DASHBOARD_TABLE", "synthetic-table")
    db = DynamoStore()
    item = event(
        "alpha", "same-session", "classification", {"labels": [{"confidence": 0.8}]}
    )
    db.put(item)
    stored = table.put_item.call_args.kwargs["Item"]
    assert isinstance(stored["details"], str)
    table.query.return_value = {
        "Items": [stored],
        "LastEvaluatedKey": {"client_id": "alpha", "sort_key": item["sort_key"]},
    }
    rows, cursor = db.query(
        "alpha", "2026", cursor="opaque-timestamp", session="same-session"
    )
    kwargs = table.query.call_args.kwargs
    assert kwargs["IndexName"] == "sessions"
    assert kwargs["ExclusiveStartKey"] == {
        "client_id": "alpha",
        "sort_key": "opaque-timestamp",
        "session_key": session_key("alpha", "same-session"),
    }
    assert "FilterExpression" in kwargs
    assert rows[0]["details"]["labels"][0]["confidence"] == 0.8
    assert cursor == item["sort_key"]
