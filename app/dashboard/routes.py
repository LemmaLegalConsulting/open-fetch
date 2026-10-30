import asyncio
import csv
import hmac
import io
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from app.dashboard.store import enabled, store
from app.core.identity import credential_registry, legacy_credentials

router = APIRouter(prefix="/dashboard")
STATIC = Path(__file__).parent / "static"
COOKIE = "fetch_dashboard"


def client_for_token(token):
    if not token:
        raise HTTPException(401, "Sign in with a dashboard access token.")
    try:
        api_secrets = {
            secret for secret, _ in credential_registry() + legacy_credentials()
        }
    except ValueError:
        raise HTTPException(503, "API credential configuration is invalid") from None
    api_secrets.update(
        t.strip() for t in os.getenv("API_TOKENS", "").split(",") if t.strip()
    )
    entries, secrets = [], set()
    for entry in os.getenv("DASHBOARD_TOKEN_CLIENT_IDS", "").split(","):
        if not entry.strip():
            continue
        client, sep, secret = entry.strip().partition("=")
        client, secret = client.strip(), secret.strip()
        if (
            not sep
            or not client
            or not secret
            or secret in secrets
            or secret in api_secrets
        ):
            raise HTTPException(503, "Dashboard credential configuration is invalid")
        secrets.add(secret)
        entries.append((secret, client))
    for secret, client in entries:
        if hmac.compare_digest(secret.encode(), token.encode()):
            return client
    raise HTTPException(401, "Invalid dashboard access token.")


def identity(request: Request):
    if not enabled():
        raise HTTPException(404)
    return client_for_token(request.cookies.get(COOKIE))


def same_origin(request):
    origin = request.headers.get("origin")
    try:
        parsed = urlsplit(origin) if origin else None
        # TLS terminates at Fly/ALB. Production cookies require HTTPS regardless
        # of the internal hop's scheme; never trust arbitrary forwarded headers.
        expected_scheme = "https" if os.getenv("ENV") != "dev" else request.url.scheme
        mismatch = parsed and (parsed.scheme, parsed.netloc) != (
            expected_scheme,
            request.url.netloc,
        )
    except ValueError:
        mismatch = True
    if request.headers.get("sec-fetch-site") == "cross-site" or mismatch:
        raise HTTPException(403, "Cross-origin request denied.")


class Login(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


@router.post("/login")
def login(payload: Login, request: Request, response: Response):
    if not enabled():
        raise HTTPException(404)
    same_origin(request)
    client = client_for_token(payload.token)
    response.set_cookie(
        COOKIE,
        payload.token,
        httponly=True,
        secure=os.getenv("ENV") != "dev",
        samesite="strict",
        path="/dashboard",
        max_age=8 * 3600,
    )
    return {"client_id": client}


@router.post("/logout")
def logout(request: Request, response: Response):
    same_origin(request)
    response.delete_cookie(COOKIE, path="/dashboard")
    return {"signed_out": True}


@router.get("")
@router.get("/")
def index():
    if not enabled():
        raise HTTPException(404)
    return FileResponse(STATIC / "index.html")


@router.get("/static/{name}")
def static(name: str):
    if name not in {"dashboard.js", "dashboard.css"} or not enabled():
        raise HTTPException(404)
    return FileResponse(STATIC / name)


async def read_events(client, days, cursor, session):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="microseconds"
    )
    try:
        rows, next_cursor = await asyncio.to_thread(
            lambda: store().query(client, since, cursor, session)
        )
    except Exception:
        raise HTTPException(503, "Activity storage is unavailable.") from None
    return {"client_id": client, "events": rows, "next_cursor": next_cursor}


@router.get("/api/activity")
async def activity(
    days: int = Query(7, ge=1, le=90),
    cursor: str | None = Query(None, max_length=150),
    session: str | None = Query(None, max_length=512),
    client=Depends(identity),
):
    return await read_events(client, days, cursor, session)


def safe_cell(value):
    value = str(value)
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


@router.get("/api/export")
async def export(days: int = Query(7, ge=1, le=90), client=Depends(identity)):
    # Bound report size. Refuse partial reports so downloads never look complete when truncated.
    rows, cursor = [], None
    for _ in range(100):
        page = await read_events(client, days, cursor, None)
        rows.extend(page["events"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    if cursor:
        raise HTTPException(413, "Too many records; select a shorter reporting period.")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["timestamp", "conversation_id", "kind", "details"])
    for row in rows:
        writer.writerow(
            [
                safe_cell(row["timestamp"]),
                safe_cell(row["conversation_id"]),
                safe_cell(row["kind"]),
                safe_cell(json.dumps(row["details"], ensure_ascii=False)),
            ]
        )
    return Response(
        "\ufeff" + output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="fetch-activity.csv"'},
    )
