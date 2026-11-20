<script setup lang="ts">
import { computed, ref } from 'vue'

/**
 * Shows text exactly as it is: logs, JSON, recorded values. Content is placed as
 * text, never as markup, and long content starts collapsed.
 */
const props = withDefaults(defineProps<{ content: string; label: string; maxLines?: number }>(), {
  maxLines: 30,
})

const expanded = ref(false)
const lines = computed(() => props.content.split('\n'))
const shown = computed(() =>
  expanded.value ? props.content : lines.value.slice(0, props.maxLines).join('\n'),
)
const hidden = computed(() => Math.max(0, lines.value.length - props.maxLines))
const copied = ref(false)

async function copy(): Promise<void> {
  try {
    await navigator.clipboard.writeText(props.content)
    copied.value = true
    setTimeout(() => (copied.value = false), 1500)
  } catch {
    copied.value = false
  }
}
</script>

<template>
  <figure class="code">
    <figcaption class="cluster cluster--between small">
      <span class="muted">{{ label }} · {{ lines.length }} lines</span>
      <button type="button" class="code__action" @click="copy">
        {{ copied ? 'Copied' : 'Copy' }}
      </button>
    </figcaption>
    <pre class="code__body" tabindex="0"><code>{{ shown }}</code></pre>
    <button
      v-if="hidden > 0"
      type="button"
      class="code__action small"
      @click="expanded = !expanded"
    >
      {{ expanded ? 'Show less' : `Show ${hidden} more lines` }}
    </button>
  </figure>
</template>

<style scoped>
.code {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin: 0;
  min-width: 0;
}

.code__body {
  max-height: 28rem;
  margin: 0;
  padding: var(--space-3);
  overflow: auto;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  background: var(--color-surface-muted);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.code__action {
  align-self: flex-start;
  padding: 0;
  border: 0;
  background: none;
  color: var(--color-accent);
  cursor: pointer;
  text-decoration: underline;
}
</style>
