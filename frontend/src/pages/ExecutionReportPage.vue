<script setup lang="ts">
/**
 * The test report of one execution. Everything shown comes from the report the
 * server builds; this page adds no figures of its own.
 *
 * Text recorded from the application under test (titles, messages, values) is
 * only ever placed with text interpolation, never as markup.
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { executionsApi } from '@/api/admin'
import { reportsApi, saveBlob, type ExportFormat } from '@/api/reports'
import ResultSummary from '@/components/domain/ResultSummary.vue'
import BaseButton from '@/components/ui/BaseButton.vue'
import CodeViewer from '@/components/ui/CodeViewer.vue'
import ScreenshotViewer from '@/components/ui/ScreenshotViewer.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import { toUserMessage } from '@/errors/messages'
import {
  dateTime,
  duration,
  fileSize,
  label,
  percent,
  shortId,
  showValue,
  toneOf,
} from '@/pages/reportFormat'
import { useAuthStore } from '@/stores/auth'
import { useNotificationsStore } from '@/stores/notifications'
import type {
  AiSuggestion,
  EvidenceRef,
  ExecutionReport,
  ResultDetail,
  StepDetail,
} from '@/types/reports'

const props = defineProps<{ projectId: string; executionId: string }>()

const auth = useAuthStore()
const notifications = useNotificationsStore()
const org = computed(() => auth.organizationId ?? '')
const report = ref<ExecutionReport | null>(null)
const loading = ref(true)
const loadError = ref<string | null>(null)
const exporting = ref<ExportFormat | null>(null)
const open = reactive(new Set<string>())
/** Screenshots fetched so far: evidence ID -> object URL. Released when the page closes. */
const images = reactive(new Map<string, string>())

const TOTALS = [
  ['total', 'Total'],
  ['passed', 'Passed'],
  ['failed', 'Failed'],
  ['error', 'Error'],
  ['skipped', 'Skipped'],
  ['blocked', 'Blocked'],
  ['flaky', 'Flaky'],
  ['needs_review', 'Needs review'],
] as const

async function load(): Promise<void> {
  try {
    report.value = await reportsApi.report(org.value, props.projectId, props.executionId)
    // Results that did not pass start opened.
    for (const result of report.value.results) {
      if (result.status === 'failed' || result.status === 'error') open.add(result.id)
    }
  } catch (error) {
    loadError.value = toUserMessage(error)
  } finally {
    loading.value = false
  }
}

function toggle(id: string): void {
  if (open.has(id)) open.delete(id)
  else open.add(id)
}

/** The steps of the last attempt; earlier attempts are counted in the heading. */
function lastAttempt(result: ResultDetail): StepDetail[] {
  const last = Math.max(0, ...result.steps.map((step) => step.attempt))
  return result.steps.filter((step) => step.attempt === last)
}

async function exportAs(format: ExportFormat): Promise<void> {
  exporting.value = format
  try {
    const file = await reportsApi.exportReport(
      org.value,
      props.projectId,
      props.executionId,
      format,
    )
    saveBlob(file.blob, file.filename ?? `test-report-${props.executionId}.${format}`)
  } catch (error) {
    notifications.error(toUserMessage(error))
  } finally {
    exporting.value = null
  }
}

async function showScreenshot(item: EvidenceRef): Promise<void> {
  if (images.has(item.id)) return
  try {
    const file = await reportsApi.evidence(org.value, props.projectId, props.executionId, item.id)
    // Only ever shown as an image, whatever the bytes are.
    images.set(item.id, URL.createObjectURL(new Blob([file.blob], { type: 'image/png' })))
  } catch (error) {
    notifications.error(toUserMessage(error))
  }
}

/** Log files fetched so far: evidence ID -> text. */
const logs = reactive(new Map<string, string>())
const analysing = ref<string | null>(null)

async function showLog(item: EvidenceRef): Promise<void> {
  if (logs.has(item.id)) {
    logs.delete(item.id)
    return
  }
  try {
    const file = await reportsApi.evidence(org.value, props.projectId, props.executionId, item.id)
    const text = await file.blob.text()
    try {
      logs.set(item.id, JSON.stringify(JSON.parse(text), null, 2))
    } catch {
      logs.set(item.id, text)
    }
  } catch (error) {
    notifications.error(toUserMessage(error))
  }
}

