<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { reportsApi } from '@/api/reports'
import ErrorState from '@/components/ui/ErrorState.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import { toUserMessage } from '@/errors/messages'
import { dateTime, duration, label, percent, shortId, toneOf } from '@/pages/reportFormat'
import { useAuthStore } from '@/stores/auth'
import type { CaseChange, Comparison } from '@/types/reports'

const props = defineProps<{ projectId: string; base?: string; target?: string }>()

const auth = useAuthStore()
const org = computed(() => auth.organizationId ?? '')
const comparison = ref<Comparison | null>(null)
const loading = ref(true)
const loadError = ref<string | null>(null)
const hideUnchanged = ref(true)

const CHANGES: CaseChange[] = ['regressed', 'still_failing', 'fixed', 'changed', 'added', 'removed']
const rows = computed(() =>
  (comparison.value?.cases ?? []).filter(
    (row) => !hideUnchanged.value || row.change !== 'unchanged',
  ),
)

onMounted(async () => {
  if (!props.base || !props.target) {
    loadError.value = 'Choose two executions to compare.'
    loading.value = false
    return
  }
  try {
    comparison.value = await reportsApi.compare(
      org.value,
      props.projectId,
      props.base,
      props.target,
    )
  } catch (error) {
    loadError.value = toUserMessage(error)
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <LoadingState v-if="loading" label="Comparing…" />
  <ErrorState v-else-if="loadError || !comparison" :message="loadError ?? 'Nothing to compare.'">
    <RouterLink :to="{ name: 'executions', params: { projectId } }">Back to executions</RouterLink>
  </ErrorState>

  <section v-else class="stack stack--lg">
    <header class="stack">
      <RouterLink class="small" :to="{ name: 'executions', params: { projectId } }">
        ← Execution history
      </RouterLink>
      <h1>Comparison of two executions</h1>
      <p v-if="!comparison.same_environment" class="notice">
        These executions ran in different environments, so differences may come from the environment
        and not from the application.
      </p>
    </header>

    <div class="grid">
      <div
        v-for="[name, side] in [
          ['Earlier', comparison.base],
          ['Later', comparison.target],
        ] as const"
        :key="name"
        class="card stack"
      >
        <div class="cluster cluster--between">
          <strong>{{ name }}</strong>
          <StatusBadge :tone="toneOf(side.verdict)">
            {{ label(side.verdict ?? side.status) }}
          </StatusBadge>
        </div>
        <RouterLink
          class="mono"
          :to="{ name: 'execution', params: { projectId, executionId: side.id } }"
        >
          {{ shortId(side.id) }}
        </RouterLink>
        <span class="small muted"
          >{{ dateTime(side.started_at) }} · {{ duration(side.duration_ms) }}</span
        >
        <span class="small">
          Pass rate {{ percent(side.results.pass_rate) }} · {{ side.results.passed }} of
          {{ side.results.ran }} cases run
        </span>
      </div>
    </div>

    <div class="cluster">
      <template v-for="change in CHANGES" :key="change">
        <StatusBadge v-if="comparison.changes[change]" :tone="toneOf(change)">
          {{ comparison.changes[change] }} {{ label(change) }}
        </StatusBadge>
      </template>
      <span class="small muted">{{ comparison.changes.unchanged }} unchanged</span>
      <label class="checkbox small">
        <input v-model="hideUnchanged" type="checkbox" />
        Hide unchanged
      </label>
    </div>

    <p v-if="rows.length === 0" class="muted">Every test case had the same result in both.</p>
    <div v-else class="card table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>Case</th>
            <th>Earlier</th>
            <th>Later</th>
            <th>Change</th>
            <th>Category now</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.case_id">
            <td>
              <RouterLink
                :to="{ name: 'case-history', params: { projectId, caseId: row.case_id } }"
              >
                {{ row.case_key }}
              </RouterLink>
              {{ row.title }}
            </td>
            <td>{{ label(row.base_status) }}</td>
            <td>{{ label(row.target_status) }}</td>
            <td>
              <StatusBadge :tone="toneOf(row.change)">{{ label(row.change) }}</StatusBadge>
            </td>
            <td>{{ label(row.target_category) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
