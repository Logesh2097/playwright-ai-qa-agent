# Result Validation, Failure Analysis and Self-Healing

How the platform decides what a test result means, what it does when a test
does not pass, how it recovers from a locator that no longer matches, and how
it finds unstable tests (Phase 9).

One rule runs through all of it: **what a result is, is decided by code from
what was recorded. A model may explain a result. It cannot change one.**

| Part | Decided by | Where |
|------|-----------|-------|
| Did each assertion hold | The test engine (Playwright assertions, API checks, business-rule expressions) | `app/engine/assertions.py` |
| Status of a case, verdict of an execution | Rules | `app/engine/runner.py`, `app/testing/verdicts.py` |
| What kind of problem a non-pass is | Rules | `app/testing/classification.py` |
| May a missing element be replaced | Rules | `app/engine/healing.py` |
| Is a test flaky | Rules | `app/testing/flakiness.py` |
| Summary, possible cause, impact, next steps | A model, stored as a labelled suggestion | `app/services/failure_analysis.py`, prompt `failure_analysis.v2` |

## 1. Result statuses

| Status | Meaning |
|--------|---------|
| `passed` | Every step was performed as the approved script says, every assertion held, at least one assertion ran |
| `failed` | Every step could be performed and at least one assertion did not hold |
| `error` | A step could not be performed (element missing, time ran out, sign-in refused, browser broke, address not allowed) |
| `flaky` | Passed, but only on a later attempt |
| `needs_review` | Nothing failed, but the result cannot be relied on until a person has looked: it passed only with a replacement locator, or its recorded steps do not support the pass |
| `skipped` | Not run (no script, cancelled, the execution ended first) |
| `blocked` | Could not be run in this environment as written |

`pending` and `running` exist while an execution is in progress.

Execution verdict, first match wins:

| Verdict | When |
|---------|------|
| `failed` | At least one case failed |
| `error` | None failed, at least one could not be carried out |
| `inconclusive` | None failed or errored, but not every case ran |
| `needs_review` | All ran, none failed, at least one needs review |
| `flaky` | All ran and passed, at least one only after a retry |
| `passed` | Every case ran and passed first time, as written |

Only one combination gives `passed`. `flaky` and `needs_review` are counted
separately in `totals` and never added to `passed`.

## 2. Failure categories

Every result that did not pass gets one category, stored in
`test_results.failure_category` and, with the facts it rests on, in
`details.classification` (`"decided_by": "rules"`).

| Category | When | About the application? |
|----------|------|-----------------------|
| `test_assertion_failure` | An assertion did not hold: the application did something other than the case expects | Yes: a finding to look at (the expectation may also be what is wrong) |
| `application_failure` | The application visibly broke while the test ran: an HTTP 5xx answer or an uncaught script error, together with a failed assertion, a missing element or a timeout | Yes |
| `test_automation_failure` | The test could not do its work: locator matched nothing or several elements, test data missing, a step the environment does not allow, an address outside the allowed origins | No |
| `browser_infrastructure_failure` | The browser, the engine or the worker failed; or the engine could not be used, so the case never started | No |
| `network_failure` | A page or request could not be reached | No |
| `authentication_failure` | Signing in with the test account did not succeed | No |
| `inconclusive` | Nothing reliable can be said: not run, blocked, ran out of time with no sign of the application breaking, or needs review | No |

Only an assertion that did not hold and the application visibly breaking count
as evidence about the application. A 4xx answer or a console message does not.
A browser crash with a 5xx in the background is still infrastructure; an
ambiguous locator is still the script's problem.

## 3. What a verdict rests on

Expected results are verified by the engine, in the order of preference the
script format allows:

1. **Approved expectations.** Only approved case versions run. Assertions carry
   their expected values from that version; nothing at run time can alter them.
2. **Playwright assertions** on the page (text, value, visibility, URL, title,
   count, attribute, state), which wait and retry up to the step timeout.
3. **API response validation** (`assert_api_status`, `assert_api_json`).
4. **Business rules** as expressions over values read from the page or an API
   (`assert_expression`, evaluated with exact decimal arithmetic by a small
   safe evaluator, e.g. `points == floor(number(data.bill_amount) / 10)`). The
   rule comes from the approved case; the platform does not assume a formula.
5. **Application state where it is safely reachable**: a read through the
   application's own API with the test session (`api_request` + assertion).
   The platform does not connect to an application's database.

