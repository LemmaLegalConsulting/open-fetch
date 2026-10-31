from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import List, Optional, Dict, Any, Literal


class Label(BaseModel):
    """A predicted taxonomy label with optional confidence and LIST ID."""

    label: str
    confidence: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    id: Optional[str] = Field(
        default=None, description="Optional LIST taxonomy ID (e.g., 'HO-00-00-00-00')"
    )


class FollowUpQuestion(BaseModel):
    """A follow-up question to refine classification."""

    question: str
    format: Optional[str] = None
    options: Optional[List[str]] = None


class FollowUpAnswer(BaseModel):
    """A follow-up question paired with the user's answer."""

    question: str = Field(description="The follow-up question that was asked")
    answer: str = Field(description="The user's answer to the question")


class ReferralPerson(BaseModel):
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    city_id: str = ""
    state: str = ""


class ReferralAttorney(BaseModel):
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    bar_number: str = ""


class ReferralTelemetryOutcome(BaseModel):
    """OSB referral outcome and optional identified attorney."""

    success: bool
    categories: List[str] = Field(default_factory=list)
    sub_panel_ids: List[str] = Field(default_factory=list)
    response_count: Optional[int] = None
    attorney: Optional[ReferralAttorney] = None


class PresentedQuestion(BaseModel):
    question: str
    generated_question: str = ""
    kind: Literal["followup", "screening"] = "followup"
    shown: bool
    reason: str


class QuestionPresentation(BaseModel):
    iteration: int
    template: str
    screened_out_non_legal: Optional[bool] = None
    questions: List[PresentedQuestion] = Field(default_factory=list)


class ReferralTelemetryEvent(BaseModel):
    """Completed OSB interaction submitted for shared telemetry reporting."""

    event_type: Literal[
        "osbar.interaction.completed",
        "osbar.interaction.progress",
        "osbar.interaction.presentation",
    ] = "osbar.interaction.completed"
    conversation_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"
    )
    schema_version: int = 1
    environment: Literal["production", "development", "unknown"] = "unknown"
    is_test: bool = False
    test_reason: str = ""
    person: Optional[ReferralPerson] = None
    query: str = Field(max_length=50000)
    followup_questions_answers: List[FollowUpAnswer] = Field(default_factory=list)
    classification_result: List[Label] = Field(default_factory=list)
    no_legal_problem_status: bool = False
    presentation: Optional[QuestionPresentation] = None
    outcome: Literal["referred", "no_match", "no_legal_problem", "error", "in_progress"]
    referrals: List[ReferralTelemetryOutcome] = Field(default_factory=list)
    referral_attempts: List[Dict[str, Any]] = Field(default_factory=list)


class ScreeningAnswer(BaseModel):
    """Stable-ID answer to a deterministic screening protocol question."""

    protocol_id: str
    question_id: str
    choice_id: Optional[str] = Field(
        default=None,
        description="Legacy single-choice ID; use choice_ids for checkbox answers",
    )
    choice_ids: List[str] = Field(
        default_factory=list,
        description="Stable IDs for every selected checkbox choice",
    )
    question: Optional[str] = Field(
        default=None,
        description="Optional displayed question text; ignored by deterministic rules",
    )
    answer: Optional[str] = Field(
        default=None,
        description="Optional displayed answer text; ignored by deterministic rules",
    )

    @model_validator(mode="after")
    def validate_selected_choices(self) -> "ScreeningAnswer":
        selected = self.selected_choice_ids()
        if not selected:
            raise ValueError("screening answer must include choice_id or choice_ids")
        if len(selected) != len(set(selected)):
            raise ValueError("screening answer choice IDs must be unique")
        return self

    def selected_choice_ids(self) -> List[str]:
        selected = list(self.choice_ids)
        if self.choice_id is not None:
            selected.insert(0, self.choice_id)
        return selected


class LocalizedText(BaseModel):
    """Approved display translations keyed by BCP-47 language code."""

    en: str
    es: str


