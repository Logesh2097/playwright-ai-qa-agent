# Playwright and Playwright MCP Integration

How the platform drives a browser through the Playwright MCP server: what is
built (Phase 6), what was verified against the real server, and what is still
planned (the deterministic test engine, Phase 7).

**Pinned version: Playwright MCP 0.0.86** (`mcr.microsoft.com/playwright/mcp:v0.0.86`,
bundled Chromium). Everything in sections 2–9 was checked against that server
running in Docker, not taken from documentation. To upgrade, see section 10.

## 1. Responsibilities

| Concern | Who does it |
|---------|-------------|
| Expose browser tools (navigate, snapshot, click, type, …) | Playwright MCP server |
| Connect, discover tools, validate arguments, call, time out, close | MCP client, `app/mcp/` |
| Decide which tools a session may use; check addresses; redact; record | Browser layer, `app/browser/` |
| Start and destroy one server and browser per session | Runner service, `runner/` |
| Stop the browser reaching anything outside the allowed origins | Egress proxy, `app/egress/` |
| Sign in with a stored test credential | `app/browser/login.py`, from the credential's recipe; no model involved |
| Decide which tool to call during exploration | LangGraph agent (Phase 8), through `app/agent/tools/browser.py` |
| Assertions and pass/fail verdicts | Code. For browser checks: `BrowserCheckRunner`. For test runs: the test engine ([test-execution-engine.md](test-execution-engine.md)) |

MCP is **not** used for, and must not be assumed to provide: test assertions,
pass/fail decisions, reports, retries, credential handling, tenant isolation
or a security boundary. Its own origin options say so (`--allowed-origins`
"does not serve as a security boundary and does not affect redirects").

## 2. Architecture

```
 api ── queue ──► worker                                   runner container
                  ┌───────────────────────────┐   HTTP     ┌─────────────────────────────┐
                  │ BrowserCheckRunner / agent│  (token)   │ supervisor.py               │
                  │   BrowserSessionManager ──┼───────────►│   one process group per     │
                  │   BrowserSession          │            │   session:                  │
                  │     policy · scope ·      │    MCP     │   Playwright MCP ─ Chromium │
                  │     redaction · recorder  │ streamable │        │                    │
                  │   McpConnection ──────────┼── HTTP ───►│        │ all traffic        │
                  └───────────┬───────────────┘            └────────┼────────────────────┘
                              │ session rules (token)               ▼
                              └────────────────────────────► egress proxy ──► application
                                                                              under test
```

Networks (Docker Compose):

| Network | Members | Notes |
|---------|---------|-------|
| `backend` | api, worker, postgres, redis | The runner and the proxy are not on it. |
| `runner` | worker, runner, egress-proxy | `internal: true`: no route outside. Fixed subnet (`RUNNER_SUBNET`). |
| `egress` | egress-proxy (and the optional demo app) | The proxy's way out. |

Checked on the running stack: from inside the runner container, PostgreSQL,
Redis, the API, the internet and the cloud metadata address are all
unreachable; only the proxy answers. The runner and the proxy hold no database
credentials, signing keys, master key or AI key. The API holds neither the
runner token nor the proxy token; only the worker starts browsers.

Code layout, kept separate from business logic:

| Package | Knows about |
|---------|-------------|
| `app/mcp/` | The MCP protocol only: `connection.py`, `catalog.py`, `results.py`, `errors.py`. |
| `app/browser/` | Browsers and permissions: `policy.py`, `session.py`, `manager.py`, `login.py`, `redaction.py`, `outcome.py`, `events.py`, `services.py`, `factory.py`. No database access. |
| `app/egress/` | The proxy process. |
| `app/services/browser_checks.py` | The business feature (database, Redis, audit, queue). |
| `runner/supervisor.py` | The runner. Standard library only; shares no code with the backend. |

## 3. MCP client (`app/mcp/`)

