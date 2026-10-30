# Deterministic screening protocol API

FETCH returns screening questions in the `screening_questions` side channel. By
default, it also mirrors them at the beginning of `follow_up_questions` for legacy
clients. A screening question is never rewritten or semantically merged. At most
three distinct questions are returned in a classification round; eligible screening
questions consume the available positions first.

Before returning a protocol, FETCH first applies conservative keyword checks and then
runs one lightweight LLM extraction pass over only the eligible protocols. Each
validated choice is classified as `disclosed`, `ruled_out`, or left unresolved. The
LLM may only return supplied stable IDs; invalid IDs and statuses are discarded, and
keyword decisions win any conflict. If the LLM is unavailable or times out, FETCH
falls back to keywords without failing classification.

The lightweight pass uses `OPENAI_SCREENING_MODEL` (default `gpt-4.1-nano`) and an
eight-second timeout configurable with `SCREENING_RESOLUTION_TIMEOUT_SECONDS`.
Resolution `source` is `narrative_keyword` or `narrative_llm` so clients and audits
can distinguish the evidence path.

An explicitly disclosed or ruled-out checkbox choice is omitted while the other
stable choices remain available. Its opposite `none`/`no` choice is also removed
after a positive disclosure. The entire question is omitted only when no substantive
unresolved choices remain. The extraction prompt prohibits demographic inference and
requires choices to remain unresolved when the narrative is ambiguous.

Choices that are still returned carry `resolution_status: "unresolved"`.

Every narrative-derived decision is returned in `screening_resolutions`. A
`disclosed` resolution includes the choice's deterministic risk level, information
trigger, and mandatory category IDs. A `ruled_out` resolution identifies an omitted
choice and has no outcome. The strongest disclosed choice for each protocol is also
included in `screening_results` with `answer_source: "narrative"`, so clients can
display relevant messages even when no screening question was shown.

```json
{
  "screening_resolutions": [
    {
      "protocol_id": "employment_retaliation.v1",
      "protocol_version": 1,
      "question_id": "employment_retaliation.report.v1",
      "choice_id": "yes_fired",
      "status": "disclosed",
      "source": "narrative_keyword",
      "risk_level": "elevated",
      "information_trigger": "employment_retaliation_time_sensitive",
      "mandatory_category_ids": [
        "employment.whistleblowers",
        "employment.wrongful_discharge"
      ]
    },
    {
      "protocol_id": "employment_retaliation.v1",
      "protocol_version": 1,
      "question_id": "employment_retaliation.report.v1",
      "choice_id": "yes_not_fired",
      "status": "ruled_out",
      "source": "narrative_keyword",
      "risk_level": null,
      "information_trigger": null,
      "mandatory_category_ids": []
    }
  ]
}
```

## Translation contract

`protocol_id`, `question_id`, choice `id`, and `translation_key` are stable
invariants. `question` and choice `label` use the requested `screening_language`
(`en` by default; `es` is also supported). Each item also contains approved `en`
and `es` values in `translations`.

Clients may ignore all FETCH-provided display strings and use `translation_key`
with their own localization catalog. They must return the original stable IDs.

```json
{
  "text": "I am getting divorced.",
  "screening_language": "es"
}
```

```json
{
  "screening_questions": [
    {
      "protocol_id": "family_safety.v1",
      "protocol_version": 1,
      "question_id": "family_safety.behavior.v1",
      "question": "¿Alguna persona involucrada…?",
      "translation_key": "screening.family_safety.v1.question",
      "format": "checkbox",
      "choices": [
        {
          "id": "none",
          "label": "No, nada de esto",
          "translation_key": "screening.family_safety.v1.choice.none",
          "translations": {
            "en": "No, none of these",
            "es": "No, nada de esto"
          },
          "resolution_status": "unresolved"
        }
      ]
    }
  ]
}
```

## Answer contract

Send every selected checkbox ID in `choice_ids`. The legacy singular `choice_id`
remains accepted. Display text is optional and is ignored by deterministic
evaluation. Unknown IDs, duplicates, and combinations containing an exclusive
choice such as `none` or `prefer_not` receive HTTP 422.

```json
{
  "text": "I am getting divorced.",
  "screening_answers": [
    {
      "protocol_id": "family_safety.v1",
      "question_id": "family_safety.behavior.v1",
      "choice_ids": [
        "controlling_or_threatening",
        "physical_or_sexual_harm"
      ]
    }
  ]
}
```

The response keeps predictive and deterministic evidence separate:

- `labels` is unchanged ranked model output.
- `mandatory_categories` contains required protocol routes and no invented
  confidence value.
- `screening_results` is the per-answer risk/routing report.
- `information_triggers` contains controlled IDs for client-localized resources.
- `effective_categories` is a convenience union for routing. A duplicate keeps
  its real model confidence and has both `model` and `protocol` sources. A
  protocol-only route has `confidence: null`.

`risk_level` controls message prominence only. It is not model confidence, legal
merit, or a clinical assessment. Clients should show `urgent` content before
ordinary referral results, preserve every mandatory category, and avoid placing
sensitive displayed answers in URLs, analytics, notifications, email, or page
titles.

Screening presentation is enabled by default. With the default
`legacy_screening: true`, each screening checkbox question appears first in
`follow_up_questions` and is also returned in `screening_questions`. Set
`legacy_screening: false` to opt into side-channel-only presentation. Set
`enable_screening_protocols: false` to suppress new protocol questions entirely.
Submitted structured answers are evaluated even when presentation is disabled.
`skip_followups: true` suppresses both LLM and protocol questions while preserving
submitted outcomes.
