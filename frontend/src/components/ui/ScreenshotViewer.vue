<script setup lang="ts">
import { ref } from 'vue'

import BaseModal from '@/components/ui/BaseModal.vue'

/**
 * A screenshot as a thumbnail that opens full size. `src` is an object URL of a
 * file fetched with the session; it is only ever used as an image.
 */
defineProps<{ src: string; alt: string; caption?: string }>()
const open = ref(false)
</script>

<template>
  <figure class="shot">
    <button type="button" class="shot__button" :aria-label="`Enlarge: ${alt}`" @click="open = true">
      <img class="shot__thumb" :src="src" :alt="alt" loading="lazy" />
    </button>
    <figcaption v-if="caption" class="small muted">{{ caption }}</figcaption>
    <BaseModal :open="open" :title="caption ?? 'Screenshot'" @close="open = false">
      <img class="shot__full" :src="src" :alt="alt" />
    </BaseModal>
  </figure>
</template>

<style scoped>
.shot {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin: 0;
  flex-basis: 100%;
}

.shot__button {
  padding: 0;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: var(--color-surface-muted);
  cursor: zoom-in;
  overflow: hidden;
}

.shot__thumb {
  display: block;
  width: 100%;
  max-height: 22rem;
  object-fit: contain;
  object-position: top left;
}

.shot__full {
  display: block;
  max-width: 100%;
  margin: var(--space-4);
}
</style>
