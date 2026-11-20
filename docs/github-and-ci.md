# GitHub and CI/CD Integration

How a team runs approved tests from its development workflow: from a pipeline
with an API key, or automatically when a pull request changes (Phase 11).

Three rules shape everything here:

- **Only approved tests run, in a configured place.** An event or a pipeline
  never brings its own tests, address or code. It can only ask for approved
  suites and cases to run against an environment the project has set up.
- **Only a verified pass is success.** The outcome given to a pipeline or to
  GitHub is derived from the execution's verdict, which is derived from the
  tests' own assertions. No model takes part.
- **Nothing happens, and nothing is written to GitHub, unless a person
  switched it on** for that repository.

## 1. Connecting to GitHub

An organization admin stores a **connection**: credentials for GitHub's REST
API, using one of the two mechanisms GitHub supports for integrations.

| Mechanism | What is stored | Reach |
|-----------|----------------|-------|
| Fine-grained personal access token | The token | The repositories and permissions its owner chose |
| GitHub App installation | App ID, installation ID, the app's private key | The repositories the app is installed on. The platform signs a short-lived JWT and exchanges it for an installation token that lasts an hour |

Permissions to grant, and no more:

| Permission | For | Needed |
|------------|-----|--------|
| Metadata: read | Everything (GitHub requires it) | Always |
| Pull requests: read | Pull request details and changed files | Always |
| Contents: read | Commits, branches, comparing commits | Always |
| Commit statuses: read and write | Reporting results as commit statuses | Only if statuses are switched on |
| Pull requests: read and write | The pull request comment | Only if comments are switched on |

