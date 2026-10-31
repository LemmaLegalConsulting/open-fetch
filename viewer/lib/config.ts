export type Scope = {
  allowContent?: boolean;
  clientId: string;
  environment: "test" | "production";
  keyId: string;
  source:
    | { kind: "azure"; workspaceId: string }
    | { kind: "aws"; region: string; logGroup: string };
};
export type Config = {
  scopes: Record<string, Scope>;
  viewers: Record<string, string[]>;
};
const segment = /^[A-Za-z0-9._:-]+$/;
export function parseConfig(raw: string): Config {
  const c = JSON.parse(raw) as Config;
  if (
    !c.scopes ||
    !c.viewers ||
    Array.isArray(c.scopes) ||
    Array.isArray(c.viewers)
  )
    throw new Error("Invalid viewer configuration");
  for (const [id, s] of Object.entries(c.scopes)) {
    if (
      !s ||
      typeof s.clientId !== "string" ||
      typeof s.keyId !== "string" ||
      !segment.test(s.clientId) ||
      !segment.test(s.keyId) ||
      !["test", "production"].includes(s.environment) ||
      id !== `${s.clientId}/${s.environment}/${s.keyId}`
    )
      throw new Error("Invalid scope");
    if (s.allowContent !== undefined && typeof s.allowContent !== "boolean")
      throw new Error("Invalid content permission");
    if (s.source?.kind === "azure") {
      if (!/^[a-f0-9-]{36}$/i.test(s.source.workspaceId))
        throw new Error("Invalid workspace");
    } else if (s.source?.kind === "aws") {
      if (
        !/^[a-z]{2}(?:-[a-z]+)+-\d+$/.test(s.source.region) ||
        !/^[\w./#-]{1,512}$/.test(s.source.logGroup)
      )
        throw new Error("Invalid AWS source");
    } else throw new Error("Unsupported query backend");
  }
  for (const [sub, ids] of Object.entries(c.viewers)) {
    if (
      !sub ||
      !Array.isArray(ids) ||
      !ids.length ||
      ids.some((id) => !Object.hasOwn(c.scopes, id))
    )
      throw new Error("Invalid viewer grant");
  }
  return c;
}
export function config(): Config {
  return parseConfig(process.env.VIEWER_CONFIG_JSON || "{}");
}
export function allowedScopes(subject: string, c = config()): string[] {
  return Object.hasOwn(c.viewers, subject) ? c.viewers[subject] : [];
}
export function authorizedScope(
  subject: string,
  id: string,
  c = config(),
): Scope {
  if (!allowedScopes(subject, c).includes(id)) throw new Error("Scope denied");
  return c.scopes[id];
}
export function days(value: unknown): number {
  const n = Number(value ?? 7);
  if (![1, 7, 30].includes(n)) throw new Error("Invalid period");
  return n;
}
