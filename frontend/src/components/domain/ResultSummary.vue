<script setup lang="ts">
import { computed } from 'vue'

import { percent } from '@/pages/reportFormat'

/**
 * Counts of test results by status, with a proportional bar. Only `passed` is
 * drawn as a success; every other status has its own segment and label.
 */
/** Counts by status. Other entries (rates, for instance) are ignored. */
const props = defineProps<{
  totals: Record<string, number | null | undefined>
  passRate?: number | null
}>()

const PARTS = [
  ['passed', 'Passed', 'success'],
  ['failed', 'Failed', 'danger'],
  ['error', 'Error', 'danger-soft'],
  ['flaky', 'Flaky', 'warning'],
  ['needs_review', 'Needs review', 'warning'],
  ['blocked', 'Blocked', 'neutral'],
  ['skipped', 'Skipped', 'neutral'],
  ['running', 'Running', 'accent'],
  ['pending', 'Waiting', 'muted'],
] as const

const parts = computed(() =>
  PARTS.map(([key, label, tone]) => ({ key, label, tone, count: props.totals[key] ?? 0 })).filter(
    (part) => part.count > 0,
  ),
)
const total = computed(() => props.totals.total ?? parts.value.reduce((sum, p) => sum + p.count, 0))
</script>

<template>
  <div class="summary">
    <div
      v-if="total > 0"
      class="summary__bar"
      role="img"
      :aria-label="parts.map((p) => `${p.count} ${p.label.toLowerCase()}`).join(', ')"
    >
      <span
        v-for="part in parts"
        :key="part.key"
        class="summary__segment"
        :class="`summary__segment--${part.tone}`"
        :style="{ flexGrow: part.count }"
      />
    </div>
    <ul class="summary__legend">
      <li>
        <strong>{{ total }}</strong> total
      </li>
      <li v-for="part in parts" :key="part.key">
        <span class="summary__dot" :class="`summary__segment--${part.tone}`" aria-hidden="true" />
        <strong>{{ part.count }}</strong> {{ part.label.toLowerCase() }}
      </li>
      <li v-if="passRate !== undefined" class="muted">Pass rate {{ percent(passRate) }}</li>
    </ul>
  </div>
</template>

<style scoped>
.summary {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.summary__bar {
  display: flex;
  gap: 2px;
  height: 0.75rem;
}

.summary__segment {
  min-width: 4px;
  border-radius: 2px;
}

.summary__segment--success {
  background: var(--color-success);
}

.summary__segment--danger {
  background: var(--color-danger);
}

.summary__segment--danger-soft {
  background: color-mix(in srgb, var(--color-danger) 55%, var(--color-surface));
}

.summary__segment--warning {
  background: var(--color-warning);
}

.summary__segment--neutral {
  background: var(--color-text-muted);
}

.summary__segment--accent {
  background: var(--color-accent);
}

.summary__segment--muted {
  background: var(--color-border);
}

.summary__legend {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2) var(--space-4);
  margin: 0;
  padding: 0;
  list-style: none;
  font-size: var(--text-sm);
}

.summary__legend li {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.summary__dot {
  width: 0.625rem;
  height: 0.625rem;
  border-radius: 2px;
}
</style>