Creating a connection makes one call to GitHub to prove the credentials work.
They are stored encrypted with the same envelope encryption as test
credentials, are never returned by any endpoint, and are sent only in the
`Authorization` header of requests to the configured GitHub API address
(`GITHUB_API_URL`, fixed by the installation; for GitHub Enterprise Server set
it to that server's API address). Redirects are not followed.

## 2. Linking a repository

A project links repositories one at a time (`POST …/github/repositories` with a
connection and `owner/name`). Linking:

- asks GitHub for the repository with the connection's credentials, which
  proves they can see it, and records GitHub's permanent numeric ID;
- refuses archived repositories and a second link of the same repository;
- generates a webhook secret and a webhook address, and shows the secret once.

**Nothing is addressed on GitHub that is not linked.** Every later call uses a
linked repository's stored name and checks that GitHub still reports the same
numeric ID for it, so a name that was re-used by another repository is refused.

Repository selection (`GET …/github/connections/{id}/repositories`), metadata
and branches (`…/metadata`), pull request information with changed files
(`…/pull-requests/{number}`) and commit information (`…/commits/{sha}`) are all
read live from GitHub for linked repositories.

### What a link allows

Everything is off after linking. An admin sets:

| Setting | Meaning |
|---------|---------|
| `on_pull_request` | Start tests when a pull request is opened, reopened, marked ready, or gets new commits |
| `on_push` | Start tests when commits are pushed to a configured branch |
| `branches` | Branch names or patterns (`release/*`). For pull requests: the branch being merged into |
| `environment_id` | Where the tests run. Must belong to the project |
| `suite_ids` | The approved suites of a full run. Must belong to the project |
| `selection_mode`, `path_rules` | Whether and how a run may be narrowed (§5) |
| `status_enabled` | Write a commit status per suite |
| `comment_enabled` | Write one comment on the pull request |
| `enabled` | Master switch for the link |

A trigger cannot be switched on without an environment, at least one suite and
at least one branch.

## 3. Webhooks

On GitHub, add a webhook to the repository with the address and secret from
the link, content type `application/json`, events **Pull requests** and
(optionally) **Pushes**.

`POST /api/v1/webhooks/github/{organization_id}/{hook_token}`:

1. Limits: 300 deliveries a minute per link; 1 MB per payload
   (`GITHUB_WEBHOOK_MAX_BYTES`).
2. **Signature.** `X-Hub-Signature-256` must be the HMAC-SHA-256 of the exact
   bytes received, with that link's secret, compared in constant time. The
   older SHA-1 header is not accepted. An unknown address and a wrong signature
   get the same 401, and nothing is stored for either.
3. Headers: a well-formed `X-GitHub-Delivery` and `X-GitHub-Event`.
4. The payload's repository ID must be the linked repository's.
5. **Duplicates.** The delivery ID is stored once per link. A redelivery
   answers `duplicate` and does nothing.
6. The link's settings decide (§2). Anything not allowed is recorded as
   `ignored` with the reason: other actions (closed, labeled…), other branches,
   drafts, closed pull requests, triggers that are off.
7. An accepted delivery is queued and answered with 202.

Only what the event was about is stored (pull request number, commit, branch
names, author). The payload is not kept.

Processing, in the worker:

- asks GitHub for the pull request. If GitHub says it belongs to another
  repository, nothing runs. If its head has moved on, this event is dropped;
  the newer commit's own event is tested;
- reads the changed files and chooses the suites (§5);
- starts one execution per suite, as the system (no user), with trigger
  `github_webhook`. The key `repository link + commit + suite` makes this
  **idempotent**: a second event for the same commit (for example *reopened*)
  finds the executions that already exist;
- on an environment that requires approval, a suite that changes data or signs
  in is not started: nothing automated can give that approval;
- if statuses are on, marks each suite `pending` on the commit.

Pull requests from forks are tested like any other. The tests are the
project's own approved tests against the configured environment; no code or
configuration from the pull request is executed by the platform.

## 4. CI/CD

### API keys

An organization admin creates keys (`POST /organizations/{id}/api-keys`). A key

- is shown once; only its SHA-256 hash is stored;
- has scopes: `executions:write` to start, `executions:read` to read results;
- can be limited to one project, expires (90 days by default, at most 366) and
  can be revoked at once;
- is not a person: it cannot approve, manage or read anything else, and it does
  not work on any endpoint outside `/ci`.

Requests are limited per key (`CI_RATE_LIMIT`, 120 a minute).

### Endpoints

| Method | Path | Scope | |
|--------|------|-------|--|
| POST | `/api/v1/ci/executions` | write | Start approved tests: `project_id`, `environment_id`, `suite_id` or `case_ids`, optional `config` and `git` (repository, sha, ref, pull request, run URL: recorded, never fetched). `Idempotency-Key` header: a repeat with the same value returns the same execution (200) |
| GET | `/api/v1/ci/executions/{id}` | read | Status and verified outcome, for polling |
| GET | `/api/v1/ci/executions/{id}/junit` | read | Results as JUnit XML |
| GET | `/api/v1/ci/executions/{id}/report` | read | The full report as JSON |

The status answer:

```json
{ "id": "…", "status": "completed", "verdict": "failed", "finished": true,
  "outcome": "failed", "passed": false, "exit_code": 1,
  "summary": "1 of 12 test(s) failed.",
  "totals": { "total": 12, "passed": 11, "failed": 1, "…": 0 },
  "failures": [ { "case_key": "TC-0042", "title": "…", "status": "failed",
                  "failure_category": "test_assertion_failure", "summary": "…" } ],
  "report_url": "https://qa.example.com/projects/…/executions/…" }
```

### Outcome and exit code

| Outcome | Exit | When |
|---------|------|------|
| `passed` | 0 | Every test ran and passed first time, as written |
| `failed` | 1 | At least one assertion did not hold |
| `unverified` | 2 | Nothing failed, but it is not a verified pass: an error, tests that did not run, a pass on retry (flaky), a result that needs review |
| `incomplete` | 3 | The execution itself did not finish (cancelled, timed out, engine unavailable) |
| `running` | — | Not finished |

In JUnit, flaky and needs-review results are written as failures with their
own type, because JUnit has no such states and a reader would count them as
passes.

### Command-line script

`backend/ci/qa_agent_ci.py` is one file with no dependencies beyond Python 3.9.
It starts the run, waits, writes JUnit and JSON if asked, adds a summary to the
GitHub Actions job page, and exits with the code above. Exit code 4 means the
run could not be started or followed (configuration, network, time limit).

```
QA_AGENT_URL=https://qa.example.com QA_AGENT_API_KEY=… \
python qa_agent_ci.py --project <id> --environment <id> --suite <id> --junit results.xml
```

- The key is read from the environment only, so it is not in process lists.
- The address must be `https` (`--allow-insecure` exists for local testing).
- An answer that does not add up is never treated as success.
- `--allow-flaky` makes a pass on retry exit 0. Nothing else can be relaxed.

A GitHub Actions workflow is in
[examples/github-actions-qa.yml](examples/github-actions-qa.yml). It needs only
`contents: read`, keeps the key in a repository secret and skips pull requests
from forks, which do not receive secrets.

### Webhook or pipeline?

| | Webhook | Pipeline with an API key |
|--|---------|--------------------------|
| Set up in | The platform and the repository's webhook settings | The repository's workflow file |
| Runs when | The event arrives | The pipeline reaches that step, e.g. after deploying the change |
| Result reaches GitHub as | Commit status and comment (if on) | The job's own pass or fail |
| Test selection | By path rules | Chosen by the pipeline |

Use the pipeline when the change has to be deployed to the test environment
first. The webhook tests whatever is on the configured environment when the
event arrives.

## 5. Test selection

Deterministic, by rules a person wrote; no model is involved.

A link has the suites of a full run and, optionally, **path rules**:
`{"pattern": "src/billing/**", "suite_ids": [...]}`. In `**` crosses
directories, `*` and `?` do not, a trailing `/` means everything below, and a
bare name pattern such as `*.md` matches at any depth.

With `selection_mode: affected`, a run is narrowed to the suites its rules
name **only if all of this holds**:

- GitHub's list of changed files was read and is complete (it lists at most
  3000 files for a pull request; a comparison of pushed commits at most 300);
