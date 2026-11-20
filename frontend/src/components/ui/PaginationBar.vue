<script setup lang="ts">
import { computed } from 'vue'

import BaseButton from '@/components/ui/BaseButton.vue'

/** Offset paging. `total` is what the server says matches, not what is on the page. */
const props = defineProps<{ offset: number; limit: number; count: number; total: number }>()
const emit = defineEmits<{ change: [offset: number] }>()

const first = computed(() => (props.total === 0 ? 0 : props.offset + 1))
const last = computed(() => props.offset + props.count)
</script>

<template>
  <nav class="cluster cluster--between small" aria-label="Pages">
    <span class="muted">{{ first }}–{{ last }} of {{ total }}</span>
    <div class="cluster">
      <BaseButton
        variant="ghost"
        :disabled="offset === 0"
        @click="emit('change', Math.max(0, offset - limit))"
      >
        Previous
      </BaseButton>
      <BaseButton variant="ghost" :disabled="last >= total" @click="emit('change', offset + limit)">
        Next
      </BaseButton>
    </div>
  </nav>
</template>
