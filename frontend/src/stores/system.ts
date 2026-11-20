import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { fetchMeta, fetchReadiness } from '@/api/system'
import { toUserMessage } from '@/errors/messages'
import type { Meta, Readiness } from '@/types/api'

/** Backend status shown on the overview page and in the shell header. */
export const useSystemStore = defineStore('system', () => {
  const meta = ref<Meta | null>(null)
  const readiness = ref<Readiness | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)
  const checkedAt = ref<Date | null>(null)

  const state = computed<'unknown' | 'ready' | 'degraded' | 'unreachable'>(() => {
    if (error.value) return 'unreachable'
    if (!readiness.value) return 'unknown'
    return readiness.value.status === 'ready' ? 'ready' : 'degraded'
  })

  async function refresh(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      const [metaResult, readinessResult] = await Promise.all([fetchMeta(), fetchReadiness()])
      meta.value = metaResult
      readiness.value = readinessResult
    } catch (cause) {
      readiness.value = null
      error.value = toUserMessage(cause)
    } finally {
      checkedAt.value = new Date()
      loading.value = false
    }
  }

  return { meta, readiness, loading, error, checkedAt, state, refresh }
})
