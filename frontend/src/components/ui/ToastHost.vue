<script setup lang="ts">
import { useNotificationsStore } from '@/stores/notifications'

const notifications = useNotificationsStore()
</script>

<template>
  <div class="toasts" aria-live="assertive">
    <div
      v-for="item in notifications.items"
      :key="item.id"
      class="toast"
      :class="`toast--${item.kind}`"
      :role="item.kind === 'error' ? 'alert' : 'status'"
    >
      <div class="toast__body">
        <p>{{ item.message }}</p>
        <p v-if="item.reference" class="toast__reference">{{ item.reference }}</p>
      </div>
      <button class="toast__close" type="button" @click="notifications.dismiss(item.id)">
        <span aria-hidden="true">×</span>
        <span class="visually-hidden">Dismiss</span>
      </button>
    </div>
  </div>
</template>

<style scoped>
.toasts {
  position: fixed;
  right: var(--space-4);
  bottom: var(--space-4);
  left: var(--space-4);
  z-index: 100;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: var(--space-2);
  pointer-events: none;
}

.toast {
  display: flex;
  align-items: flex-start;
  gap: var(--space-3);
  width: min(26rem, 100%);
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--color-border);
  border-left-width: 4px;
  border-radius: var(--radius-sm);
  background: var(--color-surface);
  box-shadow: var(--shadow-md);
  font-size: var(--text-sm);
  pointer-events: auto;
}

.toast--error {
  border-left-color: var(--color-danger);
}

.toast--success {
  border-left-color: var(--color-success);
}

.toast--info {
  border-left-color: var(--color-accent);
}

.toast__body {
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
}

.toast__reference {
  margin-top: var(--space-1);
  color: var(--color-text-muted);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
}

.toast__close {
  flex: none;
  width: 1.75rem;
  height: 1.75rem;
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-text-muted);
  font-size: var(--text-lg);
  line-height: 1;
  cursor: pointer;
}

.toast__close:hover {
  background: var(--color-surface-muted);
}
</style>
