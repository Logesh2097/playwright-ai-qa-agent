# Evaluation Results

Measured on 2026-11-20 with `make evaluate` (harness and datasets in
`backend/evaluation/`). The command rewrites `backend/evaluation/results/latest.md`
and `latest.json`; this page is the recorded run with commentary.

**What these figures are.** Results of the platform's rule-based components on
small labelled datasets (§2), and one run of the model-based steps with one
Gemini model on 20 instructions (§4). **What they are not.** Field accuracy, or
a statement about any other model. Failure explanation and the full agent
workflow have not been run with a real model.

## 1. Method

- Labels were written from the requirements, before running the code, by the
  same team that wrote the rules. They are not independent field data.
- Cases the code gets wrong stay in the dataset and are listed here.
- The datasets are small (14 to 40 cases). One case moves a figure by 2.5 to 7
  percentage points. Read them as a regression baseline.
- The evaluation is deterministic: the same code gives the same figures.

## 2. Rule-based components (measured)

| Component | Cases | Result | False positives | False negatives |
|---|---:|---|---|---|
| Failure classification | 34 | accuracy 91.2% (31 of 34) | 0 failures blamed on the application wrongly | 1 application failure not attributed to it (9.1%) |
| Self-healing (locator recovery) | 32 | precision 100%, recall 91.7% | 0 unsafe or wrong recoveries | 1 recoverable locator not recovered (8.3%) |
| Sensitive-step detection | 40 | 22 of 24 sensitive steps found (91.7%) | 1 harmless step flagged (6.2%) | 2 missed (8.3%) |
| Test selection for a pull request | 14 | accuracy 100% | — | 0 runs narrowed that should not have been |
| Tool selection (script action → engine or MCP tool, policy) | 15 | accuracy 100% | — | — |

For each component the costly error is different, and that is the one to watch:

| Component | The error that must not happen | Measured |
|---|---|---|
| Failure classification | Calling a test or infrastructure problem an application bug | 0 of 34 |
| Self-healing | Recovering to an element that means something else | 0 of 32 |
| Sensitive-step detection | Missing a destructive step | 2 of 24 — **not zero** |
| Test selection | Skipping tests that the change affects | 0 of 14 |

### Cases the rules got wrong

Failure classification:

| Case | Expected | Got |
|---|---|---|
| Element never appears because the backend is slow (no error recorded) | application failure | test automation failure |
| Page shows "Service unavailable" but the status was 200 | application failure | test assertion failure |
| Login page changed its field names | test automation failure | authentication failure |

Self-healing:

| Case | Expected | Got |
|---|---|---|
| Abbreviation: "Configuration" became "Config" | recovered | held back (names 0.63 alike, 0.80 needed) |

Sensitive-step detection:

| Step | Expected | Got |
|---|---|---|
| "OK" on a delete confirmation | delete | not recognised |
| "Confirm" on a payment | payment | not recognised |
| "Remove filter" | harmless | flagged as delete |

The two missed sensitive steps are the important result. Detection reads the
wording of buttons, links and addresses; a neutral label hides the action from
it. The protections that do not depend on wording remain: a test case can
declare a step sensitive, production environments refuse sensitive steps
outright, and test environments with test accounts are expected.

## 3. What the evaluation changed

The first run found two unsafe locator recoveries, which are exactly the error
self-healing must not make:

| Original | Proposed replacement | Why it is wrong |
|---|---|---|
| "Pay 10.00" | "Pay 100.00" | A different amount |
| "Message Anna" | "Message Hanna" | A different person |

Both were similar enough as strings to pass the threshold. The rule was
tightened: a replacement is refused when any number differs or when a word was
replaced by a different word, however close the spelling. After the change
precision is 100% on the dataset, the two cases are part of it, and they are
also unit tests (`backend/tests/engine/test_healing.py`). The cost is the
missed abbreviation above.

## 4. Model-based components

Measured on 2026-11-20 with **Gemini `gemini-3.5-flash-lite`** (Google's native
API, `LLM_PROVIDER=gemini`, a free-tier key paced at 6 requests per minute), on
20 labelled instructions: clear, vague, missing business rules, prompt
injection, destructive requests, and text that is not a test request. No OpenAI
model has been run.

Three runs, one per version of the analysis prompt. Each is a single run, and
model output varies between runs, so differences of one or two instructions
mean little.

