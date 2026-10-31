"""Short-lived Bedrock bearer tokens from the standard AWS credential chain."""

import asyncio
from datetime import timedelta
import re
from threading import Lock
from urllib.parse import urlsplit

import httpx


class BedrockAuth(httpx.Auth):
    def __init__(self, base_url: str, protocol: str):
        url = urlsplit(base_url)
        runtime = re.fullmatch(
            r"bedrock-runtime\.([a-z0-9-]+)\.amazonaws\.com", url.hostname or ""
        )
        mantle = re.fullmatch(
            r"bedrock-mantle\.([a-z0-9-]+)\.api\.aws", url.hostname or ""
        )
        match = runtime or mantle
        path = (
            "/anthropic"
            if protocol == "anthropic"
            else ("/openai/v1" if runtime else "/v1")
        )
        if not match or url.port not in {None, 443} or url.path.rstrip("/") != path:
            raise ValueError("Bedrock authentication requires an AWS Bedrock endpoint")
        from botocore.session import Session

        self.session = Session()
        self.region = match.group(1)
        self.base_url = base_url.rstrip("/") + "/"
        self.header = "x-api-key" if protocol == "anthropic" else "Authorization"
        self.lock = Lock()

    def load(self):
        credentials = self.session.get_credentials()
        if credentials is None:
            raise RuntimeError("AWS credentials are unavailable")
        # Freeze the access key, secret and session token together during refresh.
        return credentials.get_frozen_credentials()

    def _token(self):
        from aws_bedrock_token_generator import provide_token

        with self.lock:
            return provide_token(
                region=self.region,
                aws_credentials_provider=self,
                expiry=timedelta(minutes=5),
            )

    async def async_auth_flow(self, request):
        if not str(request.url).startswith(self.base_url):
            raise ValueError(
                "Refusing to send AWS credentials outside the configured endpoint"
            )
        # STS credential refresh may perform I/O; keep it off the event loop.
        token = await asyncio.to_thread(self._token)
        request.headers[self.header] = (
            token if self.header == "x-api-key" else f"Bearer {token}"
        )
        yield request
