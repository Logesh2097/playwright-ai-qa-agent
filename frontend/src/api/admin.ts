/** Executions in progress, integrations and organization settings. */
import { api } from '@/api/client'
import type {
  ApiKey,
  AuditPage,
  BrowserStatus,
  ExecutionStartInput,
  FailureReport,
  GithubAvailableRepository,
  GithubConnection,
  GithubDelivery,
  GithubLink,
  HealProposal,
  LiveState,
  Member,
  Organization,
  OrganizationDashboard,
  RolePermissions,
} from '@/types/admin'
import type { OrgRole } from '@/types/api'
import type { ExecutionSummary } from '@/types/reports'

const org = (id: string) => `/organizations/${id}`
const project = (orgId: string, projectId: string) => `${org(orgId)}/projects/${projectId}`

export const executionsApi = {
  start: (orgId: string, projectId: string, input: ExecutionStartInput) =>
    api.post<ExecutionSummary>(`${project(orgId, projectId)}/executions`, input),
  cancel: (orgId: string, projectId: string, id: string) =>
    api.post<ExecutionSummary>(`${project(orgId, projectId)}/executions/${id}/cancel`),
  live: (orgId: string, projectId: string, id: string) =>
    api.get<LiveState>(`${project(orgId, projectId)}/executions/${id}/live`),
  eventsPath: (orgId: string, projectId: string, id: string) =>
    `${project(orgId, projectId)}/executions/${id}/events`,
  failure: (orgId: string, projectId: string, id: string, resultId: string) =>
    api.get<FailureReport>(
      `${project(orgId, projectId)}/executions/${id}/results/${resultId}/failure`,
    ),
  analyse: (orgId: string, projectId: string, id: string, resultId: string) =>
    api.post<FailureReport>(
      `${project(orgId, projectId)}/executions/${id}/results/${resultId}/failure/analysis`,
    ),
}

export const healingApi = {
  list: (orgId: string, projectId: string) =>
    api.get<HealProposal[]>(`${project(orgId, projectId)}/heal-proposals`),
  decide: (orgId: string, projectId: string, id: string, decision: 'accept' | 'reject') =>
    api.post<{ proposal: HealProposal; case_status: string | null }>(
      `${project(orgId, projectId)}/heal-proposals/${id}/${decision}`,
    ),
}

export const organizationApi = {
  dashboard: (orgId: string, days = 30) =>
    api.get<OrganizationDashboard>(`${org(orgId)}/dashboard`, { query: { days } }),
  get: (orgId: string) => api.get<Organization>(org(orgId)),
  rename: (orgId: string, name: string) => api.patch<Organization>(org(orgId), { name }),
  members: (orgId: string) => api.get<Member[]>(`${org(orgId)}/members`),
  invite: (orgId: string, email: string, role: OrgRole) =>
    api.post<Member>(`${org(orgId)}/invitations`, { email, role }),
  setRole: (orgId: string, memberId: string, role: OrgRole) =>
    api.patch<Member>(`${org(orgId)}/members/${memberId}`, { role }),
  removeMember: (orgId: string, memberId: string) =>
    api.delete(`${org(orgId)}/members/${memberId}`),
  auditLogs: (orgId: string, query: { cursor?: string; action?: string; limit?: number }) =>
    api.get<AuditPage>(`${org(orgId)}/audit-logs`, { query }),
  roles: () => api.get<RolePermissions[]>('/meta/roles'),
  browser: (orgId: string) => api.get<BrowserStatus>(`${org(orgId)}/browser`),
}

export const apiKeysApi = {
  list: (orgId: string) => api.get<ApiKey[]>(`${org(orgId)}/api-keys`),
  create: (
    orgId: string,
    input: { name: string; scopes: string[]; project_id: string | null; expires_in_days: number },
  ) => api.post<ApiKey>(`${org(orgId)}/api-keys`, input),
  revoke: (orgId: string, id: string) => api.delete<ApiKey>(`${org(orgId)}/api-keys/${id}`),
}

export const githubApi = {
  connections: (orgId: string) => api.get<GithubConnection[]>(`${org(orgId)}/github/connections`),
  connect: (orgId: string, input: Record<string, unknown>) =>
    api.post<GithubConnection>(`${org(orgId)}/github/connections`, input),
  disconnect: (orgId: string, id: string) => api.delete(`${org(orgId)}/github/connections/${id}`),
  available: (orgId: string, id: string) =>
    api.get<GithubAvailableRepository[]>(`${org(orgId)}/github/connections/${id}/repositories`),
  links: (orgId: string, projectId: string) =>
    api.get<GithubLink[]>(`${project(orgId, projectId)}/github/repositories`),
  link: (orgId: string, projectId: string, connectionId: string, fullName: string) =>
    api.post<GithubLink>(`${project(orgId, projectId)}/github/repositories`, {
      connection_id: connectionId,
      full_name: fullName,
    }),
  configure: (orgId: string, projectId: string, id: string, changes: Partial<GithubLink>) =>
    api.patch<GithubLink>(`${project(orgId, projectId)}/github/repositories/${id}`, changes),
  unlink: (orgId: string, projectId: string, id: string) =>
    api.delete(`${project(orgId, projectId)}/github/repositories/${id}`),
  rotate: (orgId: string, projectId: string, id: string) =>
    api.post<GithubLink>(`${project(orgId, projectId)}/github/repositories/${id}/webhook-secret`),
  deliveries: (orgId: string, projectId: string, id: string) =>
    api.get<GithubDelivery[]>(`${project(orgId, projectId)}/github/repositories/${id}/deliveries`),
}
