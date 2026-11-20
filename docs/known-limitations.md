# Known Limitations and Known Issues

The state of the platform on 2026-11-20, stated plainly. Design-level
limitations and the original open questions are in
[risks-and-limitations.md](risks-and-limitations.md); remaining security risks
are in [security-design.md](security-design.md) §17.

## 1. Not verified

These are the things nobody should assume work, because they have not been run.

| What | State |
|---|---|
| **A live language model, mostly** | Requirement analysis and test-case generation were run with Gemini `gemini-3.5-flash-lite` on 20 instructions ([evaluation-results.md](evaluation-results.md) §4). With the current prompt: output always valid, nothing unsafe, 18 of 20 clarification decisions as labelled; half of the generated cases lack a sourced expected result; the provider was unreachable for 2 of 31 calls. The whole agent workflow ran once with that model on one login instruction: every stage completed, but both generated tests failed because their final check pointed at the wrong element (the read-only inspection never sees the page after login), and the free tier needed up to six attempts per call. Not yet done with a real model: any measurement of script generation or failure explanation, and a run driven through the web application rather than the API. No OpenAI model has been run at all. Everything else about AI steps is tested against a scripted stand-in |
| **GitHub itself** | The GitHub client, webhook verification, status and comment publishing are tested against a stand-in for the GitHub API. No request has been sent to github.com, no real webhook received, no GitHub App installed |
| **The CI workflow** | `.github/workflows/ci.yml` has never run: the project is not yet a git repository. Each of its steps was run locally by hand and passes |
| **A production deployment** | The production stack was rehearsed on one host and passed the full walk-through, then removed. Nothing has been deployed. TLS termination, a real SMTP server, backup and restore, and the GCP layout in [deployment.md](deployment.md) are documented but not rehearsed |
| **SMTP delivery** | The sender is unit-tested with the SMTP connection replaced by a stand-in. No message has been sent through a real mail server |
| **Load** | No load or soak test. Limits (rate limits, concurrent executions, body size) are tested for being enforced, not for where the system breaks |
| **Firefox and WebKit through the whole platform** | The engine's own tests run in all three browsers; platform-level and MCP tests use Chromium |
| **Code coverage** | Backend: 83% line-and-branch coverage of `backend/app`, measured on 2026-11-20 over the unit and integration run only (the engine and real-browser suites run in other containers and are not counted). No target is enforced. Frontend coverage is not measured. Coverage of an *application under test* is never reported by the platform |

## 2. Limitations

### Testing capability

- A `passed` result means the configured assertions held, nothing more.
- Expected results must come from the requirement or a configured rule. When
  neither exists the result is `inconclusive`; the platform does not guess.
- Logins that need a person or a second device (CAPTCHA, SMS or email codes,
  interactive SSO) cannot be automated. Stored password, token, basic-auth and
  TOTP credentials are supported.
- No visual regression, accessibility, load or native-mobile testing.
  Responsive checks are layout assertions at a set viewport.
- No scheduled (cron) runs. Runs start from the web application, the API, a
  pipeline or a pull-request webhook.
- Self-healing repairs locators only, and only when the replacement clearly
  means the same thing. A healed run is `needs_review`, never `passed`.
- Discovery looks at a bounded number of pages per instruction.

### Measured weaknesses of the rule-based parts

From [evaluation-results.md](evaluation-results.md):

- Failure classification is right in 31 of 34 labelled cases. It never blamed
  the application wrongly, but a slow backend that leaves no error behind is
  filed as a test-automation failure.
- Sensitive-step detection found 22 of 24. It reads English wording: a delete
  behind a button called "OK" or a payment behind "Confirm" is missed unless the
  test case declares it. One harmless step ("Remove filter") was flagged.
- Locator recovery refused every unsafe replacement and missed one safe one
  (an abbreviation).

The datasets are small (14 to 40 cases) and were written by the same team as the
rules. Treat the figures as a regression baseline, not as field accuracy.

### Security

- Chromium runs with `--no-sandbox`; the container is the boundary.
- Screenshots, videos and traces are not scanned for sensitive content.
- Prompt injection is contained, not prevented: model output cannot change a
  verdict, a permission or the tool policy, but can produce a misleading
  explanation (always labelled a suggestion).
- No SSO or multi-factor authentication for platform users.
- Rate limits are per installation; a reverse proxy or WAF is assumed in front.

### Operations

- One host. One worker process and one engine per stack; recovery after a
  crash is tested, high availability is not provided.
- Evidence is stored on a volume. Object storage needs a new implementation of
  the existing `EvidenceStorage` interface.
- Upgrades interrupt the API for some seconds and end any execution still
  running after the worker's 60-second grace period (as `error`, re-runnable).
- Logs stay in Docker's rotated files unless a shipper is added. No tracing.
- The background job library (arq) is in maintenance mode upstream.

### Product

- English only.
- Notifications are in-app; email is used only for invitations and password
  resets. No Slack or outgoing webhooks.
- No billing. No import of existing Playwright or Selenium tests.
- Phone-width layouts are usable but were checked only at desktop and tablet widths.

## 3. Known issues

| Issue | Effect | Workaround |
|---|---|---|
| A slow backend that produces no error is classified as `test_automation_failure` | The category points at the test rather than the application | Read the evidence; add an explicit assertion on the response or raise the timeout |
| A page saying "Service unavailable" with status 200 is classified as an assertion failure | Same | Same |
| A login page whose field names changed is classified as `authentication_failure` | Suggests wrong credentials when the locator is stale | Check the screenshot of the login step |
| Sensitive steps behind neutral labels ("OK", "Confirm") are not recognised | They run without the extra approval | Declare the step's kind in the test case; use dedicated test environments |
| "Remove filter" and similar harmless steps are flagged as destructive | An extra approval is asked | Approve it |
| Repeated password-reset requests within an hour are rate-limited (3 per hour) | A fourth request is refused | Wait, or raise `PASSWORD_RESET_LIMIT` in development |
| The production engine image is 4.2 GB | Slow first pull | It carries three browsers; none can be dropped without losing a supported browser |

No test is skipped, expected to fail or disabled to make the suite pass. The
tests reported as "skipped" in a plain `make be-test` are the integration and
browser tests, which run under their own commands.
