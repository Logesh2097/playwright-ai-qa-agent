<script setup lang="ts">
import { computed, onMounted } from 'vue'

import BaseButton from '@/components/ui/BaseButton.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import { useSystemStore } from '@/stores/system'

const system = useSystemStore()

const DEPENDENCY_LABELS: Record<string, string> = { postgres: 'PostgreSQL', redis: 'Redis' }

const dependencies = computed(() =>
  Object.entries(system.readiness?.checks ?? {}).map(([name, check]) => ({
    name,
    label: DEPENDENCY_LABELS[name] ?? name,
    ...check,
  })),
)

const summary = computed(() => {
  switch (system.state) {
    case 'ready':
      return { tone: 'success', text: 'All systems ready' } as const
    case 'degraded':
      return { tone: 'warning', text: 'A dependency is unavailable' } as const
    case 'unreachable':
      return { tone: 'danger', text: 'API unreachable' } as const
    default:
      return { tone: 'neutral', text: 'Checking…' } as const
  }
})

onMounted(() => system.refresh())
</script>

<template>
  <section class="page">
    <header class="page__header">
      <div>
        <h1>Overview</h1>
        <p class="page__lead">Status of the platform services this app depends on.</p>
      </div>
      <BaseButton variant="secondary" :loading="system.loading" @click="system.refresh()">
        Refresh
      </BaseButton>
    </header>

    <LoadingState v-if="system.loading && !system.checkedAt" label="Checking services…" />

    <ErrorState
      v-else-if="system.error"
      title="Can't reach the API"
      :message="system.error"
      retryable
      :retrying="system.loading"
      @retry="system.refresh()"
    />

    <template v-else-if="system.readiness">
      <div class="card">
        <div class="card__row">
          <h2>System status</h2>
          <StatusBadge :tone="summary.tone">{{ summary.text }}</StatusBadge>
        </div>

        <ul class="dependencies">
          <li v-for="dependency in dependencies" :key="dependency.name" class="dependencies__item">
            <span class="dependencies__name">{{ dependency.label }}</span>
            <span class="dependencies__latency">{{ dependency.latency_ms.toFixed(1) }} ms</span>
            <StatusBadge :tone="dependency.status === 'ok' ? 'success' : 'danger'">
              {{ dependency.status === 'ok' ? 'Connected' : (dependency.error ?? 'Error') }}
            </StatusBadge>
          </li>
        </ul>
      </div>

      <dl v-if="system.meta" class="card facts">
        <div>
          <dt>Service</dt>
          <dd>{{ system.meta.name }}</dd>
        </div>
        <div>
          <dt>Version</dt>
          <dd>{{ system.meta.version }}</dd>
        </div>
        <div>
          <dt>Environment</dt>
          <dd>{{ system.meta.environment }}</dd>
        </div>
        <div>
          <dt>API</dt>
          <dd>{{ system.meta.api_version }}</dd>
        </div>
      </dl>
    </template>
  </section>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
}

.page__header {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-4);
}

.page__lead {
  margin-top: var(--space-1);
  color: var(--color-text-muted);
}

.card {
  padding: var(--space-5);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
}

.card__row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
}

.dependencies {
  margin: var(--space-4) 0 0;
  padding: 0;
  list-style: none;
}

.dependencies__item {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3) 0;
  border-top: 1px solid var(--color-border);
}

.dependencies__name {
  flex: 1;
  min-width: 8rem;
  font-weight: 600;
}

.dependencies__latency {
  color: var(--color-text-muted);
  font-size: var(--text-sm);
  font-variant-numeric: tabular-nums;
}

.facts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
  gap: var(--space-4);
  margin: 0;
}

.facts dt {
  color: var(--color-text-muted);
  font-size: var(--text-xs);
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.facts dd {
  margin: var(--space-1) 0 0;
  font-weight: 600;
  overflow-wrap: anywhere;
}
</style>
