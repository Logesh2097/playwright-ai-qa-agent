# Database Design

PostgreSQL 16+ is the system of record. Redis holds only transient coordination data. Schema changes go through Alembic; nothing is created with `create_all`.

## 1. Conventions

- **Primary keys**: `id uuid`, UUIDv7 generated in the application (time-ordered, index-friendly).
- **Timestamps**: `created_at`, `updated_at` as `timestamptz NOT NULL DEFAULT now()`; `updated_at` maintained by the ORM.
- **Tenant key**: every tenant-owned table has `organization_id uuid NOT NULL REFERENCES organizations(id)`, denormalized even where it could be derived through a parent. This makes scoping and RLS uniform.
- **Status fields**: `text` with a `CHECK` constraint (easier to migrate than native enums).
- **Soft delete**: `deleted_at timestamptz NULL` on user-managed configuration (`organizations`, `projects`, `applications`, `environments`, `test_suites`, `test_cases`). Uniqueness constraints on those tables are partial: `WHERE deleted_at IS NULL`.
- **Foreign keys**: `ON DELETE RESTRICT` by default. `CASCADE` only from a parent to rows that have no meaning without it (noted below).
- **JSONB** is used for structured payloads validated by Pydantic in the application, never for data that needs relational constraints.
- **Secrets** are stored only as ciphertext columns (`bytea`). No plaintext secret column exists anywhere.

## 2. Entity overview

```
organizations ─┬─ organization_members ─ users ─ refresh_tokens
               ├─ api_keys
               ├─ integrations
               ├─ mcp_connections
               └─ projects ─┬─ applications ─ environments ─┬─ environment_allowed_origins
                            │                                └─ credentials
                            ├─ test_instructions ─ test_plans
                            ├─ test_suites ─ test_suite_cases ─┐
                            ├─ test_cases ─ test_case_versions ─ test_steps
                            └─ test_executions ─┬─ test_results ─ test_execution_steps
                                                ├─ test_findings
                                                ├─ test_evidence
                                                └─ test_reports
approvals, locator_heal_proposals, notifications, audit_logs, agent_execution_logs
```

Tables beyond the requested list, and why each exists:

| Table | Reason |
|-------|--------|
| `refresh_tokens` | Refresh-token rotation and revocation. |
| `password_reset_tokens` | Single-use password reset links. |
| `api_keys` | CI/CD authentication. |
| `environment_allowed_origins` | The URL allowlist needs row-level constraints and auditing. |
| `test_case_versions` | An execution must reference an immutable snapshot of the steps it ran. |
| `test_suite_cases` | Many-to-many between suites and cases, with ordering. |
| `test_findings` | Audit-mode observations that are not pass/fail results. |
| `approvals` | Human-in-the-loop decisions across several subject types. |
| `locator_heal_proposals` | Self-healing proposals awaiting verification and sign-off. |

## 3. Identity and tenancy

### users
| Column | Type | Notes |
|--------|------|-------|
| id | uuid PK | |
| email | citext | `UNIQUE NOT NULL` |
| password_hash | text | Argon2id |
| full_name | text | |
| is_active | boolean | default true |
| is_platform_admin | boolean | default false |
| email_verified_at | timestamptz | nullable |
| password_changed_at | timestamptz | nullable |
| last_login_at | timestamptz | nullable |

Not tenant-scoped; a user may belong to several organizations. Failed-login counting and temporary lockout live in Redis (see §9), not in this table.

### organizations
| Column | Type | Notes |
|--------|------|-------|
| id | uuid PK | |
| name | text | |
| slug | citext | unique where not deleted |
| settings | jsonb | limits: `max_concurrent_executions`, `monthly_llm_budget_usd`, `evidence_retention_days`, `auto_approve_non_production` |
| deleted_at | timestamptz | |

### organization_members
| Column | Type | Notes |
|--------|------|-------|
| id | uuid PK | |
| organization_id | uuid FK | CASCADE |
| user_id | uuid FK | nullable until an invite is accepted |
| invited_email | citext | |
| role | text | `owner`, `admin`, `qa_engineer`, `developer`, `viewer` (CHECK) |
| status | text | `invited`, `active`, `suspended` |
| invite_token_hash | text | nullable, unique |
| invite_expires_at | timestamptz | nullable |
| invited_by | uuid FK users | |

Constraints: `UNIQUE (organization_id, user_id)`; unique `(organization_id, invited_email)` among pending invitations; `CHECK (user_id IS NOT NULL OR status = 'invited')`. The service guarantees at least one active `owner` per organization, taking a row lock on the owner rows so concurrent requests cannot both remove the last one.