class ScreeningChoice(BaseModel):
    """A stable protocol choice with optional client-facing translations."""

    id: str
    label: str
    translation_key: str
    translations: LocalizedText
    resolution_status: Literal["unresolved"] = "unresolved"


class ScreeningQuestion(BaseModel):
    """A deterministic question that is never rewritten by an LLM."""

    protocol_id: str
    protocol_version: int
    question_id: str
    question: str
    translation_key: str
    translations: LocalizedText
    format: Literal["checkbox"] = "checkbox"
    choices: List[ScreeningChoice]


class ScreeningResult(BaseModel):
    """Deterministic risk/routing report produced from one structured answer."""

    protocol_id: str
    protocol_version: int
    question_id: str
    choice_id: str
    answer_source: Literal["structured", "narrative"] = "structured"
    risk_level: Literal["none", "informational", "elevated", "urgent"]
    information_trigger: Optional[str] = None
    mandatory_category_ids: List[str] = Field(default_factory=list)


class ScreeningResolution(BaseModel):
    """Narrative-derived status for an omitted question or choice."""

    protocol_id: str
    protocol_version: int
    question_id: str
    choice_id: str
    status: Literal["disclosed", "ruled_out"]
    source: Literal["narrative_keyword", "narrative_llm"]
    risk_level: Optional[Literal["none", "informational", "elevated", "urgent"]] = None
    information_trigger: Optional[str] = None
    mandatory_category_ids: List[str] = Field(default_factory=list)


class MandatoryCategory(BaseModel):
    """A protocol-required route, separate from model confidence."""

    id: str
    label: str
    source: Literal["protocol"] = "protocol"
    protocol_id: str
    rule_id: str


class EffectiveCategory(BaseModel):
    """Convenience union of model labels and mandatory protocol routes."""

    id: Optional[str] = None
    label: str
    confidence: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    mandatory: bool = False
    sources: List[Literal["model", "protocol"]] = Field(default_factory=list)


class ClassificationRequest(BaseModel):
    """Request payload for the classification endpoint."""

    model_config = ConfigDict(populate_by_name=True)

    # Accept legacy/short `text` field used by tests/clients while keeping
    # the explicit `problem_description` name internally.
    problem_description: str = Field(..., alias="text", min_length=1, max_length=50000)
    client_request_id: Optional[str] = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
        description="Opaque caller-generated ID for this request; log it in your application to correlate with the returned FETCH request_id. Do not include personal information.",
    )
    user_id: Optional[str] = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
        description="Optional pseudonymous user ID within your organization. Use a random ID, never a name, email, or other personal information. Organization is determined by your API key.",
    )
    conversation_id: Optional[str] = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
        description=(
            "Optional conversation identifier supplied by the client to correlate multiple queries "
            "in a multi-turn session. Reuse an opaque random ID across initial and follow-up requests; "
            "do not include personal information. Telemetry associates it with the organization derived from your API key."
        ),
    )
    taxonomy_name: str = Field(
        default="default",
        description="Name of the taxonomy to use (e.g., 'default', 'list')",
    )
    taxonomy_format: str = Field(
        default="osb",
        description=(
            "Output format for classification labels. Options: 'osb' (default OSB taxonomy), "
            "'list' (LIST taxonomy codes). When taxonomy_format='list' and taxonomy_name='default', "
            "results are converted from OSB to LIST codes using a mapping. When taxonomy_name='list', "
            "classification is done directly against the LIST taxonomy."
        ),
    )
    include_debug_details: bool = Field(
        default=False, description="Include full voting details in the response"
    )
    decision_mode: str = Field(
        default="vote",
        description="Decision mode for combining classifier results: 'vote' or 'first'",
    )
    skip_followups: bool = Field(
        default=False,
        description="Skip generating follow-up questions while retaining the same taxonomy and classification guidance. Returns empty follow_up_questions.",
    )
    enabled_models: Optional[List[str]] = Field(
        default=None,
        min_length=1,
        description="List of enabled classifier models. If None, all enabled classifiers will be used.",
    )
    skip_semantic_merge: bool = Field(
        default=False,
        description="Skip semantic merging of follow-up questions. Reduces latency but may return duplicate questions.",
    )
    followup_answers: Optional[List["FollowUpAnswer"]] = Field(
        default=None,
        description="All accumulated question/answer pairs from previous turns. Send the original text and the same conversation_id again; conversation_id correlates telemetry but does not restore conversation history. Each answer must be a string.",
    )
    screening_answers: Optional[List[ScreeningAnswer]] = Field(
        default=None,
        description=(
            "Stable-ID answers to screening questions. Optional display text is ignored "
            "when deterministic outcomes are evaluated."
        ),
    )
    screening_language: Literal["en", "es"] = Field(
        default="en",
        description="Language used for FETCH-provided screening display text",
    )
    enable_screening_protocols: bool = Field(
        default=True,
        description="Whether deterministic screening questions are included",
    )
    legacy_screening: bool = Field(
        default=True,
        description=(
            "Also place screening questions first in follow_up_questions. Set false "
            "for side-channel-only screening presentation."
        ),
    )


