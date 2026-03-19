# Review: OpenAI API Key Tests — Potential Improvements

Improvements identified during code review, not addressed in the initial implementation.

## 1. Source-matching tests are brittle

`test_uses_responses_stream` and `test_model_is_gpt_4_1` do substring searches
on raw source files. If the model string moves to a config file, environment
variable, or constant, these tests silently pass or fail incorrectly. Consider
importing the actual value or parsing the AST instead.

## 2. Redundant test coverage

`test_no_openai_hosted_tools` is strictly weaker than
`test_all_tools_are_function_type` — if all tools are `type: "function"`, none
can be OpenAI-hosted. Both are kept for clarity of intent, but could be
collapsed into one test.

## 3. `OPENAI_HOSTED_TOOL_TYPES` duplicated across test files

The constant is identical in `backend-ephemeral/tests/test_api_usage.py` and
`backend-temporal/tests/test_api_usage.py`. If OpenAI adds a new hosted tool
type, both files need updating. Could be extracted to a shared location if a
shared test utilities package is introduced.