### refresh_tokens
`id`, `user_id` FK CASCADE, `token_hash` UNIQUE, `family_id` uuid, `expires_at`, `revoked_at`, `replaced_by` uuid, `user_agent`, `ip inet`.
Index: `(user_id)`, `(family_id)`. Reuse of a rotated token revokes the whole family.

### password_reset_tokens
`id`, `user_id` FK CASCADE, `token_hash` UNIQUE, `expires_at`, `used_at`, `created_at`. Requesting a new link invalidates earlier unused ones.

### api_keys
`id`, `organization_id`, `project_id` (nullable — org-wide if null), `name`, `key_prefix` (the organization part and the first characters of the secret, to recognise a key by), `key_hash` UNIQUE (SHA-256 of a high-entropy key), `scopes text[]`, `created_by`, `last_used_at`, `expires_at`, `revoked_at`.
Index: `(organization_id)`, `(key_hash)`.

## 4. Projects, applications, environments

### projects
`id`, `organization_id`, `name`, `slug`, `description`, `status` (`active`, `archived`), `settings jsonb` (default browser, default viewports, retry policy, approval policy), `created_by`, `deleted_at`.
Unique: `(organization_id, slug) WHERE deleted_at IS NULL`; `(id, organization_id)` as the target of composite foreign keys.

### project_members
`id`, `organization_id`, `project_id`, `user_id` FK CASCADE, `role` (`manager`, `member`), `added_by`, timestamps.
Unique: `(project_id, user_id)`. Composite FK `(project_id, organization_id) → projects` with CASCADE. Row-level security as for every tenant table. Grants access to a project; organization admins and owners need no row. Rows are removed when the user leaves the organization.

### applications
`id`, `organization_id`, `project_id` FK, `name`, `type` (`web`, `api`), `description`, `base_url`, `framework` (nullable), `auth_method` (`none`, `form_login`, `basic_auth`, `bearer_token`, `api_key`, `sso`, `other`), `default_browser_config jsonb`, `allowed_domains text[]` (host names; each covers its subdomains; the boundary for every environment URL), `status` (`active`, `archived`), `repo_full_name` (nullable, e.g. `acme/shop`), `created_by`, `deleted_at`.
Unique: `(project_id, name) WHERE deleted_at IS NULL`.

### environments
| Column | Type | Notes |
|--------|------|-------|
| id | uuid PK | |
| organization_id | uuid FK | |
| application_id | uuid FK | |
| name | text | e.g. `staging` |
| kind | text | `local`, `development`, `testing`, `staging`, `production`, `other` |
| base_url | text | must match an allowed origin |
| api_base_url | text | nullable |
| is_read_only | boolean | default true when `kind = 'production'` |
| requires_approval | boolean | default true when `kind = 'production'` |
| allow_private_network | boolean | default false; admin-only; permits RFC 1918 targets |
| default_viewport | jsonb | |
| variables | jsonb | `[{name, value} \| {name, secret_ref}]`; no secret values |
| auth_config | jsonb | method, login URL, default credential name, notes |
| allowed_actions | text[] | subset of `navigate`, `read`, `click`, `type`, `select`, `upload`, `download`, `api_read`, `api_write` |
| status | text | `active`, `disabled` |
| created_by | uuid FK users | |
| deleted_at | timestamptz | |

Unique: `(application_id, name) WHERE deleted_at IS NULL`.
`production` environments default to read-only and approval-required; only admins can create or change them, and changes are recorded in `audit_logs`.

### environment_allowed_origins
`id`, `organization_id`, `environment_id` FK CASCADE, `scheme` (`https`/`http`), `host`, `port`, `include_subdomains boolean`, `purpose` (`app`, `api`, `auth_provider`, `asset`).
Unique: `(environment_id, scheme, host, port)`.
Check: `host` contains no wildcard characters, path, or credentials.

### credentials
| Column | Type | Notes |
|--------|------|-------|
| id | uuid PK | |
| organization_id | uuid FK | |
| environment_id | uuid FK | CASCADE |
| name | text | |
| role_label | text | e.g. `customer`, `admin` — what test cases reference |
| kind | text | `username_password`, `bearer_token`, `api_key_header`, `basic_auth`, `cookie`, `totp_secret`, `secret_value` |
| source | text | `stored` (encrypted here) or `reference` (only a pointer is kept) |
| ciphertext | bytea | AES-256-GCM of a JSON payload; null for references |
| nonce | bytea | |
| wrapped_dek | bytea | data key wrapped by the master key |
| key_version | int | master key version, for rotation |
| external_ref | text | e.g. `env:STAGING_PASSWORD`; null for stored secrets |
| field_names | text[] | which fields the payload holds (not secret) |
| is_test_account | boolean | must be true (CHECK) |
| setup_instructions | text | shown to testers; scanned so it cannot contain the secret |
| login_recipe | jsonb | step DSL with `{{cred.*}}` placeholders; no secret values |
| last_rotated_at | timestamptz | |
| created_by | uuid FK | |

