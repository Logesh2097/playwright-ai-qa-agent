# Test Execution Engine

The deterministic engine that runs test cases with Playwright (Phase 7).
It executes scripts of strictly typed steps, evaluates assertions itself, and
reports what happened step by step. **No model is involved**: not in running a
step, not in judging it, not in the verdict.

Pinned versions: Playwright for Python 1.65.2 on
`mcr.microsoft.com/playwright/python:v1.65.2-noble` (Chromium, Firefox, WebKit).
The package version in `backend/pyproject.toml` and the image tag in
`backend/Dockerfile` must be changed together.

For the other way of driving a browser — MCP tools for agent exploration — see
[playwright-mcp-integration.md](playwright-mcp-integration.md). The two share
the egress proxy and nothing else.

## 1. Architecture

```
 api ── queue ──► worker ────────────── HTTP (token), event stream ────────────► engine container
                  ExecutionRunner                                               server.py
                    builds the plan                                               one process per run:
                    stores steps, results,                                        child.py ─ runner.py
                    evidence, verdict                                               browser.py   one browser per run,
                       │                                                                         one context per case attempt
                       │ rules for this run (token)                                 locators.py · actions.py · assertions.py
                       └──────────────► egress proxy ◄──── all browser traffic ──── evidence.py
                                              │
                                              ▼
                                     application under test
```

Each concern has its own module:

| Concern | Where |
|---------|-------|
| Test case representation | `app/testing/automation.py` (the script and its steps), `app/testing/cases.py` (the description for reviewers) |
| Checks before a run | `app/testing/automation_checks.py` |
| Test step execution | `app/engine/runner.py` (order, login, cleanup, retries, time limits), `app/engine/steps.py` |
| Browser management | `app/engine/browser.py` |
| Locator management | `app/engine/locators.py` |
| Actions | `app/engine/actions.py` |
| Assertion handling | `app/engine/assertions.py`, `app/engine/matching.py`, `app/testing/expressions.py` |
| Evidence collection | `app/engine/evidence.py` |
| Result persistence | `app/services/executions.py` (`ExecutionRunner`), `app/services/evidence.py` |
| Verdict | `app/testing/verdicts.py` |
| Wire format between worker and engine | `app/engine/protocol.py` |

The engine code lives in the backend package so that the step schema is one
definition used by the API, the worker and the engine. It runs in its own
image and container; the API and worker images carry the Playwright package
but no browsers.

## 2. Scripts

A test case has two forms. Its **description** (`CaseContent`, Phase 5) says in
words what is tested and is what people review. Its **script** says exactly
what the engine does. The script is stored with the case version
(`test_case_versions.automation`), so changing it creates a new version that
needs approval again, and an approved suite keeps running the versions that
were approved with it.

```json
{
  "login_as": "customer",
  "data_sets": ["checkout"],
  "steps": [
    {"action": "navigate", "url": "/loyalty"},
    {"action": "fill", "locator": {"by": "label", "value": "Bill amount"}, "value": "{{data.bill_amount}}"},
    {"action": "click", "locator": {"by": "role", "role": "button", "name": "Calculate points"}},
    {"action": "read_text", "locator": {"by": "test_id", "value": "points"}, "save_as": "points", "as_number": true},
    {"action": "assert_expression", "expression": "points == floor(number(data.bill_amount) / 10)"}
  ],
  "cleanup": []
}
```

Scripts are written by people through the API
(`PUT …/test-cases/{id}/automation`), or by the agent workflow from the
description and an outline of the application
([agent-workflow.md](agent-workflow.md)). Either way a person approves them.

### 2.1 Steps

Every step is validated against its own schema; unknown fields are rejected.
There is no step that runs code, evaluates JavaScript from a test case, sleeps
for a fixed time, or takes a raw selector string.

| Step | Fields | Environment action |
|------|--------|--------------------|
| `navigate` | `url` (a path on the environment, or a full allowed address) | navigate |
| `click` | `locator` | click |
| `fill` | `locator`, `value` | type |
| `clear` | `locator` | type |
| `select_option` | `locator`, `value` or `label` | select |
| `check`, `uncheck` | `locator` | click |
| `select_radio` | `locator` (must be a radio button) | click |
| `upload_file` | `locator`, `file` (name of a test file of the project) | upload |
| `wait_for_element` | `locator`, `state`: visible, hidden, attached, detached | read |
| `wait_for_navigation` | `url` (text match) | read |
| `read_text` | `locator`, `save_as`, `as_number` | read |
| `read_attribute` | `locator`, `attribute`, `save_as` | read |
| `screenshot` | `name`, `full_page` | read |
| `api_request` | `method`, `url`, `headers`, `json_body`, `save_as` | api_read (GET, HEAD) or api_write |
| assertions | see section 4 | read |

