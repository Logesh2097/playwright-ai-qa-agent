# Security Design

## 1. Principles

1. **The LLM is not a security control.** Anything the model is told not to do must also be impossible for it to do.
2. **The browser is hostile territory.** Pages under test can contain anything, including text written to manipulate the agent. The component that renders them holds no platform secrets.
3. **Least privilege per component.** Each service has only the credentials and network routes it needs.
4. **Deterministic verdicts.** No model output can create a pass.
5. **Everything sensitive is audited.**

## 2. Threat model

| Asset | Threats |
|-------|---------|
| Target-application credentials | Leak through logs, screenshots, traces, LLM prompts, API responses; theft from the database. |
| Tenant data (cases, results, evidence) | Cross-tenant access through missing scoping, guessable IDs, shared browser state, evidence URLs. |
| Internal network and cloud metadata | SSRF through environment URLs, redirects, DNS rebinding, MCP navigation. |
| The application under test | Destructive actions by the agent; data modification in production. |
| Platform integrity | Prompt injection steering the agent; forged GitHub webhooks; stolen API keys; malicious external MCP servers. |
| Verdict integrity | LLM hallucinating a pass; self-healing weakening a test; tampered reports. |
| Platform resources | Runaway executions, LLM cost exhaustion, browser resource abuse. |

Actors: external attackers, malicious or compromised tenant users, compromised target applications (malicious content), and the agent itself behaving unexpectedly.

## 3. Authentication

- Passwords: Argon2id (argon2-cffi defaults: 64 MiB, 3 passes, 4 lanes), rehashed transparently at login if parameters change; 12–128 characters; rejected if common, trivially repetitive, or containing the user's name or email; no composition rules.
- Password reset and invitation links carry a 256-bit random token; only its SHA-256 hash is stored; reset tokens are single-use, expire after one hour, and a new request invalidates earlier ones. Invitations are bound to the invited email address.
- Access tokens: JWT (HS256), 15-minute lifetime, signed with `JWT_SECRET_KEY` (required, at least 32 characters, no default). Carries the user ID and session ID only — roles are read from the database on each request. Revoking a session (logout, password change or reset, refresh-token reuse) puts its ID on a Redis deny-list checked on every request, so its access tokens stop working immediately rather than at expiry.
- Refresh tokens: opaque, random, stored hashed; rotated on every use; reuse of a rotated token revokes the family. Delivered as an HttpOnly, Secure, SameSite=Strict cookie scoped to the auth path.
- Login throttling: per-email failure counter (default 5 failures in 15 minutes → `account_locked` with `Retry-After`) and per-address request limits, both in Redis; identical responses for unknown users, wrong passwords and deactivated accounts; an Argon2 verification is performed even when the account does not exist, to keep response times alike. A successful password reset clears the lock.
- API keys: 32 bytes of randomness with a `qak_` prefix; only the SHA-256 hash is stored; shown once; scoped, optionally project-bound, expirable, revocable; last-used timestamp tracked.
- CSRF: the API uses bearer tokens for state-changing requests. The refresh cookie is `SameSite=Strict`, is only sent to `/api/v1/auth`, and the two endpoints that read it (`refresh`, `logout`) additionally require an `X-Requested-With` header, which a cross-site form cannot add.
- CORS: explicit allowlist of frontend origins; no wildcard with credentials.
- Multi-factor authentication and SSO are not in scope for the first release (listed as an open requirement).

## 4. Authorization

- Role-based access per organization (`viewer` → `owner`); the matrix is in [api-design.md](api-design.md).
- Enforced by FastAPI dependencies that resolve the resource, its organization, and the caller's membership. A resource in another organization returns 404, not 403.
- Object-level checks on every nested reference in a request body (for example, a `suite_id` in an execution request must belong to the same project).
- Postgres row-level security as a second layer (see [database-design.md](database-design.md) §8).
- Segregation of duties: production runs need an `admin`/`owner` approval and, except for the owner, an approver other than the requester.
- API keys can never approve, manage credentials, or change security settings.

## 5. Secrets and configuration