Unique: `(environment_id, name)`. Checks: exactly one of ciphertext / `external_ref` according to `source`; `is_test_account`. No API ever returns the decrypted payload. Credentials are hard-deleted with their environment, application or project.

## 5. Instructions, plans, cases

### test_instructions
| Column | Type | Notes |
|--------|------|-------|
| id | uuid PK | |
| organization_id, project_id, application_id, environment_id | uuid FK | |
| created_by | uuid FK users | |
| raw_text | text | the instruction, length-limited |
| test_types | text[] | subset of `ui`, `api`, `responsive`, `audit` |
| options | jsonb | viewports, credential roles, configured rules (e.g. loyalty formula) |
| status | text | `received`, `analysing`, `needs_clarification`, `analysed`, `approved`, `generating`, `awaiting_approval`, `completed`, `rejected`, `failed`, `cancelled` |
| analysis | jsonb | `RequirementAnalysis` |
| clarification_questions | jsonb | |
| clarification_answers | jsonb | |
| graph_thread_id | text | LangGraph thread (from Phase 8) |
| error | jsonb | `{code, message}` of a failed analysis |
| validation | jsonb | result of the deterministic readiness checks: `{ready, issues[]}` |
| analysis_source | text | `ai` or `human_edited` |
| analysis_model, analysis_prompt | text | which model and prompt version produced the analysis |
| analysed_at | timestamptz | |
| run_token | text | identifies the background run in progress (analysis or generation); a run whose token no longer matches discards its result |
| generation | jsonb | outcome of the last test-case generation: counts, and an error if it failed |
| llm_call_count | int | analysis runs started for this instruction; capped |

Index: `(project_id, created_at DESC)`, `(organization_id, status)`.
`options` currently holds `{rules: [{name, value, description}]}`; `clarification_answers` holds `{question_id, question, answer, answered_by, answered_at}` entries.

### test_plans
`id`, `organization_id`, `instruction_id` FK CASCADE, `version int`, `status` (`draft`, `validated`, `awaiting_approval`, `approved`, `rejected`, `superseded`), `summary text`, `scope jsonb`, `out_of_scope jsonb`, `strategy jsonb` (types, viewports, data strategy, roles), `risks jsonb`, `estimated_case_count int`, `model text`, `approved_by`, `approved_at`.
Unique: `(instruction_id, version)`.

### test_suites
`id`, `organization_id`, `project_id`, `application_id`, `name`, `description`, `source` (`ai_generated`, `manual`), `instruction_id` (nullable), `tags text[]`, `feature` (suites are grouped by it), `priority` (`p0`–`p3`, which suites run first), `status` (`draft`, `approved`, `archived`), `created_by`, `approved_by`, `approved_at`, `deleted_at`.
Unique: `(project_id, name) WHERE deleted_at IS NULL`.

### test_cases
| Column | Type | Notes |
|--------|------|-------|
| id | uuid PK | |
| organization_id, project_id, application_id | uuid FK | |
| key | text | human ID, e.g. `TC-0042`; unique per project |
| title | text | |
| description | text | |
| type | text | `ui`, `api`, `responsive`, `audit` |
| priority | text | `p0`–`p3` |
| tags | text[] | GIN index |
| status | text | `draft`, `needs_review`, `approved`, `deprecated` |
| category, severity | text | mirror the current version, for lists and filters (as do `title`, `type`, `priority`, `tags`) |
| environment_id | uuid FK | nullable |
| current_version_id | uuid FK | nullable, deferrable |
| original_version_id | uuid FK | the version exactly as the AI wrote it; null for manual cases |
| approved_version_id | uuid FK | the version a reviewer approved; kept when later versions are added |
| source | text | `ai_generated`, `manual` |
| instruction_id, plan_id | uuid FK | nullable |
| created_by | uuid FK | |
| deleted_at | timestamptz | |

Index: `(project_id, status)`, `(project_id, key)` unique.

### test_case_versions
Immutable once created.

`id`, `organization_id`, `test_case_id` FK CASCADE, `version int`, `source` (`ai`, `human`), `change_reason` (`created`, `edited`, `healed`, `regenerated`), `created_by`, `approved_by`, `approved_at`, and the complete content of the case at that version:

