<script setup lang="ts">
import { RouterView } from 'vue-router'

import ToastHost from '@/components/ui/ToastHost.vue'
import { env } from '@/config/env'
</script>

<template>
  <div class="auth">
    <main class="auth__panel card">
      <p class="auth__brand">{{ env.appName }}</p>
      <RouterView />
    </main>
    <ToastHost />
  </div>
</template>

<style scoped>
.auth {
  display: grid;
  place-items: center;
  min-height: 100dvh;
  padding: var(--space-4);
}

.auth__panel {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  width: min(26rem, 100%);
}

.auth__brand {
  color: var(--color-accent);
  font-weight: 700;
}
</style>