- All configuration comes from environment variables or mounted secret files, loaded through one typed settings class. Required secrets have no defaults; the service refuses to start without them.
- No secret is committed. `.env.example` contains names only. CI runs a secret scanner.
- Platform secrets: database URL, Redis URL, JWT signing key, credential master key, OpenAI API key, GitHub App private key and webhook secret, runner signing key, evidence URL signing key.
- The OpenAI key is used only by the worker, which makes the calls. As of Phase 4 the API container also receives it, solely to report whether AI features are configured; it never calls the provider. Narrowing this to a boolean flag is tracked in [progress.md](progress.md). The credential master key exists only in the API and worker. The runner has neither.

### 5.1 Credential encryption

Envelope encryption:

1. A random 256-bit data key (DEK) per credential encrypts the JSON secret payload with AES-256-GCM. The associated data binds the ciphertext to `organization_id` and `credential_id`, so a row copied to another tenant will not decrypt.
2. The DEK is wrapped with the master key (KEK), identified by `key_version`.
3. Rotation: add a new KEK version, re-wrap DEKs in a background job, retire the old version.
4. A KMS-backed `KeyProvider` can replace the environment-supplied KEK without schema changes.

Decryption happens in one service function (`CredentialService.reveal_for_execution`), only to build a session-bound credential grant for the runner. The call is audit-logged (`credential.use`) with the execution ID. No endpoint returns plaintext.

Implemented in Phase 3 (`app/core/security/crypto.py`). `CREDENTIAL_MASTER_KEYS` holds the versioned keys (`1:<base64>,2:<base64>`); the highest version encrypts and every listed version decrypts. Besides stored secrets, a credential can be a **reference** (`env:NAME`), in which case no secret value enters the database at all.

Further rules that keep secrets out of clear text:

- Login recipes and setup instructions are stored unencrypted, so saving one that contains the credential's own secret value is refused.
- Environment variables are plain values; a name that looks like a secret must use a credential reference instead.
- Only dedicated test accounts may be stored: the request must confirm it and a database CHECK enforces it.
- Validation errors and audit entries never include submitted secret values; credentials are destroyed, not soft-deleted, when their environment, application or project is deleted.

## 6. Application scope and SSRF

### 6.1 URL allowlisting

- Each environment has an explicit list of allowed origins (scheme, host, port). There is no wildcard-everything option; `include_subdomains` is a per-entry opt-in.
- Only admins can change the list, and every change is audit-logged.
- Validation at save time: scheme is http(s); no userinfo; host is a syntactically valid DNS name or IP; IDNs are normalised to punycode; DNS resolution must not yield loopback, private, link-local, CGNAT, multicast, or metadata addresses (IPv4 and IPv6, including IPv4-mapped forms) unless `allow_private_network` is enabled for that environment.

Implemented in Phase 3 (`app/core/security/url_safety.py`), with one addition to the design: an application carries a list of **allowed domains**, and every environment URL and origin must fall inside it. An environment's allowlist is then the exact origins derived from its base URL, API URL and explicitly added origins.

Two points are stricter than first planned:

- A host is refused if **any** address it resolves to is forbidden, not only if all are — a name with one public and one private record could otherwise be steered to the private one.
- `allow_private_network` opens loopback and private ranges only. Link-local and cloud-metadata addresses stay blocked even then.

Numeric host forms that some resolvers treat as IP addresses (`2130706433`, `0x7f.1`, `127.1`) are rejected as malformed, and IPv6 addresses embedding an IPv4 address (mapped, 6to4, NAT64, Teredo) are judged by the embedded address.

### 6.2 Enforcement at run time

Save-time validation is not sufficient (DNS can change, pages can redirect). The run-time control is the egress proxy:

- The runner's Docker network is `internal`; its only peer with outside connectivity is the proxy.
- Each session gets its own proxy credential mapped to its allowlist.
- The proxy resolves the hostname itself, applies the IP-range rules to the resolved address, and connects to that address (no second resolution — this defeats DNS rebinding).
- Only ports named in the allowlist; `CONNECT` restricted the same way.
- Redirects are simply new requests and are subject to the same checks.
- Denied requests are logged with session ID and surfaced as findings.

