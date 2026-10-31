export type ReportRow = Record<string, unknown>;
type Label = { label: string; confidence?: number };
type Visibility = { iteration: number; shown: boolean; reason: string };
type Question = {
  question: string;
  options: string[];
  answers: string[];
  visibility: Visibility[];
};
export type Session = {
  key: string;
  id: string;
  timestamp: string;
  startedAt: string;
  activity: string;
  query: string;
  labels: Label[];
  questions: Question[];
  recordCount: number;
  status: string;
  person: string;
  contact: string;
  attorneys: string[];
  screenedOut: boolean | null;
  classifications: { timestamp: string; labels: Label[] }[];
};

function decode(value: unknown): unknown {
  if (typeof value !== "string") return value;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}
function object(value: unknown): ReportRow {
  const parsed = decode(value);
  return parsed && typeof parsed === "object" && !Array.isArray(parsed)
    ? (parsed as ReportRow)
    : {};
}
function list(value: unknown): unknown[] {
  const parsed = decode(value);
  return Array.isArray(parsed) ? parsed : [];
}
function text(value: unknown): string {
  if (Array.isArray(value)) return value.map(text).filter(Boolean).join("; ");
  return typeof value === "string" || typeof value === "number"
    ? String(value)
    : "";
}
function labels(value: unknown): Label[] {
  return list(value).flatMap((item) => {
    if (typeof item === "string") return [{ label: item }];
    const entry = object(item);
    return text(entry.label)
      ? [
          {
            label: text(entry.label),
            ...(typeof entry.confidence === "number"
              ? { confidence: entry.confidence }
              : {}),
          },
        ]
      : [];
  });
}
const normalized = (value: string) =>
  value.trim().replace(/\s+/g, " ").toLowerCase();

