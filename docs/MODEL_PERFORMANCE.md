# Terra + Haiku performance note

This is a **post-publication evaluation dated October 30, 2026**, not a result reported in [*That’s So FETCH: Fashioning Ensemble Techniques for LLM Classification in Civil Legal Intake and Referral*](https://doi.org/10.3233/FAIA251588). The recommended Terra + Haiku configuration differs from the paper’s model combination. These results do not reproduce or supersede the paper’s findings.

## Configuration and classification results

The selected configuration uses GPT-6.1 Terra and Claude Haiku 5.5 for classification, Terra for question merging, and Haiku for optional narrative screening. See [model hosting and configuration](MODEL_HOSTING.md). The classification evaluation used AWS Bedrock in `us-east-1`, with model IDs `us.openai.gpt-6.1-terra` and `us.anthropic.claude-haiku-5-5`. Azure Foundry was not benchmarked.

We evaluated 355 public/anonymized D1 narratives with 494 consensus gold-label instances from the [FETCH public research repository](https://github.com/nonprofittechy/fetch_public_data). Each model generated classification and initial follow-up questions. Saved predictions were combined using FETCH’s voting implementation, equal 1.0 weights, and Terra-first ordering for ties. No keyword or SPOT member was included in this pair.

| Configuration | Top-1 hit | Top-2 hit | All gold labels within top 2 | Micro F1 | Estimated median / p95 latency | Estimated USD / 1,000 cases |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Terra alone | 91.27% | 95.77% | 80.28% | 78.76% | 0.83 / 2.54 s | $2.59 |
| Haiku alone | 91.27% | 96.34% | 79.44% | 75.85% | 3.03 / 4.95 s | $0.89 |
| **Terra + Haiku** | **91.55%** | **96.62%** | **80.56%** | **78.41%** | **3.04 / 4.96 s** | **$3.47** |

“Top-2 hit” means at least one gold label appears among the first two predictions; it does not mean every issue was identified. Micro F1 measures precision/recall across returned label sets. Terra + Haiku had 343/355 top-two hits versus Haiku’s 342/355. The paired 95% bootstrap interval for that difference was −0.845 to +1.415 percentage points: this sample does **not establish a reliable accuracy gain over Haiku alone**. Terra alone had slightly higher micro F1 than the pair.

Ensemble latency is the maximum of the component call times for each case, estimating parallel inference; it is not measured end-to-end API latency. Costs use recorded tokens and then-current AWS list prices, including observed model caching, rather than an invoice. The pair’s estimate without cache discounts/premiums was $5.08 per 1,000 cases, using the same tokens rather than a separate cold-cache run. Figures include initial question generation but exclude merging, screening, hosting, telemetry, and graders.

## Question merging

A separate pilot compared the same 16 paired outputs: 12 public benchmark cases and four synthetic stress cases. Both models received the current merge prompt and identical inputs. Codex and Claude Opus 5.5 independently graded randomized, model-blinded outputs on readability, routing relevance, preservation of meaning, and usability (0–4 each).

| Judge | Terra wins | Haiku wins | Ties | Terra mean /16 | Haiku mean /16 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Codex | 10 | 2 | 4 | 15.13 | 12.94 |
| Claude Opus 5.5 | 9 | 4 | 3 | 14.25 | 13.81 |

Both judges favored Terra by pairwise wins, supporting its selection as the merge runner. However, Claude assigned identical mean scores on the 12 public cases (14.08/16 each) and identical overall readability scores (3.94/4 each). This small, model-graded pilot does not establish superior human readability. The judges differed especially on open-text questions versus structured choices and on retaining additional routing questions.

Median merge-call latency was 2.20 s for Terra and 4.02 s for Haiku. Estimated costs were $3.34 and $0.62 per 1,000 merge calls, respectively. These are per-merge costs, not costs for every classification request.

The service skips semantic merging at three or fewer unique questions. None of the 355 Terra + Haiku benchmark cases exceeded that threshold, so the public pilot cases **forced merging**. The classification scores above therefore do not measure any benefit from the selected merge runner. Narrative screening and follow-up reclassification accuracy were not evaluated here.

## Limits and provenance

- Model selection and evaluation used the same public benchmark; fresh held-out validation is needed. One completed generation per model/case does not measure repeat-run variability.
- The classification runner allowed 85-second HTTP / 90-second outer timeouts and 8,192 output tokens, versus the shipped adapter’s 15-second HTTP timeout and default 4,096-token Claude cap. Each selected model had two calls exceeding 15 seconds. Zero final model-output failures under benchmark limits does not establish production reliability.
- Temporary authentication failures were retried after credential refresh; final classification quality and latency use the last attempt per model/case. Model-output failures were not selectively repaired. Sampling and reasoning used provider defaults.
- Historical archived runs are not a controlled comparison with this pair: model versions, prompts/runtime, provider count, and weights differ. No new Gemini/Mistral calls were made. Do not attribute any historical difference solely to replacing those providers, or label these numbers as the paper’s results.
- Model-graded merging is not an independent human usability or legal-accuracy study. Neither this legal-intake dataset nor the merging pilot establishes non-legal screening accuracy.

Classification provenance: application commit [`7b1378369cb51cd730a50fecf03d4521ec5fbf97`](https://github.com/LemmaLegalConsulting/open-fetch/tree/7b1378369cb51cd730a50fecf03d4521ec5fbf97); public data commit [`a8ebe3c1a33cd37ffbeec6ec5a11b5625fc3ff67`](https://github.com/nonprofittechy/fetch_public_data/tree/a8ebe3c1a33cd37ffbeec6ec5a11b5625fc3ff67), file `datasets/d1_consensus_labels/d1_paper_dataset_355.csv`. The subsequent separate merge-role setting does not change classification voting. Local experiment records retain prompts, usage, predictions, and judge scores; those raw artifacts are not bundled with this repository. The public data and application commits identify inputs, but this note alone is not a turnkey reproduction package.
