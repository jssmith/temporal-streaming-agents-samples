# Plan: Document Minimum OpenAI API Key Requirements

## Goal

Add a short, explicit note to the README describing the minimum OpenAI API key
permissions required by this repo, and validate that claim with a small set of
manual tests.

## Current State

- The repo documents `OPENAI_API_KEY` as a prerequisite and runtime requirement.
- It does not currently say whether a full-access key is required or whether a
  restricted key is sufficient.
- Both backends use the OpenAI Python SDK with the Responses API and model
  `gpt-4.1`.
- The app's "tools" are local application tools (`execute_sql`,
  `execute_python`, `bash`), not OpenAI-hosted tools such as file search or web
  search.

Relevant code and docs:

- `README.md`
- `backend-ephemeral/src/agent.py`
- `backend-temporal/src/activities.py`
- `backend-temporal/src/workflows.py`

## Minimal Requirement To Document

The README should state:

- A normal project API key with `All` permissions works.
- A `Restricted` key should also work if it has at least `Write` permission for
  `/v1/responses`.
- A `Read Only` key should not work for this app.
- The project using the key must also have access to the `gpt-4.1` model.
- No additional permissions for Assistants, File Search, Web Search, Code
  Interpreter, or other OpenAI-hosted tools are required by this sample.

Basis for this requirement:

- OpenAI key permission modes are `All`, `Restricted`, and `Read Only`.
- The app creates streamed responses through `POST /v1/responses`.
- `gpt-4.1` supports the Responses API.

Reference docs to cite in the README change:

- https://help.openai.com/en/articles/8867743-assign-api-key-permissions
- https://platform.openai.com/docs/api-reference/responses
- https://platform.openai.com/docs/models/gpt-4.1

## README Change Plan

### 1. Add a short note in Prerequisites

Update the existing prerequisite bullet:

- Current: `OpenAI API key`
- Proposed: `OpenAI API key (either full-access, or a restricted key with Write access to /v1/responses)`

This keeps the requirement visible in the first setup pass.

### 2. Add a dedicated note in Setup or Running

Add a short block immediately before the first `export OPENAI_API_KEY=...`
example. Keep it operational and brief:

```md
Note: this sample uses the OpenAI Responses API. A full-access project key
works, or a restricted key with at least `Write` permission for `/v1/responses`.
`Read Only` keys will fail. No additional permissions for Assistants or OpenAI-
hosted tools are required.
```

This is the most useful placement because it appears at the point where the
reader is about to provide credentials.

### 3. Optionally add a troubleshooting line

In the README troubleshooting or setup area, add one sentence clarifying the
common failure mode:

- If startup fails with an authentication or client error, verify that the key
  belongs to a project with access to `gpt-4.1` and that restricted keys allow
  `Write` on `/v1/responses`.

This should stay optional unless the README starts growing too much.

## Validation Plan

Validation should prove the documented minimum is correct, not just that one key
works on one path.

### Test Matrix

Run the same small smoke test against both backends:

- Ephemeral backend
- Temporal backend

Test these key configurations:

1. Full-access project key
2. Restricted key with `Write` on `/v1/responses`
3. Read-only key
4. Restricted key without `Write` on `/v1/responses`

### Smoke Test Procedure

For each backend and key configuration:

1. Start the backend with `OPENAI_API_KEY` set to the test key.
2. Submit one simple prompt that should produce either plain text or one local
   tool call, such as:
   - `Show me the top 3 customers by total spending`
3. Verify whether the request succeeds or fails.
4. Capture the observed error class and user-visible behavior.

Expected outcomes:

- Full-access key: success
- Restricted key with `/v1/responses: Write`: success
- Read-only key: failure
- Restricted key without `/v1/responses: Write`: failure

### What To Verify On Success

- The backend can create a response successfully.
- Streaming text appears.
- If the model triggers a local tool call, the loop continues normally after the
  tool result is returned.
- No extra OpenAI endpoint permission is needed for the local tools.

### What To Verify On Failure

- The failure is immediate and understandable.
- The error path does not mislead the user into thinking the SQLite or local
  tools are misconfigured.
- The backend logs and surfaced error distinguish auth/permission failures from
  rate-limit or transport failures.

## Suggested Test Automation

Full permission validation cannot be made fully deterministic in CI without
maintaining multiple real OpenAI keys with different permission profiles, which
is usually not worth the operational cost. The practical split is:

- Automate local checks that confirm both backends call the Responses API.
- Keep real permission verification as a documented manual smoke test.

### Lightweight Automated Checks

Add tests that mock the OpenAI client and assert:

- The code path uses `responses.stream(...)`
- The configured model is `gpt-4.1`
- No OpenAI-hosted built-in tools are requested by default

This does not prove permissions, but it does prevent the README from drifting
away from the actual implementation.

### Manual Validation Record

After running the smoke tests once, record the results in the PR description or
commit message:

- date tested
- backend tested
- key type used
- pass/fail result
- observed error text for the negative cases

## Acceptance Criteria

- README clearly states the minimum key requirement.
- README does not imply that full-access keys are mandatory when they are not.
- Validation covers both backends.
- Negative cases are tested, not just the happy path.
- The documented requirement stays aligned with the actual OpenAI endpoint usage
  in the codebase.

## Follow-Up Implementation

After this plan, the next change should be:

1. Edit `README.md` with the concise permission note.
2. Add or update a small automated test that asserts the Responses API usage
   pattern.
3. Run the manual smoke-test matrix with real keys.
4. Update the README wording if the observed behavior differs from the current
   assumption.
