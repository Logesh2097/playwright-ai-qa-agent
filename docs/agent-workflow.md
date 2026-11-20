# Agent Workflow (LangGraph)

The workflow that takes a plain-language instruction to approved test cases,
runs them, checks the results and writes a report (Phase 8). It is a LangGraph
`StateGraph` (LangGraph 1.2) that runs in the worker.

It is a workflow with a fixed shape, not a free-running agent:

- **Code decides what happens next.** Every edge is a function of the state.
- **A model is asked for content at four points**, each time for structured output against a strict schema: the requirement analysis, the test cases, the executable scripts, and explanations of failures. It is never given a tool, never chooses a node, and never decides whether a test passed.
- **Every cycle is bounded** by a counter: clarification rounds, script regeneration, retries.
- **A person approves** before anything generated is run.

Code: `backend/app/workflow/`. Tests: `tests/unit/test_workflow.py`,
`tests/integration/test_workflow.py`, `tests/integration/test_workflow_real.py`.

## 1. How a run moves

```
POST …/workflows ─► API stores the instruction and a workflow run (queued) ─► enqueue
                                                                               │
worker: WorkflowRunner.run ─► graph.ainvoke(initial state, thread = run id)    │
        … nodes run, a checkpoint is saved after each …                        │
        approval_checkpoint ─► interrupt ─► run is "waiting_for_approval", job ends
                                                                               │
POST …/workflows/{id}/approval ─► API checks who may decide, records it ─► enqueue
                                                                               │
worker: graph.ainvoke(Command(resume = the decision)) ─► … ─► save_execution_results
```

- The API never runs the graph. It records what a person asked for or decided and enqueues a job.
- One graph thread per run; the thread ID is the workflow run's ID.
- One worker per run at a time, by a Redis lock that the worker renews while it works.
- Nodes store real data (analysis, plan, cases, results, evidence) in its own tables. The state holds IDs and short summaries.

## 2. Modes

| Mode | Started with | Path |
|------|--------------|------|
| `generate_and_run` | `text` (an instruction) | All fifteen nodes |
| `run_existing` | `suite_id` (an approved suite) | `receive_instruction` → `validate_application` → `approval_checkpoint` → execution nodes. No model is needed unless failures are to be explained |

## 3. The graph

```
START
  ▼
receive_instruction
  ├─ existing suite ───────────────────────────────────────────┐
  ▼                                                            │
analyse_requirement ◄──────── request_clarification (waits)    │
  │        └─ requirement incomplete ──────────▲               │
  ▼ requirement complete                                       ▼
validate_application ─────────────────────────────────► (existing suite) ─┐
  ▼                                                                       │
generate_test_plan                                                        │
  ▼                                                                       │
generate_test_cases ◄──┐                                                  │
  ▼                    │ test cases invalid (scripts), within the limit   │
validate_test_cases ───┘                                                  │
  ▼ valid, or limit reached                                               │
approval_checkpoint ◄─────────────────────────────────────────────────────┘
  │   approval required: waits.   rejected ──► generate_report
  ▼   approved, or not required
prepare_execution
  ▼
execute_test_cases ◄─────────────────┐
  ▼                                  │
verify_test_results                  │
  ├─ everything passed ──► collect_evidence
  ▼ something did not pass           │
analyse_failures                     │
  ▼                                  │
retry_eligible_tests ── retry allowed┘
  ▼ retry not allowed
collect_evidence ──► generate_report ──► save_execution_results ──► END
```

A node that fails, a rejection and a cancellation all lead to
`generate_report`, then `save_execution_results`. Every run therefore ends with
a report and a saved outcome, whatever happened.

## 4. Nodes

