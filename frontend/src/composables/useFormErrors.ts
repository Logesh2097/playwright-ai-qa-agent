import { reactive, ref } from 'vue'

import { ApiError } from '@/api/client'
import { toUserMessage } from '@/errors/messages'

/**
 * Turns a failed request into messages a form can show: one per field where the
 * server named a field, and one general message otherwise.
 */
export function useFormErrors() {
  const fields = reactive<Record<string, string>>({})
  const general = ref<string | null>(null)

  function clear(): void {
    for (const key of Object.keys(fields)) delete fields[key]
    general.value = null
  }

  function capture(error: unknown): void {
    clear()
    if (error instanceof ApiError) {
      for (const item of error.errors) {
        if (!item.field) continue
        // "body.base_url" and "additional_origins.0.url" both belong to their first segment.
        const name = item.field.replace(/^body\./, '').split('.')[0] ?? item.field
        fields[name] ??= item.message
      }
    }
    general.value = toUserMessage(error)
  }

  return { fields, general, clear, capture }
}
