/** Shapes for executions in progress, integrations and organization settings. */
import type { OrgRole } from '@/types/api'
import type { ExecutionSummary, ResultRates, TrendPoint } from '@/types/reports'

export interface RunConfigInput {
  browser?: 'chromium' | 'firefox' | 'webkit'
  retry?: { max_attempts: number }
  evidence?: {
    screenshots?: CaptureMode
    video?: CaptureMode
    trace?: CaptureMode
    logs?: CaptureMode
  }
  healing?: { mode: 'off' | 'propose' | 'recover' }
}
export type CaptureMode = 'off' | 'on_failure' | 'always'

export interface ExecutionStartInput {
  environment_id: string
  suite_id?: string
  case_ids?: string[]
  config?: RunConfigInput
  approve_sensitive_actions?: boolean
}

export interface LiveResult {
  id: string
  case_key: string
  title: string
  status: string
  attempts: number
  failure_summary: string | null
}

export interface LiveStep {
  position: number
  phase: string
  action: string
  description: string | null
  status: string
  duration_ms: number
  page_url: string | null
  error_type: string | null
  error_message: string | null
}

export interface LiveState {
  id: string
  status: string
  verdict: string | null
  finished: boolean
  totals: Record<string, number>
  started_at: string | null
  finished_at: string | null
  results: LiveResult[]
  current: { result_id: string; attempt: number; step: LiveStep | null } | null
}

/** One entry the engine reported. */
export interface LiveEvent extends Partial<LiveStep> {
  seq: number
  at: string
  type: 'case_started' | 'step_finished' | 'case_finished'
  result_id: string
  attempt: number
  final?: boolean
}

export interface OrganizationDashboard {
  window_days: number
  truncated: boolean
  total_projects: number
  active_projects: number
  total_executions: number
  finished_executions: number
  results: ResultRates
  points: TrendPoint[]
  frequently_failing: {
    case_id: string
    case_key: string | null
    title: string | null
    runs: number
    failures: number
    failure_rate: number | null
    last_status: string
    most_common_category: string | null
    project_id: string | null
    project_name: string | null
  }[]
  recent_executions: (ExecutionSummary & { project_id: string; project_name: string })[]
}

export interface Organization {
  id: string
  name: string
  slug: string
  role: OrgRole
  created_at: string
}

export interface Member {
  id: string
  user_id: string | null
  email: string
  full_name: string | null
  role: OrgRole
  status: 'invited' | 'active' | 'suspended'
  created_at: string
}

export interface AuditEntry {
  id: string
  created_at: string
  actor_type: string
  actor_id: string | null
  action: string
  resource_type: string | null
  resource_id: string | null
  request_id: string | null
  ip: string | null
  changes: Record<string, unknown>
}

export interface AuditPage {
  items: AuditEntry[]
  next_cursor: string | null
}

export interface RolePermissions {
  role: OrgRole
  permissions: string[]
}

export interface BrowserStatus {
  configured: boolean
  connection: Record<string, unknown> | null
  restricted: boolean
  tools: {
    name: string
    category: 'read_only' | 'reversible' | 'state_changing' | 'sensitive'
    action: string | null
    arguments: string[]
    availability: 'available' | 'needs_approval' | 'forbidden' | 'internal'
    enabled: boolean
    discovered: boolean | null
  }[]
}

export interface ApiKey {
  id: string
  name: string
  key_prefix: string
  scopes: string[]
  project_id: string | null
  created_at: string
  last_used_at: string | null
  expires_at: string | null
  revoked_at: string | null
  /** Present only in the answer to creating the key. */
  key?: string
}

export interface GithubConnection {
  id: string
  name: string
  auth: 'token' | 'app'
  account: string | null
  status: string
  created_at: string
}

export interface GithubAvailableRepository {
  id: number
  full_name: string
  default_branch: string
  private: boolean
  archived: boolean
}

export interface GithubLink {
  id: string
  connection_id: string
  github_repo_id: number
  full_name: string
  default_branch: string
  private: boolean
  enabled: boolean
  on_pull_request: boolean
  on_push: boolean
  branches: string[]
  environment_id: string | null
  suite_ids: string[]
  selection_mode: 'all' | 'affected'
  path_rules: { pattern: string; suite_ids: string[] }[]
  status_enabled: boolean
  comment_enabled: boolean
  webhook_url: string
  created_at: string
  /** Present only right after linking or rotating. */
  webhook_secret?: string
}

export interface GithubDelivery {
  id: string
  delivery_id: string
  event: string
  action: string | null
  outcome: string
  reason: string | null
  subject: Record<string, unknown>
  execution_ids: string[]
  received_at: string
}

export interface HealProposal {
  id: string
  test_case_id: string | null
  result_id: string | null
  script_phase: string
  script_index: number | null
  action: string | null
  old_locator: Record<string, unknown>
  new_locator: Record<string, unknown>
  confidence: number | null
  rationale: string | null
  held_back: string | null
  status: 'proposed' | 'verified' | 'accepted' | 'rejected'
  verified_by_rerun: boolean
  created_at: string
}

export interface FailureReport {
  record: Record<string, unknown>
  ai_suggestion: Record<string, unknown> | null
}
