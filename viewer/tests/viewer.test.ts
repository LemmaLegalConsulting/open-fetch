import { test } from "node:test";
import assert from "node:assert/strict";
import { parseConfig, authorizedScope, days, type Scope } from "../lib/config";
import { normalize } from "../lib/normalize";
import { groupSessions } from "../lib/sessions";
import { azureQuery, awsQuery, azureSpans, awsSpans } from "../lib/backends";
import { csvCell } from "../lib/csv";
import { sign, verify, subject } from "../lib/auth";
const scope: Scope = {
  clientId: "school",
  environment: "test",
  keyId: "key",
  allowContent: true,
  source: { kind: "aws", region: "us-east-1", logGroup: "aws/spans" },
};
const attrs = {
  "fetch.scope_id": "school/test/key",
  "fetch.client_id": "school",
  "fetch.environment": "test",
  "fetch.api_key_id": "key",
  "fetch.content_captured": true,
  "fetch.conversation_id": "same",
  "langfuse.trace.name": "classify",
  "langfuse.trace.input": JSON.stringify({
    problem_description: "Synthetic divorce",
    followup_answers: [{ question: "Children?", answer: "Yes" }],
  }),
  "langfuse.trace.output": JSON.stringify({
    labels: [{ label: "Family Law > Divorce" }],
    follow_up_questions: [{ question: "Children?", options: ["Yes", "No"] }],
  }),
};
const span = {
  timestamp: "2026-10-30T10:00:00Z",
  id: "one",
  attributes: attrs,
};
test("authorization denies other environment/key and unknown subjects", () => {
  const c = parseConfig(
    JSON.stringify({
      scopes: { "school/test/key": scope },
      viewers: { alice: ["school/test/key"] },
    }),
  );
  assert.equal(
    authorizedScope("alice", "school/test/key", c),
    c.scopes["school/test/key"],
  );
  for (const [user, id] of [
    ["alice", "school/production/key"],
    ["alice", "school/test/other"],
    ["bob", "school/test/key"],
    ["toString", "school/test/key"],
  ])
    assert.throws(() => authorizedScope(user, id, c));
  assert.throws(() =>
    parseConfig(
      JSON.stringify({ scopes: { "bad' | take 100": scope }, viewers: {} }),
    ),
  );
  for (const x of [0, 100, "7 | limit 100"]) assert.throws(() => days(x));
});
test("normalization rejects missing or forged scope despite matching session", () => {
  for (const key of [
    "fetch.scope_id",
    "fetch.client_id",
    "fetch.environment",
    "fetch.api_key_id",
  ]) {
    assert.equal(
      normalize({ ...span, attributes: { ...attrs, [key]: "other" } }, scope),
      null,
    );
    assert.equal(
      normalize({ ...span, attributes: { ...attrs, [key]: undefined } }, scope),
      null,
    );
  }
  assert.equal(
    normalize(
      {
        ...span,
        attributes: { ...attrs, "langfuse.observation.type": "generation" },
      },
      scope,
    ),
    null,
  );
});
test("content requires both reader permission and captured flag", () => {
  for (const s of [
    normalize(span, { ...scope, allowContent: false }),
    normalize(
      { ...span, attributes: { ...attrs, "fetch.content_captured": false } },
      scope,
    ),
    normalize(
      {
        ...span,
        attributes: { ...attrs, "fetch.content_captured": undefined },
      },
      scope,
    ),
  ]) {
    assert.equal(s?.query, "");
    assert.equal(s?.raw_input, "{}");
    assert.equal(s?.raw_output, "{}");
  }
});
test("both vendor representations normalize to the same threaded conversation", () => {
  const az = azureSpans({
    tables: [
      {
        columns: ["timestamp", "id", "properties", "success"].map((name) => ({
          name,
        })),
        rows: [[span.timestamp, span.id, attrs, true]],
      },
    ],
  });
  const aws = awsSpans([
    [
      { field: "@timestamp", value: span.timestamp },
      {
        field: "@message",
        value: JSON.stringify({ spanId: span.id, attributes: attrs }),
      },
    ],
  ]);
  assert.deepEqual(normalize(az[0], scope), normalize(aws[0], scope));
  const sessions = groupSessions([normalize(az[0], scope)!]);
  assert.equal(sessions[0].query, "Synthetic divorce");
  assert.equal(sessions[0].questions[0].answers[0], "Yes");
  assert.equal(sessions[0].labels[0].label, "Family Law > Divorce");
});
test("same session across keys never merges and missing ids remain separate", () => {
  const row = normalize(span, scope)!;
  const other = { ...row, scope_id: "school/production/other" };
  assert.equal(groupSessions([row, other]).length, 2);
  assert.equal(
    groupSessions([
      { ...row, conversation_id: "", source_request_id: "a" },
      { ...row, conversation_id: "", source_request_id: "b" },
    ]).length,
    2,
  );
});
test("queries restrict scope before limits and CSV cells neutralize formulas", () => {
  for (const q of [azureQuery(scope), awsQuery(scope)]) {
    assert.ok(q.includes("school/test/key"));
    assert.ok(q.includes("fetch.api_key_id"));
    assert.ok(q.indexOf("school/test/key") < q.indexOf("500"));
  }
  assert.equal(csvCell(" =SUM(A1)"), '"\' =SUM(A1)"');
  assert.equal(csvCell('a"b'), '"a""b"');
  assert.throws(() =>
    azureSpans({ error: { code: "PartialError" }, tables: [] }),
  );
});
test("session JWT checks purpose/signature and revocation on each request", async () => {
  process.env.VIEWER_ORIGIN = "https://viewer.example";
  process.env.VIEWER_SESSION_SECRET =
    "synthetic-only-secret-with-at-least-32-bytes";
  process.env.VIEWER_CONFIG_JSON = JSON.stringify({
    scopes: { "school/test/key": scope },
    viewers: { alice: ["school/test/key"] },
  });
  const token = await sign(
    { sub: "alice", identity_issuer: process.env.OIDC_ISSUER },
    "viewer-session",
    60,
  );
  assert.equal(
    await subject({ cookie: "__Host-fetch-viewer=" + token }),
    "alice",
  );
  await assert.rejects(verify(token, "viewer-login"));
  assert.equal(
    await subject({ cookie: "__Host-fetch-viewer=" + token + "x" }),
    null,
  );
  process.env.VIEWER_CONFIG_JSON = JSON.stringify({
    scopes: { "school/test/key": scope },
    viewers: {},
  });
  assert.equal(await subject({ cookie: "__Host-fetch-viewer=" + token }), null);
});

