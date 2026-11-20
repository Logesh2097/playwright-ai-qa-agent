<script setup lang="ts" generic="Row extends object">
/**
 * A table with a caption for screen readers, a slot per column and an empty state.
 * Cell content is text unless a column slot renders something else.
 */
export interface Column {
  key: string
  label: string
  numeric?: boolean
}

defineProps<{
  columns: Column[]
  rows: Row[]
  rowKey: (row: Row) => string
  caption: string
  empty?: string
}>()

function cell(row: Row, key: string): unknown {
  return (row as Record<string, unknown>)[key] ?? '—'
}
</script>

<template>
  <p v-if="rows.length === 0" class="muted table-empty">{{ empty ?? 'Nothing to show.' }}</p>
  <div v-else class="table-wrap">
    <table class="table">
      <caption class="visually-hidden">
        {{
          caption
        }}
      </caption>
      <thead>
        <tr>
          <th
            v-for="column in columns"
            :key="column.key"
            :class="{ num: column.numeric }"
            scope="col"
          >
            {{ column.label }}
          </th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="rowKey(row)">
          <td v-for="column in columns" :key="column.key" :class="{ num: column.numeric }">
            <slot :name="column.key" :row="row">
              {{ cell(row, column.key) }}
            </slot>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.table-empty {
  margin: 0;
  padding: var(--space-4) 0;
}
</style>
