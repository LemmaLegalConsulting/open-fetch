# Validation setup

The provider adapter, selection assertions, follow-up rubrics, readability assertions, and legal easy-word list are retained. Original intake rows, human labels, model outputs, failure analyses, and study results are excluded.

Activate the Python virtual environment and install both requirements.txt and requirements-dev.txt first. Use an installed `promptfoo` CLI and configure your own model credentials. Run from the repository root:

```bash
source .venv/bin/activate
OFFLINE_MODE=1 promptfoo eval -c promptfooconfig.yaml
OFFLINE_MODE=0 promptfoo eval -c promptfoo/selection_eval.yaml
OFFLINE_MODE=0 promptfoo eval -c promptfoo/followup_questions_eval.yaml
```

The first command is a keyword-only plumbing check. Model-backed selection and follow-up evaluation requires provider credentials and may incur charges; follow-up rubrics also use an LLM grader.

All configurations initially read synthetic_examples.csv. Those two newly authored examples exercise the setup, not model quality or representative performance. For your own validations, put CSVs in private-data/ (ignored by Git), and change the tests file reference in a local configuration. Columns are problem_description, expected_category, and expected_subcategory. Selection also supports suite, expected_top_levels_all (|| separated), and min_expected_top_levels. Follow-up evaluation only requires problem_description.

Keep PromptFoo databases, exports, and caches private: they can reproduce entire inputs and responses. Do not publish your local inputs or results by force-adding ignored files.
