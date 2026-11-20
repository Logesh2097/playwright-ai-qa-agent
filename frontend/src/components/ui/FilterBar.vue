<script setup lang="ts">
/** A row of filters above a list: an optional search box, then whatever the page adds. */
const search = defineModel<string>('search', { default: '' })
defineProps<{ searchLabel?: string }>()
</script>

<template>
  <form class="filters" role="search" @submit.prevent>
    <label v-if="searchLabel" class="filters__search">
      <span class="visually-hidden">{{ searchLabel }}</span>
      <input v-model.trim="search" class="input" type="search" :placeholder="searchLabel" />
    </label>
    <slot />
  </form>
</template>

<style scoped>
.filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-3);
}

.filters__search {
  flex: 1 1 14rem;
  max-width: 24rem;
}

.filters :deep(select.input) {
  width: auto;
}
</style>