| # | Node | Uses a model | What it does |
|---|------|:---:|--------------|
| 1 | `receive_instruction` | | Checks that the person may run this and that the environment, and the instruction or suite, exist in the project. |
| 2 | `analyse_requirement` | ✔ | Has the requirement analysed (the Phase 4 analysis and its readiness checks) and summarises where that leaves it. |
| – | `request_clarification` | | Waits for a person to answer the open questions. Fails the run after three rounds, or when the requirement is still incomplete with nothing left to ask. |
| 3 | `validate_application` | | Checks the environment is usable. In `generate_and_run`, looks at the application read-only through Playwright MCP and keeps an outline of its pages (section 7). |
| 4 | `generate_test_plan` | | Derives the plan from the analysed requirement, with the tool selection. The plan awaits approval together with the cases. |
| 5 | `generate_test_cases` | ✔ | Writes the cases once (Phase 5), then the executable script of each case that needs one. On a repeat, only scripts that failed the checks are written again, with the reasons. |
| 6 | `validate_test_cases` | | Runs the deterministic checks on every case and script. |
| 7 | `approval_checkpoint` | | Works out whether approval is needed and why; waits; then acts on the decision as the person who decided. |
| 8 | `prepare_execution` | | Creates the execution: which cases, which approved versions, which configuration. |
| 9 | `execute_test_cases` | | Runs the execution with the deterministic engine (Phase 7). |
| 10 | `verify_test_results` | | Reads back what was stored and checks it supports each status (a pass the recorded steps do not support becomes `needs_review`); decides which failures could be retried. |
| 11 | `analyse_failures` | ✔ | Asks for a summary, a possible root cause, the impact, debugging steps and further test cases for what did not pass. Stored with the result as a labelled suggestion next to the category the rules gave; changes no status ([result-validation-and-healing.md](result-validation-and-healing.md) §4). |
| 12 | `retry_eligible_tests` | | Starts another attempt for cases whose failure was transient, within the limit. |
| 13 | `collect_evidence` | | Gathers references to the evidence of every attempt. |
| 14 | `generate_report` | | Builds the report from stored facts and keeps it with the run. |
| 15 | `save_execution_results` | | Closes the run: status, verdict, instruction status, audit entry; closes any execution left open. |

Nodes call the same services as the API does (`InstructionService`,
`GenerationRunner`, `ExecutionService`, `ExecutionRunner`, …) and act **as a
person**: the one who started the run, or the one who decided an approval. That
person's access is loaded again for every node, so someone removed from the
organization or the project can no longer act through a run that is still going.

## 5. State

`app/workflow/state.py`: a `TypedDict` whose nested parts are typed too.

| Asked for | Field | Holds |
|-----------|-------|-------|
| Execution ID | `execution_id` | The current execution; all of them are in `attempts` |
| User, organization, project, application, environment IDs | `user_id`, `organization_id`, `project_id`, `application_id`, `environment_id` | |
| User instruction | `user_instruction` | The text, shortened |
| Requirement analysis | `requirement_analysis` | Status, readiness, counts, blocking issue codes. The analysis itself is on the instruction |
| Test plan | `test_plan` | Plan ID, version, tool selection |
| Generated test cases | `generated_test_cases` | Per case: ID, key, title, version, ready, has a script, tool, blocking issue codes |
| Approved test cases | `approved_test_cases`, `excluded_test_cases` | Case and approved version IDs; what was left out and why |
| Browser session reference | `browser_session` | The engine run's ID, browser and viewport. A reference, never a token |
| MCP tool results | `mcp_tool_results` | Per call: tool, status, page address, error code |
| Playwright execution results | `execution_results` | Per case: result ID, status, failure class and summary |
| Validation results | `validation_results` | Execution status, verdict, totals, results whose steps did not support their status |
| Failure details | `failure_details` | Per failure: class, error type, whether a retry is allowed and why |
| Retry count | `retry_count`, `attempts` | Every attempt: its execution, verdict, what it re-ran |
| Evidence references | `evidence_refs` | Evidence IDs and kinds |
| Final report | `final_report` | Report ID, status, verdict. The report is on the run |
| Execution status | `status`, `errors` | `running`, `failed`, `rejected`, `cancelled`, `completed`; errors by node |

