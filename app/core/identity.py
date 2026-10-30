"""Trusted API identity. Secrets remain in authentication, never in request scope."""

from contextvars import ContextVar
from dataclasses import dataclass
import hmac
import json
import os
import re


@dataclass(frozen=True)
class CredentialScope:
    client_id: str
    environment: str
    key_id: str
    capture_content: bool = False

    @property
    def scope_id(self) -> str:
        return f"{self.client_id}/{self.environment}/{self.key_id}"

    def attributes(self) -> dict[str, str]:
        return {
            "client_id": self.client_id,
            "environment": self.environment,
            "api_key_id": self.key_id,
            "scope_id": self.scope_id,
        }


# None represents unscoped internal calls. HTTP authentication marks even anonymous
# dev requests as enforced, preventing an accidental fallback to shared telemetry.
current_scope: ContextVar[CredentialScope | None] = ContextVar(
    "fetch_scope", default=None
)
current_client: ContextVar[str | None] = ContextVar("fetch_client", default=None)
http_scope_enforced: ContextVar[bool] = ContextVar("fetch_http_scope", default=False)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def legacy_credentials():
    """Legacy consumer IDs cannot alias structured scope partitions."""
    entries, secrets = [], set()
    for entry in os.getenv("API_TOKEN_CLIENT_IDS", "").split(","):
        if not entry.strip():
            continue
        client, sep, token = entry.strip().partition("=")
        client, token = client.strip(), token.strip()
        if not sep or not _ID.fullmatch(client) or not token or token in secrets:
            raise ValueError("Invalid or ambiguous legacy credential mapping")
        secrets.add(token)
        entries.append((token, client))
    return entries


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate credential configuration key")
        result[key] = value
    return result


def credential_registry():
    raw = json.loads(
        os.getenv("API_CREDENTIALS", "{}").strip() or "{}",
        object_pairs_hook=_unique_object,
    )
    if not isinstance(raw, dict):
        raise ValueError("Invalid API_CREDENTIALS configuration")
    entries, secrets = [], set()
    for key_id, config in raw.items():
        if (
            not isinstance(config, dict)
            or not isinstance(key_id, str)
            or not _ID.fullmatch(key_id)
        ):
            raise ValueError("Invalid credential ID")
        client = config.get("client_id")
        environment = config.get("environment")
        token_env = config.get("token_env")
        capture = config.get("capture_content", False)
        if (
            not isinstance(client, str)
            or not _ID.fullmatch(client)
            or not isinstance(environment, str)
            or environment not in {"test", "production"}
        ):
            raise ValueError("Credential consumer and environment are required")
        if (
            not isinstance(token_env, str)
            or not token_env
            or not isinstance(capture, bool)
        ):
            raise ValueError("Invalid credential options")
        token = os.getenv(token_env, "").strip()
        if not token or token in secrets:
            raise ValueError("Credential secrets must be present and unique")
        secrets.add(token)
        entries.append((token, CredentialScope(client, environment, key_id, capture)))
    # One secret cannot have conflicting identities in legacy and structured maps.
    for token, _ in legacy_credentials():
        if token in secrets:
            raise ValueError("Credential also appears in legacy mapping")
    return entries


def scoped_auth_configured() -> bool:
    return os.getenv("API_CREDENTIALS", "{}").strip() not in {"", "{}"}