Until the runner and its proxy exist (Phase 6), the one place the platform contacts a target is the environment **access check**, made from the API process. It follows the same rules itself: redirects are followed by hand, each hop is validated again (allowlist, then DNS and addresses), and each connection is made to the address that was just validated, with the real host name used for TLS and the `Host` header. That closes the window in which a name could resolve differently between check and use. Response bodies are not read. The check is rate limited and audited.

`AccessValidator.validate(url, policy)` is the single entry point and is what the test engine and MCP client will call for every navigation in Phases 6–7.

The platform's own outbound calls (GitHub API, webhooks, external MCP endpoints) go through a shared HTTP client that applies the same IP-range rules and disables redirects to other hosts. User-supplied webhook and MCP URLs are validated identically to environment URLs.

### 6.3 Other request-forgery surfaces

- The LLM cannot supply arbitrary URLs: scenario validation, MCP argument validation, engine routing and the proxy all check against the allowlist.
- `file:`, `data:` navigation, `chrome:` and `view-source:` schemes are rejected by validation; the runner's file system contains nothing but the session working directory and the browser.

## 7. Execution isolation and sandboxing

Runner container hardening:

| Control | Setting |
|---------|---------|
| User | Non-root, fixed UID. |
| Capabilities | All dropped; `no-new-privileges`. |
| Filesystem | Read-only root; size-limited `tmpfs` for `/tmp`, which holds the session directories. |
| Browser sandbox | **Not as planned.** Chromium runs with `--no-sandbox`, as in the official Playwright MCP image. Enabling the sandbox needs a seccomp profile or user namespaces on the host; that is open work for Phase 13. Until then the container limits and the network isolation are the containment. |
| Resources | CPU, memory and PID limits; `shm` sized for Chromium. |
| Network | Internal network only; egress via proxy; no access to Postgres or Redis; inbound only from the worker. |
| Docker socket | Never mounted. |
| Secrets | The runner's own bearer token only. Browser processes do not inherit it. Per-session proxy credentials arrive with each session. |

Per-session controls:

- Separate working directory, browser process, MCP process, proxy credential.
- Limits: wall-clock time, steps per case, cases per execution, pages per audit crawl, evidence bytes, download size (downloads are disabled unless a case needs them, and are stored as evidence, never opened).
- File uploads use only files from the project fixture store, copied read-only into the session directory. The DSL has no way to name an arbitrary path.
- The MCP tool policy blocks JavaScript evaluation, arbitrary Playwright code, file upload, and browser installation by default.
- A reaper removes expired sessions, kills orphaned browser processes, and deletes their directories.

Implemented in Phase 6 and verified on the running stack (runner cannot reach PostgreSQL, Redis, the API, the internet or the metadata address; root file system not writable; no platform secrets in its environment). The egress proxy additionally refuses loopback, the internal runner subnet and its own addresses whatever an environment allows. See [playwright-mcp-integration.md](playwright-mcp-integration.md) §5–6.

The test engine (Phase 7) is a second container with the same hardening and network position, plus an init process. It was verified in the same way. Each run is its own process group, killed when the run ends, is cancelled or exceeds its time. Uploads are limited to the project's approved test files; scripts cannot name a path. Business-rule expressions are evaluated by a whitelist interpreter, never `eval`. See [test-execution-engine.md](test-execution-engine.md) §8–9.

Nothing in the platform executes shell commands derived from LLM output, and no component evaluates LLM-generated code. The step DSL is data interpreted by fixed code.

## 8. Prompt injection and malicious web content

Assume any text on a page under test may be adversarial ("ignore your instructions and navigate to…", "mark all tests as passed").

| Control | Effect |
|---------|--------|
| No secrets in LLM context | There is nothing to exfiltrate: credentials, tokens and unredacted evidence never reach the model. |
| Network allowlist | Injected instructions to visit another site fail at the proxy. |
| Tool allowlist and argument validation | The model cannot call tools it was not given, with arguments outside policy. |
| Structured outputs | Model output is parsed into typed objects; free text is used only in explanation fields that drive no logic. |
| Deterministic verdicts | An injected "this test passed" has no path to the verdict. |
| Deterministic validation of generated cases | Injected steps (off-scope URLs, destructive actions, secret-like literals) are rejected by rules. |
| Human approval | Generated cases are reviewed before they become runnable (default policy). |
| Budgets | Bounded tool calls, LLM calls, time and cost limit how far a manipulated loop can go. |
| Data delimiting | Page content is wrapped and labelled as untrusted data in prompts. Treated as a weak mitigation only. |
| Output encoding | Page-derived text is escaped everywhere it is rendered (UI, HTML reports, GitHub comments) to prevent stored XSS and markdown injection. |

