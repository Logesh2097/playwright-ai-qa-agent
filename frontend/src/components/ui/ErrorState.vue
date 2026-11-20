<script setup lang="ts">
import BaseButton from '@/components/ui/BaseButton.vue'

withDefaults(
  defineProps<{
    title?: string
    message: string
    /** Show a retry button; the parent handles the `retry` event. */
    retryable?: boolean
    retryLabel?: string
    retrying?: boolean
  }>(),
  { title: 'Something went wrong', retryable: false, retryLabel: 'Try again', retrying: false },
)

defineEmits<{ retry: [] }>()
</script>

<template>
  <div class="error-state" role="alert">
    <h2 class="error-state__title">{{ title }}</h2>
    <p class="error-state__message">{{ message }}</p>
    <BaseButton v-if="retryable" variant="secondary" :loading="retrying" @click="$emit('retry')">
      {{ retryLabel }}
    </BaseButton>
  </div>
</template>

<style scoped>
.error-state {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-3);
  padding: var(--space-5);
  border: 1px solid var(--color-danger);
  border-radius: var(--radius-md);
  background: var(--color-danger-bg);
}

.error-state__title {
  font-size: var(--text-md);
  color: var(--color-danger);
}

.error-state__message {
  font-size: var(--text-sm);
  overflow-wrap: anywhere;
}
</style>
