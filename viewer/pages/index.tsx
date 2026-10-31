import Head from "next/head";
import type { GetServerSideProps } from "next";
import { subject } from "../lib/auth";
import { allowedScopes, authorizedScope, days } from "../lib/config";
import { loadReport } from "../lib/backends";
import { groupSessions, type Session } from "../lib/sessions";
import SessionCard from "../components/SessionCard";
type Props = {
  signedIn: boolean;
  scopes: string[];
  scope: string;
  days: number;
  sessions: Session[];
  page: number;
  total: number;
  records: number;
  limited: boolean;
  error: string;
  content: boolean;
};
const empty: Props = {
  signedIn: false,
  scopes: [],
  scope: "",
  days: 7,
  sessions: [],
  page: 1,
  total: 0,
  records: 0,
  limited: false,
  error: "",
  content: false,
};
export const getServerSideProps: GetServerSideProps<Props> = async ({
  req,
  res,
  query,
}) => {
  res.setHeader("Cache-Control", "private, no-store");
  const sub = await subject(req.headers);
  if (!sub) return { props: empty };
  const props = { ...empty, signedIn: true, scopes: allowedScopes(sub) };
  try {
    props.scope =
      typeof query.scope === "string" ? query.scope : props.scopes[0];
    props.days = days(query.days);
    const s = authorizedScope(sub, props.scope);
    props.content = s.allowContent === true;
    const r = await loadReport(s, props.days);
    const sessions = groupSessions(r.rows);
    const requested = Number(query.page || 1);
    props.page = Number.isSafeInteger(requested)
      ? Math.max(1, Math.min(requested, Math.ceil(sessions.length / 25) || 1))
      : 1;
    props.sessions = sessions.slice((props.page - 1) * 25, props.page * 25);
    props.total = sessions.length;
    props.records = r.rows.length;
    props.limited = r.limited;
  } catch {
    res.statusCode = 502;
    props.error =
      "Telemetry could not be loaded for this scope and period. Check your access or try a shorter period.";
    props.scope = props.scopes[0];
  }
  return { props };
};
export default function Home(p: Props) {
  const params = new URLSearchParams({ scope: p.scope, days: String(p.days) });
  return (
    <>
      <Head>
        <title>FETCH telemetry</title>
      </Head>
      <main className="shell">
        <section className="card report" aria-labelledby="title">
          <div className="eyebrow">FETCH TELEMETRY</div>
          <h1 id="title">Intake conversations</h1>
          <p className="lede">
            Review recorded questions, follow-ups, and classification changes.
          </p>
          {!p.signedIn ? (
            <p>
              <a className="primary" href="/api/auth/login">
                Sign in
              </a>
            </p>
          ) : (
            <>
              <form method="get" className="report-actions">
                <label>
                  Scope{" "}
                  <select name="scope" defaultValue={p.scope}>
                    {p.scopes.map((s) => (
                      <option key={s}>{s}</option>
                    ))}
                  </select>
                </label>
                <label>
                  Period{" "}
                  <select name="days" defaultValue={p.days}>
                    {[1, 7, 30].map((d) => (
                      <option key={d} value={d}>
                        Last {d} {d === 1 ? "day" : "days"}
                      </option>
                    ))}
                  </select>
                </label>
                <button type="submit">Load conversations</button>
              </form>
              <div className="report-actions">
                {!p.error && !p.limited && (
                  <a href={"/api/download?" + params}>Download CSV</a>
                )}
                <form method="post" action="/api/auth/logout">
                  <button type="submit">Sign out</button>
                </form>
              </div>
              {p.error ? (
                <p role="alert">{p.error}</p>
              ) : (
                <>
                  <p className="report-totals">
                    {p.total} loaded conversations · {p.records} records ·{" "}
                    {p.scope.includes("/test/") ? "Test" : "Production"}
                  </p>
                  <p className="report-notes">
                    Counts cover loaded records, not all activity. Missing
                    traces do not prove a session was abandoned. A recorded
                    referral does not prove that someone retained a lawyer.
                    Times are UTC.
                  </p>
                  {!p.content && (
                    <p>
                      Content viewing is disabled for this scope. Only recorded
                      metadata is shown.
                    </p>
                  )}
                  {p.limited && (
                    <p role="alert">
                      The 500-record limit was reached. Conversation histories
                      may be incomplete. Select a shorter period; CSV export is
                      disabled for this result.
                    </p>
                  )}
                  {!p.sessions.length && (
                    <p>
                      No matching scoped traces were found. Legacy traces
                      without verified scope metadata are excluded.
                    </p>
                  )}
                  {p.sessions.map((s) => (
                    <SessionCard key={s.key} session={s} />
                  ))}
                  <nav
                    className="report-actions"
                    aria-label="Conversation pages"
                  >
                    {p.page > 1 && (
                      <a href={"/?" + params + "&page=" + (p.page - 1)}>
                        Previous
                      </a>
                    )}
                    <span>
                      Page {p.page} of {Math.max(1, Math.ceil(p.total / 25))}
                    </span>
                    {p.page * 25 < p.total && (
                      <a href={"/?" + params + "&page=" + (p.page + 1)}>Next</a>
                    )}
                  </nav>
                </>
              )}
            </>
          )}
        </section>
      </main>
    </>
  );
}
