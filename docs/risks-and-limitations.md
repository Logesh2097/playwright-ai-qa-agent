# Risks, Limitations and Open Questions

> This is the planning document from before Phase 1. The limitations of the
> platform as built are in [known-limitations.md](known-limitations.md).

## 1. Decisions needed before Phase 1

These are choices the brief leaves open. Each has a recommended default so work is not blocked, but each changes scope or cost if answered differently.

| # | Question | Recommended default | Impact if different |
|---|----------|---------------------|---------------------|
| Q1 | Deployment model: one shared SaaS installation for many customers, or self-hosted per customer? | Multi-tenant, single installation (the brief asks for organizations and tenant isolation). | Self-hosted simplifies isolation but adds packaging and upgrade work. |
| Q2 | Egress proxy: standard proxy (e.g. Squid) or a small purpose-built Python proxy? | Purpose-built, with a focused test suite. | A standard proxy adds an operated component outside the mandated stack. |
| Q3 | UI components: build in-house primitives, or adopt a Vue component library? | In-house (the brief says no unnecessary libraries). | A library speeds up Phase 12 considerably. |
| Q4 | Evidence storage: local volume or S3-compatible object storage? | Volume first, S3 backend optional behind the same interface. | Multi-host deployment requires object storage. |
| Q5 | Notification channels: in-app and webhook only, or also email / Slack? | In-app + signed webhook; email via SMTP if confirmed. | Each extra channel is small but needs credentials and templates. |
| Q6 | Browsers: Chromium only, or Firefox and WebKit too? | Chromium first; the engine is browser-agnostic. | Three browsers triple runner image size and test time. |
| Q7 | Background jobs: arq, or another Redis-based queue? | arq, behind a `JobQueue` interface. | arq is in maintenance mode upstream; swapping later is contained by the interface. |
| Q8 | External tracing (LangSmith / OpenTelemetry)? | Off; everything logged to Postgres. | Better debugging at the cost of sending prompts and traces to another service. |
| Q9 | Which OpenAI models, and what are the data-retention terms on the account? | Configurable per role; must be confirmed before production data is used. | Cost, quality and compliance. |
| Q10 | Are scheduled (cron) test runs required? | Not in scope; easy to add on top of the execution API. | Adds a schedules table and UI. |
| Q11 | SSO / MFA for platform users? | Not in the first release. | Enterprise customers commonly require it. |
| Q12 | Retention periods for evidence, agent logs, audit logs? | 30 days / 90 days / 1 year. | Storage cost; compliance. |
| Q13 | Visual regression testing (screenshot comparison against baselines)? | Not in scope; responsive checks use layout assertions. | Needs baseline management and a diffing approach. |
| Q14 | Should the platform manage test-data setup and teardown in target applications (seed scripts, API hooks)? | Cases create their own data through UI/API steps. | Without it, data-dependent cases are more fragile. |

## 2. Requirements missing from the brief

Identified during analysis; each is either covered by a default above or needs input.

- **Billing and plans** — organizations have limits, but no billing model is specified. Not planned.
- **Target-application authentication beyond username/password** — SSO, CAPTCHA, SMS or email one-time codes. Only stored-secret flows (password, token, basic auth, TOTP) are planned.
- **Test-data management** — no source of truth for data such as "a product that exists" or "a customer with 500 points".
- **Preview environments with dynamic URLs** — conflict with a strict origin allowlist; needs a registration mechanism (see Phase 11 risks).
- **Accessibility testing** — not requested; the audit mode could host it later.
- **Performance / load testing** — out of scope; the platform deliberately limits request rates.
- **Mobile native apps** — out of scope; "different screen sizes" is treated as responsive web.
- **File download verification, email verification, multi-tab and multi-user scenarios** — partially supported by Playwright; not planned as first-class DSL features.
- **Localisation** of the platform UI and of target applications — not planned.
- **Compliance requirements** (data residency, SOC 2, GDPR data-subject requests) — not specified; affect retention, logging and the choice to send page content to OpenAI.
- **Importing existing tests** (Playwright, Selenium) — not planned; export only.
- **Roles** — the brief names developers and QA engineers but no role model; five roles are proposed.

## 3. Technical risks