Each result stores `details.verification`: how many assertions were evaluated,
how many held, and of which kind (`ui`, `api`, `business_rule`). A case with no
assertion cannot pass. In a workflow, `verify_test_results` reads the stored
steps back and turns a pass that they do not support into `needs_review`.

## 4. Failure reports

`GET …/executions/{id}/results/{result_id}/failure` returns two things, kept
apart:

**`record`** — what was recorded:

- the test case (key, title, approved expected result);
- the failed step: position, action, locator used, expected, actual, error
  type, the browser's error message, page address;
- console errors, uncaught page errors, failed and blocked requests;
- evidence IDs of the screenshot taken at the failure;
- the execution log up to and including the failed step;
- the category from §2 and the verification summary from §3.

**`ai_suggestion`** — a model's reading, or `null`:

| Field | |
|-------|--|
| `summary` | What happened |
| `possible_root_cause` | A hypothesis, worded as one |
| `impact` | What it would mean if the failure is real |
| `supporting_facts` | Recorded values it relies on |
| `debugging_steps` | Up to five next steps |
| `suggested_test_cases` | Up to three further cases (title and why) |
| `likely_cause`, `confidence` | The model's own classification and how sure it is |
| `label` | "AI-generated suggestion. Not a confirmed root cause: …" |
| `advisory` | Always `true` |
| `category_by_rules`, `agrees_with_rules` | The category from §2, and whether the model's `likely_cause` fits it |

In a workflow the suggestion is written by the `analyse_failures` node for
results that failed or errored. For any execution it can be requested with
`POST …/failure/analysis` (needs `executions:run`; 409 for a result that did
not fail or error; 503 without a configured model).

Safeguards:

- The model is given the facts only (no IDs, no evidence files), inside an
  untrusted-data fence, together with the rules' category. Text from the
  application cannot instruct it.
- Its answer must match a strict schema. It is stored in
  `test_results.ai_analysis` and nowhere else. No code that computes a status,
  category or verdict reads that column.
- A model that writes "this passed" or blames the application for a browser
  crash changes nothing: the status stays, the category stays, and
  `agrees_with_rules: false` shows the disagreement.
- Suggested test cases are text. They are not created as cases.
- If no analysis can be obtained, the result is simply without one.

## 5. Self-healing

Self-healing means one thing here: when a step's locator matches **no
element**, the engine may find the element under a changed name and carry out
the same step on it. Only the locator is replaced. The action, the value typed,
the assertion and its expected value are never changed, so a test whose
expectation no longer holds still fails.

It is off by default. Per execution: `config.healing.mode`

| Mode | Behaviour |
|------|-----------|
| `off` (default) | A missing element is an error |
| `propose` | Look for a replacement and record it; do not use it |
| `recover` | Use the replacement in this run if every rule below holds; otherwise record it |

`config.healing.max_per_case` (1–5, default 3) limits recovered steps per case
attempt. No model is involved.

### The recovery process

1. **Detect.** The step ended with `locator_not_found` in the main phase.
   Ambiguous locators, failed assertions, timeouts, sign-in and cleanup steps
   are not healed. `assert_visible` on a missing element is a failed assertion,
   not a missing locator.
2. **Inspect the page** as it is: the accessibility snapshot of the page, or of
   the `within` region if the locator has one.
3. **Identify alternatives**: named elements whose role fits. For a `role`
   locator the role must be the same; otherwise it must be a role the action
   can be performed on (a `fill` needs a text field, a `check` a checkbox).
   A CSS selector or test ID carries no name to compare with, so it is never
   healed.
4. **Check uniqueness and meaning.** Names are compared ignoring case,
   punctuation and spacing. A replacement is used only if:
   - similarity is at least 0.80 for steps that act (click, fill, check…),
     0.75 for steps that read or assert;
   - no other candidate is within 0.15 of it;
   - the new name does not add a destructive word (delete, pay, send, cancel,
     submit, deactivate, unsubscribe…), is not the opposite of the old one
     (in/out, enable/disable, subscribe/unsubscribe, an `un-`/`dis-` prefix…)
     and is not its negation.
5. **Verify the element**: the replacement (`role` + exact name, inside the
   same `within`) must match exactly one element.
6. **Retry once** with the replacement, only if everything above held. If the
   step still does not pass, the original error stands and the replacement is
   only a proposal.
