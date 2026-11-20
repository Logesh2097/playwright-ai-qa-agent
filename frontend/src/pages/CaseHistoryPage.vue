<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { reportsApi } from '@/api/reports'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import { toUserMessage } from '@/errors/messages'
import { dateTime, duration, label, shortId, toneOf } from '@/pages/reportFormat'
import { useAuthStore } from '@/stores/auth'
import type { HistoryEntry } from '@/types/reports'

const props = defineProps<{ projectId: string; caseId: string }>()

const auth = useAuthStore()
const org = computed(() => auth.organizationId ?? '')
const entries = ref<HistoryEntry[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)

onMounted(async () => {
  try {
    entries.value = await reportsApi.caseHistory(org.value, props.projectId, props.caseId)
  } catch (error) {
    loadError.value = toUserMessage(error)
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <LoadingState v-if="loading" label="Loading history…" />
  <ErrorState v-else-if="loadError" :message="loadError" />

  <section v-else class="stack stack--lg">
    <header class="stack">
      <RouterLink class="small" :to="{ name: 'case', params: { projectId, caseId } }">
        ← Test case
      </RouterLink>
      <h1>
        Result history
        <template v-if="entries[0]"> · {{ entries[0].case_key }}</template>
      </h1>
      <p v-if="entries[0]" class="muted">{{ entries[0].title }}</p>
    </header>

    <EmptyState
      v-if="entries.length === 0"
      title="Never run"
      message="This test case has no results yet."
    />
    <div v-else class="card table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>Finished</th>
            <th>Execution</th>
            <th>Result</th>
            <th>Category</th>
            <th class="num">Attempts</th>
            <th class="num">Duration</th>
            <th>Summary</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="entry in entries" :key="entry.result_id">
            <td>{{ dateTime(entry.finished_at) }}</td>
            <td>
              <RouterLink
                class="mono"
                :to="{ name: 'execution', params: { projectId, executionId: entry.execution_id } }"
              >
                {{ shortId(entry.execution_id) }}
              </RouterLink>
            </td>
            <td>
              <StatusBadge :tone="toneOf(entry.status)">{{ label(entry.status) }}</StatusBadge>
              <span v-if="entry.healed" class="tag">locator replaced</span>
            </td>
            <td>{{ label(entry.failure_category) }}</td>
            <td class="num">{{ entry.attempts }}</td>
            <td class="num">{{ duration(entry.duration_ms) }}</td>
            <td>{{ entry.failure_summary ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