Also: `run_id`, `mode`, `options`, `instruction_id`, `suite_id`,
`clarification_rounds`, `regeneration_count`, `script_problems`, `approval`,
`application`.

**No secrets.** The state is serialized into checkpoints, so anything in it is
stored at rest. Credentials are decrypted only inside `ExecutionRunner`, for the
engine, and never enter the state. Page content, scripts and screenshots are not
in it either. Tests read the stored checkpoints and assert that the test
account's password and the run's proxy secret are absent.

## 6. Conditional edges

All in `app/workflow/routing.py`, as pure functions.

| Decision | Function | Outcomes |
|----------|----------|----------|
| Requirement complete / incomplete | `after_analysis` | complete → `validate_application`; questions open → `request_clarification`; blocking issues questions cannot resolve → report |
| Test cases valid / invalid | `after_case_validation` | scripts fail the checks and the limit is not reached → `generate_test_cases`; otherwise → `approval_checkpoint` |
| Approval required / not required | `after_approval` | not required, or approved → `prepare_execution`; rejected, or nothing usable → report |
| Browser execution successful / failed, and test passed / failed | `after_verification` | everything passed → `collect_evidence`; anything else → `analyse_failures` |
| Retry allowed / not allowed | `after_retry_decision` | a new attempt was prepared → `execute_test_cases`; otherwise → `collect_evidence` |
| Report generation successful / failed | `after_report` | results are saved either way; a failed report is recorded on the run |
| Existing suite or instruction | `after_receive`, `after_application` | skips analysis and generation for a suite |
| Stopped | every router | a failed, rejected or cancelled run goes to the report |

## 7. Tool selection

`app/workflow/tools.py`. A fixed policy; no model chooses.

| Work | Tool | Why |
|------|------|-----|
| Looking at the application before tests exist | Playwright MCP, read-only tools (`browser_navigate`, `browser_snapshot`) | Exploratory, nothing to assert yet |
| Running approved, repeatable test cases | The deterministic Playwright engine | Repeatable, strict, produces the verdict |
| Backend and API checks | The engine's API steps; a case whose script only uses them is marked `api_engine` | No page needed |
| Application discovery | The same read-only MCP session | It cannot click, type or submit |

**Discovery** (`app/workflow/discovery.py`) opens the environment's address, the
login pages named in credential recipes, and then same-site links found on those
pages, up to six pages. Which pages are opened is decided by code in a fixed
order. Links that look like they change something (sign out, delete, remove,
cancel) are not followed. What is kept is an outline: each page's address and
title and the roles and names of its elements. That outline is what the model is
shown when it writes scripts, fenced as untrusted data.

No tool is handed to a model. The adapter that could offer MCP tools to a model
(`app/agent/tools/browser.py`, Phase 6) is not used by this workflow: a bounded,
code-driven look at the application was enough, and it leaves nothing for page
content to steer.

If the browser service is unavailable, the run continues without an outline and
says so in the report. If the application's own address cannot be opened, the
run fails before anything is generated.

## 8. Human in the loop

Two kinds of wait, both LangGraph interrupts. The run's status says which, and
`waiting_for` says what is needed.

**Clarification** (`waiting_for_clarification`): the open questions. Answered
with `POST …/workflows/{id}/clarifications`.

**Approval** (`waiting_for_approval`), with the reasons:

| Reason | When | Who may decide |
|--------|------|----------------|
| `generated_test_cases` | Always, when cases were generated | QA engineer or admin |
| `state_changing_operations` | Scripts click, type, select, upload, send non-GET API requests or sign in, on an environment that requires approval | Organization admin |
| `destructive_cleanup` | A script's cleanup steps change data | QA engineer or admin |
| `outside_default_scope` | A case goes beyond the requirement's objectives | QA engineer or admin |

