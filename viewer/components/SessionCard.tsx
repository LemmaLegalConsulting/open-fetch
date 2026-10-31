import type { Session } from "../lib/sessions";

const date = (value: string) =>
  value
    ? new Date(value).toLocaleString("en-US", { timeZone: "UTC" })
    : "Date not recorded";

export default function SessionCard({ session }: { session: Session }) {
  return (
    <article
      className="session-card"
      aria-label={session.query || "Recorded session"}
    >
      <header className="session-meta">
        <span
          className={session.activity === "TEST" ? "badge badge-test" : "badge"}
        >
          {session.activity === "TEST"
            ? "TEST"
            : session.activity === "PRODUCTION"
              ? "Production"
              : "Unknown"}
        </span>
        <time dateTime={session.startedAt}>{date(session.startedAt)} UTC</time>
        <span>
          {session.recordCount}{" "}
          {session.recordCount === 1 ? "record" : "records"}
        </span>
      </header>
      <div className="session-focus">
        <section>
          <h3>Original question</h3>
          <p className="original-question">
            {session.query || "Question not recorded."}
          </p>
        </section>
        <section className="classification">
          <h3>Final recorded classification</h3>
          {session.labels.length ? (
            <ul className="classification-list">
              {session.labels.map((label, i) => (
                <li key={i}>
                  <strong>{label.label}</strong>
                  {label.confidence !== undefined && (
                    <span className="score">
                      Score:{" "}
                      {label.confidence.toLocaleString("en-US", {
                        maximumFractionDigits: 3,
                      })}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <p>No classification recorded.</p>
          )}
          <p className="status">{session.status}</p>
          <p className="screen-out">
            <strong>Screened out as non-legal:</strong>{" "}
            {session.screenedOut === true
              ? "Yes"
              : session.screenedOut === false
                ? "No"
                : "Unknown — app decision not recorded"}
          </p>
        </section>
      </div>
      <section className="followup-thread">
        <h3>Follow-up questions</h3>
        {session.questions.length ? (
          <ol>
            {session.questions.map((question, i) => (
              <li key={i}>
                <p className="generated-question">{question.question}</p>
                <div className="question-visibility">
                  {question.visibility.length ? (
                    question.visibility.map((v, index) => (
                      <p key={index}>
                        <strong>{v.shown ? "Shown" : "Not shown"}</strong> ·
                        Round {v.iteration + 1}
                        {!v.shown && ` · ${v.reason.replaceAll("_", " ")}`}
                      </p>
                    ))
                  ) : (
                    <p>Display status unknown — not recorded</p>
                  )}
                </div>
                {question.options.length > 0 && (
                  <details className="question-options">
                    <summary>Answer choices</summary>
                    <ul>
                      {question.options.map((option, index) => (
                        <li key={index}>{option}</li>
                      ))}
                    </ul>
                  </details>
                )}
                {question.answers.length ? (
                  question.answers.map((answer, index) => (
                    <div className="recorded-answer" key={index}>
                      <span>{index === 0 ? "Answer" : "Updated answer"}</span>
                      <p>{answer}</p>
                    </div>
                  ))
                ) : (
                  <p className="fine">No answer recorded.</p>
                )}
              </li>
            ))}
          </ol>
        ) : (
          <p className="fine">No follow-up questions recorded.</p>
        )}
      </section>
      {(session.person || session.contact || session.attorneys.length > 0) && (
        <section className="referral-details">
          <h3>Referral details</h3>
          {session.person && (
            <p>
              <strong>Person:</strong> {session.person}
            </p>
          )}
          {session.contact && <p>{session.contact}</p>}
          {session.attorneys.map((attorney, i) => (
            <p key={i}>
              <strong>Referred to:</strong> {attorney}
            </p>
          ))}
        </section>
      )}
      {session.classifications.length > 1 && (
        <details className="classification-history">
          <summary>How the classification changed</summary>
          <ol>
            {session.classifications.map((entry, i) => (
              <li key={i}>
                <time>{date(entry.timestamp)}</time>
                <ul>
                  {entry.labels.map((label, j) => (
                    <li key={j}>{label.label}</li>
                  ))}
                </ul>
              </li>
            ))}
          </ol>
        </details>
      )}
      <p className="session-id">
        Session ID: {session.id || "Not recorded — shown as a separate request"}
      </p>
    </article>
  );
}
