# Testing Guide

How to run the platform's own tests and what each suite proves. The reasoning
behind the levels is in [testing-strategy.md](testing-strategy.md).

Everything runs in Docker. Except for the first three rows, the stack must be up
(`make up`).

## 1. The suites

| Command | What runs | Needs | Tests (2026-11-20) | Time |
|---|---|---|---:|---|
| `make be-check` | Ruff, format check, mypy strict, unit tests | nothing | 884 | under 1 min |
| `make fe-check` | ESLint, Prettier, vue-tsc, Vitest | nothing | 90 | under 1 min |
| `make runner-test` | The browser runner's supervisor | runner image | passes | seconds |
| `make be-test-integration` | Unit tests plus the API against real PostgreSQL, Redis and the worker's jobs | stack | 1287 | about 3 min |
| `make engine-test` | Every action, locator strategy and assertion in real browsers; the demo scenarios | stack, demo app | 125 | about 3 min |
| `make be-test-browser` | The platform end to end with the real Playwright MCP server, engine, egress proxy and browser | stack, demo app | 24 | about 2 min |
| `make e2e` | A real browser walks through the web application from registration to a report | stack, demo app | 173 checks | about 2 min |
| `make evaluate` | Rule-based components against labelled data; model-based ones if a model is configured | engine image | see [evaluation-results.md](evaluation-results.md) | seconds |
| `make fe-build` | Production build of the web application | nothing | — | seconds |

Backend coverage: `docker compose run --rm -e RUN_INTEGRATION=1 migrate pytest --cov` (83% of `backend/app` on 2026-11-20, unit and integration run only).

`make check` is `be-check` plus `fe-check`: run it before every commit.

A plain `make be-test` reports the integration and browser tests as skipped;
they are not disabled, they run under the commands above. Integration tests use
a scratch database (`<name>_test`, recreated on every run) and Redis database
15, so development data is untouched.

## 2. What is covered

| Area | Where |
|---|---|
| **Unit**: settings, crypto, URL safety and SSRF rules, permissions, test-case and script validation, business-rule expressions, verdict rules, classification, healing rules, sensitive-step rules, reporting, GitHub selection and signatures, LLM gateway (retries, budget, schema validation), MCP client, browser policy, workflow nodes and edges | `backend/tests/unit/`, `backend/tests/engine/` |
| **API**: every route for authentication, permission by role, tenant isolation, validation, pagination and error shape | `backend/tests/integration/` |
| **Database**: migrations from empty, `alembic check`, row-level security with the application role, constraints | `backend/tests/integration/` (`test_database`, `test_tenant_isolation`, `test_security`) |
| **Agent workflow**: each node, routing, human approval, checkpoints, resume after a stopped worker, retry limits, hostile and malformed model output | `tests/unit/test_workflow.py`, `tests/integration/test_workflow.py`, `test_workflow_real.py` |
| **MCP**: discovery, argument validation, tool permission policy, real navigation and snapshots, refusals | `tests/unit/test_mcp_client.py`, `test_browser_*.py`, `tests/integration/test_browser_real.py` |
| **Playwright**: engine actions, locators, assertions, evidence, timeouts, isolation between runs | `backend/tests/engine/` |
| **Frontend components and pages** | `frontend/src/**/__tests__/` |
| **End to end** | `tests/integration/test_workflow_real.py`, `test_engine_real.py`, `e2e/walkthrough.py` |
| **Security**: SSRF and redirects, tenant sweep over every route, secret-exposure sweep, prompt injection, unauthorized actions, oversized requests | `tests/unit/test_url_safety.py`, `test_egress_proxy.py`, `test_safeguards.py`, `tests/integration/test_security.py` |
| **Failure recovery**: dead worker, job delivered twice, engine or runner unavailable, cleanup after a run | `tests/integration/test_security.py`, `test_workflow.py`, `test_engine_real.py` |

## 3. The demonstration scenarios

`backend/tests/demo_scenarios.py` holds twelve scripts against the demo
application (`fixtures/demo-app/`). Each states the verdict and failure category
it must end with, including scenarios that must **not** pass:

