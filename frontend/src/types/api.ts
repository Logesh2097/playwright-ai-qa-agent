/**
 * Shapes of API requests and responses, written by hand to match the backend's
 * OpenAPI schema (/api/docs). Keep them in step when an endpoint changes.
 */

export interface ErrorItem {
  field: string | null
  code: string
  message: string
}

/** Error envelope returned by every failing request. */
export interface ProblemDetail {
  type: string
  title: string
  status: number
  code: string
  detail: string | null
  errors: ErrorItem[]
  request_id: string | null
}

export interface Page<T> {
  items: T[]
  next_cursor: string | null
}

export interface Meta {
  name: string
  version: string
  environment: string
  api_version: string
}

export interface DependencyCheck {
  status: 'ok' | 'error'
  latency_ms: number
  error: string | null
}

export interface Readiness {
  status: 'ready' | 'not_ready'
  checks: Record<string, DependencyCheck>
}

// --- authentication ---------------------------------------------------------------------

export type OrgRole = 'owner' | 'admin' | 'qa_engineer' | 'developer' | 'viewer'

export interface User {
  id: string
  email: string
  full_name: string
  is_active: boolean
  created_at: string
  last_login_at: string | null
}

export interface Membership {
  organization_id: string
  organization_name: string
  organization_slug: string
  role: OrgRole
}

export interface TokenResponse {
  access_token: string
  token_type: 'bearer'
  expires_in: number
}

export interface Session extends TokenResponse {
  user: User
  memberships: Membership[]
}

export interface Me {
  user: User
  memberships: Membership[]
}

export interface OrgMember {
  id: string
  user_id: string | null
  email: string
  full_name: string | null
  role: OrgRole
  status: 'invited' | 'active' | 'suspended'
  created_at: string
}

// --- projects ---------------------------------------------------------------------------

export type ProjectRole = 'manager' | 'member'

export interface Project {
  id: string
  organization_id: string
  name: string
  slug: string
  description: string | null
  status: 'active' | 'archived'
  created_by: string | null
  created_at: string
  updated_at: string
  member_count: number
  application_count: number
  my_role: ProjectRole | null
  can_manage: boolean
}

export interface ProjectMember {
  user_id: string
  email: string
  full_name: string
  role: ProjectRole
  organization_role: string
  added_at: string
}

// --- applications -----------------------------------------------------------------------

export type ApplicationType = 'web' | 'api'
export type AuthMethod =
  'none' | 'form_login' | 'basic_auth' | 'bearer_token' | 'api_key' | 'sso' | 'other'
export type BrowserName = 'chromium' | 'firefox' | 'webkit'

export interface BrowserConfig {
  browser: BrowserName
  viewport: { width: number; height: number }
  locale?: string | null
  timezone?: string | null
}

export interface Application {
  id: string
  project_id: string
  name: string
  description: string | null
  base_url: string
  type: ApplicationType
  framework: string | null
  auth_method: AuthMethod
  default_browser_config: Partial<BrowserConfig>
  allowed_domains: string[]
  status: 'active' | 'archived'
  environment_count: number
  created_at: string
  updated_at: string
}

export interface ApplicationInput {
  name: string
  description?: string | null
  base_url: string
  type: ApplicationType
  framework?: string | null
  auth_method: AuthMethod
  default_browser_config?: BrowserConfig
  allowed_domains?: string[]
  status?: 'active' | 'archived'
}

// --- environments -----------------------------------------------------------------------

export type EnvironmentKind =
  'local' | 'development' | 'testing' | 'staging' | 'production' | 'other'
export type BrowserAction =
  | 'navigate'
  | 'read'
  | 'click'
  | 'type'
  | 'select'
  | 'upload'
  | 'download'
  | 'api_read'
  | 'api_write'
export type OriginPurpose = 'app' | 'api' | 'auth_provider' | 'asset'

export interface AllowedOrigin {
  origin: string
  scheme: string
  host: string
  port: number
  purpose: OriginPurpose
}

export interface EnvironmentVariable {
  name: string
  value?: string | null
  secret_ref?: string | null
}