/** Ask for the AI's reading of one failure. It is stored and shown as a suggestion only. */
async function analyse(result: ResultDetail): Promise<void> {
  analysing.value = result.id
  try {
    const answer = await executionsApi.analyse(
      org.value,
      props.projectId,
      props.executionId,
      result.id,
    )
    if (answer.ai_suggestion) {
      result.ai_suggestion = {
        ...(answer.ai_suggestion as unknown as AiSuggestion),
        advisory: true,
        note: 'AI-generated suggestion. Not a confirmed root cause, and it does not affect any status.',
      }
    }
  } catch (error) {
    notifications.error(toUserMessage(error))
  } finally {
    analysing.value = null
  }
}

const unfinished = computed(
  () => report.value !== null && report.value.execution.finished_at === null,
)

async function download(item: EvidenceRef): Promise<void> {
  try {
    const address = await reportsApi.evidenceUrl(
      org.value,
      props.projectId,
      props.executionId,
      item.id,
    )
    window.location.assign(address.url)
  } catch (error) {
    notifications.error(toUserMessage(error))
  }
}

onMounted(load)
onBeforeUnmount(() => {
  for (const url of images.values()) URL.revokeObjectURL(url)
})
</script>

<template>
  <LoadingState v-if="loading" label="Loading report…" />
  <ErrorState v-else-if="loadError || !report" :message="loadError ?? 'Report not found.'">
    <RouterLink :to="{ name: 'executions', params: { projectId } }">Back to executions</RouterLink>
  </ErrorState>

  <article v-else class="stack stack--lg">
    <header class="stack">
      <RouterLink class="small" :to="{ name: 'executions', params: { projectId } }">
        ← Execution history
      </RouterLink>
      <div class="cluster cluster--between">
        <div class="cluster">
          <h1>Test report</h1>
          <StatusBadge :tone="toneOf(report.execution.verdict)">
            {{ label(report.execution.verdict ?? report.execution.status) }}
          </StatusBadge>
        </div>
        <div class="cluster">
          <BaseButton
            v-for="format in ['pdf', 'csv', 'json'] as const"
            :key="format"
            variant="ghost"
            :loading="exporting === format"
            @click="exportAs(format)"
          >
            Export {{ format.toUpperCase() }}
          </BaseButton>
        </div>
      </div>
    </header>

    <p v-if="unfinished" class="notice">
      This execution has not finished; the report is provisional.
      <RouterLink :to="{ name: 'execution-live', params: { projectId, executionId } }">
        Watch it live
      </RouterLink>
    </p>

    <section class="card stack">
      <ResultSummary :totals="{ ...report.totals }" :pass-rate="report.totals.pass_rate" />
    </section>

    <section class="card">
      <dl class="facts">
        <div>
          <dt>Project</dt>
          <dd>{{ report.project.name }}</dd>
        </div>
        <div>
          <dt>Application</dt>
          <dd>{{ report.application?.name ?? '—' }}</dd>
        </div>
        <div>
          <dt>Environment</dt>
          <dd>
            <template v-if="report.environment">
              {{ report.environment.name }} ({{ report.environment.kind }})
              <span class="mono muted">{{ report.environment.base_url }}</span>
            </template>
            <template v-else>—</template>
          </dd>
        </div>
        <div>
          <dt>Test suite</dt>
          <dd>{{ report.suite?.name ?? '—' }}</dd>
        </div>
        <div>
          <dt>Execution ID</dt>
          <dd class="mono">{{ report.execution.id }}</dd>
        </div>
        <div>
          <dt>Started</dt>
          <dd>{{ dateTime(report.execution.started_at) }}</dd>
        </div>
        <div>
          <dt>Finished</dt>
          <dd>{{ dateTime(report.execution.finished_at) }}</dd>
        </div>
        <div>
          <dt>Duration</dt>
          <dd>{{ duration(report.execution.duration_ms) }}</dd>
        </div>
        <div>
          <dt>Started by</dt>
          <dd>{{ report.execution.triggered_by ?? '—' }}</dd>
        </div>
        <div>
          <dt>Browser</dt>
          <dd>{{ report.execution.browser ?? '—' }}</dd>
        </div>
      </dl>
      <div v-if="report.instruction" class="stack instruction">
        <h2>Testing instruction</h2>
        <p class="pre">{{ report.instruction.text }}</p>
      </div>
    </section>

    <section class="tiles" aria-label="Totals">
      <div v-for="[key, name] in TOTALS" :key="key" class="card tile">
        <span class="tile__value">{{ report.totals[key] }}</span>
        <span class="tile__label">{{ name }}</span>
      </div>
      <div class="card tile">
        <span class="tile__value">{{ percent(report.totals.pass_rate) }}</span>
        <span class="tile__label">Pass rate of cases run</span>
      </div>
    </section>

    <section v-if="report.failures.length > 0" class="card stack">
      <h2>Failure summary</h2>
      <ul class="list">
        <li v-for="failure in report.failures" :key="failure.result_id">
          <div class="grow">
            <a :href="`#result-${failure.result_id}`" @click="open.add(failure.result_id)">
              {{ failure.case_key }}
            </a>
            {{ failure.title }}
            <p class="small muted">
              {{ failure.failed_step
              }}<template v-if="failure.summary"> · {{ failure.summary }}</template>
            </p>
          </div>
          <span v-if="failure.failure_category" class="tag">
            {{ label(failure.failure_category) }}
          </span>
          <StatusBadge :tone="toneOf(failure.status)">{{ label(failure.status) }}</StatusBadge>
        </li>
      </ul>
      <p class="small muted">Categories are assigned by fixed rules from what was recorded.</p>
    </section>

    <section class="stack">
      <h2>Test cases</h2>
      <div
        v-for="result in report.results"
        :id="`result-${result.id}`"
        :key="result.id"
        class="card stack result"
      >
        <button
          type="button"
          class="result__head cluster cluster--between"
          :aria-expanded="open.has(result.id)"
          @click="toggle(result.id)"
        >
          <span class="grow">
            <strong>{{ result.case_key }}</strong> {{ result.title }}
          </span>
          <span class="small muted">{{ duration(result.duration_ms) }}</span>
          <span v-if="result.healed" class="tag">locator replaced</span>
          <StatusBadge :tone="toneOf(result.status)">{{ label(result.status) }}</StatusBadge>
        </button>

        <div v-if="open.has(result.id)" class="stack">
          <dl class="facts">
            <div v-if="result.failure_category">
              <dt>Category (by rules)</dt>
              <dd>{{ label(result.failure_category) }}</dd>
            </div>
            <div v-if="result.failure_summary">
              <dt>Summary</dt>
              <dd>{{ result.failure_summary }}</dd>
            </div>
            <div v-if="result.expected_result">
              <dt>Expected result</dt>
              <dd>{{ result.expected_result }}</dd>
            </div>
            <div>
              <dt>Attempts</dt>
              <dd>{{ result.attempts }}</dd>
            </div>
            <div>
              <dt>Started</dt>
              <dd>{{ dateTime(result.started_at) }}</dd>
            </div>
            <div>
              <dt>Case</dt>
              <dd>
                <RouterLink
                  :to="{ name: 'case-history', params: { projectId, caseId: result.case_id } }"
                >
                  Result history
                </RouterLink>
              </dd>
            </div>
          </dl>

          <div v-if="result.steps.length > 0" class="table-wrap">
            <table class="table">
              <thead>
                <tr>
                  <th class="num">#</th>
                  <th>Step</th>
                  <th>Result</th>
                  <th>Expected</th>
                  <th>Found</th>
                  <th class="num">Time</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="step in lastAttempt(result)" :key="`${step.attempt}-${step.position}`">
                  <td class="num">{{ step.position }}</td>
                  <td>
                    {{ step.description ?? step.action }}
                    <span class="tag">{{ step.action }}</span>
                    <span v-if="step.healed_from" class="tag">locator replaced</span>
                    <p v-if="step.error_message" class="small step-error">
                      {{ step.error_message }}
                    </p>
                  </td>
                  <td>
                    <StatusBadge :tone="toneOf(step.status)">{{ step.status }}</StatusBadge>
                  </td>
                  <td class="mono">{{ showValue(step.expected) }}</td>
                  <td class="mono">{{ showValue(step.actual) }}</td>
                  <td class="num">{{ duration(step.duration_ms) }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div v-if="result.console_errors.length + result.page_errors.length > 0" class="stack">
            <h3>Console errors</h3>
            <ul class="plain mono">
              <li
                v-for="(line, index) in [...result.console_errors, ...result.page_errors]"
                :key="index"
              >
                {{ line }}
              </li>
            </ul>
          </div>
          <div v-if="result.network_errors.length > 0" class="stack">
            <h3>Network errors</h3>
            <ul class="plain mono">
              <li v-for="(item, index) in result.network_errors" :key="index">
                {{ showValue(item) }}
              </li>
            </ul>
          </div>

          <div v-if="result.evidence.length > 0" class="stack">
            <h3>Evidence</h3>
            <ul class="list">
              <li v-for="item in result.evidence" :key="item.id">
                <span class="grow">
                  {{ label(item.kind) }}
                  <template v-if="item.step_position"> · step {{ item.step_position }}</template>
                  <span class="small muted"> · {{ fileSize(item.size_bytes) }}</span>
                  <span v-if="item.expires_at" class="small muted">
                    · kept until {{ dateTime(item.expires_at) }}
                  </span>
                </span>
                <BaseButton
                  v-if="item.kind === 'screenshot' && !images.has(item.id)"
                  variant="ghost"
                  @click="showScreenshot(item)"
                >
                  Show
                </BaseButton>
                <BaseButton
                  v-if="item.kind === 'console_log' || item.kind === 'network_log'"
                  variant="ghost"
                  @click="showLog(item)"
                >
                  {{ logs.has(item.id) ? 'Hide' : 'View' }}
                </BaseButton>
                <BaseButton variant="ghost" @click="download(item)">Download</BaseButton>
                <ScreenshotViewer
                  v-if="images.has(item.id)"
                  :src="images.get(item.id) ?? ''"
                  :alt="`Screenshot of ${result.case_key}${item.step_position ? ` at step ${item.step_position}` : ''}`"
                  :caption="`${result.case_key}${item.step_position ? ` · step ${item.step_position}` : ''}`"
                />
                <div v-if="logs.has(item.id)" class="log">
                  <CodeViewer :content="logs.get(item.id) ?? ''" :label="label(item.kind)" />
                </div>
              </li>
            </ul>
          </div>

          <div
            v-if="
              !result.ai_suggestion &&
              auth.canContribute &&
              (result.status === 'failed' || result.status === 'error')
            "
            class="cluster"
          >
            <BaseButton variant="ghost" :loading="analysing === result.id" @click="analyse(result)">
              Ask the AI for a failure analysis
            </BaseButton>
            <span class="small muted">A suggestion only. It cannot change the result.</span>
          </div>
          <aside v-if="result.ai_suggestion" class="suggestion stack">
            <div class="cluster">
              <strong>AI-generated suggestion</strong>
              <span class="tag">not a confirmed root cause</span>
            </div>
            <p class="small muted">{{ result.ai_suggestion.note }}</p>
            <dl class="facts">
              <div v-if="result.ai_suggestion.summary">
                <dt>Summary</dt>
                <dd>{{ result.ai_suggestion.summary }}</dd>
              </div>
              <div v-if="result.ai_suggestion.possible_root_cause">
                <dt>Possible cause</dt>
                <dd>{{ result.ai_suggestion.possible_root_cause }}</dd>
              </div>
              <div v-if="result.ai_suggestion.impact">
                <dt>Impact</dt>
                <dd>{{ result.ai_suggestion.impact }}</dd>
              </div>
            </dl>
            <ol v-if="result.ai_suggestion.debugging_steps?.length" class="small">
              <li v-for="(text, index) in result.ai_suggestion.debugging_steps" :key="index">
                {{ text }}
              </li>
            </ol>
          </aside>
        </div>
      </div>
    </section>

    <footer class="stack small muted">
      <p>
        Report generated {{ dateTime(report.generated_at) }} · execution
        {{ shortId(report.execution.id) }}
      </p>
      <ul class="plain">
        <li v-for="(note, index) in report.notes" :key="index">{{ note }}</li>
      </ul>
    </footer>
  </article>
</template>

<style scoped>
.facts {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(14rem, 1fr));
  gap: var(--space-3) var(--space-5);
  margin: 0;
}

.facts dt {
  color: var(--color-text-muted);
  font-size: var(--text-xs);
  font-weight: 600;
}

.facts dd {
  margin: 0;
  overflow-wrap: anywhere;
}

.instruction {
  margin-top: var(--space-5);
  padding-top: var(--space-4);
  border-top: 1px solid var(--color-border);
}

.pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.result__head {
  width: 100%;
  padding: 0;
  border: 0;
  background: none;
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.step-error {
  margin: var(--space-1) 0 0;
  color: var(--color-danger);
  overflow-wrap: anywhere;
}

.plain {
  margin: 0;
  padding: 0;
  list-style: none;
}

.plain li {
  padding: var(--space-1) 0;
}

.log {
  flex-basis: 100%;
  min-width: 0;
}

.shot {
  flex-basis: 100%;
  max-width: 100%;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
}

.suggestion {
  padding: var(--space-4);
  border: 1px dashed var(--color-border);
  border-radius: var(--radius-sm);
  background: var(--color-surface-muted);
}
</style>
