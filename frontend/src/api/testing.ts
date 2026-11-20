import { api } from '@/api/client'
import type {
  CaseContent,
  CaseSummary,
  CaseVersion,
  Instruction,
  InstructionInput,
  Priority,
  RequirementAnalysis,
  Suite,
  TestCase,
} from '@/types/api'

const base = (org: string, project: string) => `/organizations/${org}/projects/${project}`

export const instructionsApi = {
  list: (org: string, project: string) =>
    api.get<Instruction[]>(`${base(org, project)}/instructions`),
  get: (org: string, project: string, id: string) =>
    api.get<Instruction>(`${base(org, project)}/instructions/${id}`),
  submit: (org: string, project: string, input: InstructionInput) =>
    api.post<Instruction>(`${base(org, project)}/instructions`, input),
  analyse: (org: string, project: string, id: string) =>
    api.post<Instruction>(`${base(org, project)}/instructions/${id}/analyse`),
  validate: (org: string, project: string, id: string) =>
    api.post<Instruction>(`${base(org, project)}/instructions/${id}/validate`),
  clarify: (
    org: string,
    project: string,
    id: string,
    answers: { question_id: string; answer: string }[],
  ) =>
    api.post<Instruction>(`${base(org, project)}/instructions/${id}/clarifications`, { answers }),
  editAnalysis: (org: string, project: string, id: string, analysis: RequirementAnalysis) =>
    api.put<Instruction>(`${base(org, project)}/instructions/${id}/analysis`, analysis),
  approve: (org: string, project: string, id: string) =>
    api.post<unknown>(`${base(org, project)}/instructions/${id}/approve`),
  cancel: (org: string, project: string, id: string) =>
    api.post<Instruction>(`${base(org, project)}/instructions/${id}/cancel`),
  generate: (org: string, project: string, id: string) =>
    api.post<Instruction>(`${base(org, project)}/instructions/${id}/generate`),
  regenerate: (org: string, project: string, id: string, caseIds: string[], feedback: string) =>
    api.post<Instruction>(`${base(org, project)}/instructions/${id}/regenerate`, {
      case_ids: caseIds,
      feedback: feedback || null,
    }),
}

export const casesApi = {
  list: (org: string, project: string, instructionId?: string) =>
    api.get<CaseSummary[]>(`${base(org, project)}/test-cases`, {
      query: { instruction_id: instructionId },
    }),
  get: (org: string, project: string, id: string) =>
    api.get<TestCase>(`${base(org, project)}/test-cases/${id}`),
  create: (
    org: string,
    project: string,
    input: {
      application_id: string
      environment_id?: string | null
      instruction_id?: string | null
      content: CaseContent
    },
  ) => api.post<TestCase>(`${base(org, project)}/test-cases`, input),
  /** Saved as a new version; earlier versions are kept. */
  edit: (org: string, project: string, id: string, content: CaseContent) =>
    api.put<TestCase>(`${base(org, project)}/test-cases/${id}`, content),
  remove: (org: string, project: string, id: string) =>
    api.delete(`${base(org, project)}/test-cases/${id}`),
  approve: (org: string, project: string, id: string) =>
    api.post<TestCase>(`${base(org, project)}/test-cases/${id}/approve`),
  versions: (org: string, project: string, id: string) =>
    api.get<CaseVersion[]>(`${base(org, project)}/test-cases/${id}/versions`),
  version: (org: string, project: string, id: string, number: number) =>
    api.get<CaseVersion>(`${base(org, project)}/test-cases/${id}/versions/${number}`),
}

export interface SuiteInput {
  name: string
  application_id: string
  description?: string | null
  feature?: string | null
  priority?: Priority
  case_ids?: string[]
}

export const suitesApi = {
  list: (org: string, project: string) => api.get<Suite[]>(`${base(org, project)}/test-suites`),
  get: (org: string, project: string, id: string) =>
    api.get<Suite>(`${base(org, project)}/test-suites/${id}`),
  create: (org: string, project: string, input: SuiteInput) =>
    api.post<Suite>(`${base(org, project)}/test-suites`, input),
  update: (org: string, project: string, id: string, input: Partial<SuiteInput>) =>
    api.patch<Suite>(`${base(org, project)}/test-suites/${id}`, input),
  remove: (org: string, project: string, id: string) =>
    api.delete(`${base(org, project)}/test-suites/${id}`),
  /** The complete list, in execution order. */
  setCases: (org: string, project: string, id: string, caseIds: string[]) =>
    api.put<Suite>(`${base(org, project)}/test-suites/${id}/cases`, { case_ids: caseIds }),
  approve: (org: string, project: string, id: string) =>
    api.post<Suite>(`${base(org, project)}/test-suites/${id}/approve`),
}
