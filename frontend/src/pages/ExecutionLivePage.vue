<script setup lang="ts">
/**
 * An execution while it runs. Shows the server's state and the entries the engine
 * reported, in order. When it has ended the report takes over.
 */
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { executionsApi } from '@/api/admin'
import ExecutionProgress from '@/components/domain/ExecutionProgress.vue'
import ResultSummary from '@/components/domain/ResultSummary.vue'
import BaseButton from '@/components/ui/BaseButton.vue'
import DataTable, { type Column } from '@/components/ui/DataTable.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import { useExecutionStream } from '@/composables/useExecutionStream'
import { toUserMessage } from '@/errors/messages'
import { dateTime, duration, label, shortId, toneOf } from '@/pages/reportFormat'
import { useAuthStore } from '@/stores/auth'
import { useNotificationsStore } from '@/stores/notifications'

const props = defineProps<{ projectId: string; executionId: string }>()

const auth = useAuthStore()
const notifications = useNotificationsStore()
const org = computed(() => auth.organizationId ?? '')
const { state, events, connection, error, start } = useExecutionStream(
  org.value,
  props.projectId,
  props.executionId,
)
const cancelling = ref(false)
const confirmCancel = ref(false)

const keys = computed(() => new Map(state.value?.results.map((r) => [r.id, r.case_key]) ?? []))
const log = computed(() => [...events.value].reverse())
const canCancel = computed(
  () =>
    auth.canContribute &&
    state.value !== null &&
    !state.value.finished &&
    state.value.status !== 'cancelling',
)
const resultColumns: Column[] = [
  { key: 'case', label: 'Test case' },
  { key: 'attempts', label: 'Attempts', numeric: true },
  { key: 'status', label: 'Result' },
]
const CONNECTION: Record<string, string> = {
  connecting: 'Connecting…',
  live: 'Live',
  reconnecting: 'Connection lost. Reconnecting…',
  polling: 'Live updates unavailable. Checking every few seconds.',
  closed: 'Finished',
}

async function cancel(): Promise<void> {
  cancelling.value = true
  try {
    await executionsApi.cancel(org.value, props.projectId, props.executionId)
    notifications.info('Cancellation requested. The test in progress is being stopped.')
  } catch (cause) {
    notifications.error(toUserMessage(cause))
  } finally {
    cancelling.value = false
    confirmCancel.value = false
  }
}

onMounted(start)
</script>

