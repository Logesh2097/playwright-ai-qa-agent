<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

import { casesApi, instructionsApi, suitesApi } from '@/api/testing'
import IssueList from '@/components/domain/IssueList.vue'
import BaseButton from '@/components/ui/BaseButton.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import FormField from '@/components/ui/FormField.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import { usePolling } from '@/composables/usePolling'
import { toUserMessage } from '@/errors/messages'
import { CASE_STATUS_LABELS, isWorking, STATUS_LABELS, statusTone } from '@/pages/instructionStatus'
import { useAuthStore } from '@/stores/auth'
import { useNotificationsStore } from '@/stores/notifications'
import type { CaseSummary, Instruction, Suite } from '@/types/api'

const props = defineProps<{ projectId: string; instructionId: string }>()

const auth = useAuthStore()
const notifications = useNotificationsStore()
const router = useRouter()
const org = computed(() => auth.organizationId ?? '')
const canApprove = computed(
  () => auth.role !== null && !['viewer', 'developer'].includes(auth.role),
)

const instruction = ref<Instruction | null>(null)
const cases = ref<CaseSummary[]>([])
const suite = ref<Suite | null>(null)
const loading = ref(true)
const loadError = ref<string | null>(null)
const busy = ref(false)

const analysis = computed(() => instruction.value?.analysis ?? null)
const hasCases = computed(() =>
  ['awaiting_approval', 'completed', 'generating'].includes(instruction.value?.status ?? ''),
)

function behaviour(objectiveId: string) {
  return analysis.value?.expected_behavior.find((b) => b.objective_id === objectiveId)
}

async function refresh(): Promise<void> {
  instruction.value = await instructionsApi.get(org.value, props.projectId, props.instructionId)
  if (hasCases.value) {
    cases.value = await casesApi.list(org.value, props.projectId, props.instructionId)
    const suites = await suitesApi.list(org.value, props.projectId)
    suite.value = suites.find((s) => s.instruction_id === props.instructionId) ?? null
  }
}

const polling = usePolling(refresh, () => isWorking(instruction.value?.status ?? 'failed'))

async function load(): Promise<void> {
  try {
    await refresh()
    document.title = analysis.value?.feature_name ?? 'Instruction'
    polling.start()
  } catch (error) {
    loadError.value = toUserMessage(error)
  } finally {
    loading.value = false
  }
}

/** Runs an action, then reloads and resumes polling if work started. */
async function act(action: () => Promise<unknown>, done?: string): Promise<void> {
  busy.value = true
  try {
    await action()
    await refresh()
    if (done) notifications.success(done)
    polling.start()
  } catch (error) {
    notifications.error(toUserMessage(error))
  } finally {
    busy.value = false
  }
}

// --- clarification --------------------------------------------------------------------------

const answers = reactive<Record<string, string>>({})

const sendAnswers = () =>
  act(async () => {
    const given = Object.entries(answers)
      .filter(([, answer]) => answer.trim() !== '')
      .map(([question_id, answer]) => ({ question_id, answer }))
    if (given.length === 0) throw new Error('Answer at least one question.')
    await instructionsApi.clarify(org.value, props.projectId, props.instructionId, given)
    for (const key of Object.keys(answers)) delete answers[key]
  })

const api = instructionsApi
const args = () => [org.value, props.projectId, props.instructionId] as const

// --- cases ----------------------------------------------------------------------------------

const selected = ref<string[]>([])
const feedback = ref('')

const regenerate = () =>
  act(async () => {
    await api.regenerate(...args(), selected.value, feedback.value)
    selected.value = []
    feedback.value = ''
  })

const approveSuite = () =>
  act(async () => {
    if (suite.value) await suitesApi.approve(org.value, props.projectId, suite.value.id)
  }, 'Suite approved.')

async function addCase(): Promise<void> {
  await router.push({
    name: 'case-new',
    params: { projectId: props.projectId },
    query: { instruction: props.instructionId },
  })
}

onMounted(load)
</script>

