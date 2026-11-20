<script setup lang="ts">
defineProps<{ tone: 'success' | 'warning' | 'danger' | 'neutral' }>()
</script>

<template>
  <span class="badge" :class="`badge--${tone}`">
    <span class="badge__dot" aria-hidden="true" />
    <slot />
  </span>
</template>

<style scoped>
.badge {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  padding: 0.125rem var(--space-3);
  border-radius: var(--radius-full);
  font-size: var(--text-xs);
  font-weight: 600;
  white-space: nowrap;
}

.badge__dot {
  width: 0.5rem;
  height: 0.5rem;
  border-radius: var(--radius-full);
  background: currentcolor;
}

.badge--success {
  background: var(--color-success-bg);
  color: var(--color-success);
}

.badge--warning {
  background: var(--color-warning-bg);
  color: var(--color-warning);
}

.badge--danger {
  background: var(--color-danger-bg);
  color: var(--color-danger);
}

.badge--neutral {
  background: var(--color-neutral-bg);
  color: var(--color-text-muted);
}
</style>