class ClassificationResponse(BaseModel):
    """Response payload with aggregated labels, questions, and consensus analysis."""

    degraded: bool = False
    failed_providers: List[str] = Field(default_factory=list)

    request_id: Optional[str] = Field(
        default=None,
        description="Server-generated unique ID for this classification call. Save it with your client_request_id for support and end-to-end correlation.",
    )
    client_id: Optional[str] = Field(
        default=None,
        description="Organization/deployment identity derived from the authenticated API key; null for unmapped keys or development mode. Cannot be overridden by request fields.",
    )
    labels: List[Label] = Field(
        description="Top aggregated legal categories selected by the default multi-issue-aware selector"
    )
    follow_up_questions: List[FollowUpQuestion] = Field(
        description="Up to 3 follow-up questions prioritized by semantic relevance"
    )
    screening_questions: List[ScreeningQuestion] = Field(
        default_factory=list,
        description="Stable deterministic questions, separate from LLM follow-ups",
    )
    screening_results: List[ScreeningResult] = Field(
        default_factory=list,
        description="Deterministic risk and routing outcomes for submitted answers",
    )
    screening_resolutions: List[ScreeningResolution] = Field(
        default_factory=list,
        description=(
            "Narrative-derived disclosed or ruled-out choice statuses, including "
            "message/routing outcomes for disclosed choices"
        ),
    )
    mandatory_categories: List[MandatoryCategory] = Field(
        default_factory=list,
        description="Required referral routes; these values are not model confidence",
    )
    effective_categories: List[EffectiveCategory] = Field(
        default_factory=list,
        description="Deduplicated union of model labels and mandatory routes",
    )
    information_triggers: List[str] = Field(
        default_factory=list,
        description="Controlled message IDs for client-localized informational content",
    )

    class LikelyNoLegalProblem(BaseModel):
        """Consensus analysis for non-legal problem detection.

        - value: Final decision (True if weighted_result meets threshold)
        - weighted_result: Fraction of total provider weight voting 'no legal problem' (0.0-1.0)
        - disagreement_score: Measure of category disagreement (0.0 = full consensus, 1.0 = max disagreement)
        """

        value: bool = Field(
            description="Whether the problem is likely not legal (based on a strict majority of attempted provider weight)"
        )
        weighted_result: float = Field(
            description="Weighted fraction voting for 'no legal problem' (0.0-1.0)"
        )
        disagreement_score: float = Field(
            description="Category disagreement level (0.0-1.0, higher = more disagreement)"
        )

    likely_no_legal_problem: LikelyNoLegalProblem = Field(
        description="Consensus metrics for non-legal problem detection"
    )

    # Debug fields, populated when include_debug_details is True
    raw_provider_results: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Raw results from each classifier provider (debug mode only)",
    )
    weighted_label_scores: Optional[Dict[str, float]] = Field(
        default=None, description="Weighted scores for each label (debug mode only)"
    )
    weighted_question_scores: Optional[Dict[str, float]] = Field(
        default=None, description="Weighted scores for each question (debug mode only)"
    )
