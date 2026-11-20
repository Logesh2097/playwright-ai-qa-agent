import { api } from '@/api/client'
import type {
  Comparison,
  Dashboard,
  ExecutionPage,
  ExecutionReport,
  HistoryEntry,
  Trend,
} from '@/types/reports'

const base = (org: string, project: string) => `/organizations/${org}/projects/${project}`

export type ExportFormat = 'json' | 'csv' | 'pdf'

export interface HistoryFilters {
  verdict?: string
  status?: string
  environment_id?: string
  limit?: number
  offset?: number
}

export const reportsApi = {
  dashboard: (org: string, project: string, days = 30) =>
    api.get<Dashboard>(`${base(org, project)}/dashboard`, { query: { days } }),
  trends: (org: string, project: string, days = 30) =>
    api.get<Trend>(`${base(org, project)}/trends`, { query: { days } }),
  executions: (org: string, project: string, filters: HistoryFilters = {}) =>
    api.get<ExecutionPage>(`${base(org, project)}/execution-history`, { query: { ...filters } }),
  failures: (org: string, project: string, days = 30) =>
    api.get<HistoryEntry[]>(`${base(org, project)}/failures`, { query: { days } }),
  caseHistory: (org: string, project: string, caseId: string) =>
    api.get<HistoryEntry[]>(`${base(org, project)}/test-cases/${caseId}/history`),
  compare: (org: string, project: string, baseId: string, targetId: string) =>
    api.get<Comparison>(`${base(org, project)}/execution-comparison`, {
      query: { base: baseId, target: targetId },
    }),
  report: (org: string, project: string, executionId: string) =>
    api.get<ExecutionReport>(`${base(org, project)}/executions/${executionId}/report`),
  exportReport: (org: string, project: string, executionId: string, format: ExportFormat) =>
    api.download(`${base(org, project)}/executions/${executionId}/report/export`, { format }),
  /** The file itself, fetched with the session: for showing a screenshot on the page. */
  evidence: (org: string, project: string, executionId: string, evidenceId: string) =>
    api.download(`${base(org, project)}/executions/${executionId}/evidence/${evidenceId}/content`),
  /** A short-lived address for the browser to download one file. Not a share link. */
  evidenceUrl: (org: string, project: string, executionId: string, evidenceId: string) =>
    api.post<{ url: string; expires_at: string }>(
      `${base(org, project)}/executions/${executionId}/evidence/${evidenceId}/download-url`,
    ),
}

/** Hand a downloaded file to the browser's own download handling. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.append(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}
