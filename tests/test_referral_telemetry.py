import pytest

from app.models.api_models import ReferralTelemetryEvent
from fastapi.testclient import TestClient
from app.main import app


def test_presentation_event_preserves_display_and_screenout_decisions():
    event = ReferralTelemetryEvent(event_type="osbar.interaction.presentation",
        conversation_id="session", query="Question", outcome="in_progress", schema_version=3,
        presentation={"iteration":0, "template":"contact.html", "screened_out_non_legal":True,
            "questions":[{"question":"Where?", "shown":False, "reason":"non_legal_screen_out"}]})
    assert event.model_dump()["presentation"]["questions"][0]["shown"] is False
    assert event.presentation.screened_out_non_legal is True
    assert ReferralTelemetryEvent(conversation_id="old", query="Q", outcome="referred").presentation is None


def test_referral_telemetry_event_requires_osbar_event_type():
    event = ReferralTelemetryEvent(
        conversation_id="conversation-1",
        query="Housing issue",
        outcome="referred",
        followup_questions_answers=[
            {"question": "When did this happen?", "answer": "Last week"}
        ],
        classification_result=[{"label": "Housing", "confidence": 2.0}],
        referrals=[
            {
                "success": True,
                "categories": ["Housing"],
                "sub_panel_ids": ["101"],
            }
        ],
    )

    assert event.event_type == "osbar.interaction.completed"
    assert event.followup_questions_answers[0].answer == "Last week"
    assert event.referrals[0].success is True


def test_referral_telemetry_event_rejects_other_event_types():
    with pytest.raises(ValueError):
        ReferralTelemetryEvent(
            event_type="other.event",
            conversation_id="conversation-1",
            query="Housing issue",
            outcome="no_match",
        )


def test_referral_event_requires_server_side_osb_token_mapping(monkeypatch):
    monkeypatch.setattr("app.telemetry.record_telemetry_event", lambda **_: None)
    monkeypatch.setenv("API_TOKENS", "shared-token")
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.delenv("API_TOKEN_CLIENT_IDS", raising=False)

    response = TestClient(app).post(
        "/api/telemetry/referral-completed",
        headers={"Authorization": "Bearer shared-token"},
        json={"conversation_id": "c1", "query": "Q", "outcome": "no_match"},
    )

    assert response.status_code == 403


def test_referral_event_accepts_only_osb_mapped_token(monkeypatch):
    monkeypatch.setattr("app.telemetry.record_telemetry_event", lambda **_: None)
    monkeypatch.setenv("API_TOKENS", "shared-token")
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.setenv("API_TOKEN_CLIENT_IDS", "osbar-referral=shared-token")

    response = TestClient(app).post(
        "/api/telemetry/referral-completed",
        headers={"Authorization": "Bearer shared-token"},
        json={"conversation_id": "c1", "query": "Q", "outcome": "no_match"},
    )

    assert response.status_code == 202
    assert response.json() == {"accepted": True}


def test_dev_token_cannot_claim_production(monkeypatch):
    events = []
    monkeypatch.setattr("app.telemetry.record_telemetry_event", lambda **event: events.append(event))
    monkeypatch.setenv("API_TOKENS", "dev-token,other-token")
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("API_TOKEN_CLIENT_IDS", "osbar-referral-dev=dev-token,other-client=other-token")
    payload = {
        "conversation_id": "dev-1", "query": "Issue", "outcome": "referred",
        "schema_version": 2, "environment": "production", "is_test": False,
        "person": {"first_name": "Test", "last_name": "Person"},
        "referrals": [{"success": True, "attorney": {"first_name": "Test", "bar_number": "123"}}],
    }
    client = TestClient(app)
    result = client.post("/api/telemetry/referral-completed", headers={"Authorization": "Bearer dev-token"}, json=payload)
    assert result.status_code == 202
    assert events[0]["client_id"] == "osbar-referral-dev"
    assert events[0]["event_payload"]["is_test"] is True
    assert events[0]["event_payload"]["environment"] == "development"
    assert events[0]["event_payload"]["person"]["first_name"] == "Test"
    payload["client_id"] = "osbar-referral"
    rejected = client.post("/api/telemetry/referral-completed", headers={"Authorization": "Bearer other-token"}, json=payload)
    assert rejected.status_code == 403
    assert len(events) == 1


def test_progress_and_legacy_environment(monkeypatch):
    events = []
    monkeypatch.setattr("app.telemetry.record_telemetry_event", lambda **event: events.append(event))
    monkeypatch.setenv("API_TOKENS", "osb-token")
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("API_TOKEN_CLIENT_IDS", "osbar-referral=osb-token")
    result = TestClient(app).post("/api/telemetry/referral-completed",
        headers={"Authorization": "Bearer osb-token"},
        json={"conversation_id": "old", "query": "Issue", "outcome": "in_progress", "event_type": "osbar.interaction.progress"})
    assert result.status_code == 202
    assert events[0]["name"] == "osbar.interaction.progress"
    assert events[0]["event_payload"]["environment"] == "unknown"


def test_wisconsin_cannot_spoof_osb_identity(monkeypatch):
    events = []
    monkeypatch.setattr(
        "app.telemetry.record_telemetry_event", lambda **event: events.append(event)
    )
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("API_TOKENS", "wisc-test")
    monkeypatch.setenv("API_TOKEN_CLIENT_IDS", "wisconsin=wisc-test")
    result = TestClient(app).post(
        "/api/telemetry/referral-completed",
        headers={"Authorization": "Bearer wisc-test"},
        json={
            "conversation_id": "s1",
            "query": "Private",
            "outcome": "no_match",
            "client_id": "osbar-referral",
        },
    )
    assert result.status_code == 403
    assert events == []
