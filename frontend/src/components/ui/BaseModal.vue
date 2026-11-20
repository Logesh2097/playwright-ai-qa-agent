<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'

const props = defineProps<{ open: boolean; title: string }>()
const emit = defineEmits<{ close: [] }>()

// The native <dialog> gives focus trapping, Escape to close and the backdrop for free.
const dialog = ref<HTMLDialogElement | null>(null)

function sync(): void {
  const element = dialog.value
  if (!element) return
  if (props.open && !element.open) element.showModal?.()
  if (!props.open && element.open) element.close?.()
}

onMounted(sync)
watch(() => props.open, sync, { flush: 'post' })
</script>

<template>
  <dialog ref="dialog" class="modal" @close="emit('close')" @cancel.prevent="emit('close')">
    <div v-if="open" class="modal__body">
      <header class="modal__header">
        <h2>{{ title }}</h2>
        <button class="modal__close" type="button" @click="emit('close')">
          <span aria-hidden="true">×</span>
          <span class="visually-hidden">Close</span>
        </button>
      </header>
      <slot />
    </div>
  </dialog>
</template>

<style scoped>
.modal {
  width: min(40rem, calc(100vw - 2 * var(--space-4)));
  max-height: calc(100dvh - 2 * var(--space-4));
  padding: 0;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  color: var(--color-text);
  box-shadow: var(--shadow-md);
}

.modal::backdrop {
  background: var(--color-overlay);
}

.modal__body {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  padding: var(--space-5);
}

.modal__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}

.modal__close {
  flex: none;
  width: 2rem;
  height: 2rem;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-text-muted);
  font-size: var(--text-lg);
  cursor: pointer;
}

.modal__close:hover {
  background: var(--color-surface-muted);
}
</style>