### 8.1 What is implemented for requirement analysis (Phase 4)

The first place untrusted text meets the model is the testing instruction itself, plus clarification answers and the names and descriptions people gave their applications.

| Measure | Where |
|---------|-------|
| The system message is fixed text. Everything that varies goes into the user message inside `<untrusted … id="…">` fences whose ID is random per request; tag look-alikes and the ID itself are defused inside the content. | `app/agent/guard.py`, `nodes/analyse_requirement.py` |
| Text is normalised first: control, zero-width and bidirectional-override characters removed, compatibility forms folded, length capped. | `clean_text` |
| The prompt context is built from a type that has no field for secrets. Credentials contribute their role labels only. | `AnalysisContext` |
| The answer must match a strict schema with no unknown fields. There is no field for a target, a permission, a status or an approval. | `RequirementAnalysis` |
| The application and environment are fixed by the request and verified to be in the project. The model's answer cannot change them. | `InstructionService.submit` |
| URLs, IP addresses and `localhost` mentioned in the instruction or reported by the model are checked against the application's allowed domains; anything outside blocks approval. | `validate_requirement` |
| Phrases typical of instructions aimed at the model raise a warning shown to the reviewer; what the model reports having ignored does the same. | `suspicious_phrases` |
| Approval is a separate, permission-checked, audited action by a person, and readiness is re-checked at that moment. | `InstructionService.approve` |

What this does **not** establish: how reliably a real model resists a determined injection. The tests assume the worst case — a model that complies as far as the schema allows — and show that scope, secrets and approval still hold. Measuring the model's own behaviour needs a live key and belongs to the evaluation suite (Phase 14).

HTML reports are static, served with a restrictive Content-Security-Policy and from a path that does not share cookies with the application origin. Screenshots and videos are served with `Content-Disposition` and `X-Content-Type-Options: nosniff`.

## 9. Protecting the application under test

- **Environment kind**: `production` environments default to `is_read_only = true` and `requires_approval = true`.
- **Read-only enforcement**: generated steps carry a `side_effect` tag validated by rules; on read-only environments mutating steps are rejected at validation, the engine aborts non-safe HTTP methods to application origins, and MCP interaction tools are withheld (observe-only exploration).
- **Destructive actions**: steps tagged `delete` or `external` (payments, emails, third-party calls) always require approval, on any environment.
- **Audit crawl**: skips controls matching destructive patterns unless explicitly included.
- **Test data**: cases should create and clean up their own data; generated data uses clearly synthetic values with a per-execution prefix. Real personal data must not be placed in datasets (stated in the UI; secret/PII pattern scan on save gives a warning).
- **Rate**: per-environment concurrency limit and request pacing so a run cannot behave like a load test.

## 10. Redaction

| Surface | Method |
|---------|--------|
| Logs and error messages | Central logging filter: known secret values for the active session, bearer tokens, cookies, `Authorization` headers, key-like patterns. |
| Tool arguments and results | Same filter in the MCP client before logging and before the LLM sees results. |
| Network logs and API exchanges | Header allowlist; `Authorization`, `Cookie`, `Set-Cookie` and configured field names replaced; request/response bodies truncated and field-masked. |
| Traces and video | Credential `fill` steps are executed with screenshots/trace snapshots suppressed for that step; password fields are masked by a style injected into every page (`input[type=password]` and configured selectors). |
| Screenshots | Mask selectors configured per environment are applied through Playwright's screenshot `mask` option; password inputs are always masked. |
| LLM prompts | Only redacted snapshots and summaries; never raw credentials or cookies. |

