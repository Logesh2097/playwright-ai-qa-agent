import { ApiError } from '@/api/client'

const FALLBACK = 'Something went wrong. Please try again.'

/** Turns any thrown value into text that is safe and useful to show a person. */
export function toUserMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status >= 500) return 'The server ran into a problem. Please try again shortly.'
    return error.message || FALLBACK
  }
  return FALLBACK
}

export function toReference(error: unknown): string | undefined {
  return error instanceof ApiError && error.requestId ? `Request ${error.requestId}` : undefined
}
