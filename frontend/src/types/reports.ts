/** Shapes of the reporting API: executions, reports, history, comparison, dashboard. */

export type ResultStatus =
  | 'pending'
  | 'running'
  | 'passed'
  | 'failed'
  | 'error'
  | 'flaky'
  | 'skipped'
  | 'blocked'
  | 'inconclusive'
  | 'needs_review'

export type Verdict = 'passed' | 'failed' | 'error' | 'flaky' | 'inconclusive' | 'needs_review'

export interface ExecutionSummary {
  id: string
  environment_id: string
  suite_id: string | null
  status: string
  verdict: Verdict | null
  totals: Record<string, number>
  triggered_by: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface ExecutionPage {
  items: ExecutionSummary[]
  total: number
  limit: number
  offset: number
}

export interface Named {
  id: string
  name: string
}

export interface ReportTotals {
  total: number
  passed: number
  failed: number
  skipped: number
  blocked: number
  error: number
  flaky: number
  needs_review: number
  pass_rate: number | null
}

export interface EvidenceRef {
  id: string
  kind: 'screenshot' | 'video' | 'trace' | 'console_log' | 'network_log'
  content_type: string
  size_bytes: number
  step_position: number | null
  expires_at: string | null
}

export interface StepDetail {
  attempt: number
  position: number
  phase: string
  action: string
  description: string | null
  status: string
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  locator: Record<string, unknown> | null
  healed_from: Record<string, unknown> | null
  expected: Record<string, unknown> | null
  actual: Record<string, unknown> | null
  error_type: string | null
  error_message: string | null
  page_url: string | null
}

/** Written by a model. Always shown labelled as a suggestion. */
export interface AiSuggestion {
  summary?: string
  possible_root_cause?: string
  impact?: string
  debugging_steps?: string[]
  suggested_test_cases?: { title: string; why: string }[]
  confidence?: string
  note: string
  advisory: true
}

export interface ResultDetail {
  id: string
  case_id: string
  case_key: string
  title: string
  version: number
  expected_result: string | null
  status: ResultStatus
  failure_category: string | null
  failure_summary: string | null
  healed: boolean
  attempts: number
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  console_errors: string[]
  page_errors: string[]
  network_errors: Record<string, unknown>[]
  verification: Record<string, unknown> | null
  steps: StepDetail[]
  evidence: EvidenceRef[]
  ai_suggestion: AiSuggestion | null
}

export interface FailureSummary {
  result_id: string
  case_key: string
  title: string
  status: ResultStatus
  failure_category: string | null
  summary: string | null
  failed_step: string | null
  expected: Record<string, unknown> | null
  actual: Record<string, unknown> | null
  screenshot_ids: string[]
}

export interface ExecutionReport {
  report_version: number
  generated_at: string
  project: Named
  application: Named | null
  environment: (Named & { kind: string; base_url: string }) | null
  instruction: { id: string; text: string } | null
  suite: Named | null
  execution: {
    id: string
    status: string
    verdict: Verdict | null
    started_at: string | null
    finished_at: string | null
    duration_ms: number | null
    triggered_by: string | null
    browser: string | null
    config: Record<string, unknown>
    error: Record<string, unknown> | null
  }
  totals: ReportTotals
  failure_categories: Record<string, number>
  failures: FailureSummary[]
  results: ResultDetail[]
  notes: string[]
}

export interface HistoryEntry {
  result_id: string
  execution_id: string
  environment_id: string
  case_id: string
  case_key: string | null
  title: string | null
  status: ResultStatus
  failure_category: string | null
  failure_summary: string | null
  healed: boolean
  attempts: number
  duration_ms: number | null
  finished_at: string | null
}

export interface ResultRates {
  ran: number
  passed: number
  failed: number
  error: number
  flaky: number
  needs_review: number
  skipped: number
  blocked: number
  pass_rate: number | null
  failure_rate: number | null
}

export type CaseChange =
  'regressed' | 'fixed' | 'still_failing' | 'changed' | 'added' | 'removed' | 'unchanged'

export interface ComparedExecution {
  id: string
  status: string
  verdict: Verdict | null
  environment_id: string
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  results: ResultRates
}

export interface Comparison {
  base: ComparedExecution
  target: ComparedExecution
  same_environment: boolean
  changes: Record<CaseChange, number>
  cases: {
    case_id: string
    case_key: string | null
    title: string | null
    base_status: ResultStatus | null
    target_status: ResultStatus | null
    base_category: string | null
    target_category: string | null
    base_duration_ms: number | null
    target_duration_ms: number | null
    change: CaseChange
  }[]
}

export interface TrendPoint {
  date: string
  executions: number
  verdicts: Record<string, number>
  results: ResultRates
  average_duration_ms: number | null
}

export interface Trend {
  window_days: number
  truncated: boolean
  points: TrendPoint[]
}

export interface Dashboard {
  window_days: number
  truncated: boolean
  total_executions: number
  finished_executions: number
  executions_by_verdict: Record<string, number>
  execution_pass_rate: number | null
  execution_failure_rate: number | null
  results: ResultRates
  flaky_test_count: number
  flaky_case_ids: string[]
  average_execution_duration_ms: number | null
  frequently_failing: {
    case_id: string
    case_key: string | null
    title: string | null
    runs: number
    failures: number
    failure_rate: number | null
    last_status: ResultStatus
    last_failed_at: string | null
    most_common_category: string | null
  }[]
  coverage: {
    test_cases: number
    approved: number
    automated: number
    executed: number
    passing: number
    approved_share: number | null
    automated_share: number | null
    executed_share: number | null
    passing_share: number | null
    note: string
  }
  recent_executions: ExecutionSummary[]
}
