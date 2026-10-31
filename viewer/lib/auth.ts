import { createHash, randomBytes } from "node:crypto";
import { createRemoteJWKSet, jwtVerify, SignJWT, type JWTPayload } from "jose";
import type { IncomingHttpHeaders } from "node:http";
import { allowedScopes } from "./config";
const sessionName = "__Host-fetch-viewer";
const stateName = "__Host-fetch-login";
function key() {
  const secret = process.env.VIEWER_SESSION_SECRET || "";
  if (Buffer.byteLength(secret) < 32)
    throw new Error("Session secret must be at least 32 bytes");
  return new TextEncoder().encode(secret);
}
export function origin() {
  const u = new URL(process.env.VIEWER_ORIGIN || "");
  if (
    u.protocol !== "https:" ||
    u.username ||
    u.password ||
    u.search ||
    u.hash ||
    u.pathname !== "/"
  )
    throw new Error("VIEWER_ORIGIN must be an HTTPS origin");
  return u.origin;
}
export function cookie(name: string, value: string, seconds: number) {
  return `${name}=${value}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=${seconds}`;
}
function readCookie(headers: IncomingHttpHeaders, name: string) {
  return (headers.cookie || "")
    .split(";")
    .map((x) => x.trim())
    .find((x) => x.startsWith(name + "="))
    ?.slice(name.length + 1);
}
export async function sign(
  payload: JWTPayload,
  purpose: string,
  seconds: number,
) {
  return new SignJWT(payload)
    .setProtectedHeader({ alg: "HS256" })
    .setIssuedAt()
    .setIssuer(origin())
    .setAudience(purpose)
    .setExpirationTime(`${seconds}s`)
    .sign(key());
}
export async function verify(token: string, purpose: string) {
  return (
    await jwtVerify(token, key(), {
      algorithms: ["HS256"],
      issuer: origin(),
      audience: purpose,
      requiredClaims: ["exp", "iat"],
    })
  ).payload;
}
export async function subject(
  headers: IncomingHttpHeaders,
): Promise<string | null> {
  try {
    const token = readCookie(headers, sessionName);
    if (!token) return null;
    const p = await verify(token, "viewer-session");
    return p.identity_issuer === process.env.OIDC_ISSUER &&
      typeof p.sub === "string" &&
      allowedScopes(p.sub).length
      ? p.sub
      : null;
  } catch {
    return null;
  }
}
export function logoutCookie() {
  return cookie(sessionName, "", 0);
}
async function metadata() {
  const issuer = process.env.OIDC_ISSUER || "";
  const url = new URL(issuer);
  if (
    url.protocol !== "https:" ||
    url.username ||
    url.password ||
    url.search ||
    url.hash
  )
    throw new Error("Invalid issuer");
  const r = await fetch(
    issuer.replace(/\/$/, "") + "/.well-known/openid-configuration",
    { signal: AbortSignal.timeout(8000), redirect: "error" },
  );
  if (!r.ok) throw new Error("OIDC discovery failed");
  const m = await r.json();
  if (m.issuer !== issuer) throw new Error("Issuer mismatch");
  for (const field of [
    "authorization_endpoint",
    "token_endpoint",
    "jwks_uri",
  ]) {
    const u = new URL(m[field]);
    if (u.protocol !== "https:" || u.username || u.password)
      throw new Error("Invalid OIDC endpoint");
  }
  return m;
}
function clientId() {
  const id = process.env.OIDC_CLIENT_ID;
  if (!id) throw new Error("Missing OIDC client");
  return id;
}
export async function beginLogin() {
  const m = await metadata();
  const state = randomBytes(32).toString("base64url");
  const nonce = randomBytes(32).toString("base64url");
  const verifier = randomBytes(32).toString("base64url");
  const u = new URL(m.authorization_endpoint);
  u.search = new URLSearchParams({
    client_id: clientId(),
    response_type: "code",
    scope: "openid",
    redirect_uri: origin() + "/api/auth/callback",
    state,
    nonce,
    code_challenge: createHash("sha256").update(verifier).digest("base64url"),
    code_challenge_method: "S256",
  }).toString();
  return {
    url: u.toString(),
    cookie: cookie(
      stateName,
      await sign({ state, nonce, verifier }, "viewer-login", 600),
      600,
    ),
  };
}
export async function completeLogin(
  headers: IncomingHttpHeaders,
  code: string,
  state: string,
) {
  const stored = readCookie(headers, stateName);
  if (!stored || !state || !code) throw new Error("Missing login state");
  const flow = await verify(stored, "viewer-login");
  if (
    flow.state !== state ||
    typeof flow.verifier !== "string" ||
    typeof flow.nonce !== "string"
  )
    throw new Error("Invalid login state");
  const m = await metadata();
  const id = clientId();
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    code,
    client_id: id,
    redirect_uri: origin() + "/api/auth/callback",
    code_verifier: flow.verifier,
  });
  const headersOut: Record<string, string> = {
    "Content-Type": "application/x-www-form-urlencoded",
  };
  if (process.env.OIDC_CLIENT_SECRET) {
    // Cognito supports basic; Entra supports client_secret_post. Select explicitly.
    if (process.env.OIDC_CLIENT_AUTH === "basic")
      headersOut.Authorization =
        "Basic " +
        Buffer.from(
          `${encodeURIComponent(id)}:${encodeURIComponent(process.env.OIDC_CLIENT_SECRET)}`,
        ).toString("base64");
    else body.set("client_secret", process.env.OIDC_CLIENT_SECRET);
  }
  const r = await fetch(m.token_endpoint, {
    method: "POST",
    headers: headersOut,
    body,
    signal: AbortSignal.timeout(8000),
    redirect: "error",
  });
  if (!r.ok) throw new Error("Token exchange failed");
  const tokens = await r.json();
  if (typeof tokens.id_token !== "string")
    throw new Error("Missing identity token");
  const { payload } = await jwtVerify(
    tokens.id_token,
    createRemoteJWKSet(new URL(m.jwks_uri)),
    {
      issuer: m.issuer,
      audience: id,
      algorithms: ["RS256", "ES256"],
      requiredClaims: ["sub", "exp", "iat", "nonce"],
    },
  );
  if (
    payload.nonce !== flow.nonce ||
    (payload.azp !== undefined && payload.azp !== id) ||
    (Array.isArray(payload.aud) &&
      payload.aud.length > 1 &&
      payload.azp !== id) ||
    !payload.sub ||
    !allowedScopes(payload.sub).length
  )
    throw new Error("Identity not authorized");
  return [
    cookie(
      sessionName,
      await sign(
        { sub: payload.sub, identity_issuer: process.env.OIDC_ISSUER },
        "viewer-session",
        3600,
      ),
      3600,
    ),
    cookie(stateName, "", 0),
  ];
}