<template>
  <LoadingState v-if="loading" label="Loading…" />
  <ErrorState
    v-else-if="loadError || !instruction"
    :message="loadError ?? 'Instruction not found.'"
  />

  <section v-else class="stack stack--lg">
    <header class="stack">
      <RouterLink class="small" :to="{ name: 'instructions', params: { projectId } }">
        ← Instructions
      </RouterLink>
      <div class="cluster cluster--between">
        <h1>{{ analysis?.feature_name ?? 'Test instruction' }}</h1>
        <StatusBadge :tone="statusTone(instruction.status)">
          {{ STATUS_LABELS[instruction.status] }}
        </StatusBadge>
      </div>
      <blockquote class="quote">{{ instruction.text }}</blockquote>
    </header>

    <LoadingState
      v-if="isWorking(instruction.status)"
      :label="
        instruction.status === 'generating' ? 'Writing test cases…' : 'Analysing the requirement…'
      "
    />

    <div v-if="instruction.status === 'failed' && instruction.error" class="card stack">
      <p class="notice notice--danger">{{ instruction.error.message }}</p>
      <div>
        <BaseButton :loading="busy" @click="act(() => api.analyse(...args()))"
          >Try again</BaseButton
        >
      </div>
    </div>

    <!-- The structured requirement -->
    <section v-if="analysis" class="card stack">
      <div class="cluster cluster--between">
        <h2>Requirement</h2>
        <span v-if="instruction.analysis_source === 'human_edited'" class="tag"
          >edited by a person</span
        >
      </div>
      <p>{{ analysis.summary }}</p>

      <IssueList v-if="instruction.validation" :issues="instruction.validation.issues" />

      <table class="table">
        <thead>
          <tr>
            <th scope="col">Objective</th>
            <th scope="col">Expected result</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="objective in analysis.test_objectives" :key="objective.id">
            <td>
              <span class="tag">{{ objective.id }}</span> {{ objective.title }}
              <span class="small muted">· {{ objective.category.replace('_', ' ') }}</span>
            </td>
            <td>
              <template v-if="behaviour(objective.id)?.description">
                {{ behaviour(objective.id)?.description }}
                <span class="small muted">
                  · {{ behaviour(objective.id)?.source.replaceAll('_', ' ') }}
                </span>
              </template>
              <span v-else class="unknown">Not stated — must be answered, it is not guessed</span>
            </td>
          </tr>
        </tbody>
      </table>

      <dl class="facts">
        <div v-if="analysis.business_rules.length">
          <dt>Business rules</dt>
          <dd v-for="rule in analysis.business_rules" :key="rule.name">
            {{ rule.name }}: {{ rule.description }}
            <span v-if="!rule.value_known" class="unknown">(value not provided)</span>
          </dd>
        </div>
        <div v-if="analysis.required_test_data.length">
          <dt>Test data needed</dt>
          <dd v-for="item in analysis.required_test_data" :key="item.name">
            {{ item.name
            }}<template v-if="item.credential_role"> ({{ item.credential_role }})</template>
          </dd>
        </div>
        <div v-if="analysis.preconditions.length">
          <dt>Preconditions</dt>
          <dd v-for="item in analysis.preconditions" :key="item">{{ item }}</dd>
        </div>
        <div v-if="analysis.assumptions.length">
          <dt>Assumptions</dt>
          <dd v-for="item in analysis.assumptions" :key="item">{{ item }}</dd>
        </div>
      </dl>

      <form
        v-if="
          instruction.clarification_questions.length > 0 &&
          instruction.status === 'needs_clarification'
        "
        class="stack"
        @submit.prevent="sendAnswers"
      >
        <h3 class="subtitle">Questions</h3>
        <FormField
          v-for="question in instruction.clarification_questions"
          :key="question.id"
          v-slot="field"
          :label="question.question"
          :hint="
            question.options.length ? `For example: ${question.options.join(' / ')}` : undefined
          "
        >
          <textarea :id="field.id" v-model="answers[question.id]" class="input" rows="2" />
        </FormField>
        <div class="cluster">
          <BaseButton type="submit" :loading="busy">Send answers</BaseButton>
          <BaseButton variant="ghost" :loading="busy" @click="act(() => api.validate(...args()))">
            Check again
          </BaseButton>
        </div>
      </form>
      <div v-else-if="instruction.status === 'needs_clarification'">
        <BaseButton variant="secondary" :loading="busy" @click="act(() => api.validate(...args()))">
          Check again
        </BaseButton>
      </div>

      <div v-if="instruction.status === 'analysed'" class="cluster">
        <BaseButton
          v-if="canApprove"
          :loading="busy"
          @click="act(() => api.approve(...args()), 'Requirement approved.')"
        >
          Approve requirement
        </BaseButton>
        <p v-else class="small muted">A QA engineer or admin has to approve this requirement.</p>
      </div>
      <div v-if="instruction.status === 'approved'" class="stack">
        <p v-if="instruction.generation?.error" class="notice notice--danger">
          {{ instruction.generation.error.message }}
        </p>
        <div>
          <BaseButton :loading="busy" @click="act(() => api.generate(...args()))">
            Generate test cases
          </BaseButton>
        </div>
      </div>
    </section>

    <!-- Review of the generated cases -->
    <section v-if="hasCases && !isWorking(instruction.status)" class="card stack">
      <div class="cluster cluster--between">
        <div>
          <h2>Test cases</h2>
          <p v-if="instruction.generation" class="small muted">
            {{ cases.length }} cases
            <template v-if="instruction.generation.duplicates_removed">
              · {{ instruction.generation.duplicates_removed }} duplicates removed
            </template>
            <template v-if="instruction.generation.partial">
              · generation stopped early: {{ instruction.generation.error?.message }}
            </template>
          </p>
        </div>
        <div class="cluster">
          <RouterLink
            v-if="suite"
            :to="{ name: 'suite', params: { projectId, suiteId: suite.id } }"
          >
            Open suite
          </RouterLink>
          <BaseButton v-if="auth.canContribute" variant="secondary" @click="addCase">
            Add a case by hand
          </BaseButton>
        </div>
      </div>

      <ul class="list">
        <li v-for="item in cases" :key="item.id">
          <label v-if="auth.canContribute && instruction.status === 'awaiting_approval'">
            <input v-model="selected" type="checkbox" :value="item.id" />
            <span class="visually-hidden">Select {{ item.key }}</span>
          </label>
          <div class="grow">
            <RouterLink :to="{ name: 'case', params: { projectId, caseId: item.id } }">
              {{ item.key }} · {{ item.title }}
            </RouterLink>
            <p class="small muted">
              {{ item.category.replace('_', ' ') }} · {{ item.priority.toUpperCase() }} ·
              {{ item.version_source === 'ai' ? 'written by AI' : 'edited by a person' }}
              <template v-if="!item.objective_id"> · beyond the requirement</template>
            </p>
          </div>
          <span v-if="!item.has_expected_result" class="tag tag--warn">no expected result</span>
          <span v-else-if="item.blocking_issues > 0" class="tag tag--warn">
            {{ item.blocking_issues }} to fix
          </span>
          <StatusBadge :tone="item.status === 'approved' ? 'success' : 'neutral'">
            {{ CASE_STATUS_LABELS[item.status] }}
          </StatusBadge>
        </li>
      </ul>

      <form v-if="selected.length > 0" class="stack" @submit.prevent="regenerate">
        <FormField
          v-slot="field"
          :label="`What is wrong with the ${selected.length} selected case(s)?`"
          hint="They will be rewritten as new versions. Nothing is deleted."
        >
          <textarea :id="field.id" v-model="feedback" class="input" rows="2" />
        </FormField>
        <div>
          <BaseButton type="submit" variant="secondary" :loading="busy"
            >Regenerate selected</BaseButton
          >
        </div>
      </form>

      <div v-if="suite && suite.status !== 'approved' && canApprove">
        <BaseButton :loading="busy" @click="approveSuite">Approve the whole suite</BaseButton>
      </div>
      <p v-if="suite?.status === 'approved'" class="notice notice--success">
        The suite was approved on {{ new Date(suite.approved_at ?? '').toLocaleDateString() }}.
      </p>
    </section>
  </section>
</template>

<style scoped>
.quote {
  margin: 0;
  padding: var(--space-3) var(--space-4);
  border-left: 3px solid var(--color-border);
  color: var(--color-text-muted);
  overflow-wrap: anywhere;
}

.subtitle {
  margin: 0;
  font-size: var(--text-md);
}

.table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--text-sm);
}

.table th,
.table td {
  padding: var(--space-2) var(--space-3) var(--space-2) 0;
  border-top: 1px solid var(--color-border);
  text-align: left;
  vertical-align: top;
}

.table th {
  border-top: 0;
  color: var(--color-text-muted);
  font-size: var(--text-xs);
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.unknown {
  color: var(--color-warning);
  font-weight: 600;
}

.facts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
  gap: var(--space-4);
  margin: 0;
  font-size: var(--text-sm);
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
}

.tag--warn {
  background: var(--color-warning-bg);
  color: var(--color-warning);
}

@media (max-width: 640px) {
  .table,
  .table tbody,
  .table tr,
  .table td {
    display: block;
  }

  .table thead {
    display: none;
  }

  .table td {
    border-top: 0;
    padding-right: 0;
  }

  .table tr {
    padding: var(--space-2) 0;
    border-top: 1px solid var(--color-border);
  }
}
</style>