<template>
  <LoadingState
    v-if="!state && connection !== 'closed' && connection !== 'polling'"
    :label="error ? 'Connection lost. Reconnecting…' : 'Connecting to the execution…'"
  />
  <ErrorState v-else-if="!state" :message="error ?? 'Execution not found.'">
    <RouterLink :to="{ name: 'executions', params: { projectId } }">Back to executions</RouterLink>
  </ErrorState>

  <section v-else class="stack stack--lg">
    <header class="stack">
      <RouterLink class="small" :to="{ name: 'executions', params: { projectId } }">
        ← Execution history
      </RouterLink>
      <div class="cluster cluster--between">
        <div>
          <h1>Execution {{ shortId(state.id) }}</h1>
          <p class="small muted" role="status">
            <span class="dot" :class="`dot--${connection}`" aria-hidden="true" />
            {{ CONNECTION[connection] }}
            <template v-if="state.started_at"> · started {{ dateTime(state.started_at) }}</template>
          </p>
        </div>
        <div class="cluster">
          <template v-if="canCancel">
            <BaseButton v-if="!confirmCancel" variant="ghost" @click="confirmCancel = true">
              Cancel execution
            </BaseButton>
            <template v-else>
              <span class="small">Stop the test in progress and skip the rest?</span>
              <BaseButton :loading="cancelling" @click="cancel">Yes, cancel</BaseButton>
              <BaseButton variant="ghost" @click="confirmCancel = false">Keep running</BaseButton>
            </template>
          </template>
          <RouterLink
            v-if="state.finished"
            class="button-link"
            :to="{ name: 'execution', params: { projectId, executionId } }"
          >
            Open the report
          </RouterLink>
        </div>
      </div>
    </header>

    <p v-if="connection === 'reconnecting' || connection === 'polling'" class="notice" role="alert">
      {{ CONNECTION[connection] }} What is shown is the last state received; the execution itself
      keeps running on the server.
    </p>
    <p v-if="state.status === 'cancelling'" class="notice">
      Cancelling: the test in progress is being stopped. Its result will be an error; tests not
      started are skipped.
    </p>

    <section class="card stack">
      <ExecutionProgress :state="state" />
      <ResultSummary :totals="state.totals" />
    </section>

    <section class="card stack">
      <h2>Test cases</h2>
      <DataTable
        :columns="resultColumns"
        :rows="state.results"
        :row-key="(row) => row.id"
        caption="Test cases of this execution and their results so far"
      >
        <template #case="{ row }">
          <strong>{{ row.case_key }}</strong> {{ row.title }}
          <p v-if="row.failure_summary" class="small muted">{{ row.failure_summary }}</p>
        </template>
        <template #status="{ row }">
          <StatusBadge :tone="toneOf(row.status)">
            {{ row.status === 'pending' ? 'waiting' : label(row.status) }}
          </StatusBadge>
        </template>
      </DataTable>
    </section>

    <section class="card stack">
      <h2>Browser events</h2>
      <p class="small muted">
        Each entry is something the test engine reported, newest first. Values typed, expected and
        found are in the report.
      </p>
      <p v-if="log.length === 0" class="muted">Nothing reported yet.</p>
      <ol v-else class="events" aria-live="polite">
        <li v-for="entry in log" :key="entry.seq" :class="`events__item--${entry.type}`">
          <span class="mono muted">{{ new Date(entry.at).toLocaleTimeString() }}</span>
          <span class="tag">{{ keys.get(entry.result_id) ?? '—' }}</span>
          <template v-if="entry.type === 'step_finished'">
            <span class="grow">
              {{ entry.position }}. {{ entry.description ?? entry.action }}
              <span class="tag">{{ entry.action }}</span>
              <span v-if="entry.error_message" class="events__error">{{
                entry.error_message
              }}</span>
            </span>
            <span class="small muted">{{ duration(entry.duration_ms) }}</span>
            <StatusBadge :tone="toneOf(entry.status)">{{ entry.status }}</StatusBadge>
          </template>
          <template v-else-if="entry.type === 'case_started'">
            <span class="grow">
              Test case started<template v-if="entry.attempt > 1">
                (attempt {{ entry.attempt }})</template
              >
            </span>
          </template>
          <template v-else>
            <span class="grow">Test case finished</span>
            <StatusBadge :tone="toneOf(entry.status)">{{ label(entry.status) }}</StatusBadge>
          </template>
        </li>
      </ol>
    </section>
  </section>
</template>

<style scoped>
.dot {
  display: inline-block;
  width: 0.5rem;
  height: 0.5rem;
  margin-right: var(--space-1);
  border-radius: var(--radius-full);
  background: var(--color-text-muted);
}

.dot--live {
  background: var(--color-success);
}

.dot--reconnecting,
.dot--polling {
  background: var(--color-warning);
}

.button-link {
  padding: var(--space-2) var(--space-4);
  border-radius: var(--radius-sm);
  background: var(--color-accent);
  color: var(--color-accent-contrast);
  font-weight: 600;
  text-decoration: none;
}

.events {
  display: flex;
  flex-direction: column;
  max-height: 32rem;
  margin: 0;
  padding: 0;
  overflow-y: auto;
  list-style: none;
}

.events li {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2) var(--space-3);
  padding: var(--space-2) 0;
  border-top: 1px solid var(--color-border);
  font-size: var(--text-sm);
}

.events li:first-child {
  border-top: 0;
}

.events__item--case_started,
.events__item--case_finished {
  font-weight: 600;
}

.events__error {
  display: block;
  color: var(--color-danger);
  overflow-wrap: anywhere;
}
</style>