Built on the official MCP Python SDK (`mcp` 2.3.0): `ClientSession` over
`streamable_http_client`, the transport Playwright MCP serves at `/mcp`.

| Capability | Implementation |
|------------|----------------|
| Connection management | `McpConnection`. One owner task holds the SDK's transport and session contexts and serves requests from a queue. The SDK requires those contexts to be entered and left in one task and reports a broken transport by cancelling its scope; the owner task turns that into ordinary errors for callers. |
| Server configuration | A `Transport` factory (`streamable_http_transport(url)`); tests substitute in-memory streams. Timeouts are constructor arguments. |
| Initialization, capabilities | `connect()` performs the handshake and returns `ServerInfo` (name, version, protocol version, capability names). |
| Tool discovery | `list_tools()`, following pagination, returns `ToolSpec` objects (name, description, input schema, the server's read-only and destructive hints). |
| Argument validation | `ToolCatalog` compiles each discovered input schema with `jsonschema`. A tool whose schema is not valid is left out. Error messages name fields and rules, never values. |
| Invocation, result parsing | `call_tool()` returns `RawToolResult` (error flag, text blocks, image blocks). |
| Health check | `ping()` (MCP ping) returns the round-trip time. |
| Timeouts | Per call, enforced by the SDK and mapped to `McpTimeoutError`; the connection stays usable. A request that never returns at all marks the connection failed. |
| Termination | `close()` is idempotent and leaves no tasks behind. |
| Errors | `McpServerUnavailableError`, `McpInitializationError`, `McpDiscoveryError`, `McpTimeoutError`, `McpConnectionLostError`, `McpNotConnectedError`, `McpProtocolError`, `InvalidToolArgumentsError`, `UnknownToolError`. Transport exceptions can quote URLs, so only their type name is kept. |
| Reconnection | `reconnect()` opens a new MCP session. With Playwright MCP that means a new, empty browser, so it is used automatically only where that is harmless: while starting, and before a session has performed any action. After that a lost connection fails the session. |

No tool name appears in `app/mcp/`. Names, schemas and hints come from discovery.

### What discovery returns (0.0.86)

25 tools: `browser_navigate`, `browser_navigate_back`, `browser_snapshot`,
`browser_find`, `browser_take_screenshot`, `browser_console_messages`,
`browser_network_requests`, `browser_network_request`, `browser_wait_for`,
`browser_tabs`, `browser_resize`, `browser_hover`, `browser_click`,
`browser_drag`, `browser_handle_dialog`, `browser_type`, `browser_fill_form`,
`browser_press_key`, `browser_select_option`, `browser_evaluate`,
`browser_run_code_unsafe`, `browser_file_upload`, `browser_drop`,
`browser_emulate_media`, `browser_close`.

Observed behaviour that the code depends on, each covered by a test against the
real server:

| Observation | Consequence |
|-------------|-------------|
| Elements are addressed by `target`: a reference from a snapshot or a selector such as `role=button[name="Sign in"]`. | Login recipes use role and name; no snapshot parsing is needed to sign in. |
| With the default settings, results repeat the generated code, **including typed text**: `fill('the-password')`. | The runner starts the server with `--codegen none`, and the client redacts secret values regardless. |
| Results are Markdown with `### ` sections (`Page`, `Error`, `Events`, `Modal state`, `Snapshot`). Action results link to a snapshot file; only `browser_snapshot` returns the tree inline. | `outcome.py` reads the sections. A missing section yields "unknown", never a guess. |
| The server's hints mark almost every tool as destructive (`browser_resize` and `browser_click` alike). | Hints cannot classify tools; see section 4. They are used only as a cross-check. |
| A tool error is a normal result with `isError`, including an unknown tool name. | Error results are mapped to typed errors by their text (`classify_error`); unrecognised text is `tool_failed`. |
| A browser that dies is relaunched silently; the next result shows `about:blank`. | A session that had a page and suddenly sees `about:blank` raises `BrowserClosedError`. |
| Navigating to a download saves the file on the server's disk. | The runner sets `acceptDownloads: false`; a download event in a result ends the session. |
| Each HTTP client gets its own browser context; contexts do not share cookies. | Still, one server process per session is used, for per-session settings and process isolation. |
| Pages can register their own tools through WebMCP. | The runner passes `--no-webmcp`; unknown tools are withheld anyway. |
| The host check needs host and port (`--allowed-hosts runner:9000`). | Set per session by the runner. |

## 4. Tool permissions (`app/browser/policy.py`)

Default deny. A session may use a tool only if **all** of these hold:

1. the server listed it in discovery, with a valid schema;
2. a rule exists for it, and the server's hints do not contradict the rule (a tool classified read-only that the server no longer calls read-only is withheld);
3. the environment allows the action it performs, and is not read-only for state-changing tools;
4. the workflow asked for it, and an organization admin has not switched it off.

Arguments are restricted too: an argument the rule does not name is refused
before anything is sent. `filename` (write output to a file on the browser
host) and `isolatedContext` are not allowed on any tool.

| Category | Tools | Environment action | Notes |
|----------|-------|--------------------|-------|
| Read-only | `browser_snapshot`, `browser_find`, `browser_take_screenshot`, `browser_console_messages`, `browser_network_requests`, `browser_wait_for` | `read` | |
| Reversible UI | `browser_navigate`, `browser_navigate_back`, `browser_tabs` | `navigate` | URL arguments are checked against the allowed origins. |
| | `browser_resize` | `read` | |
| | `browser_hover` | `click` | |
| State-changing UI | `browser_click`, `browser_drag`, `browser_handle_dialog` | `click` | Refused on read-only environments. Need approval where the environment requires it. Accepting a dialog can confirm a deletion, so it is here. |
| | `browser_type`, `browser_fill_form`, `browser_press_key` | `type` | |
| | `browser_select_option` | `select` | |
| Sensitive | `browser_network_request` | `read` | Shows headers and bodies. Needs approval for each session; nothing grants that yet. |
| Sensitive, forbidden | `browser_evaluate`, `browser_run_code_unsafe`, `browser_file_upload`, `browser_drop` | — | Arbitrary code and files on the browser host. No approval can enable them. |
| Internal | `browser_close` | — | Used by the session to close the browser; never offered. |
| No rule | `browser_emulate_media`, and anything a future version adds | — | Withheld until someone classifies it on purpose. |

Approval in this phase: on an environment with `requires_approval`,
state-changing tools are withheld unless the session carries
`state_changes_approved`. For a login check that flag is set only when an
organization admin starts it, and that is audited. The worker enforces the flag
itself; a job enqueued without it cannot type or click.

Organization admins can narrow the tool set (`PUT …/browser/allowed-tools`).
They cannot enable a sensitive or internal tool that way.

## 5. Sessions (`app/browser/session.py`, `manager.py`)

One session is one Playwright MCP process with one browser, started for one
piece of work and destroyed afterwards. Nothing is pooled or reused, so browser
state cannot pass between organizations, users or runs.

`BrowserSessionManager.open(spec, recorder)` is an async context manager:

1. register the session's rules and a fresh proxy credential with the egress proxy;
2. ask the runner for a browser with that proxy, viewport, timeouts and lifetime;
3. connect (retrying while the process starts), discover tools, compute what is exposed;
4. yield a `BrowserSession`;
5. always, also on error or cancellation: close the browser, close the connection, stop the runner session, remove the proxy rules and collect what the proxy refused.

Configurable per session (`LaunchOptions`): browser, viewport (the
environment's `default_viewport` or 1280×720), action timeout, navigation
timeout, lifetime. Installation defaults: `BROWSER_NAME`,
`BROWSER_ACTION_TIMEOUT_MS`, `BROWSER_NAVIGATION_TIMEOUT_MS`,
`BROWSER_SESSION_TTL_S`. The runner image contains Chromium only.

Every tool call goes through `BrowserSession.call(tool, arguments)`:

| Step | Refusal |
|------|---------|
| Session is open | `SessionClosedError` |
| Tool was granted | `ToolNotPermittedError` or `ApprovalRequiredError` |
| Only arguments the rule names | `InvalidToolArgumentsError` |
| Arguments fit the discovered schema | `InvalidToolArgumentsError` |
| URL arguments are http(s), in the allowed origins, not a forbidden literal address | `ScopeViolationError` |
| — the call is sent — | |
| Result shows a download | `DownloadBlockedError`; session ends |
| Result is an error | `BrowserLaunchError`, `BrowserTimeoutError`, `NavigationFailedError`, `ToolFailedError`, … |
| Page is outside the allowed origins | `ScopeViolationError`; session ends |
| Page vanished (`about:blank`) | `BrowserClosedError`; session ends |

A call that does not succeed raises. There is no return value that could be
read as success, and `stats.performed` counts only calls the server completed
without error.

### Runner (`runner/supervisor.py`)

`POST /sessions`, `DELETE /sessions/{id}`, `GET /healthz`, `GET /livez`;
bearer token on everything except liveness. Per session: its own directory
under `/tmp`, its own port, its own process group, a minimal environment
(without the runner's token). Stopping kills the whole group, so the browser
cannot outlive its server, and deletes the directory. A reaper stops sessions
whose time is up or whose server died. Capacity is capped
(`RUNNER_MAX_SESSIONS`).

Request values are validated against strict patterns and passed as separate
arguments, never through a shell. The command line uses only options listed by
`--help` of the pinned version:

```
node /app/cli.js --headless --isolated --no-sandbox --browser chromium
  --host 0.0.0.0 --port <p> --allowed-hosts runner:<p>
  --codegen none --no-webmcp --block-service-workers
  --config <dir>/config.json            # {"browser":{"contextOptions":{"acceptDownloads":false}}}
  --output-dir <dir>/output --viewport-size <w>x<h>
  --timeout-action <ms> --timeout-navigation <ms>
  --allowed-origins "<a>;<b>"           # convenience filter, not the boundary
  --proxy-server http://<session>:<secret>@egress-proxy:3128 --proxy-bypass "<-loopback>"
```

Container: read-only root file system, `/tmp` as tmpfs, all capabilities
dropped, `no-new-privileges`, process and memory limits, non-root user.

## 6. Staying inside the authorized application

Layers, strongest first:

1. **Egress proxy.** The browser can reach nothing else. For every request and every tunnel the proxy identifies the session from its proxy credentials, checks scheme, host and port against that session's allowed origins, resolves the name itself, refuses if any resolved address is forbidden, and connects to the address it checked. This holds for redirects, links, scripts and images, whatever caused them. Always refused, whatever an environment allows: loopback, cloud metadata, the platform's internal subnet (`EGRESS_DENY_CIDRS`) and any address of the proxy's own machine. Private addresses need the environment's `allow_private_network`, which only organization admins can set.
2. **Session checks.** URL arguments are checked before a call; the page address is checked after each call, and leaving the allowed origins ends the session.
3. **Server options.** `--allowed-origins`, `acceptDownloads: false`, `--no-webmcp`, blocked `file:` URLs. Useful, not relied upon.

A refused plain-HTTP request gets no answer from the proxy (the connection is
closed), so the browser reports a failed load. Answering with an error page
would leave a page at the refused address, which looks like a successful
navigation. This was found by a test against the real browser.

Text on a page is never treated as an instruction. The login workflow reads no
page text to decide what to do. Agent tools wrap results in a fence the page
cannot forge (`app/agent/tools/browser.py`). And whatever a page says, a call
it provokes meets the same checks as any other.

## 7. Authentication (`app/browser/login.py`)

A credential's **login recipe** is a list of steps:

```json
[
  {"action": "goto",   "path": "/login"},
  {"action": "fill",   "target": {"role": "textbox", "name": "Username"}, "value": "{{cred.username}}"},
  {"action": "fill",   "target": {"role": "textbox", "name": "Password"}, "value": "{{cred.password}}"},
  {"action": "click",  "target": {"role": "button", "name": "Sign in"}},
  {"action": "expect", "text": "Signed in as", "url_path": "/account"}
]
```

Rules, checked when the credential is saved: the first step is `goto` with a
path on the environment; `fill` values are exactly one `{{cred.<field>}}`
placeholder; the last step is `expect`, so a failed login is noticed; unknown
actions and fields are rejected.

How secrets are protected:

- The secret is decrypted in the worker by `CredentialService.reveal_for_job`, which writes a `credential.use` audit entry naming who started the job.
- No model is involved in signing in. Secret values never enter a prompt.
- Values are registered with the session before the first step. Tool arguments, results, errors, log lines and database rows show `[REDACTED]`, in plain and URL-, HTML- and JSON-encoded forms. Usernames stay readable.
- The server is started with `--codegen none` so it does not echo typed text.
- The login workflow does not request the screenshot tool.
- Cookies and storage live in the browser's in-memory profile and die with the process. Login state is not saved or reused; each session signs in again.
- Independently of known values, bearer tokens, JSON web tokens, cookie and authorization headers and sensitive URL parameters are masked by shape.

A login succeeds only if every step ran and every `expect` held.

Not supported: CAPTCHA, one-time codes by SMS or email, SSO that needs
interactive approval, TOTP, and credentials stored by reference (`env:…`).

## 8. Observability

Every tool call produces a `ToolCallEvent`, also when it is refused: session
ID, sequence number, tool name, category, sanitized arguments, status (`ok`,
`error`, `timeout`, `denied`, `blocked`), duration, error code and message,
page address, and a short result summary (page line, events, dialog state;
never page content). Connection status is reported as `connecting`,
`connected` (server, version, protocol, tools discovered and exposed),
`reconnected`, `failed`, `closed` (with counts).

Recorders: `LogRecorder` (one JSON log line per event) and `DbRecorder`
(`agent_execution_logs` rows). A recorder that fails is logged and never stops
a browser action. The organization's managed connection in `mcp_connections`
keeps the server version, the discovered tools, the time of the last check,
status and last error.

## 9. Failure handling

| Failure | What happens |
|---------|--------------|
| Runner or proxy unavailable, runner full | `RunnerUnavailableError` / `RunnerBusyError`; no browser is started; anything already registered is removed. |
| MCP server unavailable | Connect is retried while the process starts, then `ConnectionFailedError` (`mcp_server_unavailable`). |
| Tool discovery fails | `ConnectionFailedError` (`mcp_discovery_failed`); the session does not open. |
| Invalid tool arguments | `InvalidToolArgumentsError`; nothing is sent. |
| Browser launch failure | `BrowserLaunchError`; session ends. |
| Browser or navigation timeout | `BrowserTimeoutError`; not counted as performed. The action may or may not have taken effect, so the caller must not assume either. |
| Navigation failure | `NavigationFailedError`, or `ScopeViolationError` when the proxy or browser blocked the destination. |
| Unexpected browser closure | `BrowserClosedError`; session ends. Not restarted: its login and page are gone. |
| Connection interruption | Before any action: reconnect once. Afterwards: `ConnectionFailedError`; session ends. |

In each case the connection is marked `unhealthy` where the cause is the
infrastructure, cleanup still runs, and the browser check is `failed`.
`passed` is assigned in one place, and only when the browser work completed.

## 10. Upgrading Playwright MCP

1. Change the tag in `runner/Dockerfile`; `make build`.
2. `make be-test-browser`. `test_pinned_server_matches_the_permission_table` fails if a tool in the permission table is gone or lost an argument, if a read-only tool is no longer described as read-only, or if the server offers a tool nobody has classified.
3. Read `--help` of the new version against the command line in `runner/supervisor.py`.
4. Classify new tools in `policy.py` deliberately; update the version noted there and here.

## 11. API

| Endpoint | Purpose | Who |
|----------|---------|-----|
| `GET /organizations/{org}/browser` | Every tool with category, permitted arguments and availability; last known connection status, server version, discovered and unclassified tools | Members |
| `PUT /organizations/{org}/browser/allowed-tools` | Narrow the tool set, or remove the restriction (`null`) | Organization admins |
| `POST …/environments/{env}/browser-checks` | Open the environment's address in a browser; with `credential_id`, also sign in. Runs in the worker; answers 202 | Developers and above; a login on an approval-required environment needs an admin |
| `GET …/environments/{env}/browser-checks/{id}` | Status, result, error, session facts, sanitized tool calls | Developers and above |

A check's state is kept in Redis for one hour (`BROWSER_CHECK_RESULT_TTL_S`);
its tool-call trace is in `agent_execution_logs`. Checks are rate-limited per
organization. Without `RUNNER_URL` the endpoints answer 503
`browser_not_configured`.

## 12. Tests

| Suite | Command | What it uses |
|-------|---------|--------------|
| MCP client, permissions, redaction, sessions, cleanup, login, agent tools | `make be-test` | A mock MCP server: a real server built with the SDK over in-memory streams, with a pretend browser behind it (`tests/mcp_fakes.py`) |
| Egress proxy | `make be-test` | Real loopback sockets, fake DNS and upstream |
| Browser checks through the API | `make be-test-integration` | Real database and Redis, mock MCP server |
| Runner supervisor | `make runner-test` | A stand-in server process; no browser |
| Real server and browser | `make be-test-browser` | Playwright MCP 0.0.86, Chromium, the proxy and `fixtures/demo-app` |

## 13. Not built yet

- The deterministic test engine was built in Phase 7 as a separate service; see [test-execution-engine.md](test-execution-engine.md). Verdicts for test runs come from that engine, not from MCP tool results.
- **A model driving the browser.** The agent workflow (Phase 8) uses MCP for a read-only, code-driven look at the application ([agent-workflow.md](agent-workflow.md) §7). The adapter that could hand tools to a model exists and is tested but is not used.
- **Approvals for MCP sessions**: the session accepts approvals; only the admin-started login check issues one. The workflow's discovery session is read-only and needs none.
- **External MCP servers**: the table supports them; only the managed server is implemented.
- **Evidence from MCP sessions**: screenshots are returned to the caller and not stored (the test engine stores its own evidence).
- **Web pages** for the status and check endpoints.

## 14. Known limitations

- **Chromium runs with `--no-sandbox`**, as in the official image. The container hardening and the network isolation are the containment. A browser exploit would land in a container that can reach only the proxy and the worker's outbound connections.
- **MCP endpoints of running sessions have no authentication of their own**; the server offers none. Anything on the internal `runner` network can connect to them. Only the worker, the runner and the proxy are on that network, and the proxy refuses to forward to it.
- **Result parsing depends on an undocumented layout.** It fails towards "unknown", and the real-server tests detect a change, but only when they are run.
- **Redaction by value** cannot catch a secret the application transforms (for example shows reversed or hashed), and a very short secret can mask unrelated text.
- **A timed-out action may still have happened.** The session reports the timeout; it cannot undo the action.
- **One runner.** Sessions are capped per runner; there is no scheduling across several runners yet.
- **DNS lookups by the browser** for prefetching can reach Docker's resolver even though connections cannot leave.
- **Private-network environments** are reached from the proxy's network position. With `allow_private_network` an allowlisted origin on the Docker host's own networks is reachable; that switch is for administrators for this reason.
