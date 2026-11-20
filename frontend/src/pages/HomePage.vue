<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'

import { organizationApi } from '@/api/admin'
import ResultSummary from '@/components/domain/ResultSummary.vue'
import TrendChart from '@/components/domain/TrendChart.vue'
import DataTable, { type Column } from '@/components/ui/DataTable.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import StatCard from '@/components/ui/StatCard.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import { useAsync } from '@/composables/useAsync'
import { dateTime, label, percent, shortId, toneOf } from '@/pages/reportFormat'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const org = computed(() => auth.organizationId ?? '')
const days = ref(30)
const { data, error, loading, run } = useAsync(() =>
  organizationApi.dashboard(org.value, days.value),
)

const failingColumns: Column[] = [
  { key: 'case', label: 'Test case' },
  { key: 'project_name', label: 'Project' },
  { key: 'failures', label: 'Failures', numeric: true },
  { key: 'runs', label: 'Runs', numeric: true },
  { key: 'rate', label: 'Failure rate', numeric: true },
  { key: 'last', label: 'Last result' },
]
const recentColumns: Column[] = [
  { key: 'id', label: 'Execution' },
  { key: 'project_name', label: 'Project' },
  { key: 'started', label: 'Started' },
  { key: 'cases', label: 'Cases', numeric: true },
  { key: 'status', label: 'Final status' },
]

onMounted(run)
watch(days, () => run())
</script>

<template>
  <section class="stack stack--lg">
    <header class="cluster cluster--between">
      <div>
        <h1>Dashboard</h1>
        <p class="muted">Across the projects you can see. Counted from stored test results.</p>
      </div>
      <label class="cluster small">
        Period
        <select v-model.number="days" class="input period">
          <option :value="7">Last 7 days</option>
          <option :value="30">Last 30 days</option>
          <option :value="90">Last 90 days</option>
        </select>
      </label>
    </header>

    <LoadingState v-if="loading && !data" label="Loading figures…" />
    <ErrorState v-else-if="error || !data" :message="error ?? 'Nothing to show.'" />

    <template v-else>
      <p v-if="data.truncated" class="notice">
        This period holds more results than are read at once; the figures cover the most recent.
      </p>
      <div class="tiles">
        <StatCard
          label="Projects"
          :value="data.total_projects"
          :hint="`${data.active_projects} active`"
        />
        <StatCard label="Test executions" :value="data.total_executions" />
        <StatCard
          label="Passed tests"
          :value="data.results.passed"
          :hint="`Pass rate ${percent(data.results.pass_rate)}`"
          tone="success"
        />
        <StatCard
          label="Failed tests"
          :value="data.results.failed + data.results.error"
          :hint="`${data.results.failed} failed · ${data.results.error} error`"
          :tone="data.results.failed + data.results.error > 0 ? 'danger' : undefined"
        />
      </div>

      <EmptyState
        v-if="data.total_projects === 0"
        title="No projects yet"
        message="Create a project, register the application to test, and describe what to test."
      >
        <RouterLink :to="{ name: 'projects' }">Go to projects</RouterLink>
      </EmptyState>
      <EmptyState
        v-else-if="data.total_executions === 0"
        title="No executions in this period"
        message="Figures and trends appear once approved test cases have been run."
      />

      <template v-else>
        <section class="card stack">
          <h2>Test results</h2>
          <ResultSummary
            :totals="{
              ...data.results,
              total: data.results.ran + data.results.skipped + data.results.blocked,
            }"
            :pass-rate="data.results.pass_rate"
          />
        </section>

        <section class="card"><TrendChart :points="data.points" /></section>

        <section class="card stack">
          <h2>Frequently failing tests</h2>
          <DataTable
            :columns="failingColumns"
            :rows="data.frequently_failing"
            :row-key="(row) => row.case_id"
            caption="Test cases with the most failed or errored results"
            empty="No test failed in this period."
          >
            <template #case="{ row }">
              <RouterLink
                v-if="row.project_id"
                :to="{
                  name: 'case-history',
                  params: { projectId: row.project_id, caseId: row.case_id },
                }"
              >
                {{ row.case_key }}
              </RouterLink>
              {{ row.title }}
            </template>
            <template #rate="{ row }">{{ percent(row.failure_rate) }}</template>
            <template #last="{ row }">
              <StatusBadge :tone="toneOf(row.last_status)">{{
                label(row.last_status)
              }}</StatusBadge>
            </template>
          </DataTable>
        </section>

        <section class="card stack">
          <h2>Recent executions</h2>
          <DataTable
            :columns="recentColumns"
            :rows="data.recent_executions"
            :row-key="(row) => row.id"
            caption="The most recent executions"
          >
            <template #id="{ row }">
              <RouterLink
                class="mono"
                :to="{
                  name: 'execution',
                  params: { projectId: row.project_id, executionId: row.id },
                }"
              >
                {{ shortId(row.id) }}
              </RouterLink>
            </template>
            <template #project_name="{ row }">
              <RouterLink :to="{ name: 'project', params: { projectId: row.project_id } }">
                {{ row.project_name }}
              </RouterLink>
            </template>
            <template #started="{ row }">{{ dateTime(row.started_at ?? row.created_at) }}</template>
            <template #cases="{ row }">{{ row.totals.total ?? '—' }}</template>
            <template #status="{ row }">
              <StatusBadge :tone="toneOf(row.verdict)">
                {{ label(row.verdict ?? row.status) }}
              </StatusBadge>
            </template>
          </DataTable>
        </section>
      </template>
    </template>
  </section>
</template>

<style scoped>
.period {
  width: auto;
}
</style>