export interface EnvironmentAuthConfig {
  method: AuthMethod
  login_url?: string | null
  default_credential?: string | null
  notes?: string | null
}

export interface Environment {
  id: string
  application_id: string
  name: string
  kind: EnvironmentKind
  base_url: string
  api_base_url: string | null
  allowed_origins: AllowedOrigin[]
  variables: EnvironmentVariable[]
  auth_config: EnvironmentAuthConfig
  allowed_actions: BrowserAction[]
  is_read_only: boolean
  requires_approval: boolean
  allow_private_network: boolean
  status: 'active' | 'disabled'
  credential_count: number
  created_at: string
  updated_at: string
}

export interface EnvironmentInput {
  name: string
  kind: EnvironmentKind
  base_url: string
  api_base_url?: string | null
  additional_origins?: { url: string; purpose: OriginPurpose }[]
  variables?: EnvironmentVariable[]
  auth_config?: EnvironmentAuthConfig
  allowed_actions?: BrowserAction[]
  is_read_only?: boolean
  requires_approval?: boolean
  allow_private_network?: boolean
  status?: 'active' | 'disabled'
}

export interface AccessCheck {
  reachable: boolean
  final_url: string | null
  status: number | null
  duration_ms: number
  error_code: string | null
  error: string | null
  hops: {
    url: string
    status: number | null
    blocked_code: string | null
    blocked_reason: string | null
  }[]
}

// --- credentials ------------------------------------------------------------------------

export type CredentialKind =
  | 'username_password'
  | 'bearer_token'
  | 'api_key_header'
  | 'basic_auth'
  | 'cookie'
  | 'totp_secret'
  | 'secret_value'

/** Metadata only. The API has no field that carries a secret value back. */
export interface Credential {
  id: string
  environment_id: string
  name: string
  role_label: string
  kind: CredentialKind
  source: 'stored' | 'reference'
  field_names: string[]
  external_ref: string | null
  is_test_account: boolean
  setup_instructions: string | null
  login_recipe: Record<string, unknown>[]
  last_rotated_at: string | null
  created_at: string
  updated_at: string
}

export interface CredentialInput {
  name: string
  role_label: string
  kind: CredentialKind
  secret?: Record<string, string>
  external_ref?: string
  is_test_account: boolean
  setup_instructions?: string | null
}

// --- instructions and requirement analysis -------------------------------------------------

export type InstructionStatus =
  | 'received'
  | 'analysing'
  | 'needs_clarification'
  | 'analysed'
  | 'generating'
  | 'awaiting_approval'
  | 'approved'
  | 'rejected'
  | 'failed'
  | 'cancelled'
  | 'completed'

export type ExpectationSource =
  'stated_in_instruction' | 'provided_configuration' | 'clarification_answer' | 'unknown'

export interface RequirementAnalysis {
  summary: string
  feature_name: string | null
  is_feature_identifiable: boolean
  application_context: { application: string; module: string | null; user_actions: string[] }
  preconditions: string[]
  test_objectives: { id: string; title: string; category: string }[]
  expected_behavior: {
    objective_id: string
    description: string | null
    source: ExpectationSource
  }[]
  business_rules: {
    name: string
    description: string
    source: ExpectationSource
    value_known: boolean
  }[]
  validation_requirements: string[]
  negative_scenarios: string[]
  edge_cases: string[]
  required_test_data: {
    name: string
    kind: string
    description: string
    credential_role: string | null
    values: string[]
    source: ExpectationSource
  }[]
  dependencies: string[]
  constraints: string[]
  assumptions: string[]
  missing_information: { topic: string; why_needed: string; blocking: boolean }[]
  clarification_questions: ClarificationQuestion[]
  requires_data_changes: boolean
  mentioned_targets: string[]
  ignored_instructions: string[]
}

export interface ClarificationQuestion {
  id: string
  question: string
  options: string[]
  related_to: string | null
}

export interface Issue {
  code: string
  severity: 'blocking' | 'warning'
  message: string
  subject?: string | null
  step?: number | null
}

export interface ConfiguredRule {
  name: string
  value: string
  description?: string | null
}

