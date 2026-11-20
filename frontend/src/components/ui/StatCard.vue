<script setup lang="ts">
/** One headline figure. `value` is shown as given: a missing figure is a dash, never a zero. */
defineProps<{
  label: string
  value: string | number | null
  hint?: string
  tone?: 'danger' | 'success'
}>()
</script>

<template>
  <div class="card stat">
    <span class="stat__value" :class="tone ? `stat__value--${tone}` : ''">{{ value ?? '—' }}</span>
    <span class="stat__label">{{ label }}</span>
    <span v-if="hint" class="stat__hint">{{ hint }}</span>
  </div>
</template>

<style scoped>
.stat {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  min-width: 0;
}

.stat__value {
  font-size: var(--text-xl);
  font-weight: 650;
  font-variant-numeric: tabular-nums;
  line-height: 1.2;
}

.stat__value--danger {
  color: var(--color-danger);
}

.stat__value--success {
  color: var(--color-success);
}

.stat__label {
  font-size: var(--text-sm);
  font-weight: 600;
}

.stat__hint {
  color: var(--color-text-muted);
  font-size: var(--text-xs);
}
</style>
