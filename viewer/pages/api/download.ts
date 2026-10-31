import type { NextApiRequest, NextApiResponse } from "next";
import { subject } from "../../lib/auth";
import { authorizedScope, days } from "../../lib/config";
import { loadReport } from "../../lib/backends";
import { csv } from "../../lib/csv";
export default async function handler(
  req: NextApiRequest,
  res: NextApiResponse,
) {
  res.setHeader("Cache-Control", "private, no-store");
  if (req.method !== "GET") {
    res.setHeader("Allow", "GET");
    return res.status(405).end();
  }
  const sub = await subject(req.headers);
  if (!sub) return res.status(401).json({ error: "Sign in required" });
  let scope, period;
  try {
    scope = authorizedScope(
      sub,
      typeof req.query.scope === "string" ? req.query.scope : "",
    );
    period = days(req.query.days);
  } catch {
    return res.status(403).json({ error: "Scope or period denied" });
  }
  try {
    const report = await loadReport(scope, period);
    // Never silently export an incomplete conversation history.
    if (report.limited)
      return res.status(422).json({
        error:
          "Result limit reached. Select a shorter period before exporting.",
      });
    res.setHeader("Content-Type", "text/csv; charset=utf-8");
    res.setHeader(
      "Content-Disposition",
      'attachment; filename="fetch-telemetry.csv"',
    );
    return res.send(csv(report.rows));
  } catch {
    return res.status(502).json({ error: "Telemetry could not be loaded." });
  }
}