| ID | Risk | Likelihood | Impact | Mitigation |
|----|------|:---:|:---:|------------|
| R1 | LLM produces a false pass (wrong expected value, or influences a verdict). | Medium | Critical | Verdicts only from deterministic assertions; oracle provenance; inferred oracles need approval; zero-tolerance evaluation gate. |
| R2 | Prompt injection from page content steers the agent. | High | High | No secrets in context; network and tool allowlists; structured outputs; budgets; human review. Injection is assumed to succeed at the model level and to be contained by the controls. |
| R3 | SSRF or scope escape through redirects, DNS rebinding, or MCP navigation. | Medium | Critical | Network-level egress proxy with its own resolution and IP filtering; layered checks. |
| R4 | Credential leakage through logs, traces, screenshots or prompts. | Medium | Critical | Login by the engine only; placeholder substitution in the runner; masking; redaction filters; automated leak scan in tests. |
| R5 | Playwright MCP server changes tools, flags or transport between versions. | High | Medium | Version pinned in the runner image; runtime discovery; allowlist; contract tests; open verification list in Phase 6. |
| R6 | Generated locators are brittle or wrong. | High | Medium | Exploration grounding; locator policy favouring test IDs and roles; strict resolution; healing with verification; locator-validity metric. |
| R7 | Self-healing selects the wrong element and hides a real defect. | Medium | High | Locator-only healing; uniqueness and role checks; deterministic re-run; human acceptance; visible `healed` flag. |
| R8 | Flaky tests erode trust in results. | High | Medium | Web-first assertions; no fixed sleeps; `flaky` reported separately; per-case flakiness history. |
| R9 | LLM cost or latency exceeds expectations. | Medium | Medium | Budgets per execution and organization; `run_existing` needs no LLM; small structured calls; snapshot truncation; per-role model choice. |
| R10 | Accessibility snapshots too large for the model context on complex pages. | High | Medium | Truncation and scoping to relevant regions; exploration budget; fall back to ungrounded generation flagged for review. |
| R11 | Agent modifies or destroys data in a real environment. | Medium | High | Environment kind and read-only enforcement at three layers; side-effect tags; approval for destructive steps; observe-only MCP on production. |
| R12 | Non-idempotent graph nodes create duplicates after a crash and resume. | Medium | Medium | Upserts on natural keys; resume tests at every node. |
| R13 | Cross-tenant data exposure through a missed filter. | Low | Critical | Tenant-scoped repositories, RLS, automated cross-tenant sweep over all endpoints and tables. |
| R14 | Browser exploit in a malicious target page escapes the browser. | Low | High | Chromium sandbox on; hardened non-root container; no secrets or internal network access in the runner; pinned, regularly updated browsers. |
| R15 | Purpose-built egress proxy has a bypass bug. | Medium | High | Small scope; dedicated test matrix; defence in depth from engine routing; option to switch to a standard proxy. |
| R16 | Evidence storage grows without bound. | High | Low | Video on failure only; size caps; retention job; monitoring of volume usage. |
| R17 | Redaction misses sensitive content in screenshots or free text. | High | Medium | Mask selectors; password fields always masked; access control and short retention; documented as a limitation. |
| R18 | OpenAI outage or model deprecation. | Medium | Medium | LLM gateway isolates the provider; deterministic runs unaffected; per-role model configuration; nightly smoke detects drift. |
| R19 | LangGraph / LangChain API changes. | Medium | Low | Pinned versions; thin wrappers around checkpointer, interrupts and the MCP adapter. |
| R20 | Sending page content to OpenAI conflicts with a customer's data policy. | Medium | High | Per-organization switches for exploration and analysis; redaction before sending; terms confirmed before production (Q9). |
| R21 | Phase order places security hardening (13) and full UI (12) late. | — | Medium | Controls are built with the features that need them; UI is incremental; see the implementation plan's reading notes. |
| R22 | Concurrency limits of one session per runner make large suites slow. | Medium | Low | Horizontal runner scaling; configurable sessions per runner; parallel contexts within a session for the same execution. |

## 4. Limitations by design

Stated plainly so they are not discovered later as surprises.