// Group before pagination: a conversation must never be split across pages.
// Tenant identity is part of the key; missing session IDs remain separate.
export function groupSessions(rows: ReportRow[]): Session[] {
  const buckets = new Map<string, ReportRow[]>();
  rows.forEach((row, index) => {
    const input = object(row.raw_input);
    const id = text(row.conversation_id || input.conversation_id);
    const key = JSON.stringify([
      text(row.scope_id) ||
        JSON.stringify([row.client_id, row.environment, row.api_key_id]),
      id ||
        "request:" +
          text(row.source_request_id || row.source_operation_id || index),
    ]);
    buckets.set(key, [...(buckets.get(key) || []), row]);
  });
  return [...buckets.entries()]
    .map(([key, records]) => {
      records.sort((a, b) =>
        text(a.timestamp).localeCompare(text(b.timestamp)),
      );
      const first = records[0];
      const latest = records[records.length - 1];
      const session: Session = {
        key,
        id: text(
          first.conversation_id || object(first.raw_input).conversation_id,
        ),
        timestamp: text(latest.timestamp),
        startedAt: text(first.timestamp),
        activity: text(latest.activity_type) || "UNVERIFIED HISTORICAL",
        query: "",
        labels: [],
        questions: [],
        recordCount: records.length,
        status: text(latest.completion_status) || "No completion recorded",
        person: "",
        contact: "",
        attorneys: [],
        classifications: [],
        screenedOut: null,
      };
      const questions = new Map<string, Question>();
      for (const row of records) {
        const input = object(row.raw_input);
        const output = object(row.raw_output);
        // Presentation events are not referral completion events.
        if (
          text(row.completion_status) &&
          row.completion_status !== "No completion recorded"
        )
          session.status = text(row.completion_status);
        const presentation = object(input.presentation);
        if (typeof presentation.screened_out_non_legal === "boolean")
          session.screenedOut = presentation.screened_out_non_legal;
        else if (
          input.outcome === "no_legal_problem" ||
          row.outcome === "no_legal_problem"
        )
          session.screenedOut = true;
        const questionText = text(row.query || input.problem_description);
        if (!session.query && questionText) session.query = questionText;
        if (row.activity_type === "TEST") session.activity = "TEST";
        let result = labels(row.classification_result).length
          ? labels(row.classification_result)
          : labels(input.classification_result).length
            ? labels(input.classification_result)
            : labels(output.effective_categories).length
              ? labels(output.effective_categories)
              : labels(output.labels);
        const noLegal = decode(
          row.no_legal_problem_status ??
            input.no_legal_problem_status ??
            output.likely_no_legal_problem,
        );
        if (noLegal === true || object(noLegal).value === true)
          result = [{ label: "No legal problem identified" }];
        if (result.length) {
          session.labels = result;
          if (
            JSON.stringify(result) !==
            JSON.stringify(session.classifications.at(-1)?.labels)
          ) {
            session.classifications.push({
              timestamp: text(row.timestamp),
              labels: result,
            });
          }
        }
        const qas = list(row.followup_questions_answers).length
          ? list(row.followup_questions_answers)
          : list(input.followup_questions_answers || input.followup_answers);
        for (const item of qas) {
          const qa = object(item);
          const question = text(qa.question);
          if (!question) continue;
          const id = normalized(question);
          const entry = questions.get(id) || {
            question,
            options: [],
            answers: [],
            visibility: [],
          };
          const answer = text(decode(qa.answer));
          if (answer && entry.answers.at(-1) !== answer)
            entry.answers.push(answer);
          questions.set(id, entry);
        }
        for (const item of list(
          output.follow_up_questions || row.generated_questions,
        )) {
          const generated =
            typeof item === "string" ? { question: item } : object(item);
          const question = text(generated.question);
          if (!question) continue;
          const id = normalized(question);
          const entry = questions.get(id) || {
            question,
            options: [],
            answers: [],
            visibility: [],
          };
          const options = list(generated.options)
            .map((option) =>
              typeof option === "string" ? option : text(object(option).label),
            )
            .filter(Boolean);
          if (options.length) entry.options = options;
          questions.set(id, entry);
        }
        for (const item of list(presentation.questions)) {
          const shown = object(item);
          const question = text(shown.question);
          if (!question || typeof shown.shown !== "boolean") continue;
          const id = normalized(text(shown.generated_question) || question);
          const displayId = normalized(question);
          const entry = questions.get(id) ||
            questions.get(displayId) || {
              question,
              options: [],
              answers: [],
              visibility: [],
            };
          if (shown.shown) entry.question = question;
          const evidence = {
            iteration: Number(presentation.iteration) || 0,
            shown: shown.shown,
            reason: text(shown.reason),
          };
          if (
            !entry.visibility.some(
              (v) => JSON.stringify(v) === JSON.stringify(evidence),
            )
          )
            entry.visibility.push(evidence);
          // Preserve localized display text without duplicating the generated prompt.
          if (displayId !== id) questions.delete(displayId);
          questions.set(id, entry);
        }
        const person = object(input.person);
        const name = [
          text(row.first_name || person.first_name),
          text(row.last_name || person.last_name),
        ]
          .filter(Boolean)
          .join(" ");
        if (name) session.person = name;
        const contact = [
          text(row.email || person.email),
          text(row.phone || person.phone),
        ]
          .filter(Boolean)
          .join(" · ");
        if (contact) session.contact = contact;
        const referrals = list(row.referrals).length
          ? list(row.referrals)
          : list(input.referrals);
        const attorneys = referrals
          .filter((r) => object(r).success)
          .map((r) => {
            const attorney = object(object(r).attorney);
            return (
              [
                text(attorney.first_name),
                text(attorney.last_name),
                attorney.bar_number ? `Bar ${text(attorney.bar_number)}` : "",
                text(attorney.email),
                text(attorney.phone),
              ]
                .filter(Boolean)
                .join(" · ") || "Attorney details not recorded"
            );
          });
        if (attorneys.length) session.attorneys = attorneys;
      }
      session.questions = [...questions.values()];
      return session;
    })
    .sort((a, b) => b.timestamp.localeCompare(a.timestamp));
}
