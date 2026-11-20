<script setup lang="ts">
import type { Issue } from '@/types/api'

defineProps<{ issues: Issue[] }>()
</script>

<template>
  <ul v-if="issues.length > 0" class="issues">
    <li
      v-for="(issue, index) in issues"
      :key="index"
      class="issue"
      :class="`issue--${issue.severity}`"
    >
      <span class="issue__label">{{
        issue.severity === 'blocking' ? 'Blocks approval' : 'Note'
      }}</span>
      <span>
        <template v-if="issue.step">Step {{ issue.step }}: </template>{{ issue.message }}
      </span>
    </li>
  </ul>
</template>

<style scoped>
.issues {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin: 0;
  padding: 0;
  list-style: none;
}

.issue {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  font-size: var(--text-sm);
}

.issue--blocking {
  background: var(--color-danger-bg);
  color: var(--color-danger);
}

.issue--warning {
  background: var(--color-warning-bg);
  color: var(--color-warning);
}

.issue__label {
  font-weight: 700;
  white-space: nowrap;
}
</style>
