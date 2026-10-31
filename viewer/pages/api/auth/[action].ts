import type { NextApiRequest, NextApiResponse } from "next";
import {
  beginLogin,
  completeLogin,
  logoutCookie,
  origin,
} from "../../../lib/auth";
export default async function handler(
  req: NextApiRequest,
  res: NextApiResponse,
) {
  res.setHeader("Cache-Control", "private, no-store");
  try {
    if (req.query.action === "logout") {
      if (req.method !== "POST" || req.headers.origin !== origin())
        return res.status(403).json({ error: "Forbidden" });
      res.setHeader("Set-Cookie", logoutCookie());
      return res.redirect(303, "/");
    }
    if (req.method !== "GET") {
      res.setHeader("Allow", "GET");
      return res.status(405).end();
    }
    if (req.query.action === "login") {
      const flow = await beginLogin();
      res.setHeader("Set-Cookie", flow.cookie);
      return res.redirect(302, flow.url);
    }
    if (
      req.query.action === "callback" &&
      typeof req.query.code === "string" &&
      typeof req.query.state === "string"
    ) {
      res.setHeader(
        "Set-Cookie",
        await completeLogin(req.headers, req.query.code, req.query.state),
      );
      return res.redirect(303, "/");
    }
    return res.status(400).json({ error: "Invalid authentication request" });
  } catch {
    return res
      .status(403)
      .json({ error: "Sign-in failed or this account has no viewer access." });
  }
}
