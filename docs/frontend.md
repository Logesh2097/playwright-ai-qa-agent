# Web Application

The Vue 3 + TypeScript interface (Phase 12). The architecture is the one set
out in [architecture.md](architecture.md) §5; this document says what exists,
how it is put together and how it was checked.

## 1. Screens

| Area | Route | What it does |
|------|-------|--------------|
| Sign in, register, accept invitation | `/login`, `/register`, `/accept-invite` | |
| Password reset | `/forgot-password`, `/reset-password?token=` | Request a link; choose a new password. The answer never says whether an address has an account |
| Dashboard | `/` | Across the projects the person can see: projects, executions, passed and failed tests, results by status, pass-rate trend, frequently failing tests, recent executions |
| Projects | `/projects`, `/projects/{id}` | List, create, details, settings, members |
| Applications | `/projects/{id}/applications/{id}` | Edit, environments, allowed origins, test credentials (write-only) |
| Instructions | `/projects/{id}/instructions[/{id}]` | Enter an instruction, choose application and environment, analysis preview, clarifications, generate cases |
| Test cases and suites | `/projects/{id}/cases/{id}`, `/suites[/{id}]` | Generated cases, details, edit, approve, suite ordering and approval, **run suite** |
| Execution in progress | `/projects/{id}/executions/{id}/live` | Status, progress, current test case, last step reported, engine events, cancel |
| Test report | `/projects/{id}/executions/{id}` | Totals, failures, steps with expected and found, screenshots, logs, AI analysis on request, exports |
| History | `/projects/{id}/executions`, `/executions/compare`, `/cases/{id}/history`, `/dashboard` | History with filters and paging, start execution, comparison, case history, project dashboard |
| Replacement locators | `/projects/{id}/heal-proposals` | Review, accept or reject |
| Integrations | `/integrations`, `/projects/{id}/integrations` | GitHub connections, CI API keys, browser (MCP) service and its tools; per project: linked repositories and what each may trigger |
| Settings | `/settings` | Profile, security (password, sessions), organization and members, roles and permissions, audit log |
| System status | `/system` | Service health |

Not built: pages to start and steer an agent workflow run (clarification and
approval inside one run). The same steps exist as separate screens
(instruction → cases → approve → run).

## 2. Structure

```
src/
  api/          client.ts (the one HTTP client) and one module per area
  types/        typed shapes of every request and answer
  stores/       Pinia: auth (session, organization, role), notifications, projects, system
  composables/  useAsync, useFormErrors, usePolling, useExecutionStream
  components/
    ui/         BaseButton, BaseModal, FormField, StatusBadge, StatCard, DataTable, FilterBar,
                PaginationBar, TabList, CodeViewer, ScreenshotViewer, Loading/Error/EmptyState
    domain/     ExecutionProgress, ResultSummary, TrendChart, StartExecutionDialog, forms
  pages/        one component per screen; reportFormat.ts for how statuses and figures are shown
  router/       routes, guard, safe redirects, navigation
```

Rules the code follows:

- **One client.** Every request goes through `api/client.ts`: bearer token from
  memory, one silent renewal on 401, one error type (`ApiError`). Pages call an
  API module, never `fetch`.
- **No business logic in pages.** Figures, statuses, categories, pass rates and
  permissions come from the server. The role table on the settings page is read
  from `GET /meta/roles`, not copied.
- **Text from tests is text.** Titles, messages and recorded values are placed
  with interpolation, never as HTML. Screenshots are fetched with the session
  and shown only as images.
- **Secrets do not linger.** Passwords, tokens and keys are cleared from form
  state after sending; a created API key or webhook secret is shown once.
- **Only `passed` is green.** Flaky, needs review and inconclusive are amber
  everywhere (`reportFormat.toneOf`).
- **Showing a control is not permission.** Controls follow the role
  (`auth.isAdmin`, `auth.canContribute`) for convenience; the server decides.

## 3. Live execution updates

Server-sent events over `fetch`, so the request carries the bearer token
(`EventSource` cannot send one).

- The worker writes what the engine reports, as it reports it, to Redis: the
  case in progress, each finished step (`app/services/progress.py`).
- `GET …/executions/{id}/events` streams `state` (when it changes), `step` (each
  reported entry, numbered) and `end`. `GET …/executions/{id}/live` gives the
  state once.
- `useExecutionStream` reads the stream, reconnects from the last entry
  received (`Last-Event-ID`) with growing pauses, and after repeated failures
  falls back to polling the state. A missing or forbidden execution is not
  retried.

Nothing is invented while waiting: progress is the number of test cases that
have a result over the number selected; there is no percentage of a test, no
time remaining, no placeholder step. The page says when it is live,
reconnecting, or polling, and keeps the last state received on screen.

Values typed, expected and found are not in the live view; they are in the
report.

## 4. States every screen handles

| Situation | Behaviour |
|-----------|-----------|
| Loading | `LoadingState` with what is being loaded |
| Nothing yet | `EmptyState` saying what would put something there. Rates with nothing behind them are a dash, not 0% |
| Request failed | `ErrorState` with the server's message and reference; forms show field errors next to fields |
| Not signed in | Redirect to sign in, and back to the same page afterwards (only to paths inside the app) |
| Not allowed | Controls hidden by role; a refused request shows the server's message |
| Long execution | Live page stays connected; the server closes a stream after 30 minutes and the page reconnects |
| Cancelling | Confirmation first; then "cancelling" until the engine has stopped |
| Connection lost | Stated on the page; last state kept; automatic reconnect, then polling |

## 5. Layout

Desktop: header, fixed sidebar, content. The sidebar stays down to 769 px (a
tablet in portrait); below that it becomes a drawer behind a menu button.
Tables scroll inside their own container; nothing makes the page scroll
sideways. Light and dark themes follow the system setting. Spacing, type and
colour come from `styles/tokens.css` only.

## 6. Checks

| Check | Command | Result (2026-11-19) |
|-------|---------|---------------------|
| ESLint, Prettier, vue-tsc | `make fe-check` | Pass |
| Unit and component tests (Vitest) | `make fe-test` | 90 pass |
| Production build | `make fe-build` | Pass |
| Browser walk-through against the running stack | `make e2e` | 173 checks pass |

The walk-through (`e2e/walkthrough.py`) registers an account through the
registration page, prepares a project through the API, runs a suite from its
page, watches it live to the end, opens the report, shows the failure
screenshot, downloads the PDF and CSV, then visits 26 screens at 1366 px and
820 px. For each screen it checks the heading, that no error state is shown and
that the page does not scroll sideways, and at the end that no page raised a
script error and that a signed-out visitor is sent to sign in.

## 7. Known limitations

- No pages for agent workflow runs (see §1).
- Path rules for test selection are shown but edited through the API.
- The profile is read-only; name and email cannot be changed in the interface.
- Phone widths work through the drawer layout but were not part of the checks.
- Permissions in the interface were checked by component tests and the API's
  own tests; the walk-through runs as an owner.
