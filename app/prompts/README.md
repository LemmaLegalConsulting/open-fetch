# Adding custom prompt files

You can add a custom file named, e.g., gemini.txt, to make a model-specific prompt. For now the default prompt
might perform fine across all models.

The default initial and no-question templates both include
`classification_instructions.txt` through `{{classification_instructions}}`.
Keep taxonomy interpretation, applicant-role definitions, classification rules,
and non-legal classification criteria in that shared file. `skip_followups`
changes only question generation and the output format; it must not remove
classification guidance. Both paths receive the same taxonomy and hints.

Refinement calls still exclude keyword and SPOT by default because they cannot
use conversational answers. This prompt sharing does not change provider selection.