`title`, `description`, `category` (ten values, see [api-design.md](api-design.md) §6.2), `test_type`, `objective_id`, `preconditions jsonb`, `test_data jsonb`, `expected_result text`, `expected_source` (`plan_objective`, `configured_rule`, `human_specified`, `unknown`), `priority`, `severity`, `tags text[]`, `dependencies jsonb`, `automation_feasibility`, `automation_notes`, `verification jsonb`, `assumptions jsonb`, `validation jsonb` (issues found by the deterministic checks).

`automation jsonb` (nullable): the executable script of the case (`app.testing.automation.Script`), added in Phase 7. Null means the case is described but cannot be run. Changing it creates a new version like any other edit.

Reserved and still unused: `credential_role`, `parameters_schema`, `datasets`. `oracle_status` is set from the expected-result source.

Unique: `(test_case_id, version)`.
Check `expected_result_matches_source`: `(expected_source = 'unknown') = (expected_result IS NULL)` — a result is either known and present, or unknown and absent.

### test_suite_cases
`suite_id` FK CASCADE, `test_case_id` FK CASCADE, `organization_id`, `position int` (execution order), `approved_version_id` (set when the suite is approved: exactly which version of the case was approved with it).
PK: `(suite_id, test_case_id)`. Unique: `(suite_id, position)` deferrable.

### test_steps
| Column | Type | Notes |
|--------|------|-------|
| id | uuid PK | |
| organization_id | uuid FK | |
| test_case_version_id | uuid FK | CASCADE |
| position | int | |
| action | text | DSL action (`navigate`, `click`, `fill`, `assert_text`, `api_request`, …) |
| target | jsonb | As of Phase 5 `{"description": "the Email field"}`: the element in words. A locator (strategy, value, fallbacks) is added when the browser integration has seen the page. |
| args | jsonb | `{"value": …}`; may contain `{{data.*}}` / `{{cred.*}}` placeholders |
| description | text | human-readable |
| is_assertion | boolean | |
| expected | jsonb | nullable |
| expected_source | text | `user_specified`, `derived_rule`, `dom_constraint`, `llm_inferred` |
| side_effect | text | `none`, `create`, `update`, `delete`, `external` |
| timeout_ms | int | |
| continue_on_failure | boolean | default false |

Unique: `(test_case_version_id, position)`.
Check: `NOT is_assertion OR expected_source IS NOT NULL`.

## 6. Execution

### test_executions
| Column | Type | Notes |
|--------|------|-------|
| id | uuid PK | |
| organization_id, project_id, environment_id | uuid FK | |
| suite_id, instruction_id | uuid FK | nullable |
| trigger | text | `manual`, `api`, `ci`, `github_webhook` |
| triggered_by | uuid FK users | nullable |
| api_key_id | uuid FK | nullable |
| status | text | `queued`, `awaiting_approval`, `preparing`, `running`, `analysing`, `reporting`, `completed`, `failed`, `cancelling`, `cancelled`, `timed_out` |
| verdict | text | nullable: `passed`, `failed`, `flaky`, `error`, `inconclusive`, `needs_review` (Phase 9) |
| config | jsonb | browser, viewports, retry policy, video, selected case IDs |
| git_context | jsonb | repo, sha, branch, PR number |
| totals | jsonb | counts per result status |
| runner_id | text | |
| mcp_connection_id | uuid FK | nullable |
| graph_thread_id | text | |
| idempotency_key | text | nullable |
| timeout_s | int | |
| llm_cost_usd | numeric(10,4) | |
| queued_at, started_at, finished_at, cancel_requested_at | timestamptz | |
| cancelled_by | uuid FK | nullable |
| error | jsonb | |

Indexes: `(project_id, created_at DESC)`, `(organization_id, status) WHERE status IN ('queued','preparing','running','analysing')`, `(environment_id, created_at DESC)`, unique `(organization_id, idempotency_key) WHERE idempotency_key IS NOT NULL`.

`status` is the lifecycle; `verdict` is the outcome. An execution can be `completed` with verdict `failed`.

### test_results
One row per case × dataset row × viewport × browser.