Common fields: `description`, `timeout_ms`. Assertions also take
`continue_on_failure`, which records the failure and carries on so that one run
reports several problems; the case still fails.

A script has 1 to 100 steps and up to 30 cleanup steps. Cleanup steps cannot be
assertions.

### 2.2 Values

Text in steps may contain placeholders, resolved at the moment the step runs:

| Placeholder | Meaning |
|-------------|---------|
| `{{env.NAME}}` | A variable of the environment |
| `{{data.NAME}}` | Test data: the case's own, or from a data set the script names |
| `{{cred.ROLE.FIELD}}` | A field of a stored test credential |
| `{{unique.KIND}}`, `{{unique.KIND_label}}` | A value generated for this attempt. Kinds: `id`, `email`, `number`, `text`. The same name gives the same value within one attempt |
| `{{var.NAME}}` | A value an earlier step saved with `save_as` |

### 2.3 Checks before a run

Made when a script is saved, when the case is approved, when an execution is
requested and again when the worker builds the plan
(`check_script`). Blocking problems prevent approval, and a case whose script
no longer fits the environment is recorded as `blocked` instead of being run.

- every placeholder can be resolved; a `var` is saved by an earlier step;
- addresses are paths or inside the environment's allowed origins;
- each step's action is allowed by the environment, and state-changing steps are not used on a read-only one;
- the uploaded file, the data sets, the login credential and its recipe exist;
- API assertions refer to a response an earlier step saved;
- the script contains at least one assertion.

A CSS locator produces a warning, not a refusal.

## 3. Locators

```json
{"by": "role", "role": "button", "name": "Delete", "exact": true,
 "within": {"by": "role", "role": "row", "name": "Tea"}}
```

| `by` | Maps to | Notes |
|------|---------|-------|
| `role` | `get_by_role(role, name=…)` | Preferred. Roles from a fixed list. |
| `label` | `get_by_label` | Preferred for form fields. |
| `test_id` | `get_by_test_id` | Stable where the application provides IDs. |
| `placeholder` | `get_by_placeholder` | |
| `text` | `get_by_text` | |
| `css` | `locator("css=…")` | When nothing else reaches the element. Flagged as fragile. |

`within` narrows the search to inside another element, one level deep.

Rules:

- **A locator must match exactly one element.** None is `locator_not_found`, several is `locator_ambiguous`. The engine never takes "the first one".
- **XPath is not accepted**, nor chained selectors (`>>`) or engine prefixes (`text=`, `xpath=`). The `css=` prefix is added by the engine, so the text cannot switch to another selector engine.
- What was used and how many elements it matched is stored with each step.

Not built: fallback locators and self-healing (the table for proposals exists
since Phase 2; the feature is later work).

## 4. Assertions

