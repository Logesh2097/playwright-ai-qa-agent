<script setup lang="ts">
defineProps<{ title: string; message?: string }>()
</script>

<template>
  <div class="empty">
    <h2 class="empty__title">{{ title }}</h2>
    <p v-if="message" class="empty__message">{{ message }}</p>
    <slot />
  </div>
</template>

<style scoped>
.empty {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-3);
  padding: var(--space-6) var(--space-5);
  border: 1px dashed var(--color-border);
  border-radius: var(--radius-md);
}

.empty__title {
  font-size: var(--text-md);
}

.empty__message {
  color: var(--color-text-muted);
  font-size: var(--text-sm);
}
</style>