`id`, `organization_id`, `execution_id` FK CASCADE, `test_case_id`, `test_case_version_id`, `dataset_index int`, `viewport text`, `browser text`, `status` (`pending`, `running`, `passed`, `failed`, `flaky`, `error`, `skipped`, `blocked`, `inconclusive`, `needs_review`), `attempt_count int`, `failure_class` (`assertion`, `locator`, `timeout`, `navigation`, `network`, `app_error`, `test_data`, `environment`, `infrastructure`, `unknown`), `failure_category` (Phase 9: `application_failure`, `test_assertion_failure`, `test_automation_failure`, `browser_infrastructure_failure`, `network_failure`, `authentication_failure`, `inconclusive`; set by rules, see [result-validation-and-healing.md](result-validation-and-healing.md) §2), `failure_summary text`, `ai_analysis jsonb` (a labelled suggestion; never read by verdict or classification logic), `healed boolean` (a locator was replaced during the run), `duration_ms`, `started_at`, `finished_at`. `details jsonb` (Phase 7): what the browser reported during the case (console errors, failed and blocked requests), whether cleanup worked, notes about evidence. Observations, not a verdict. From Phase 9 it also holds `classification` (the category and the facts it rests on) and `verification` (how many assertions of which kind were evaluated and held).

As of Phase 7 an execution has one row per case: `dataset_index` is always 0 and `viewport` holds the configured size such as `1280x720`.
Unique: `(execution_id, test_case_id, dataset_index, viewport, browser)`.
Index: `(test_case_id, finished_at DESC)` for history and flakiness queries; `(execution_id, status)`.