Production environments always need an admin. An approved suite on an
environment that does not require approval needs none of this and runs straight
through.

On approval, the node approves each case **as the person who decided**, with the
normal checks: a case is approved only if it is ready and has a script. Cases
that are not are left out and listed in the report; the rest run. People can
edit cases and scripts through the ordinary endpoints while the run waits.
On rejection the run ends, the instruction and plan are marked rejected, and
nothing is executed. Approval requests expire after 72 hours.

Resuming: the decision is recorded by the API, which enqueues a job; the worker
continues the graph from its checkpoint with the decision as the resume value.

## 9. Retries

Decided in `app/workflow/retry.py` from recorded facts, never by a model.

| Outcome of a case | Retried? |
|-------------------|----------|
| The browser or engine failed (`internal`), a network request failed, navigation failed with a network error | Yes |
| The engine could not be used at all (unavailable, busy, runner unavailable), so the case never started | Yes |
| An assertion did not hold | **No.** That is a finding |
| Element not found or ambiguous, timeout, test data missing, address not allowed, sign-in failed | No: running it again gives the same answer |
| Any of the above, when the first attempt may already have changed data | **No** |

"May already have changed data" means a step that can change state had passed
before the failure; for a case interrupted by a worker stopping, it means the
script contains such a step at all.

Retries are bounded by `options.max_retries` (0 to 2, default 1). Each retry is
a new execution containing only the eligible cases, so every attempt is on
record with its own steps and evidence; the run's `attempts` list them. A case
that passes only on a retry is **flaky**, never passed. The engine has its own,
separate retry setting inside one execution (Phase 7).

## 10. Persistence and recovery

| What | Where |
|------|-------|
| Workflow execution state | `workflow_checkpoints`, `workflow_checkpoint_writes`: LangGraph checkpoints, one after every node |
| The run: status, current node, what it waits for, executions, retries, verdict, error, report | `workflow_runs` |
| Node status and execution history | `agent_execution_logs`: `node_start`, `node_end` with duration and outcome, `interrupt`, `resume`, `error` |
| Retry attempts | One `test_executions` row per attempt, linked to the run; `attempts` in the state and the report |
| Approval status | `approvals`: reasons, who decided, when, note |
| Failure information | `test_results`, `test_execution_steps`, `workflow_runs.error` |

The checkpointer (`app/workflow/checkpoint.py`) is written on the application's
own database layer rather than taken from `langgraph-checkpoint-postgres`. That
package needs a second database driver, and its tables would sit outside
row-level security. Here checkpoints are tenant data like any other.

**Recovery.** A worker lists the runs it is working on in Redis and holds a lock
on each, renewed every ten seconds. If the worker dies, the lock expires within
thirty seconds and the run is still listed. Twice a minute every worker looks
for such runs and continues each from its last checkpoint
(`recover_interrupted`). A person can also do this with
`POST …/workflows/{id}/recover`. A run that keeps being interrupted is failed
after three recoveries.

What "where safe" means:

- Nodes that had finished are not run again; their results are in the checkpoint.
- The node that was running starts over. Nodes are written so that this is harmless: an analysis or generation in progress is picked up by its token, an existing plan, approval or execution for the same attempt is found and reused.
- **An execution that was running is not run again.** It is closed as interrupted. Its cases that had not started are retried (within the limit); a case that was in progress is retried only if its script cannot change data.

Verified by killing the worker container in the middle of an execution: the run
was continued automatically, the case in progress was reported as an error and
not repeated, the case that had not started was run and marked flaky.

## 11. Observability

