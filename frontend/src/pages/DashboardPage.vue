<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'

import { reportsApi } from '@/api/reports'
import TrendChart from '@/components/domain/TrendChart.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import { toUserMessage } from '@/errors/messages'
import { dateTime, duration, label, percent, shortId, toneOf } from '@/pages/reportFormat'
import { useAuthStore } from '@/stores/auth'
import type { Dashboard, Trend } from '@/types/reports'

const props = defineProps<{ projectId: string }>()

const auth = useAuthStore()
const org = computed(() => auth.organizationId ?? '')
const days = ref(30)
const dashboard = ref<Dashboard | null>(null)
const trend = ref<Trend | null>(null)
const loading = ref(true)
const loadError = ref<string | null>(null)

async function load(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    ;[dashboard.value, trend.value] = await Promise.all([
      reportsApi.dashboard(org.value, props.projectId, days.value),
      reportsApi.trends(org.value, props.projectId, days.value),
    ])
  } catch (error) {
    loadError.value = toUserMessage(error)
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(days, load)
</script>

<template>
  <section class="stack stack--lg">
    <header class="stack">
      <RouterLink class="small" :to="{ name: 'project', params: { projectId } }">
        ← Project
      </RouterLink>
      <div class="cluster cluster--between">
        <div>
          <h1>Test dashboard</h1>
          <p class="muted">Figures are counted from stored test results.</p>
        </div>
        <label class="cluster small">
          Period
          <select v-model.number="days" class="input period">
            <option :value="7">Last 7 days</option>
            <option :value="30">Last 30 days</option>
            <option :value="90">Last 90 days</option>
          </select>
        </label>
      </div>
    </header>

    <LoadingState v-if="loading" label="Loading figures…" />
    <ErrorState v-else-if="loadError || !dashboard" :message="loadError ?? 'Nothing to show.'" />

    <template v-else>
      <p v-if="dashboard.truncated" class="notice">
        This period holds more results than are read at once; the figures cover the most recent.
      </p>
      <EmptyState
        v-if="dashboard.total_executions === 0"
        title="No executions in this period"
        message="Figures appear once approved test cases have been run."
      />

      <template v-else>
        <div class="tiles">
          <div class="card tile">
            <span class="tile__value">{{ dashboard.total_executions }}</span>
            <span class="tile__label">Executions</span>
          </div>
          <div class="card tile">
            <span class="tile__value">{{ percent(dashboard.results.pass_rate) }}</span>
            <span class="tile__label"
              >Pass rate · {{ dashboard.results.passed }} of {{ dashboard.results.ran }} cases
              run</span
            >
          </div>
          <div class="card tile">
            <span class="tile__value">{{ percent(dashboard.results.failure_rate) }}</span>
            <span class="tile__label">Failure rate · failed or error</span>
          </div>
          <div class="card tile">
            <span class="tile__value">{{ dashboard.flaky_test_count }}</span>
            <span class="tile__label">Flaky test cases</span>
          </div>
          <div class="card tile">
            <span class="tile__value">{{ duration(dashboard.average_execution_duration_ms) }}</span>
            <span class="tile__label">Average execution time</span>
          </div>
          <div class="card tile">
            <span class="tile__value">{{ percent(dashboard.execution_pass_rate) }}</span>
            <span class="tile__label">Executions fully passed</span>
          </div>
        </div>

        <div v-if="trend" class="card">
          <TrendChart :points="trend.points" />
        </div>

        <section class="card stack">
          <h2>Test coverage indicators</h2>
          <div class="tiles">
            <div class="tile">
              <span class="tile__value">{{ dashboard.coverage.test_cases }}</span>
              <span class="tile__label">Test cases</span>
            </div>
            <div class="tile">
              <span class="tile__value">{{ percent(dashboard.coverage.automated_share) }}</span>
              <span class="tile__label"
                >Automated · {{ dashboard.coverage.automated }} of
                {{ dashboard.coverage.approved }} approved</span
              >
            </div>
            <div class="tile">
              <span class="tile__value">{{ percent(dashboard.coverage.executed_share) }}</span>
              <span class="tile__label"
                >Run in this period · {{ dashboard.coverage.executed }}</span
              >
            </div>
            <div class="tile">
              <span class="tile__value">{{ percent(dashboard.coverage.passing_share) }}</span>
              <span class="tile__label"
                >Passing at last run · {{ dashboard.coverage.passing }}</span
              >
            </div>
          </div>
          <p class="small muted">{{ dashboard.coverage.note }}</p>
        </section>

        <section class="card stack">
          <h2>Frequently failing test cases</h2>
          <p v-if="dashboard.frequently_failing.length === 0" class="muted">
            No test case failed in this period.
          </p>
          <div v-else class="table-wrap">
            <table class="table">
              <thead>
                <tr>
                  <th>Case</th>
                  <th class="num">Failures</th>
                  <th class="num">Runs</th>
                  <th class="num">Failure rate</th>
                  <th>Usual category</th>
                  <th>Last result</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="item in dashboard.frequently_failing" :key="item.case_id">
                  <td>
                    <RouterLink
                      :to="{ name: 'case-history', params: { projectId, caseId: item.case_id } }"
                    >
                      {{ item.case_key }}
                    </RouterLink>
                    {{ item.title }}
                  </td>
                  <td class="num">{{ item.failures }}</td>
                  <td class="num">{{ item.runs }}</td>
                  <td class="num">{{ percent(item.failure_rate) }}</td>
                  <td>{{ label(item.most_common_category) }}</td>
                  <td>
                    <StatusBadge :tone="toneOf(item.last_status)">
                      {{ label(item.last_status) }}
                    </StatusBadge>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <section class="card stack">
          <div class="cluster cluster--between">
            <h2>Recent executions</h2>
            <RouterLink :to="{ name: 'executions', params: { projectId } }">
              All executions
            </RouterLink>
          </div>
          <ul class="list">
            <li v-for="execution in dashboard.recent_executions" :key="execution.id">
              <RouterLink
                class="grow mono"
                :to="{ name: 'execution', params: { projectId, executionId: execution.id } }"
              >
                {{ shortId(execution.id) }}
              </RouterLink>
              <span class="small muted">{{ dateTime(execution.created_at) }}</span>
              <span class="small muted">{{ execution.totals.total ?? 0 }} cases</span>
              <StatusBadge :tone="toneOf(execution.verdict)">
                {{ label(execution.verdict ?? execution.status) }}
              </StatusBadge>
            </li>
          </ul>
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