export interface Instruction {
  id: string
  project_id: string
  application_id: string
  environment_id: string
  created_by: string | null
  text: string
  test_types: string[]
  options: { rules: ConfiguredRule[] }
  status: InstructionStatus
  analysis: RequirementAnalysis | null
  analysis_source: 'ai' | 'human_edited' | null
  analysis_model: string | null
  analysed_at: string | null
  validation: { ready: boolean; issues: Issue[] } | null
  clarification_questions: ClarificationQuestion[]
  clarification_answers: { question_id: string; question: string; answer: string }[]
  error: { code: string; message: string } | null
  generation: {
    state?: string
    mode?: string
    generated?: number
    regenerated?: number
    duplicates_removed?: number
    needs_attention?: number
    partial?: boolean
    error?: { code: string; message: string }
  } | null
  usage: { llm_calls: number; input_tokens: number; output_tokens: number; cost_usd: string | null }
  created_at: string
  updated_at: string
}

export interface InstructionInput {
  application_id: string
  environment_id: string
  text: string
  test_types: string[]
  options: { rules: ConfiguredRule[] }
}

// --- test cases and suites ----------------------------------------------------------------

export type CaseCategory =
  | 'positive'
  | 'negative'
  | 'boundary'
  | 'edge_case'
  | 'form_validation'
  | 'navigation'
  | 'authentication'
  | 'api_validation'
  | 'responsive'
  | 'business_logic'
export type Priority = 'p0' | 'p1' | 'p2' | 'p3'
export type Severity = 'critical' | 'major' | 'minor' | 'trivial'
export type CaseExpectedSource =
  'plan_objective' | 'configured_rule' | 'human_specified' | 'unknown'
export type SideEffect = 'none' | 'create' | 'update' | 'delete' | 'external'

export interface CaseStep {
  action: string
  description: string
  target: string | null
  value: string | null
  side_effect: SideEffect
}

export interface CaseContent {
  title: string
  description: string | null
  category: CaseCategory
  test_type: 'ui' | 'api' | 'responsive'
  objective_id: string | null
  preconditions: string[]
  test_data: { name: string; value: string | null; description: string | null }[]
  steps: CaseStep[]
  expected_result: string | null
  expected_source: CaseExpectedSource
  priority: Priority
  severity: Severity
  tags: string[]
  dependencies: string[]
  automation_feasibility: 'automatable' | 'partially_automatable' | 'manual_only'
  automation_notes: string | null
  verification: { method: string; description: string }
  assumptions: string[]
}

export type CaseStatus = 'draft' | 'needs_review' | 'approved' | 'deprecated'

export interface CaseSummary {
  id: string
  key: string
  title: string
  category: CaseCategory
  test_type: string
  priority: Priority
  severity: Severity
  tags: string[]
  status: CaseStatus
  source: 'ai_generated' | 'manual'
  instruction_id: string | null
  objective_id: string | null
  version: number
  version_source: 'ai' | 'human'
  has_expected_result: boolean
  ready: boolean
  blocking_issues: number
  updated_at: string
}

export interface TestCase extends CaseSummary {
  project_id: string
  application_id: string
  environment_id: string | null
  content: CaseContent
  validation: { ready: boolean; issues: Issue[] }
  original_version: number | null
  approved_version: number | null
  created_at: string
}

export interface CaseVersion {
  version: number
  source: 'ai' | 'human'
  change_reason: string
  title: string
  created_at: string
  is_current: boolean
  is_original: boolean
  is_approved: boolean
  content?: CaseContent
}

export interface SuiteCase {
  position: number
  id: string
  key: string
  title: string
  category: CaseCategory
  priority: Priority
  status: CaseStatus
  current_version: number | null
  approved_version: number | null
  outdated: boolean
}

export interface Suite {
  id: string
  project_id: string
  application_id: string
  instruction_id: string | null
  name: string
  description: string | null
  feature: string | null
  priority: Priority
  tags: string[]
  status: 'draft' | 'approved' | 'archived'
  source: 'ai_generated' | 'manual'
  approved_at: string | null
  cases: SuiteCase[]
  created_at: string
  updated_at: string
}
