<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

import { reportsApi } from '@/api/reports'
import StartExecutionDialog from '@/components/domain/StartExecutionDialog.vue'
import BaseButton from '@/components/ui/BaseButton.vue'
import PaginationBar from '@/components/ui/PaginationBar.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import { toUserMessage } from '@/errors/messages'
import { dateTime, label, shortId, toneOf } from '@/pages/reportFormat'
import { useAuthStore } from '@/stores/auth'
import type { ExecutionPage, HistoryEntry } from '@/types/reports'

const props = defineProps<{ projectId: string }>()
const PAGE = 25

const auth = useAuthStore()
const router = useRouter()
const org = computed(() => auth.organizationId ?? '')
const filters = reactive({ verdict: '', offset: 0 })
const page = ref<ExecutionPage | null>(null)
const failures = ref<HistoryEntry[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)
/** Up to two executions picked for comparison, in the order they were picked. */
const picked = ref<string[]>([])
const startOpen = ref(false)
const ACTIVE = ['queued', 'preparing', 'running', 'analysing', 'cancelling']

async function started(executionId: string): Promise<void> {
  startOpen.value = false
  await router.push({
    name: 'execution-live',
    params: { projectId: props.projectId, executionId },
  })
}

function page_to(offset: number): void {
  filters.offset = offset
  void load()
}

async function load(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    ;[page.value, failures.value] = await Promise.all([
      reportsApi.executions(org.value, props.projectId, {
        verdict: filters.verdict || undefined,
        limit: PAGE,
        offset: filters.offset,
      }),
      reportsApi.failures(org.value, props.projectId),
    ])
  } catch (error) {
    loadError.value = toUserMessage(error)
  } finally {
    loading.value = false
  }
}

function toggle(id: string): void {
  picked.value = picked.value.includes(id)
    ? picked.value.filter((item) => item !== id)
    : [...picked.value, id].slice(-2)
}

async function compare(): Promise<void> {
  const items = page.value?.items ?? []
  // The older of the two is the base, whatever order they were ticked in.
  const [target, base] = items.filter((item) => picked.value.includes(item.id))
  if (!base || !target) return
  await router.push({
    name: 'execution-comparison',
    params: { projectId: props.projectId },
    query: { base: base.id, target: target.id },
  })
}

watch(
  () => filters.verdict,
  () => {
    filters.offset = 0
    void load()
  },
)
onMounted(load)
</script>

<template>
  <section class="stack stack--lg">
    <header class="stack">
      <RouterLink class="small" :to="{ name: 'project', params: { projectId } }">
        ← Project
      </RouterLink>
      <div class="cluster cluster--between">
        <div>
          <h1>Execution history</h1>
          <p class="muted">Every run of test cases in this project, newest first.</p>
        </div>
        <div class="cluster">
          <RouterLink :to="{ name: 'dashboard', params: { projectId } }">Dashboard</RouterLink>
          <BaseButton v-if="auth.canContribute" @click="startOpen = true"
            >Start execution</BaseButton
          >
        </div>
      </div>
    </header>

    <div class="cluster">
      <label class="cluster small">
        Final status
        <select v-model="filters.verdict" class="input filter">
          <option value="">Any</option>
          <option value="passed">Passed</option>
          <option value="failed">Failed</option>
          <option value="error">Error</option>
          <option value="flaky">Flaky</option>
          <option value="needs_review">Needs review</option>
          <option value="inconclusive">Inconclusive</option>
        </select>
      </label>
      <BaseButton variant="ghost" :disabled="picked.length !== 2" @click="compare">
        Compare selected ({{ picked.length }}/2)
      </BaseButton>
    </div>

    <LoadingState v-if="loading" label="Loading executions…" />
    <ErrorState v-else-if="loadError || !page" :message="loadError ?? 'Nothing to show.'" />
    <EmptyState
      v-else-if="page.total === 0"
      title="No executions"
      message="Nothing has been run yet, or nothing matches the filter."
    />

    <template v-else>
      <div class="card table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th><span class="visually-hidden">Compare</span></th>
              <th>Execution</th>
              <th>Started</th>
              <th class="num">Cases</th>
              <th class="num">Passed</th>
              <th class="num">Failed</th>
              <th class="num">Error</th>
              <th>Final status</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="execution in page.items" :key="execution.id">
              <td>
                <input
                  type="checkbox"
                  :checked="picked.includes(execution.id)"
                  :aria-label="`Select execution ${shortId(execution.id)} for comparison`"
                  @change="toggle(execution.id)"
                />
              </td>
              <td>
                <RouterLink
                  class="mono"
                  :to="{
                    name: ACTIVE.includes(execution.status) ? 'execution-live' : 'execution',
                    params: { projectId, executionId: execution.id },
                  }"
                >
                  {{ shortId(execution.id) }}
                </RouterLink>
              </td>
              <td>{{ dateTime(execution.started_at ?? execution.created_at) }}</td>
              <td class="num">{{ execution.totals.total ?? '—' }}</td>
              <td class="num">{{ execution.totals.passed ?? '—' }}</td>
              <td class="num">{{ execution.totals.failed ?? '—' }}</td>
              <td class="num">{{ execution.totals.error ?? '—' }}</td>
              <td>
                <StatusBadge :tone="toneOf(execution.verdict)">
                  {{ label(execution.verdict ?? execution.status) }}
                </StatusBadge>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <PaginationBar
        :offset="page.offset"
        :limit="PAGE"
        :count="page.items.length"
        :total="page.total"
        @change="page_to"
      />

      <section class="card stack">
        <h2>Failed tests, last 30 days</h2>
        <p v-if="failures.length === 0" class="muted">No test failed or ended in an error.</p>
        <ul v-else class="list">
          <li v-for="entry in failures" :key="entry.result_id">
            <div class="grow">
              <RouterLink
                :to="{ name: 'case-history', params: { projectId, caseId: entry.case_id } }"
              >
                {{ entry.case_key }}
              </RouterLink>
              {{ entry.title }}
              <p class="small muted">{{ entry.failure_summary }}</p>
            </div>
            <span class="tag">{{ label(entry.failure_category) }}</span>
            <RouterLink
              class="small"
              :to="{ name: 'execution', params: { projectId, executionId: entry.execution_id } }"
            >
              {{ dateTime(entry.finished_at) }}
            </RouterLink>
            <StatusBadge :tone="toneOf(entry.status)">{{ label(entry.status) }}</StatusBadge>
          </li>
        </ul>
      </section>
    </template>
    <StartExecutionDialog
      :open="startOpen"
      :org="org"
      :project-id="projectId"
      @close="startOpen = false"
      @started="started"
    />
  </section>
</template>

<style scoped>
.filter {
  width: auto;
}
</style>
