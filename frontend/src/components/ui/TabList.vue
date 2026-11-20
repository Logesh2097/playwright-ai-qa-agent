<script setup lang="ts">
/** Tabs kept in the address (`?tab=`), so a tab can be linked to and survives a reload. */
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const props = defineProps<{ tabs: { id: string; label: string }[]; label: string }>()
const route = useRoute()
const router = useRouter()

const active = computed(() => {
  const wanted = typeof route.query.tab === 'string' ? route.query.tab : ''
  return props.tabs.some((tab) => tab.id === wanted) ? wanted : (props.tabs[0]?.id ?? '')
})

function select(id: string): void {
  void router.replace({ query: { ...route.query, tab: id } })
}

defineExpose({ active })
</script>

<template>
  <div class="stack stack--lg">
    <div class="tabs" role="tablist" :aria-label="label">
      <button
        v-for="tab in tabs"
        :id="`tab-${tab.id}`"
        :key="tab.id"
        class="tabs__tab"
        :class="{ 'tabs__tab--active': tab.id === active }"
        type="button"
        role="tab"
        :aria-selected="tab.id === active"
        :aria-controls="`panel-${tab.id}`"
        @click="select(tab.id)"
      >
        {{ tab.label }}
      </button>
    </div>
    <div :id="`panel-${active}`" role="tabpanel" :aria-labelledby="`tab-${active}`">
      <slot :active="active" />
    </div>
  </div>
</template>

<style scoped>
.tabs {
  display: flex;
  gap: var(--space-1);
  overflow-x: auto;
  border-bottom: 1px solid var(--color-border);
}

.tabs__tab {
  padding: var(--space-2) var(--space-4);
  border: 0;
  border-bottom: 2px solid transparent;
  background: none;
  color: var(--color-text-muted);
  font: inherit;
  font-weight: 600;
  white-space: nowrap;
  cursor: pointer;
}

.tabs__tab--active {
  border-bottom-color: var(--color-accent);
  color: var(--color-text);
}
</style>
