"""Append-only activity storage. No raw prompts are stored unless opted in."""

from contextlib import closing
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from app.core.identity import current_scope

import boto3
from boto3.dynamodb.conditions import Key, Attr
from botocore.config import Config


def enabled():
    return os.getenv("DASHBOARD_ENABLED", "false").lower() == "true"


def session_key(client, session):
    return hashlib.sha256(json.dumps([client, session]).encode()).hexdigest()


def event(client, session, kind, details):
    scope = current_scope.get()
    now = datetime.now(timezone.utc)
    timestamp = now.isoformat(timespec="microseconds")
    return {
        "client_id": client,
        "sort_key": timestamp + "#" + uuid4().hex,
        "session_key": session_key(client, session),
        "conversation_id": session,
        **(
            {
                "consumer_id": scope.client_id,
                "environment": scope.environment,
                "api_key_id": scope.key_id,
            }
            if scope and client == scope.scope_id
            else {}
        ),
        "timestamp": timestamp,
        "kind": kind,
        "details": details,
        "expires_at": int(now.timestamp())
        + int(os.getenv("DASHBOARD_RETENTION_DAYS", "30")) * 86400,
    }


class SQLiteStore:
    def connect(self):
        path = Path(os.getenv("DASHBOARD_SQLITE_PATH", "private-data/activity.sqlite3"))
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=5)
        db.execute("PRAGMA secure_delete=ON")
        db.execute(
            "CREATE TABLE IF NOT EXISTS events (client TEXT, sort_key TEXT, session_key TEXT, expires INTEGER, payload TEXT, PRIMARY KEY(client,sort_key))"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS sessions ON events(client, session_key, sort_key)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS expiry ON events(expires)")
        return db

    def put(self, item):
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM events WHERE expires <= ?", (int(time.time()),))
            db.execute(
                "INSERT OR REPLACE INTO events VALUES (?,?,?,?,?)",
                (
                    item["client_id"],
                    item["sort_key"],
                    item["session_key"],
                    item["expires_at"],
                    json.dumps(item),
                ),
            )

    def query(self, client, since, cursor=None, session=None, limit=100):
        with closing(self.connect()) as db, db:
            sql = "SELECT payload FROM events WHERE client=? AND sort_key>=? AND expires>?"
            args = [client, since, int(time.time())]
            if session:
                sql += " AND session_key=?"
                args.append(session_key(client, session))
            if cursor:
                sql += " AND sort_key<?"
                args.append(cursor)
            sql += " ORDER BY sort_key DESC LIMIT ?"
            args.append(limit + 1)
            rows = [json.loads(row[0]) for row in db.execute(sql, args)]
        return rows[:limit], rows[limit - 1]["sort_key"] if len(rows) > limit else None


class DynamoStore:
    def __init__(self):
        self.table = (
            boto3.session.Session()
            .resource(
                "dynamodb",
                region_name=os.getenv("AWS_REGION", "us-east-1"),
                config=Config(
                    connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 2}
                ),
            )
            .Table(os.environ["DASHBOARD_TABLE"])
        )

    def put(self, item):
        self.table.put_item(Item={**item, "details": json.dumps(item["details"])})

    def query(self, client, since, cursor=None, session=None, limit=100):
        partition = session_key(client, session) if session else client
        field = "session_key" if session else "client_id"
        condition = Key(field).eq(partition) & Key("sort_key").gte(since)
        kwargs = dict(
            KeyConditionExpression=condition,
            ScanIndexForward=False,
            Limit=limit,
            FilterExpression=Attr("expires_at").gt(int(time.time())),
        )
        if session:
            kwargs["IndexName"] = "sessions"
        if cursor:
            # Reconstruct the entire key from authenticated scope, never from browser input.
            key = {"client_id": client, "sort_key": cursor}
            if session:
                key["session_key"] = partition
            kwargs["ExclusiveStartKey"] = key
        result = self.table.query(**kwargs)
        rows = result.get("Items", [])
        for row in rows:
            row["details"] = json.loads(row["details"])
            row["expires_at"] = int(row["expires_at"])
        last = result.get("LastEvaluatedKey")
        return rows, last["sort_key"] if last else None


def store():
    backend = os.getenv("DASHBOARD_STORAGE", "sqlite")
    if backend == "dynamodb":
        return DynamoStore()
    if backend == "sqlite":
        return SQLiteStore()
    raise ValueError("Unknown DASHBOARD_STORAGE")


def capture_classification(client, request, response):
    if not enabled() or not client:
        return
    details = {
        "request_id": response.request_id,
        "labels": [x.model_dump(mode="json") for x in response.labels],
        "likely_no_legal_problem": (
            response.likely_no_legal_problem.model_dump(mode="json")
            if hasattr(response.likely_no_legal_problem, "model_dump")
            else response.likely_no_legal_problem
        ),
        "content_captured": False,
    }
    scope = current_scope.get()
    if os.getenv("DASHBOARD_CAPTURE_CONTENT", "false").lower() == "true" and (
        scope is None or scope.capture_content
    ):
        details.update(
            query=request.problem_description,
            followup_answers=[
                x.model_dump(mode="json") for x in request.followup_answers or []
            ],
            follow_up_questions=[
                x.model_dump(mode="json") for x in response.follow_up_questions
            ],
            content_captured=True,
        )
    item = event(
        client,
        request.conversation_id or response.request_id or uuid4().hex,
        "classification",
        details,
    )
    # DynamoDB has a 400 KB item limit. Refuse oversize content rather than truncate silently.
    if len(json.dumps(item).encode()) > 300_000:
        item["details"] = {
            k: v
            for k, v in details.items()
            if k not in {"query", "followup_answers", "follow_up_questions"}
        }
        item["details"].update(content_captured=False, content_omitted="size_limit")
    store().put(item)