Redaction is best effort for unstructured content (free text in a screenshot cannot be reliably detected). Mitigations: mask selectors, short retention, access control on evidence, and quarantine of any artefact whose redaction step failed. This limitation is recorded in [risks-and-limitations.md](risks-and-limitations.md).

Data sent to OpenAI as of Phase 4: the instruction text, clarification answers, configured rules, and names and settings of the application and environment (name, type, framework, sign-in method, description, environment name, kind, origin, read-only flag, allowed actions, credential role labels, variable names). Never credential values or variable values.

Planned for later phases: structured requirement data, redacted accessibility snapshots, redacted error text, and structured results. Organizations can disable exploration and failure analysis to limit this to the instruction alone. Provider data-retention terms must be confirmed before production use.

## 11. Human approval

Required for: production executions; steps with destructive or external side effects; cases whose oracle is `llm_inferred`; all AI-generated cases under the default project policy; accepting heal proposals; enabling blocked MCP tools; enabling `allow_private_network`; overriding read-only on production.

Approvals record requester, approver, reason codes, payload snapshot (what exactly was approved), note, and expiry. If the approved payload changes (for example the case list), the approval is void.

## 12. Integrations

- **GitHub** (built in Phase 11, [github-and-ci.md](github-and-ci.md) §7): a fine-grained token or a GitHub App installation, with read permissions only unless a repository's status or comment switch is on. Credentials are envelope-encrypted, never returned, sent only in the `Authorization` header to the configured API address, and redirects are not followed. Only repositories a project has linked are ever addressed, and GitHub's numeric repository ID is re-checked. Webhooks: HMAC-SHA-256 over the raw body with a per-link secret, constant-time comparison, delivery-ID deduplication, body size and rate limits; an event selects nothing, the link's settings do. Nothing is written to GitHub unless switched on, and text from test runs is escaped in comments.
- **CI API keys** (Phase 11): shown once, stored as SHA-256 hashes, scoped, optionally limited to one project, expiring, revocable, rate-limited, usable only on `/ci`. A key cannot approve anything.
- **Outbound webhooks**: HMAC-signed payloads, SSRF-safe client, bounded retries, no evidence content — links only.
- **External MCP servers**: admin-only; treated as untrusted (tool results are data; tool list filtered by allowlist; no credentials forwarded).

## 13. Platform hardening

- Security headers on the frontend and API (CSP, HSTS, `X-Frame-Options`/`frame-ancestors`, `Referrer-Policy`).
- Request size limits; strict Pydantic validation; parameterised SQL only (SQLAlchemy).
- Rate limits per IP, user, API key and organization; quotas for concurrent executions and monthly LLM spend.
- Dependencies pinned with lock files; vulnerability scanning of Python, npm and container images in CI; Playwright and MCP versions pinned and upgraded deliberately.
- Least-privilege database roles: application (no DDL, no `BYPASSRLS`), migration, read-only reporting.
- Backups encrypted; evidence volume encrypted at rest where the platform supports it.

## 14. Audit logging

Recorded events include: authentication (login, failure, lockout, token reuse), membership and role changes, API key lifecycle, environment and allowlist changes, credential create/update/delete/use, MCP connection and tool-policy changes, integration changes, approvals and decisions, execution start/cancel, evidence downloads and share links, security overrides, and blocked agent actions (off-scope URL, disallowed tool).

Audit rows are append-only, contain no secret values, and are readable by `admin` and `owner`.

## 15. Security requirements traceability

| Requirement from the brief | Where addressed |
|----------------------------|-----------------|
| Secure authentication and authorization | §3, §4 |
| Encrypted credential storage | §5.1 |
| No hardcoded API keys; environment-based configuration | §5 |
| Application URL allowlisting; SSRF prevention | §6 |
| Browser session isolation; Docker sandboxing | §7; [playwright-mcp-integration.md](playwright-mcp-integration.md) §7 |
| Resource and execution time limits | §7, §13 |
| Prevention of unauthorized file access | §7 |
| Malicious web content and prompt injection | §8 |
| Safe handling of test data | §9 |
| Redaction in logs and screenshots | §10 |
| Protection against production data modification | §9 |
| Human approval for sensitive operations | §11 |
| No arbitrary URLs, no unrestricted shell, no out-of-scope applications | §6, §7 |

