import { ref, type Ref } from 'vue'

import { toUserMessage } from '@/errors/messages'

export interface AsyncState<T, Args extends unknown[]> {
  data: Ref<T | null>
  error: Ref<string | null>
  loading: Ref<boolean>
  /** Runs the task. Resolves to the result, or `null` if it failed. */
  run: (...args: Args) => Promise<T | null>
}

/**
 * Wraps an async function with loading and error state for use in components.
 * If calls overlap, only the most recent one updates the state.
 */
export function useAsync<T, Args extends unknown[] = []>(
  task: (...args: Args) => Promise<T>,
): AsyncState<T, Args> {
  const data = ref<T | null>(null) as Ref<T | null>
  const error = ref<string | null>(null)
  const loading = ref(false)
  let latest = 0

  async function run(...args: Args): Promise<T | null> {
    const call = ++latest
    loading.value = true
    error.value = null
    try {
      const result = await task(...args)
      if (call === latest) data.value = result
      return result
    } catch (cause) {
      if (call === latest) error.value = toUserMessage(cause)
      return null
    } finally {
      if (call === latest) loading.value = false
    }
  }

  return { data, error, loading, run }
}