test("CloudWatch adapter waits for completion, drops foreign rows, and cancels failed queries", async () => {
  const { mock } = await import("node:test");
  const { CloudWatchLogsClient } =
    await import("@aws-sdk/client-cloudwatch-logs");
  const { loadReport } = await import("../lib/backends");
  const calls: string[] = [];
  const stub = mock.method(
    CloudWatchLogsClient.prototype,
    "send",
    async (command: {
      constructor: { name: string };
      input: Record<string, unknown>;
    }) => {
      calls.push(command.constructor.name);
      if (command.constructor.name === "StartQueryCommand") {
        assert.equal(command.input.logGroupName, "aws/spans");
        return { queryId: "private-query-id" };
      }
      if (command.constructor.name === "GetQueryResultsCommand")
        return {
          status: "Complete",
          results: [
            [
              { field: "@timestamp", value: span.timestamp },
              {
                field: "@message",
                value: JSON.stringify({ spanId: "one", attributes: attrs }),
              },
            ],
            [
              {
                field: "@message",
                value: JSON.stringify({
                  attributes: { ...attrs, "fetch.environment": "production" },
                }),
              },
            ],
          ],
        };
      return {};
    },
  );
  try {
    const report = await loadReport(scope, 7);
    assert.equal(report.rows.length, 1);
    assert.equal(report.limited, false);
    stub.mock.mockImplementation(
      async (command: { constructor: { name: string } }) => {
        calls.push(command.constructor.name);
        if (command.constructor.name === "StartQueryCommand")
          return { queryId: "private-query-id" };
        if (command.constructor.name === "GetQueryResultsCommand")
          return { status: "Failed" };
        return {};
      },
    );
    await assert.rejects(loadReport(scope, 7));
    assert.ok(calls.includes("StopQueryCommand"));
  } finally {
    stub.mock.restore();
  }
});