7. **Record both locators**: the step keeps `healed_from` (the script's
   locator) and `locator` (what was used), and a row in
   `locator_heal_proposals` holds old and new locator, confidence, rationale,
   and why it was held back if it was.
8. **Mark the result.** A case in which every step passed but a locator was
   replaced is `needs_review`, with `healed: true` — not `passed`. A case with
   an unused proposal stays `error` (`test_automation_failure`).

### What happens to a proposal

`GET …/heal-proposals` lists them (`status`: `proposed` — found, not used;
`verified` — used in the run and the step then passed).

- **Accept** (`cases:manage`): writes a new version of the case in which that
  one locator is replaced (`change_reason: healed`). The step must still have
  the same action and the same old locator, otherwise 409. The new version is
  `needs_review` and has to be approved like any edit. Until then executions
  keep using the last approved version.
- **Reject**: nothing changes.

So a replacement locator reaches an approved script only through two human
decisions, and the engine alone never uses one whose name points at a
different or destructive action. Both decisions are audited.

## 6. Flaky tests

`GET …/stability` and `GET …/test-cases/{id}/stability` judge each case from
its last results (`window`, default 20):

A case is flaky if, within the same case version and the same environment,

- a result has status `flaky` (it passed only after a retry), or
- its outcome changed at least twice across at least three runs, or
- a run needed a replacement locator.

Not flaky: always passing, always failing, a regression that stays (passed,
then keeps failing), a fix, fewer than three runs, and differences between
versions or environments.

The kind of instability comes from the non-passing results:

| Kind | From |
|------|------|
| `timing` | Timeouts |
| `unstable_locator` | Missing or ambiguous elements, healed runs |
| `network` | Unreachable pages or requests |
| `environment` | Browser/engine failures, sign-in failures |
| `intermittent` | Assertions that sometimes hold and sometimes do not |

The answer includes run counts, how often the outcome flipped, the share of
runs that did not pass, and the evidence in words. It is a read-only view:
nothing is quarantined, re-labelled or left out of a verdict. A flaky result
stays `flaky` and its execution's verdict stays `flaky`.

## 7. Retry boundaries

| Where | Limit |
|-------|-------|
| Engine attempts per case | `retry.max_attempts` 1–3; a pass on a later attempt is `flaky` |
| Workflow re-runs | At most 2, only for transient errors, never for a failed assertion or after data may have changed |
| Recovery per step | One attempt with one replacement |
| Recovery per case attempt | `healing.max_per_case` |
| Failure analysis | One model call per request; up to 10 results per workflow run |

## 8. Tests

| Subject | Where |
|---------|-------|
| Classification table, verdict precedence, failure record, suggestion labelling, flaky rules | `tests/unit/test_result_quality.py` |
| Locator recovery in a real browser: renamed field and button, ambiguous and opposite names, destructive names, wrong role, no name, limits, cleanup, `within` | `tests/engine/test_healing.py` |
| Categories as stored, failure report and analysis endpoints, a model that claims a pass, proposals (accept, reject, stale, permissions, tenants), stability endpoints | `tests/integration/test_quality.py` |
| Whole path with nothing faked: error → recovered → reviewed → accepted → approved → passes as written; real failures in the right category | `tests/integration/test_engine_real.py` |
| Workflow: healed case ends the run as `needs_review`; report shows categories and replaced locators | `tests/integration/test_workflow.py` |

## 9. Known limitations

- **Never run against a live model.** The analysis prompt has only been
  exercised with a scripted stand-in.
- **By default Playwright matches names loosely.** A locator without
  `exact: true` matches any name containing the words, so `"Subscribe"` also
  matches an "Unsubscribe" button if it is the only one. That is Playwright's
  behaviour and predates healing; scripts for sensitive actions should set
  `exact: true`.
- **Recovery compares names, not behaviour.** The word lists catch common
  destructive and opposite names in English only. That is why a recovered run
  is never a pass and a replacement needs acceptance and approval.
- **Only missing elements are healed.** Changed page structure, moved flows,
  changed test IDs or CSS selectors need a person.
- **Application state is checked only through the application's API.**
- **Flaky detection needs history**: three runs of the same version in the same
  environment. Accepting a replacement starts a new version, and with it a new
  history.
- **The model sees no screenshot**, only the recorded text.
- **No web pages yet** for failure reports, proposals or stability; API only.
- **Analysis on request runs inside the API request** (one model call).