| Scenario | What it checks | Must end as |
|---|---|---|
| `successful_login` | The right password opens the account page | passed |
| `failed_login` | A wrong password is refused with a message | passed |
| `customer_registration` | A customer with valid details can register | passed |
| `form_validation` | Registration lists every problem and keeps what was typed | passed |
| `product_creation` | A new product appears in the catalogue and in its API | passed |
| `invalid_input` | A negative price and no stock is refused and not listed | passed |
| `business_rule` | Loyalty points are one per 10 spent, rounded down (a rule given by the test, not assumed) | passed |
| `business_rule_defect` | The same rule on a build with a seeded rounding defect | failed — test assertion failure |
| `api_response_validation` | The points API has the documented shape and value; a broken endpoint is caught | failed — application failure |
| `responsive_layout` | On a narrow window the menu button replaces the navigation | passed |
| `responsive_layout_wide` | On a wide window the navigation is shown | passed |
| `browser_navigation` | Links lead between pages and back; a link that is not there | error — test automation failure |

They run twice: directly in the engine (`make engine-test`) and through the
whole platform — API, worker, engine, stored results, report —
(`make be-test-browser`). A scenario that passes when it should fail fails the suite.

## 4. The complete workflow test

`test_the_complete_platform_workflow_in_fifteen_steps`
(`tests/integration/test_workflow_real.py`, part of `make be-test-browser`)
drives one request through everything with real services and a real browser.
Only the language model is a stand-in.

1. A user registers and signs in.
2. Creates a project.
3. Registers the application under test.
4. Adds an environment with its allowed addresses.
5. Submits a plain-language instruction.
6. The workflow analyses the requirement.
7. Test cases and executable scripts are generated and validated.
8. The run waits for a person; the user reviews and approves.
9. The approved cases are executed by the engine in a real browser.
10. The application is inspected through Playwright MCP, read-only (at least four MCP calls are recorded).
11. The verdict rests on a deterministic assertion, with the expected and the found value recorded. One case passes; one fails because the script expects a message the application does not show.
12. Evidence is captured, served to the owner, 404 for another organization and 401 anonymously.
13. The failure is classified by rule; the AI explanation is marked advisory ("Not a confirmed root cause").
14. The report is built from the stored results and exported as PDF, CSV and JSON.
15. The run appears in the execution history, the case's history and the dashboard.

`make e2e` covers the same path from the outside, through the web application.

## 5. Testing with a real model

Without a model key (`OPENAI_API_KEY` or `GEMINI_API_KEY`) every AI step is tested with scripted model output,
which proves the handling of good and bad output but says nothing about the
quality of a real model's answers. With a key and model names in `.env`:

```sh
make evaluate
```

runs the 20 labelled instructions through the real analysis and generation
steps and reports the model-based metrics. This uses quota or costs tokens. It
was run once, with Gemini; see [evaluation-results.md](evaluation-results.md) §4.

## 6. Continuous integration

`.github/workflows/ci.yml` runs on pull requests and on `main`:

| Job | Steps |
|---|---|
| `backend` | Ruff, format check, mypy, migrations and `alembic check`, unit and integration tests with coverage measurement |
| `browser` | runner tests, engine tests and real-browser tests |
| `frontend` | ESLint, Prettier, vue-tsc, Vitest, build, `npm audit` |
| `end-to-end` | `make e2e`, `make evaluate` |
| `security` | `pip-audit`, `npm audit`, gitleaks |
| `images` | compose files valid; production compose refuses empty secrets; production images build |

There is no deployment job, on purpose. The workflow has not run on GitHub yet;
every step was run locally.

## 7. Adding tests

- A new route needs nothing to be covered by the tenant, anonymous-access and
  viewer sweeps: they enumerate the application's routes. It does need its own
  tests for behaviour and validation.
- A new script action or assertion needs a unit test for validation and an
  engine test against the demo application.
- A rule change in classification, healing or sensitive-step detection should
  come with new labelled cases in `backend/evaluation/datasets.py`. Labels are
  written from the requirement, not from what the code returns, and cases the
  code gets wrong stay in the dataset.
- Never weaken an expected result to make a test pass.