test("Azure adapter sends fixed workspace/scoped query and rejects partial errors", async () => {
  const { mock } = await import("node:test");
  const { loadReport } = await import("../lib/backends");
  const dependencies = { azureToken: async () => "synthetic" };
  const fetcher = mock.method(
    globalThis,
    "fetch",
    async (url: unknown, init?: RequestInit) => {
      assert.equal(
        String(url),
        "https://api.loganalytics.io/v1/workspaces/00000000-0000-0000-0000-000000000000/query",
      );
      assert.ok(String(init?.body).includes("school/test/key"));
      return new Response(
        JSON.stringify({
          tables: [
            {
              columns: ["timestamp", "id", "properties", "success"].map(
                (name) => ({ name }),
              ),
              rows: [[span.timestamp, "one", attrs, true]],
            },
          ],
        }),
      );
    },
  );
  const azure = {
    ...scope,
    source: {
      kind: "azure" as const,
      workspaceId: "00000000-0000-0000-0000-000000000000",
    },
  };
  try {
    assert.equal(
      (await loadReport(azure, 7, Date.now(), dependencies)).rows.length,
      1,
    );
    fetcher.mock.mockImplementation(
      async () =>
        new Response(JSON.stringify({ error: { code: "PartialError" } })),
    );
    await assert.rejects(loadReport(azure, 7, Date.now(), dependencies));
  } finally {
    fetcher.mock.restore();
  }
});

test("CSV route denies an ungranted scope before querying", async () => {
  const { default: handler } = await import("../pages/api/download");
  process.env.VIEWER_CONFIG_JSON = JSON.stringify({
    scopes: { "school/test/key": scope },
    viewers: { alice: ["school/test/key"] },
  });
  const token = await sign(
    { sub: "alice", identity_issuer: process.env.OIDC_ISSUER },
    "viewer-session",
    60,
  );
  let status = 0;
  const res = {
    setHeader() {},
    status(n: number) {
      status = n;
      return this;
    },
    json() {
      return this;
    },
  };
  await handler(
    {
      method: "GET",
      headers: { cookie: "__Host-fetch-viewer=" + token },
      query: { scope: "school/production/key" },
    } as never,
    res as never,
  );
  assert.equal(status, 403);
});

test("OIDC flow validates nonce, state, audience, and PKCE before issuing a session", async () => {
  const { mock } = await import("node:test");
  const { generateKeyPair, exportJWK, SignJWT } = await import("jose");
  const { beginLogin, completeLogin } = await import("../lib/auth");
  process.env.OIDC_ISSUER = "https://issuer.example";
  process.env.OIDC_CLIENT_ID = "viewer-client";
  process.env.VIEWER_CONFIG_JSON = JSON.stringify({
    scopes: { "school/test/key": scope },
    viewers: { alice: ["school/test/key"] },
  });
  const { publicKey, privateKey } = await generateKeyPair("RS256");
  const jwk = await exportJWK(publicKey);
  jwk.kid = "synthetic";
  let nonce = "";
  let audience = "viewer-client";
  let challenge = "";
  const fetcher = mock.method(
    globalThis,
    "fetch",
    async (url: unknown, init?: RequestInit) => {
      if (String(url).endsWith("/.well-known/openid-configuration"))
        return new Response(
          JSON.stringify({
            issuer: "https://issuer.example",
            authorization_endpoint: "https://issuer.example/authorize",
            token_endpoint: "https://issuer.example/token",
            jwks_uri: "https://issuer.example/keys",
          }),
        );
      if (String(url).endsWith("/keys"))
        return new Response(JSON.stringify({ keys: [jwk] }));
      if (String(url).endsWith("/token")) {
        const body = new URLSearchParams(String(init?.body));
        const { createHash } = await import("node:crypto");
        assert.equal(
          createHash("sha256")
            .update(body.get("code_verifier") || "")
            .digest("base64url"),
          challenge,
        );
        const id_token = await new SignJWT({ nonce })
          .setProtectedHeader({ alg: "RS256", kid: "synthetic" })
          .setIssuer("https://issuer.example")
          .setAudience(audience)
          .setSubject("alice")
          .setIssuedAt()
          .setExpirationTime("5m")
          .sign(privateKey);
        return new Response(JSON.stringify({ id_token }));
      }
      throw new Error("Unexpected network request");
    },
  );
  try {
    const login = await beginLogin();
    const url = new URL(login.url);
    nonce = url.searchParams.get("nonce")!;
    challenge = url.searchParams.get("code_challenge")!;
    const headers = { cookie: login.cookie.split(";")[0] };
    const state = url.searchParams.get("state")!;
    await assert.rejects(completeLogin(headers, "code", "forged-state"));
    const cookies = await completeLogin(headers, "code", state);
    assert.equal(await subject({ cookie: cookies[0].split(";")[0] }), "alice");
    audience = "another-app";
    await assert.rejects(completeLogin(headers, "code", state));
    audience = "viewer-client";
    nonce = "wrong-nonce";
    await assert.rejects(completeLogin(headers, "code", state));
  } finally {
    fetcher.mock.restore();
  }
});
