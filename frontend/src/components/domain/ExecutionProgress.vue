<script setup lang="ts">
import { computed } from 'vue'

import StatusBadge from '@/components/ui/StatusBadge.vue'
import { duration, label, toneOf } from '@/pages/reportFormat'
import type { LiveState } from '@/types/admin'

/**
 * Where a running execution stands, from what the server reports. Progress is the
 * number of test cases that have a result, over the number selected. There is no
 * estimate of time remaining: none is known.
 */
const props = defineProps<{ state: LiveState }>()

const total = computed(() => props.state.results.length)
const done = computed(
  () => props.state.results.filter((r) => r.status !== 'pending' && r.status !== 'running').length,
)
const currentCase = computed(
  () => props.state.results.find((r) => r.id === props.state.current?.result_id) ?? null,
)
const step = computed(() => props.state.current?.step ?? null)
</script>

<template>
  <div class="stack">
    <div class="cluster cluster--between">
      <strong>{{ done }} of {{ total }} test cases finished</strong>
      <StatusBadge :tone="toneOf(state.verdict ?? (state.finished ? 'neutral' : null))">
        {{ label(state.verdict ?? state.status) }}
      </StatusBadge>
    </div>
    <progress class="progress" :value="done" :max="Math.max(total, 1)">
      {{ done }} of {{ total }}
    </progress>

    <dl v-if="!state.finished" class="now">
      <div>
        <dt>Current test case</dt>
        <dd v-if="currentCase">
          <strong>{{ currentCase.case_key }}</strong> {{ currentCase.title }}
          <span v-if="(state.current?.attempt ?? 1) > 1" class="tag">
            attempt {{ state.current?.attempt }}
          </span>
        </dd>
        <dd v-else class="muted">
          {{ state.status === 'queued' ? 'Waiting for a worker to start.' : 'Between test cases.' }}
        </dd>
      </div>
      <div>
        <dt>Last step reported</dt>
        <dd v-if="step">
          {{ step.position }}. {{ step.description ?? step.action }}
          <span class="tag">{{ step.action }}</span>
          <StatusBadge :tone="toneOf(step.status)">{{ step.status }}</StatusBadge>
          <span class="small muted">{{ duration(step.duration_ms) }}</span>
        </dd>
        <dd v-else class="muted">No step reported yet.</dd>
      </div>
    </dl>
  </div>
</template>

<style scoped>
.progress {
  width: 100%;
  height: 0.625rem;
  accent-color: var(--color-accent);
}

.now {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr));
  gap: var(--space-4);
  margin: 0;
}

.now dt {
  color: var(--color-text-muted);
  font-size: var(--text-xs);
  font-weight: 600;
}

.now dd {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  margin: 0;
  overflow-wrap: anywhere;
}
</style>