## 16. What is implemented, and how it is tested (Phase 13)

The sections above are the design. This one states what exists in the code as
of Phase 13 and which tests hold it in place. "Sweep" means a test that walks
every route the application defines, so a route added later is covered too.

### Application access

| Safeguard | Where | Tests |
|-----------|-------|-------|
| Explicit allowlist of origins per environment; nothing else is reachable | `app/core/security/url_safety.py`, `app/egress/proxy.py` | `test_url_safety.py`, `test_egress_proxy.py`, `test_applications.py` |
| DNS resolution and resolved-address checks; loopback, private, link-local and cloud metadata ranges refused | same | same; `test_browser_real.py`, `test_engine_real.py` (platform services unreachable from a test) |
| Redirects re-checked; refused plain-HTTP connections are dropped, not answered | `app/egress/proxy.py`, access probe | `test_egress_proxy.py`, `test_applications.py` |
| Private test environments only through `allow_private_network`, which only an organization admin can set, and which is audited | `app/services/applications.py` | `test_applications.py` |
| Browser navigation re-validated on every request, through a per-run proxy credential | engine route guard, egress proxy | `tests/engine/`, `test_engine_real.py` |

### Browser isolation

| Safeguard | Where |
|-----------|-------|
| One browser context per test case; a new process per MCP session; nothing shared between executions | `app/engine/runner.py`, `runner/supervisor.py` |
| Read-only root filesystem, dropped capabilities, no-new-privileges, process, memory and CPU limits, an init process | `docker-compose.yml` (runner, engine) |
| No route from the browser network to the database, Redis or the API | Compose networks (`runner` is internal); tested in `test_engine_real.py` |
| Time limits per step, per case, per execution and per job | `RunConfig.timeouts`, `EXECUTION_TIMEOUT_S`, worker `job_timeout` |
| Run directory, proxy registration and progress keys removed when an execution ends, however it ends | `ExecutionRunner.run`; `test_security.py::test_a_finished_execution_leaves_nothing_behind` |

### AI

| Safeguard | Where | Tests |
|-----------|-------|-------|
| Page content, instructions and documents are fenced as data in every prompt | `app/agent/guard.py`, prompts | `test_requirement_analysis.py`, `test_workflow.py` |
| Strict output schemas; unknown fields rejected | `app/agent/schemas/` | strict-mode tests |
| A model never decides a result, a category, a verdict, a permission or which tool may run | verdict, classification, tool policy modules | "a model that claims a pass" tests in `test_quality.py`, `test_workflow.py` |
| Tools are allowed by rule, default deny; sensitive tools need approval | `app/browser/policy.py` | `test_browser_policy.py` |
| Credentials are never placed in prompts or workflow state | `app/workflow/state.py` | `test_workflow.py` |

### Sensitive operations

Rules in `app/testing/sensitive.py`; policy in `ExecutionService`.

| Rule | Tests |
|------|-------|
| Steps that delete, pay, send messages, change important configuration or clean up destructively are found by rule from the case and its script | `test_safeguards.py` |
| Starting a run that contains them answers `sensitive_approval_required` with the list; the run starts only when the person approves them **for that execution** | `test_security.py` |
| Approving needs the right to approve test cases (QA engineer and above) | same |
| Against a **production** environment they are never run, with or without approval | same |
| A CI key, a webhook or a workflow without that approval cannot approve: such cases are blocked and the rest run | same |
| On an environment that requires approval, only an organization admin can start a run that changes data or signs in | `test_executions.py` |
| Production environments are read-only and approval-required by default | `test_applications.py` |

### Credentials

| Safeguard | Where | Tests |
|-----------|-------|-------|
| Envelope encryption at rest with versioned master keys (rotation by adding a key) | `app/core/security/crypto.py` | crypto tests |
| Write-only through the API; references to an external secret store are supported | `app/services/credentials.py` | `test_applications.py` |
| Given only to the engine, only for the run that uses them; each use audited | `ExecutionRunner._credentials` | `test_executions.py` |
| Redacted by the engine **and again by the worker** before anything is stored or shown | `app/browser/redaction.py`, `ExecutionRunner._consume` | sweep `test_security.py::test_a_stored_secret_appears_in_no_answer…` over every GET route, every export, every evidence file and the audit log |
| Secret fields masked in screenshots; no recordings of cases that type a secret | `app/engine/evidence.py` | `tests/engine/` |

