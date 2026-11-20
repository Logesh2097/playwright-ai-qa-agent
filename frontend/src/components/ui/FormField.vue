<script setup lang="ts">
import { useId } from 'vue'

defineProps<{ label: string; error?: string; hint?: string }>()

// Passed to the default slot so the control can be tied to its label and messages.
const id = useId()
</script>

<template>
  <div class="field">
    <label class="field__label" :for="id">{{ label }}</label>
    <slot :id="id" :invalid="Boolean(error)" :described-by="`${id}-help`" />
    <p v-if="error" :id="`${id}-help`" class="field__error" role="alert">{{ error }}</p>
    <p v-else-if="hint" :id="`${id}-help`" class="field__hint">{{ hint }}</p>
  </div>
</template>

<style scoped>
.field {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  min-width: 0;
}

.field__label {
  font-size: var(--text-sm);
  font-weight: 600;
}

.field__hint,
.field__error {
  font-size: var(--text-xs);
  overflow-wrap: anywhere;
}

.field__hint {
  color: var(--color-text-muted);
}

.field__error {
  color: var(--color-danger);
}
</style>