### test_execution_steps
`id`, `organization_id`, `execution_id`, `result_id` FK CASCADE, `test_step_id` FK (unused: steps come from the script, see `test_case_versions.automation`), `attempt int`, `position int`, `phase` (`setup` for sign-in, `main`, `cleanup`), `action text`, `description text`, `status` (`passed`, `failed`, `error`, `skipped`), `page_url text`, `resolved_locator jsonb` (the locator as used and how many elements it matched), `expected jsonb`, `actual jsonb`, `error_type text`, `error_message text` (redacted), `script_index int` (Phase 9: which step of the script, 0-based within its phase; null for sign-in), `healed_from jsonb` (the script's own locator when a replacement was used), `started_at`, `finished_at`, `duration_ms`.

`position` counts across the phases of one attempt, so it is unique together with `result_id` and `attempt`.
Unique: `(result_id, attempt, position)`.
Index: `(execution_id)`.

### test_data_sets (Phase 7)
`id`, `organization_id`, `project_id`, `environment_id` (nullable), `name`, `description`, `values jsonb` (name → text), `is_synthetic boolean`, `created_by`.
Unique: `(project_id, name)` where `environment_id IS NULL`; `(project_id, name, environment_id)` otherwise. A set for one environment replaces the general set of the same name there.
Check `synthetic_only`: `is_synthetic` must be true.

### test_files (Phase 7)
Files a test may upload into the application under test.
`id`, `organization_id`, `project_id`, `name`, `content_type`, `size_bytes`, `sha256`, `content bytea` (loaded only when a run needs it), `is_synthetic boolean`, `created_by`.
Unique: `(project_id, name)`. Check `synthetic_only`.

Both are tenant tables with row-level security.

### test_findings
`id`, `organization_id`, `execution_id` FK CASCADE, `result_id` (nullable), `kind` (`console_error`, `failed_request`, `dead_control`, `validation_gap`, `layout_overflow`, `js_exception`), `severity` (`info`, `low`, `medium`, `high`), `detection` (`deterministic`, `heuristic`, `ai`), `url text`, `details jsonb`, `evidence_id` (nullable).
Index: `(execution_id, kind)`.

### test_evidence
`id`, `organization_id`, `execution_id` FK CASCADE, `result_id`, `execution_step_id` (both nullable), `kind` (`screenshot`, `video`, `trace`, `console_log`, `network_log`, `dom_snapshot`, `api_exchange`), `storage_key text` UNIQUE, `content_type`, `size_bytes bigint`, `sha256 text`, `redaction_status` (`not_required`, `applied`, `failed_quarantined`), `viewport text`, `captured_at`, `expires_at`.
Index: `(execution_id)`, `(result_id)`, `(expires_at)` for the retention job.
Storage keys always begin with `{organization_id}/{execution_id}/`.
From Phase 10: kinds written are `screenshot`, `video`, `trace`, `console_log` and `network_log`; log files have `redaction_status = applied`; `expires_at` depends on the kind; an hourly job deletes expired rows and their files ([reports-and-evidence.md](reports-and-evidence.md) §2).

### test_reports
`id`, `organization_id`, `execution_id` FK CASCADE, `format` (`json`, `html`, `junit`), `status` (`pending`, `ready`, `failed`), `summary jsonb`, `storage_key text`, `generated_at`, `share_token_hash text` (nullable), `share_expires_at`.
Unique: `(execution_id, format)`.
Holds the report the agent workflow stores at the end of a run. Execution reports and their exports (Phase 10) are generated on request and not stored.

### locator_heal_proposals
A replacement the engine found for a locator that matched nothing (rewritten in Phase 9). A row changes nothing about a test case by itself.

`id`, `organization_id`, `test_case_id` FK CASCADE, `test_case_version_id` FK CASCADE, `result_id` FK SET NULL, `execution_step_id` FK SET NULL, `script_phase` (`setup`, `main`, `cleanup`), `script_index int`, `action text`, `old_locator jsonb`, `new_locator jsonb`, `confidence numeric(3,2)`, `rationale text`, `held_back text` (why the engine did not use it by itself), `status` (`proposed` found only, `verified` used in the run and the step passed, `accepted`, `rejected`), `verified_by_rerun boolean`, `decided_by`, `decided_at`, `test_step_id` FK (unused).

Index: `(organization_id, status)`, `(test_case_id, created_at)`.
Index: `(organization_id, status)`.

### approvals
`id`, `organization_id`, `subject_type` (`test_plan`, `test_cases`, `execution`, `heal_proposal`), `subject_id uuid`, `reason_code` (`production_environment`, `mutating_steps`, `llm_inferred_oracle`, `policy`), `payload jsonb`, `status` (`pending`, `approved`, `rejected`, `expired`), `requested_by`, `decided_by`, `decided_at`, `decision_note text`, `expires_at`.
Index: `(organization_id, status)`, `(subject_type, subject_id)`.

## 7. Integrations and operations

### mcp_connections
| Column | Type | Notes |
|--------|------|-------|
| id | uuid PK | |
| organization_id | uuid FK | |
| project_id | uuid FK | nullable (org default) |
| name | text | |
| mode | text | `managed` (runner starts the server), `external` (operator-provided URL) |
| transport | text | `streamable_http` |
| endpoint_url | text | external mode only; must pass the same URL validation as environments |
| auth_secret_ciphertext, nonce, wrapped_dek, key_version | | external mode only |
| allowed_tools | text[] | tool allowlist applied by the MCP client |
| browser | text | `chromium`, `firefox`, `webkit` |
| capabilities | text[] | opt-in capability sets |
| status | text | `active`, `disabled`, `unhealthy` |
| discovered_tools | jsonb | last `tools/list` result |
| server_version | text | |
| last_health_check_at | timestamptz | |
| last_error | text | |

Unique: `(organization_id, name)`. Per-execution MCP sessions are runtime state; they are recorded on `test_executions` (`runner_id`, `mcp_connection_id`) and in `agent_execution_logs`, not in a separate table.

### integrations
`id`, `organization_id`, `project_id` (nullable), `provider` (`github`, `webhook`, `email`), `status` (`pending`, `active`, `error`, `disabled`), `config jsonb` (repo mappings, event selection, target URL), `secret_ciphertext`/`nonce`/`wrapped_dek`/`key_version`, `external_account text`, `installation_id bigint` (GitHub App), `created_by`.
Unique: `(organization_id, provider, installation_id)`.
From Phase 11 a GitHub connection is a row with `provider = github`, `project_id` null, `config = {name, auth: token|app, app_id?}`, the encrypted token or app private key, and the account in `external_account`.

### github_repositories (Phase 11)
One repository a project has linked, and what that link allows.

`id`, `organization_id`, `project_id` FK CASCADE, `integration_id` FK CASCADE, `github_repo_id bigint` (GitHub's permanent ID), `full_name`, `default_branch`, `is_private`, `hook_token text` UNIQUE (in the webhook address), `secret_ciphertext`/`nonce`/`wrapped_dek`/`key_version` (the webhook secret), `enabled`, `on_pull_request`, `on_push`, `branches text[]`, `environment_id` FK SET NULL, `suite_ids jsonb`, `selection_mode` (`all`, `affected`), `path_rules jsonb`, `status_enabled`, `comment_enabled`, `created_by`. Every switch defaults to off.
Unique: `(project_id, github_repo_id)`.

### github_deliveries (Phase 11)
One webhook delivery that passed signature verification.

`id`, `organization_id`, `repository_id` FK CASCADE, `delivery_id text` (GitHub's), `event`, `action`, `outcome` (`accepted`, `ignored`, `duplicate`, `failed`, `processed`), `reason`, `subject jsonb` (pull request number, commit, branches, the selection made; no payload), `execution_ids jsonb`, `created_at`.
Unique: `(repository_id, delivery_id)`: the duplicate check.

Executions started by CI or a webhook have `trigger` `ci` or `github_webhook`, `triggered_by` null, `api_key_id` for CI, `git_context jsonb` (repository, commit, branch, pull request, selection, what was published) and `idempotency_key`.

### notifications
`id`, `organization_id`, `user_id` (nullable for webhook deliveries), `channel` (`in_app`, `email`, `webhook`), `event_type`, `subject_type`, `subject_id`, `title`, `body`, `payload jsonb`, `status` (`pending`, `sent`, `failed`, `read`), `attempts int`, `next_attempt_at`, `sent_at`, `read_at`.
Index: `(user_id, status, created_at DESC)`, `(status, next_attempt_at) WHERE status = 'pending'`.

### audit_logs
Append-only. `id`, `organization_id` (nullable for platform events), `actor_type` (`user`, `api_key`, `system`, `agent`), `actor_id`, `action` (e.g. `credential.create`, `execution.approve`), `resource_type`, `resource_id`, `request_id`, `ip inet`, `user_agent`, `changes jsonb` (redacted before/after), `created_at`.
Index: `(organization_id, id)` (IDs are time-ordered, so this serves newest-first paging), `(resource_type, resource_id)`. The application role has `INSERT` and `SELECT` only; `UPDATE` and `DELETE` are revoked. Monthly range partitioning is deferred until volume calls for it (Phase 13).

### agent_execution_logs
From Phase 4 one row is written per call to the AI provider (successful or not), in its own transaction, with `event_type = 'llm_call'`, the model, token counts, cost (if prices are configured), latency, the prompt version in `tool_name`, and the outcome in `status`.

`id`, `organization_id`, `thread_id`, `execution_id`, `instruction_id` (nullable), `node text`, `event_type` (`node_start`, `node_end`, `llm_call`, `tool_call`, `interrupt`, `resume`, `error`, `budget_exceeded`), `model`, `prompt_tokens`, `completion_tokens`, `cost_usd numeric(10,6)`, `latency_ms`, `tool_name`, `tool_args jsonb` (redacted), `tool_result_summary text`, `status`, `error jsonb`, `created_at`.
Index: `(execution_id, created_at)`, `(organization_id)`. Partitioning deferred as for `audit_logs`.

From Phase 8 the agent workflow also writes `node_start`, `node_end` (with `latency_ms` and the outcome in `status`), `interrupt`, `resume` and `error` rows, with `thread_id` set to the workflow run's ID. Extra detail (which cases a retry covers, why) is in `tool_args`.

### workflow_runs (Phase 8)
One run of the agent workflow. Its ID is the LangGraph thread ID.

`id`, `organization_id`, `project_id`, `environment_id`, `instruction_id` (nullable), `suite_id` (nullable), `mode` (`generate_and_run`, `run_existing`), `status` (`queued`, `running`, `waiting_for_clarification`, `waiting_for_approval`, `cancelling`, `completed`, `failed`, `rejected`, `cancelled`), `current_node`, `started_by`, `options jsonb`, `execution_ids jsonb` (every attempt, in order), `retry_count`, `verdict`, `pending jsonb` (what the run waits for), `error jsonb`, `report jsonb`, `report_id` (nullable FK `test_reports`), `started_at`, `finished_at`, `heartbeat_at`.
Index: `(project_id, created_at)`, `(organization_id, status)`.

An execution started by a run has `test_executions.graph_thread_id` set to the run's ID and its attempt number in `config.attempt`. An approval the run waits for is an `approvals` row with `subject_type = 'test_cases'` and `subject_id` = the run's ID; its `payload` holds the reasons and whether an admin is needed.

### workflow_checkpoints, workflow_checkpoint_writes (Phase 8)
LangGraph's checkpoints, stored by the application's own checkpointer (`app/workflow/checkpoint.py`) instead of `langgraph-checkpoint-postgres`, so that they are tenant tables under row-level security and need no second database driver.

`workflow_checkpoints`: `id`, `organization_id`, `thread_id`, `checkpoint_ns`, `checkpoint_id`, `parent_checkpoint_id`, `checkpoint_type`, `checkpoint bytea`, `metadata_type`, `checkpoint_metadata bytea`, `created_at`. Unique `(thread_id, checkpoint_ns, checkpoint_id)`.

`workflow_checkpoint_writes`: `id`, `organization_id`, `thread_id`, `checkpoint_ns`, `checkpoint_id`, `task_id`, `task_path`, `idx`, `channel`, `value_type`, `value bytea`, `created_at`. Unique `(thread_id, checkpoint_ns, checkpoint_id, task_id, idx)`.

The bytes are LangGraph's own serialization of the workflow state, which holds identifiers and summaries only (see [agent-workflow.md](agent-workflow.md) §5). Nothing prunes these tables yet.

## 8. Tenant isolation

Implemented in Phase 2. Three layers, each tested on its own.

1. **Application layer.** `TenantRepository` is constructed with an organization ID and adds `organization_id = :org` to every query it builds. Adding an entity of another organization raises. Organization-scoped API routes resolve the caller's membership first; a non-member gets 404.

2. **Row-level security.** Every table with an `organization_id` column has RLS enabled *and forced*, with the policy

   ```sql
   USING (organization_id = app_current_org()) WITH CHECK (organization_id = app_current_org())
   ```

   `app_current_org()` reads the transaction-local setting `app.current_org`, which the API sets with `set_config(..., true)` once membership is verified. An unset value is `NULL` and matches nothing, so the default is "see nothing". Because the setting is transaction-local, a pooled connection cannot carry it into another request.

   Special cases:

   | Table | Policy |
   |-------|--------|
   | `organizations` | The current organization, plus those the current user (`app.current_user_id`) is an active member of — needed to list "my organizations". |
   | `organization_members` | Rows of the current organization, rows of the current user, and the single invitation whose token hash equals `app.invite_token_hash` (so an invitee can find their invitation before they belong to the organization). |
   | `audit_logs` | Anyone may insert; only the current organization's rows can be selected. |
   | `users`, `refresh_tokens`, `password_reset_tokens` | Not tenant data; no RLS. Access is by user ID or token hash only. |

3. **Database roles.** PostgreSQL superusers and table owners with `BYPASSRLS` ignore policies, so two roles are used:

   | Role | Used by | Rights |
   |------|---------|--------|
   | Migration role (`MIGRATION_DATABASE_URL`) | the migration job only | Owns the schema; runs DDL. |
   | Application role (`DATABASE_URL`) | API and worker | `LOGIN NOSUPERUSER NOBYPASSRLS`; `SELECT/INSERT/UPDATE/DELETE` on tables; no DDL; no `UPDATE/DELETE` on `audit_logs`. |

   `python -m app.db.bootstrap` (run by the migration job before Alembic) creates the application role from the credentials in `DATABASE_URL`, idempotently.

4. **Composite foreign keys.** RLS does not apply to foreign-key checks, so the parent chain is protected by constraints: `applications (project_id, organization_id) → projects (id, organization_id)`, and likewise `environments → applications`, and `test_instructions`, `test_suites`, `test_cases`, `test_executions → projects` (executions also `→ environments`). A row cannot reference a parent in another organization even if application code passes a foreign ID. Other cross-table references are validated in services as each feature lands.

5. **Evidence paths** are prefixed by organization ID and checked on every download (Phase 10).

Timestamps (`created_at`, `updated_at`) are supplied by the application as well as by column defaults. This avoids `INSERT … RETURNING`, which under RLS requires the new row to be readable — audit rows for events outside the current organization are not.

## 9. Redis usage

| Purpose | Key pattern | Notes |
|---------|-------------|-------|
| Job queue | arq-managed | Generation, execution, report, notification, cleanup jobs. |
| Execution lock | `lock:execution:{id}` | Ensures one worker advances a given graph thread. TTL with heartbeat. |
| Concurrency limit | `sem:org:{id}:executions` | Per-organization running-execution counter. |
| Cancellation flag | `cancel:execution:{id}` | Checked between steps by worker and runner. |
| Live events | `events:execution:{id}` (pub/sub) | Fan-out to SSE; last N events also kept in a capped list for reconnects. |
| Rate limiting | `rl:{scope}:{sha256(subject)}` | Fixed-window counters: failed logins per email (lockout), logins / registrations / reset requests per address. Subjects are hashed. |
| Session deny-list | `auth:revoked_session:{session_id}` | Set on logout, password change and refresh-token reuse; TTL slightly over the access-token lifetime. Makes revocation immediate. |
| Stream tickets | `sse-ticket:{token}` | One-time, 60-second tickets for SSE authentication. |
| Dashboard cache | `dash:{org}:{project}:{query-hash}` | 60-second TTL. |

Redis is never the only copy of anything that matters: losing it loses queued jobs (re-enqueued from `test_executions.status = 'queued'` by a reconciler) and live events (recoverable from Postgres).

## 10. Migration and data lifecycle

- One Alembic revision per phase at minimum; revisions are reviewed and must have a working downgrade until first production release.
- Partition maintenance (creating next month's partitions) is a scheduled job.
- Retention: evidence per organization setting (default 30 days); `agent_execution_logs` 90 days; `audit_logs` 1 year minimum. Values are open for confirmation.