- every changed file is covered by at least one rule;
- the rules name suites of this link's full set.

Otherwise the full set runs, and the reason is recorded: a file no rule
covers, an incomplete list, GitHub not answering, no rules, an empty change.
Selection never returns nothing.

`GET …/pull-requests/{number}` shows the selection for a pull request and
`POST …/impact` for any list of files, so rules can be tried before they are
relied on. The choice made for a run is stored with the execution
(`git_context.selection`) and shown in the pull request comment.

## 6. Reporting to GitHub

Only for executions a webhook started, and only what the link has switched on.

**Commit status** (`status_enabled`), one per suite, context `QA Agent / <suite>`:

| State | When |
|-------|------|
| `pending` | Started |
| `success` | Outcome `passed` |
| `failure` | Outcome `failed` or `unverified` (including flaky and needs review) |
| `error` | Outcome `incomplete` |

The status links to the report in the web application.

**Pull request comment** (`comment_enabled`): one comment per pull request,
rewritten as suites finish and for each new commit. It contains

- the headline (the worst outcome) and the commit;
- per suite: outcome, total, passed, failed, error, skipped, and a link to the
  report;
- which suites ran and why (§5);
- failed tests (up to ten): case, category by rules, failed step, expected,
  found, error;
- suggested debugging steps: the AI suggestion's steps where one exists,
  labelled "AI-generated suggestion, not a confirmed root cause"; otherwise
  general next steps for the category;
- flaky and needs-review results under "Not counted as passed".

**Evidence is never attached or linked directly.** The comment links to the
report page, which requires signing in with access to the project.

Text that came from a test run is escaped or put in code spans, and a
zero-width space follows every `@`, so it cannot become a link, an image, HTML
or a mention that notifies someone.

If GitHub refuses or is unavailable, the failure is recorded with the
execution (`git_context.published`) and logged without credentials. The
execution's result is unaffected.

## 7. Security summary

| Concern | Measure |
|---------|---------|
| Forged webhooks | HMAC-SHA-256 over the raw body, constant-time comparison, per-link secret, rotation |
| Replayed or repeated deliveries | Delivery ID stored once per link; executions idempotent per commit and suite |
| An event choosing what runs | It cannot: suites, environment and branches come from the link |
| Arbitrary repository access | Calls only for linked repositories, by stored name, with the numeric ID re-checked; names validated before any call |
| Repository ownership | Linking requires the credentials to see the repository; processing asks GitHub which repository a pull request is in |
| Token scope | Fine-grained token or app installation; the permissions in §1; write permissions only where a link uses them |
| Tokens in logs or answers | Never returned; never in URLs; errors carry a fixed message and a code, not GitHub's text |
| Token sent elsewhere | The API address is installation configuration; redirects are not followed |
| Automated approval | Not possible: no key or event can approve data-changing runs on a protected environment |
| Who configures | Organization admins (`integrations:manage`); members can read links, never secrets |
| Other tenants | Connections, links, deliveries and keys are scoped to the organization, with row-level security |
| Untrusted text in comments | Escaped; no mentions; no direct evidence links |

## 8. Tests

| Subject | Where |
|---------|-------|
| Signatures, event parsing, branch matching, path patterns and selection, API client failures and redirects, app authentication, CI outcome, JUnit, comment content and escaping, key format, the script | `tests/unit/test_github.py` |
| Connecting, linking, permissions and tenants, configuration, webhook verification, pull request events, duplicates, superseded commits, selection in the flow, GitHub failures, statuses and comments, API keys, CI endpoints, idempotency, limits | `tests/integration/test_github_ci.py` |

GitHub is a stand-in in both (`httpx.MockTransport`): it answers the REST
paths the platform uses and records what was written.

## 9. Known limitations

- **Never run against GitHub itself.** No GitHub credentials were available.
  Paths, headers and payload fields follow GitHub's documented REST API and
  webhook formats; the first real connection is the test of that.
- **The webhook tests what is deployed.** The platform does not deploy a pull
  request's code. Unless the environment is updated by something else, use the
  pipeline route.
- **Commit statuses, not check runs.** Check runs need a GitHub App; statuses
  work with both mechanisms.
- **No web pages** for connections, links or keys; API only.
- **A redelivered event is always answered as a duplicate**, including one
  whose processing started nothing. To have a commit tested then, reopen the
  pull request or push again. A commit that already has executions is not run
  a second time by the webhook; start a run from the platform or a pipeline.
- **Path rules are the only source of impact.** No analysis of imports or of
  which tests touch which code.
- **One comment per pull request** is found through earlier executions; if
  those are gone, a new comment is written.
- **No GitLab, Bitbucket or other providers.**
