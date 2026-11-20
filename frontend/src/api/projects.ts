import { api } from '@/api/client'
import type {
  AccessCheck,
  Application,
  ApplicationInput,
  Credential,
  CredentialInput,
  Environment,
  EnvironmentInput,
  OrgMember,
  Project,
  ProjectMember,
  ProjectRole,
} from '@/types/api'

/** Builds the nested URLs: every resource is addressed through its organization and parents. */
const paths = {
  projects: (org: string) => `/organizations/${org}/projects`,
  project: (org: string, project: string) => `/organizations/${org}/projects/${project}`,
  applications: (org: string, project: string) => `${paths.project(org, project)}/applications`,
  application: (org: string, project: string, app: string) =>
    `${paths.applications(org, project)}/${app}`,
  environments: (org: string, project: string, app: string) =>
    `${paths.application(org, project, app)}/environments`,
  environment: (org: string, project: string, app: string, environment: string) =>
    `${paths.environments(org, project, app)}/${environment}`,
  credentials: (org: string, project: string, app: string, environment: string) =>
    `${paths.environment(org, project, app, environment)}/credentials`,
}

export interface AppRef {
  org: string
  project: string
  application: string
}

export interface EnvRef extends AppRef {
  environment: string
}

export const projectsApi = {
  list: (org: string) => api.get<Project[]>(paths.projects(org)),
  get: (org: string, id: string) => api.get<Project>(paths.project(org, id)),
  create: (org: string, input: { name: string; description?: string | null }) =>
    api.post<Project>(paths.projects(org), input),
  update: (org: string, id: string, input: { name?: string; description?: string | null }) =>
    api.patch<Project>(paths.project(org, id), input),
  archive: (org: string, id: string) => api.post<Project>(`${paths.project(org, id)}/archive`),
  restore: (org: string, id: string) => api.post<Project>(`${paths.project(org, id)}/restore`),
  remove: (org: string, id: string, confirmName: string) =>
    api.delete(paths.project(org, id), { query: { confirm_name: confirmName } }),

  members: (org: string, id: string) =>
    api.get<ProjectMember[]>(`${paths.project(org, id)}/members`),
  addMember: (org: string, id: string, userId: string, role: ProjectRole) =>
    api.post<ProjectMember[]>(`${paths.project(org, id)}/members`, { user_id: userId, role }),
  setMemberRole: (org: string, id: string, userId: string, role: ProjectRole) =>
    api.patch<ProjectMember[]>(`${paths.project(org, id)}/members/${userId}`, { role }),
  removeMember: (org: string, id: string, userId: string) =>
    api.delete(`${paths.project(org, id)}/members/${userId}`),

  organizationMembers: (org: string) => api.get<OrgMember[]>(`/organizations/${org}/members`),
}

export const applicationsApi = {
  list: (org: string, project: string) => api.get<Application[]>(paths.applications(org, project)),
  get: (ref: AppRef) =>
    api.get<Application>(paths.application(ref.org, ref.project, ref.application)),
  create: (org: string, project: string, input: ApplicationInput) =>
    api.post<Application>(paths.applications(org, project), input),
  update: (ref: AppRef, input: Partial<ApplicationInput>) =>
    api.patch<Application>(paths.application(ref.org, ref.project, ref.application), input),
  remove: (ref: AppRef) => api.delete(paths.application(ref.org, ref.project, ref.application)),
}

export const environmentsApi = {
  list: (ref: AppRef) =>
    api.get<Environment[]>(paths.environments(ref.org, ref.project, ref.application)),
  create: (ref: AppRef, input: EnvironmentInput) =>
    api.post<Environment>(paths.environments(ref.org, ref.project, ref.application), input),
  update: (ref: EnvRef, input: Partial<EnvironmentInput>) =>
    api.patch<Environment>(
      paths.environment(ref.org, ref.project, ref.application, ref.environment),
      input,
    ),
  remove: (ref: EnvRef) =>
    api.delete(paths.environment(ref.org, ref.project, ref.application, ref.environment)),
  checkAccess: (ref: EnvRef) =>
    api.post<AccessCheck>(
      `${paths.environment(ref.org, ref.project, ref.application, ref.environment)}/access-check`,
    ),
}

export const credentialsApi = {
  list: (ref: EnvRef) =>
    api.get<Credential[]>(
      paths.credentials(ref.org, ref.project, ref.application, ref.environment),
    ),
  create: (ref: EnvRef, input: CredentialInput) =>
    api.post<Credential>(
      paths.credentials(ref.org, ref.project, ref.application, ref.environment),
      input,
    ),
  /** Sending `secret` replaces the stored value; it can never be read back. */
  update: (ref: EnvRef, id: string, input: Partial<CredentialInput>) =>
    api.patch<Credential>(
      `${paths.credentials(ref.org, ref.project, ref.application, ref.environment)}/${id}`,
      input,
    ),
  remove: (ref: EnvRef, id: string) =>
    api.delete(
      `${paths.credentials(ref.org, ref.project, ref.application, ref.environment)}/${id}`,
    ),
}
