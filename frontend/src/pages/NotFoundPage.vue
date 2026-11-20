<script setup lang="ts">
import { RouterLink } from 'vue-router'
</script>

<template>
  <section class="not-found">
    <h1>Page not found</h1>
    <p>The page you're looking for doesn't exist or has moved.</p>
    <RouterLink :to="{ name: 'projects' }">Go to your projects</RouterLink>
  </section>
</template>

<style scoped>
.not-found {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.not-found p {
  color: var(--color-text-muted);
}
</style>