An assertion waits: it re-reads the page until what it expects is there or its
time is up (the step's `timeout_ms`, else the action timeout). There are no
fixed sleeps.

| Assertion | Checks |
|-----------|--------|
| `assert_visible`, `assert_hidden` | Element visibility |
| `assert_exists`, `assert_absent`, `assert_count` | Element existence and number |
| `assert_enabled`, `assert_disabled` | Enabled state |
| `assert_checked` | Checkbox or radio state |
| `assert_text` | Text content |
| `assert_value` | Input value |
| `assert_validation_message` | The browser's own validation message of a field |
| `assert_url` | Address. A plain path ignores the query; a path with `?` compares path and query; a full address compares everything |
| `assert_title` | Page title |
| `assert_api_status` | Status code of a saved API response; one or a list |
| `assert_api_json` | One place in a JSON response by JSON Pointer: `equals`, `text`, `exists` or `type` |
| `assert_expression` | A business rule over saved values |

Text comparisons (`equals`, `contains`, `matches`, `not_equals`, optional
`ignore_case`) treat runs of white space as one space. Form validation messages
shown by the application itself are checked with `assert_text` on the message
element.

**Three outcomes, never merged:**

| Outcome | Meaning | Step status |
|---------|---------|-------------|
| Holds | What was expected is there | `passed` |
| Does not hold | The application did something else. Expected and actual are stored | `failed` |
| Cannot be evaluated | No such element, several elements, no saved response, a rule with a missing value | `error` |

An error says nothing about the application, so it is never counted as a
failure of the application, and never as a pass.

### Business rules

```
points == floor(number(data.bill_amount) / 10)
total == subtotal + tax and total <= 5000
```

`app/testing/expressions.py` parses the expression into a syntax tree and
evaluates it with a fixed set of operators (`+ - * / // %`, comparisons,
`and or not`, `in`, `x if c else y`) and functions (`floor ceil round abs min
max number text lower upper strip length`). No attribute access on values, no
indexing, no other calls, no exponentiation; nothing is passed to `eval`.
Numbers are decimals, so `0.1 + 0.2 == 0.3` holds and `round` rounds half up.
`number("₹1,250.50")` is 1250.50; text with two numbers is refused, not guessed.

The rule itself comes from the script, which a person wrote and approved. The
engine does not know or assume any formula.

## 5. How a case and an execution are judged

One case attempt:

| Status | When |
|--------|------|
| `error` | A step could not be performed: no such element, time ran out, address not allowed, sign-in failed, the browser broke. Also when every step passed but no assertion ran |
| `failed` | Every step could be performed and at least one assertion did not hold |
| `passed` | Every step was performed, every assertion held, and at least one assertion ran |

After a step fails or errors, the remaining steps are `skipped` (unless the
step was an assertion with `continue_on_failure`). With retries, a case that
passes only on a later attempt is `flaky`, never `passed`; every attempt's
steps are stored.

One execution (`app/testing/verdicts.py`):

| Verdict | When |
|---------|------|
| `failed` | At least one case failed |
| `error` | None failed, at least one could not be carried out |
| `inconclusive` | None failed or errored, but not every selected case ran: skipped (no script), blocked (script does not fit the environment), cancelled, or nothing to run |
| `needs_review` | All ran and none failed, but at least one result has to be looked at by a person (Phase 9: it passed only with a replacement locator) |
| `flaky` | All ran and passed, at least one only after a retry |
| `passed` | Every selected case ran and passed first time, as written |

From Phase 9 every result that did not pass also has a `failure_category`, and a
case may end as `needs_review`. Categories, failure reports, locator recovery and
flaky detection are described in
[result-validation-and-healing.md](result-validation-and-healing.md).

## 6. Test data

| Need | Mechanism |
|------|-----------|
| Fixtures, reusable data | **Data sets**: named sets of values per project (`test_data_sets`), used with `data_sets` in a script |
| Environment-specific data | A data set of the same name for one environment replaces the general one there, value by value. Environment variables are available as `{{env.NAME}}` |
| Data of one case | The case's own `test_data`; it wins over data sets |
| Validation | Unknown placeholders block approval. Names are identifiers; values are limited in size and number |
| Unique data | `{{unique.email}}` and the other kinds, new for every attempt, so reruns do not collide |
| Files to upload | **Test files** (`test_files`): added by QA engineers and admins, which is what approves them. Limited types and size. `upload_file` can name nothing else; the engine writes them under names of its own choosing |
| Cleanup | A script's `cleanup` steps always run after its steps, also after a failure or a timeout, each one attempted even if an earlier one failed. A failed cleanup is flagged on the result (`details.cleanup`) and does not change the verdict. The browser context, the run's directory and its processes are removed regardless |

**No real customer data.** Whoever saves a data set or a test file must confirm
it is made up (`is_synthetic`, enforced by a database constraint). Values that
are valid payment card numbers are refused unless they are one of the
well-known test numbers. Production environments are read-only by default, so
state-changing steps do not pass the pre-run checks there. Beyond that this is
a rule for people: the platform cannot tell a made-up name from a real one.

## 7. Configuration

Per execution (`config` of `POST …/executions`), all validated:

| Setting | Values | Default |
|---------|--------|---------|
| `browser` | chromium, firefox, webkit | chromium |
| `viewport` | width 320–3840, height 240–2160 | 1280×720 |
| `timeouts.action_ms` | 500–60000 | 5000 |
| `timeouts.navigation_ms` | 1000–120000 | 30000 |
| `timeouts.test_ms` | 5000–900000, per case attempt | 120000 |
| `retry.max_attempts` | 1–3 | 1 |
| `retry.retry_on` | `error`, `failed` | `["error"]` |
| `evidence.screenshots` | off, on_failure, always (after every step) | on_failure |
| `evidence.video` | off, on_failure, always | off |
| `evidence.trace` | off, on_failure, always | off |
| `evidence.logs` | off, on_failure, always (console and network logs as files; [reports-and-evidence.md](reports-and-evidence.md) §1) | on_failure |
| `evidence.full_page` | true, false | false |
| `healing.mode` | off, propose, recover (what to do when a locator matches nothing; [result-validation-and-healing.md](result-validation-and-healing.md) §5) | off |
| `healing.max_per_case` | 1–5 recovered steps per case attempt | 3 |

A step may override its own timeout with `timeout_ms`. Installation limits:
`EXECUTION_TIMEOUT_S` (whole execution, default 30 minutes),
`EXECUTION_MAX_CASES`, `EXECUTION_MAX_ACTIVE`, `ENGINE_MAX_RUNS`.

## 8. Isolation and limits

| Boundary | Mechanism |
|----------|-----------|
| Between cases | A new browser context for every case attempt: own cookies, storage and cache. Tested: a case without sign-in after a signed-in case is not signed in |
| Between executions | A separate operating-system process and browser per run, with its own directory; nothing is reused |
| Engine and the platform | Its own container on the internal `runner` network. Checked on the running stack: PostgreSQL, Redis, the API, the internet and the metadata address are unreachable; the file system and the code are read-only; no platform secret is in its environment. The run process does not inherit the service's token either |
| Engine and the outside | All browser traffic, including API steps, goes through the egress proxy with credentials that exist for this run only. The proxy connects only to the environment's allowed origins and never to the platform's own addresses (see [playwright-mcp-integration.md](playwright-mcp-integration.md) §6) |
| Inside the browser | Requests to other origins are aborted and recorded (`details.blocked_requests`). On a read-only environment, so is every request that is not GET, HEAD or OPTIONS, whoever caused it. Addresses in steps are checked before use, and the address is checked again after navigation. Downloads are refused; service workers are blocked |
| Time | Per step, per case attempt, per run (the engine kills the run's process group), per execution (the worker) |
| CPU, memory, processes | Container limits (`ENGINE_CPUS`, `ENGINE_MEMORY`, `pids_limit`), all capabilities dropped, `no-new-privileges`, an init process that reaps finished browsers |
| Concurrency | `ENGINE_MAX_RUNS` runs at a time; `EXECUTION_MAX_ACTIVE` executions per organization |

A refusal by the proxy is reported as `destination_not_allowed`, not as an
answer from the application: the engine recognises the proxy's response to API
steps, and the proxy does not answer refused page loads at all.

Stopping a run: closing the connection of the event stream stops it. That is
how cancellation works, and it means a worker that dies cannot leave a browser
running.

## 9. Sign-in and secrets

- `login_as` signs in before the case with the credential's login recipe (the format from Phase 6), in a browser context of its own with no video and no trace. Only the resulting session is carried into the case, in memory.
- Credentials are decrypted by the worker only for roles the scripts of this execution refer to, each use audited (`credential.use`, purpose `execution`), and sent to the engine inside the plan over the internal network. The plan is passed to the run process on standard input, never as a command line or a file.
- Every event passes a redactor before it leaves the engine: secret values appear as `[REDACTED]` in recorded values, errors and addresses.
- A case that types a secret itself (`{{cred.ROLE.password}}` in a `fill`, as a test of the login form would) gets no video and no trace, because both would record the value; the result notes `recordings_skipped_secret_input`. Screenshots are still taken, with the field painted over.

## 10. What is stored

| Asked for | Where |
|-----------|-------|
| Test execution ID | `test_executions.id`; status, verdict, totals, configuration, who started it, times, error |
| Test case ID | `test_results.test_case_id` and `test_case_version_id`; browser, viewport, attempts |
| Step ID | `test_execution_steps.id`, with `attempt`, `position` and `phase` (setup, main, cleanup) |
| Started and completed time | `started_at`, `finished_at`, `duration_ms` on steps, results and executions |
| Action result | Step `status`, `actual`, `page_url`, `resolved_locator` |
| Assertion result | Step `status`, `expected`, `actual` |
| Error details | Step `error_type`, `error_message`; result `failure_class`, `failure_summary`; execution `error` |
| Evidence references | `test_evidence` rows pointing at the result and the step, with kind, size, SHA-256 and expiry |
| Final status | Result `status`; execution `status` and `verdict` |

Evidence files are fetched from the engine by the worker and written under
`EVIDENCE_ROOT/<organization>/<execution>/` on a volume the API mounts
read-only. Storage keys are generated, never taken from a request, and every
read checks that the path is inside the organization's branch. Downloads are
served as attachments with `nosniff`.

Also stored per result (`details`): console errors, page errors, failed and
blocked requests, whether cleanup worked, and notes about evidence. These are
observations, not verdicts.

## 11. API

All under `/api/v1/organizations/{org}/projects/{project}`.

| Method | Path | Who |
|--------|------|-----|
| GET, PUT, DELETE | `/test-cases/{id}/automation` | read: members · write: developers and above |
| GET, POST | `/test-data-sets` | read: members · write: developers and above |
| PUT, DELETE | `/test-data-sets/{id}` | developers and above |
| GET, POST | `/test-files` | read: members · add: QA engineers and admins |
| DELETE | `/test-files/{id}` | QA engineers and admins |
| POST | `/executions` | developers and above; answers 202 |
| GET | `/executions`, `/executions/{id}` | members |
| POST | `/executions/{id}/cancel` | developers and above |
| GET | `/executions/{id}/results/{result}/steps` | members |
| GET | `/executions/{id}/evidence`, `/executions/{id}/evidence/{evidence}/content` | members |
| GET | `/executions/{id}/results/{result}/failure` | members |
| POST | `/executions/{id}/results/{result}/failure/analysis` | developers and above |
| GET | `/heal-proposals` | members |
| POST | `/heal-proposals/{id}/accept`, `/heal-proposals/{id}/reject` | developers and above |
| GET | `/stability`, `/test-cases/{id}/stability` | members |

Only approved work runs: an approved suite with the versions approved with it,
or individually approved cases. On an environment that requires approval, an
execution that changes data or signs in can only be started by an organization
admin; that is recorded, and the worker restricts the run to read-only actions
if the record is missing.

Details and error codes: [api-design.md](api-design.md) §7.

## 12. Tests

| Suite | Command | What it covers |
|-------|---------|----------------|
| Schema, pre-run checks, expressions, matching, verdicts, wire format, engine client, evidence store | `make be-test` | No browser, no services |
| Engine in real browsers | `make engine-test` | Every action, every locator strategy, every assertion with passing and failing cases, timeouts, cleanup, retries, sign-in, isolation between cases, secrets, evidence, the three browsers, the engine service |
| Executions through the API | `make be-test-integration` | Real database and Redis, engine faked: selection, plan contents, storage of steps, results and evidence, verdicts, permissions, tenant isolation, cancellation, limits, failures of the engine |
| End to end | `make be-test-browser` | Nothing faked: API, worker code, engine service, proxy, browsers, demo application, including a seeded defect and cancellation |

The demo application (`fixtures/demo-app`) has a loyalty page with the rule
"one point per 10 spent, rounded down" and a defective variant that rounds to
the nearest point; the engine passes the first and fails the second at 506,
where they differ.

## 13. Not built

- Scripts can now also be written by the agent workflow (Phase 8, [agent-workflow.md](agent-workflow.md)): from the reviewed case and an outline of the application, checked by the same schema and pre-run checks, and approved by a person before they run. Entering them by hand through the API still works.
- **Web pages** for scripts, test data, executions and evidence.
- **Fallback locators and self-healing.**
- **Viewport matrix and data-driven runs**: one viewport and one data row per execution.
- **Audit crawl and findings** (`test_findings` is not written).
- **Reports** (JUnit, HTML) and live event streaming to clients.
- **Removing expired evidence**: `expires_at` is set; no job deletes files yet.
- **Rerun of failed cases**, idempotency keys, triggers other than manual.
- **Approvals** for executions started directly are still "an admin starts it". The agent workflow has its own approval step.

## 14. Known limitations

- **Chromium runs without its own sandbox** in the engine container, as in the runner. Container limits and the network isolation are the containment.
- **Evidence can show test data.** Screenshots, videos and traces show whatever the application displays. Only credential secrets are kept out, as described in section 9.
- **A secret typed into a field that is not a password field** is painted over in screenshots but is visible to the application under test, as it must be.
- **Redirects of API steps are not followed**; the redirect response is what the step gets.
- **A timed-out step may still have taken effect** in the application.
- **Results are written when a case ends**, not step by step; a worker that dies mid-case loses that case's steps (the result is then an error).
- **One engine instance.** Runs are capped per instance; there is no scheduling across several.
- **The engine's event stream has no resume.** A broken connection ends the run and the execution fails.
- **Text matching is literal** apart from white space: no locale-aware number or date comparison.