1. **The platform cannot prove an application is correct.** A `passed` result means the configured assertions held. Coverage depends on the cases written and approved.
2. **Expected results need a source.** When the requirement does not state the expected outcome and no rule is configured, the platform will ask, or will mark results `inconclusive` until a human approves the inferred expectation. It will not guess silently.
3. **"Find broken buttons" produces findings, not verdicts.** Deterministic signals (exceptions, failed requests) are reliable; "no observable effect" is a heuristic and will include false positives.
4. **LLM-generated cases require review.** Auto-approval is possible for non-production environments by policy, but it is a choice with a known cost in false failures.
5. **Authentication flows that need a human or a second device cannot be automated**: CAPTCHA, SMS/email codes, interactive SSO approval. A test-mode bypass in the target application or a stored TOTP secret is required.
6. **Redaction is best effort for unstructured content.** Sensitive text rendered in an unmasked area of a screenshot will be stored.
7. **Self-healing repairs locators only.** If the application's behaviour or flow changed, the case fails and must be updated by a person (the agent can suggest the change).
8. **Results are only as stable as the environment.** Shared, changing test data and unstable staging environments produce `flaky`, `blocked` and `error` results that the platform can classify but not fix.
9. **Exploration is bounded.** The agent sees a limited number of pages per instruction; large applications need more specific instructions.
10. **External MCP servers are outside the platform's isolation guarantees.**
11. **The initial deployment is single-host.** High availability is not provided; a cloud layout is sketched in [deployment.md](deployment.md) §11.
12. **Not a load-testing, native-mobile, or visual-regression tool** in this scope.

## 5. Potential implementation problems

| Area | Problem | Plan |
|------|---------|------|
| Async SQLAlchemy + RLS | The tenant setting must be transaction-local or pooled connections leak context. | `SET LOCAL` inside every transaction via a session hook; pooled-connection test. |
| LangGraph interrupts across processes | Resume must happen in a job, with the same thread ID and a lock, and nodes before the interrupt re-run on resume. | Interrupt nodes contain no side effects before the interrupt call; lock per thread. |
| Streaming step events | Runner → worker → Redis → API → browser is four hops; ordering and loss on reconnect. | Monotonic event IDs; capped replay list in Redis; Postgres as the fallback source of truth. |
| Chromium in a hardened container | Sandbox needs specific seccomp permissions and enough shared memory. | Use Playwright's recommended seccomp profile and `shm` sizing; verified in Phase 6. |
| Proxy and HTTPS | The proxy sees only `CONNECT host:port`, not paths or methods. | Host/port allowlisting at the proxy; method-level read-only enforcement in the engine's request routing. |
| MCP read-only enforcement | The MCP server has no hook for blocking request methods. | Withhold interaction tools on read-only environments rather than filtering requests. |
| Trace files and secrets | Playwright traces record typed values and network bodies. | Suppress tracing around credential steps; login recipe runs before tracing starts; scan in tests. |
| Data-driven expected values | Rule expressions must be evaluated safely and match the application's numeric semantics (rounding, currency). | Whitelisted evaluator; decimal arithmetic; rule shown to the approver with computed examples. |
| Viewport × dataset × case explosion | Work-item count multiplies quickly. | Limits per execution; plan shows the estimated count before approval. |
| GitHub forks | Running tests with stored credentials on untrusted fork PRs. | Fork PRs require maintainer approval before execution. |
| Docs/code drift | Ten planning documents can go stale. | Updating the relevant document is part of each phase's definition of done. |

## 6. Dependencies

External:

- OpenAI API account, model access, and agreed data-handling terms.
- GitHub App registration (development and production).
- A host (or hosts) with Docker and enough memory for browsers (roughly 1–2 GB per concurrent session).
- Domain and TLS certificate.
- SMTP service, if email notifications are confirmed.
- Target applications reachable from the egress proxy, with test accounts.

Internal ordering constraints (see the dependency diagram in [implementation-plan.md](implementation-plan.md)):

- The runner and egress proxy (Phase 6) must exist before any browser touches a target.
- The engine (Phase 7) must exist before the full graph (Phase 8) can execute anything.
- Reports (Phase 10) must exist before GitHub check runs (Phase 11) have something to link to.
- The demo app and its defect manifest must keep pace with Phases 5–9; they are the basis of every meaningful test of the agent.

## 7. Items to verify during implementation

These are assumptions made during planning that could not be checked without building.

- ~~Exact tool names, schemas and command-line options of the pinned `@playwright/mcp` release.~~ Verified in Phase 6 against 0.0.86; recorded in [playwright-mcp-integration.md](playwright-mcp-integration.md) §3 and guarded by a contract test.
- ~~Behaviour of the LangChain MCP adapter with the streamable HTTP transport behind a reverse proxy.~~ Not applicable: Phase 6 uses the official MCP Python SDK directly and connects to the session's port without a reverse proxy.
- Structured-output strictness and schema limits for the chosen OpenAI models (nested depth, optional fields).
- Size of accessibility snapshots on representative target pages.
- Chromium sandbox operation under the hardened container settings. **Still open**: Phase 6 runs Chromium with `--no-sandbox` (see [security-design.md](security-design.md) §7).
- Current maintenance status of arq at project start.
- PostgreSQL version available in the target hosting environment.