| Asked for | Recorded as |
|-----------|-------------|
| Node execution time | `node_end` rows with `latency_ms` and outcome |
| LLM calls, token usage | `llm_call` rows: model, prompt version, tokens in and out, cost, status |
| MCP calls | `tool_call` rows: tool, sanitized arguments, status, duration |
| Browser actions | `test_execution_steps` |
| Retry counts | `workflow_runs.retry_count`; a row when a retry starts or the limit is reached |
| Execution status | `workflow_runs.status`, `current_node`, `heartbeat_at` |
| Workflow errors | `error` rows; `errors` in the state; `workflow_runs.error` |

`GET …/workflows/{id}` returns the totals (`usage`) and the ordered `history`.
The report repeats both.

## 12. Failure handling

| Situation | What happens |
|-----------|--------------|
| A node raises | Recorded; the run goes to the report and ends as `failed` |
| The model is unavailable during analysis or case generation | The run fails with the provider's error code |
| The model is unavailable for scripts | Cases without a script are left out; if none is usable the run ends without a pass |
| The model is unavailable for failure explanations | The explanation is absent. Nothing else changes |
| The model's script does not fit the engine's schema | Not stored, not repaired; the reason goes back to the model if another round is allowed |
| Browser service unavailable during discovery | The run continues without an outline |
| Application cannot be opened | The run fails before generation |
| Engine unavailable | The attempt fails; its cases are retried within the limit |
| Cancellation | The run stops at the next node, the engine is told to stop, the report is still written. A waiting run is cancelled at once |
| Worker stops | Section 10 |
| The person a step acts for has lost access | The run fails with `actor_not_permitted` |
| Report cannot be generated | Results and verdict are saved anyway; the run records `report_failed` |

## 13. What a model may and may not do

| May | May not |
|-----|---------|
| Interpret the requirement and ask questions | Choose or change the application or environment |
| Write test cases | Invent an expected result (schema, checks and prompt all forbid it) |
| Write scripts for reviewed cases | Use a step type, selector or address the engine's schema does not allow |
| Explain failures | Change a status, a verdict or an assertion; cause a retry |
| | Call a tool, open an address, or see a credential, token or screenshot |
| | Approve anything |

Everything a model is shown that came from the application or from a user is
fenced as untrusted data, and what it returns is validated by code before it is
used. These are mitigations. The controls are that its output is only content,
that a person approves that content, and that results come from the engine.

## 14. API

All under `/api/v1/organizations/{org}/projects/{project}/workflows`.

| Method | Path | Who |
|--------|------|-----|
| POST | `` | Start a run. Developers and above. 202 |
| GET | ``, `/{id}` | Members |
| POST | `/{id}/clarifications` | Developers and above |
| POST | `/{id}/approval` | QA engineers and admins; admins only where section 8 says so |
| POST | `/{id}/cancel`, `/{id}/recover` | Developers and above |
| GET | `/{id}/report` | Members |

Details: [api-design.md](api-design.md) §15.

## 15. Not built

- **A model choosing browser tools.** Exploration is the bounded, code-driven discovery of section 7.
- **Web pages** for runs, approvals and reports.
- **Live progress** to clients (events over SSE); the run is polled.
- **A report narrative**, and HTML or JUnit output. The report is JSON.
- **Budgets** per run beyond the existing monthly cap and per-instruction limit.
- **Approval expiry as a job**: an expired approval is refused when someone tries to decide it; nothing cancels the run by itself.
- **Self-healing of locators.**

## 16. Known limitations

- **No real model has run this workflow.** The prompts for scripts and failure explanations, like the earlier ones, have been checked against a stand-in endpoint and the strict-schema rules, not against a provider.
- **Scripts are grounded in an outline taken without signing in.** Pages behind the login are not seen, so their elements are written from the case's wording and flagged as unresolved.
- **A retried case that passes is flaky even when the first failure was purely the platform's.** That is deliberate, and conservative.
- **Recovery takes up to about a minute** (lock expiry plus the next check).
- **Checkpoints are kept for ever.** Nothing prunes them yet.
- **One approval covers the whole run.** Cases cannot be approved one by one within a run, though they can be edited before the decision.