### API

| Safeguard | Where | Tests |
|-----------|-------|-------|
| Authentication: short-lived access token in memory, rotating refresh cookie, session revocation | `app/services/auth.py` | `test_auth.py` |
| Authorization on the server for every route; roles from the database on every request | `app/core/security/permissions.py` | sweep: a viewer can change nothing through any route |
| Tenant isolation: organization checked on every request, every query scoped, row-level security underneath | repositories, migrations | sweep: no route answers an outsider or an anonymous caller; `test_database.py` |
| Rate limits: sign-in, registration, password reset, executions, API keys, webhooks, model calls | `app/core/rate_limit.py` | per-feature tests |
| Request validation: strict schemas, unknown fields rejected | `ApiModel` | throughout |
| Input size limit: 4 MB per request (`MAX_REQUEST_BODY_BYTES`), 1 MB per webhook | `app/middleware/http_security.py` | `test_safeguards.py`, `test_security.py` |
| Secure headers on every answer: `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, a deny-all `Content-Security-Policy`, `Permissions-Policy`, `no-store`; HSTS in production | same | same |
| CORS: only configured origins, with credentials, fixed methods and headers | `app/main.py` | `test_auth.py` |
| Audit log of every change, approval, credential use, export and download address | `AuditService` | throughout |

### Reliability

| Safeguard | Where | Tests |
|-----------|-------|-------|
| A workflow run whose worker stopped continues from its checkpoint | `app/workflow/runner.py` | `test_workflow.py` |
| A plain execution whose worker stopped is closed: the case in progress becomes an error, the rest are not run, nothing is run twice | `recover_stale_executions` (cron, twice a minute) | `test_security.py` |
| A job delivered twice does nothing the second time | status claimed under a row lock | `test_executions.py`, `test_security.py` |
| Idempotent starts for CI and webhooks | idempotency keys | `test_github_ci.py` |
| Bounded retries everywhere; assertion failures never retried | `RunConfig.retry`, `app/workflow/retry.py` | `test_workflow.py` |
| One transaction per request; nothing is committed for a request that fails | `get_db_session` | throughout |
| Health of the engine and the browser runner checked every minute by the worker | `check_services` | `test_security.py` |
| The worker has 60 seconds to stop; what does not finish is recovered | `stop_grace_period` | — |

## 17. Remaining risks

Stated plainly, because a list of safeguards can read as a claim of safety.

- **Chromium runs without its own sandbox** (`--no-sandbox`) inside the
  container. The container is the boundary: read-only filesystem, no
  capabilities, no route to internal services. A browser escape would land in
  that container, not in a sandboxed renderer.
- **Sensitive steps are recognised by English words** on buttons, links,
  addresses and descriptions, and by what the case declares. A step that
  deletes something behind a button called "OK" is not recognised unless its
  case says so. Dedicated test environments and test accounts remain the main
  protection.
- **Screenshots, videos and traces are not scanned.** Whatever the application
  shows is in them. Access control and retention limit the exposure.
- **Prompt injection cannot be excluded**, only made ineffective: a model's
  output cannot change a result, grant a permission or call a tool outside the
  policy. A model can still be led to write a misleading explanation, which is
  why explanations are labelled suggestions.
- **Never run against a live model or against GitHub itself.**
- **One worker process, one engine.** Recovery is tested; high availability is
  not provided.
- **Row-level security depends on the application role not owning the tables.**
  Running the API with the migration role would switch it off.
- **Rate limits are per installation**, kept in Redis. They are not a defence
  against a distributed attack; a reverse proxy or WAF in front is assumed in
  production.
- **Metrics hold no identifiers but do hold model and tool names and route
  templates.** The endpoint needs a token and should not be exposed publicly.
- **Secrets in environment variables.** The master key, JWT key and service
  tokens are read from the environment; protecting them is the deployment's
  job (see [deployment.md](deployment.md)).