| Metric | Prompt v1 | v2, first draft | **v2, current** |
|---|---|---|---|
| Analysis output valid (of calls answered) | 20 of 20 | 18 of 18 | 20 of 20 |
| Generation output valid (of calls answered) | 4 of 4 | 8 of 10 | 11 of 11 |
| Provider unreachable after every retry | 0 | 3 | 2 |
| Clarification decisions correct | 45% (9 of 20) | 78% (14 of 18) | **90% (18 of 20)** |
| Asked needlessly | 11 | 3 | **2** |
| Did not ask when needed | 0 | 1 | **0** |
| Expected objectives found | 100% | 96% | 100% |
| Values given in the instruction kept | 100% | 100% | 100% |
| Invented rules | 0 of 7 | 0 of 7 | 0 of 7 |
| Instructions with generated cases | 4 | 8 | 11 |
| Cases with a sourced expected result | 11% (1 of 9) | 35% | 50% |
| Instructions with a negative case | 50% | 63% | 55% |
| Prompt injection resisted | 2 of 2 | 2 of 2 | 2 of 2 |
| Latency per call, median / max | 9.3 s / 41 s | 11.5 s / 33 s | 11.8 s / 123 s |
| Calls, tokens in / out | 24, 32k / 17k | 26, 44k / 20k | 31, 54k / 27k |

Cost is not computed: no price is configured.

### What was changed, and why

**Prompt v1 asked too much because it was told to.** It required a blocking
question whenever an expected result was not spelled out, so "test login with
valid and invalid credentials" produced questions about the exact error
message. Version 2 (`requirement_analysis.v2.md`; v1 is kept) says:

- an outcome the instruction's wording fixes ("can", "is rejected", a stated
  limit) counts as stated, described in the instruction's terms and no further;
- a question is asked only when a correct test cannot be written without the
  answer: an unidentifiable feature, an outcome neither stated nor implied, a
  rule, formula or value not supplied;
- details that are observed in the application or chosen later (message
  wording, the next page, labels, test values) are not asked about;
- the rule against inventing results, rules and values is unchanged.

**The first draft of v2 was wrong in two ways**, both visible in the middle
column and both corrected:

- it treated "make sure checkout works" as a stated outcome and stopped asking.
  That was the one failure in the unsafe direction in any run. The prompt now
  says that a bare "works" or "is correct" states no outcome;
- its examples were concrete sentences, and the model copied one of them
  verbatim into an unrelated analysis. The examples are now patterns.

The examples in the prompt were written not to repeat the evaluation's
instructions, so the result is not the model reading back its own exam. The
labels were not changed after seeing any result.

### Found in the platform by these runs

| Found | What was done |
|---|---|
| When Gemini returned an answer that did not fit the schema, its client reported a parser error that the gateway did not recognise. The repair attempt was skipped and the call ended as an unexplained error (2 of 10 generations) | The gateway now treats that as invalid output: one repair attempt, then `llm_invalid_output`, with only field paths passed on and never the model's text. Unit-tested. After the fix, 11 of 11 |
| The evaluation counted "provider unreachable" as invalid output | Reported separately; validity is now of the calls the provider answered |

### What the current figures say

- **The safe direction holds** in the current version: nothing needed was left
  unasked, no rule was invented, every given value was kept, output was always
  valid, and neither injected instruction was followed.
- **The two remaining "needless" questions are both on the injection
  instructions.** Once the injected sentence is set aside, what is left is "Test
  the login page" and "Test password reset", and the model asked what should be
  verified. That is arguably the right call, and the label (do not ask) is
  arguably the one at fault. It is left as labelled.
- **The provider was unreachable for 2 of 31 calls** ("high demand", after three
  attempts each). In the product that is an `llm_unavailable` error the user can
  retry. It is a property of this free-tier model, and worth knowing.
- **Half of the generated cases have no sourced expected result** (the model
  marks them for a person, and they end `inconclusive` rather than `passed`).
  That is the next thing to look at, and the generation prompt has not been
  tuned.
- 20 instructions, one model, single runs: enough to steer by, not enough to
  quote as accuracy.

Also tested without a model: invalid structure is rejected and retried within a
limit, invented rules and out-of-scope addresses are refused by validators, and
injected instructions in model output cannot change a verdict, a permission or
the tool policy.

## 5. Measured elsewhere

| Subject | Where | Result |
|---|---|---|
| MCP tool execution | `make be-test-browser` | 24 pass: real Playwright MCP server and browser — discovery, navigation, snapshots, policy refusals |
| Deterministic execution and assertions | `make engine-test` | 125 pass: every action, locator strategy and assertion in a real browser |
| Demo scenarios ending as labelled | both of the above | 12 of 12, including the three that must not pass |
| False passes | whole suite | A model claiming a pass, an unverified pass, a flaky pass and a healed pass are each tested never to be reported as `passed` |

## 6. Reproducing

```sh
make evaluate                         # rule-based only
# with a provider key and LLM_MODEL_ANALYSIS in .env: also the model-based metrics (uses quota or costs tokens)
```
