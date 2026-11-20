import { api, ApiError, rootApi } from '@/api/client'
import type { Meta, Readiness } from '@/types/api'

function isReadiness(value: unknown): value is Readiness {
  return (
    typeof value === 'object' &&
    value !== null &&
    'status' in value &&
    'checks' in value &&
    typeof (value as Readiness).checks === 'object'
  )
}

export function fetchMeta(signal?: AbortSignal): Promise<Meta> {
  return api.get<Meta>('/meta', { signal })
}

/**
 * The readiness probe answers 503 with a normal report body when a dependency
 * is down. That is a valid answer here, not a failure of the request.
 */
export async function fetchReadiness(signal?: AbortSignal): Promise<Readiness> {
  try {
    return await rootApi.get<Readiness>('/readyz', { signal })
  } catch (error) {
    if (error instanceof ApiError && error.status === 503 && isReadiness(error.body)) {
      return error.body
    }
    throw error
  }
}
