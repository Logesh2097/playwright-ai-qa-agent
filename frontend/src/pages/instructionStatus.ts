import type { CaseStatus, InstructionStatus } from '@/types/api'

type Tone = 'success' | 'warning' | 'danger' | 'neutral'

export const STATUS_LABELS: Record<InstructionStatus, string> = {
  received: 'Received',
  analysing: 'Analysing…',
  needs_clarification: 'Needs your input',
  analysed: 'Ready to approve',
  approved: 'Requirement approved',
  generating: 'Writing test cases…',
  awaiting_approval: 'Cases to review',
  completed: 'Suite approved',
  rejected: 'Rejected',
  failed: 'Failed',
  cancelled: 'Cancelled',
}

export function statusTone(status: InstructionStatus): Tone {
  if (status === 'completed' || status === 'approved') return 'success'
  if (status === 'failed' || status === 'rejected') return 'danger'
  if (status === 'needs_clarification' || status === 'awaiting_approval' || status === 'analysed') {
    return 'warning'
  }
  return 'neutral'
}

export function isWorking(status: InstructionStatus): boolean {
  return status === 'analysing' || status === 'generating' || status === 'received'
}

export const CASE_STATUS_LABELS: Record<CaseStatus, string> = {
  draft: 'Draft',
  needs_review: 'Needs review',
  approved: 'Approved',
  deprecated: 'Deprecated',
}
