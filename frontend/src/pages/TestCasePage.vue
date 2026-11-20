<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import { applicationsApi } from '@/api/projects'
import { casesApi, instructionsApi } from '@/api/testing'
import CaseForm from '@/components/domain/CaseForm.vue'
import IssueList from '@/components/domain/IssueList.vue'
import BaseButton from '@/components/ui/BaseButton.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import { toUserMessage } from '@/errors/messages'
import { CASE_STATUS_LABELS } from '@/pages/instructionStatus'
import { useAuthStore } from '@/stores/auth'
import { useNotificationsStore } from '@/stores/notifications'
import type { CaseContent, CaseVersion, Instruction, TestCase } from '@/types/api'

/** Without `caseId` this page is the form for adding a case by hand. */
const props = defineProps<{ projectId: string; caseId?: string }>()

const auth = useAuthStore()
const notifications = useNotificationsStore()
const route = useRoute()
const router = useRouter()
const org = computed(() => auth.organizationId ?? '')
const canApprove = computed(
  () => auth.role !== null && !['viewer', 'developer'].includes(auth.role),
)

const testCase = ref<TestCase | null>(null)
const versions = ref<CaseVersion[]>([])
const shownVersion = ref<CaseVersion | null>(null)
const instruction = ref<Instruction | null>(null)
const editing = ref(!props.caseId)
const loading = ref(true)
const loadError = ref<string | null>(null)
const busy = ref(false)

const instructionId = computed(
  () => testCase.value?.instruction_id ?? (route.query.instruction as string | undefined) ?? null,
)
const objectives = computed(() => instruction.value?.analysis?.test_objectives ?? [])
/** The content on screen: an older version if one was picked, else the current one. */
const content = computed<CaseContent | null>(
  () => shownVersion.value?.content ?? testCase.value?.content ?? null,
)
const backTo = computed(() =>
  instructionId.value
    ? {
        name: 'instruction',
        params: { projectId: props.projectId, instructionId: instructionId.value },
      }
    : { name: 'project', params: { projectId: props.projectId } },
)

async function load(): Promise<void> {
  loading.value = true
  try {
    if (props.caseId) {
      ;[testCase.value, versions.value] = await Promise.all([
        casesApi.get(org.value, props.projectId, props.caseId),
        casesApi.versions(org.value, props.projectId, props.caseId),
      ])
      document.title = `${testCase.value.key} ${testCase.value.title}`
    }
    if (instructionId.value) {
      instruction.value = await instructionsApi.get(org.value, props.projectId, instructionId.value)
    }
  } catch (error) {
    loadError.value = toUserMessage(error)
  } finally {
    loading.value = false
  }
}

async function save(next: CaseContent): Promise<void> {
  if (props.caseId) {
    testCase.value = await casesApi.edit(org.value, props.projectId, props.caseId, next)
    versions.value = await casesApi.versions(org.value, props.projectId, props.caseId)
    shownVersion.value = null
    editing.value = false
    notifications.success(`Saved as version ${testCase.value.version}.`)
    return
  }
  // A new case goes to the application of its instruction, or the project's first one.
  const applicationId =
    instruction.value?.application_id ??
    (await applicationsApi.list(org.value, props.projectId))[0]?.id
  if (!applicationId) throw new Error('Register an application first.')
  const created = await casesApi.create(org.value, props.projectId, {
    application_id: applicationId,
    environment_id: instruction.value?.environment_id ?? null,
    instruction_id: instruction.value?.id ?? null,
    content: next,
  })
  await router.replace({ name: 'case', params: { projectId: props.projectId, caseId: created.id } })
}

async function showVersion(number: number): Promise<void> {
  if (!props.caseId) return
  shownVersion.value =
    number === testCase.value?.version
      ? null
      : await casesApi.version(org.value, props.projectId, props.caseId, number)
}

async function act(action: () => Promise<void>): Promise<void> {
  busy.value = true
  try {
    await action()
  } catch (error) {
    notifications.error(toUserMessage(error))
  } finally {
    busy.value = false
  }
}

const approve = () =>
  act(async () => {
    if (!props.caseId) return
    testCase.value = await casesApi.approve(org.value, props.projectId, props.caseId)
    versions.value = await casesApi.versions(org.value, props.projectId, props.caseId)
    notifications.success('Test case approved.')
  })

const remove = () =>
  act(async () => {
    if (
      !props.caseId ||
      !window.confirm(`Delete ${testCase.value?.key}? It is removed from its suites.`)
    ) {
      return
    }
    await casesApi.remove(org.value, props.projectId, props.caseId)
    await router.push(backTo.value)
  })

onMounted(load)
</script>

<template>
  <LoadingState v-if="loading" label="Loading…" />
  <ErrorState v-else-if="loadError" :message="loadError" />

  <section v-else class="stack stack--lg">
    <header class="stack">
      <RouterLink class="small" :to="backTo">← Back</RouterLink>
      <div class="cluster cluster--between">
        <div class="cluster">
          <h1>{{ testCase ? `${testCase.key} · ${testCase.title}` : 'New test case' }}</h1>
          <StatusBadge
            v-if="testCase"
            :tone="testCase.status === 'approved' ? 'success' : 'neutral'"
          >
            {{ CASE_STATUS_LABELS[testCase.status] }}
          </StatusBadge>
        </div>
        <div v-if="testCase && !editing && auth.canContribute" class="cluster">
          <BaseButton variant="secondary" @click="((shownVersion = null), (editing = true))">
            Edit
          </BaseButton>
          <BaseButton
            v-if="canApprove && testCase.status !== 'approved'"
            :loading="busy"
            :disabled="!testCase.ready"
            @click="approve"
          >
            Approve
          </BaseButton>
          <BaseButton variant="ghost" :loading="busy" @click="remove">Delete</BaseButton>
        </div>
      </div>
    </header>

    <div v-if="editing" class="card">
      <CaseForm
        :key="testCase?.version ?? 'new'"
        :content="testCase?.content"
        :objectives="objectives"
        :save="save"
        @cancel="testCase ? (editing = false) : router.push(backTo)"
      />
    </div>

    <template v-else-if="testCase && content">
      <IssueList v-if="!shownVersion" :issues="testCase.validation.issues" />
      <p v-if="shownVersion" class="notice">
        You are looking at version {{ shownVersion.version }} ({{
          shownVersion.source === 'ai' ? 'written by the AI' : 'edited by a person'
        }}). The current version is {{ testCase.version }}.
        <button class="link" type="button" @click="shownVersion = null">Show current</button>
      </p>

      <div class="card stack">
        <p v-if="content.description">{{ content.description }}</p>
        <dl class="facts">
          <div>
            <dt>Category</dt>
            <dd>{{ content.category.replace('_', ' ') }}</dd>
          </div>
          <div>
            <dt>Priority</dt>
            <dd>{{ content.priority.toUpperCase() }}</dd>
          </div>
          <div>
            <dt>Severity</dt>
            <dd>{{ content.severity }}</dd>
          </div>
          <div>
            <dt>Automation</dt>
            <dd>{{ content.automation_feasibility.replaceAll('_', ' ') }}</dd>
          </div>
          <div v-if="content.objective_id">
            <dt>Verifies</dt>
            <dd>{{ content.objective_id }}</dd>
          </div>
          <div v-if="content.tags.length">
            <dt>Tags</dt>
            <dd>{{ content.tags.join(', ') }}</dd>
          </div>
        </dl>

        <div v-if="content.preconditions.length">
          <h2 class="subtitle">Preconditions</h2>
          <ul>
            <li v-for="item in content.preconditions" :key="item">{{ item }}</li>
          </ul>
        </div>
        <div v-if="content.test_data.length">
          <h2 class="subtitle">Test data</h2>
          <ul>
            <li v-for="item in content.test_data" :key="item.name" class="mono">
              {{ item.name }} = {{ item.value ?? '(to be chosen)' }}
            </li>
          </ul>
        </div>

        <h2 class="subtitle">Steps</h2>
        <ol class="steps">
          <li v-for="(step, index) in content.steps" :key="index">
            <span class="tag">{{ step.action.replace('_', ' ') }}</span> {{ step.description }}
            <p v-if="step.target || step.value" class="small muted">
              <template v-if="step.target">{{ step.target }}</template>
              <template v-if="step.target && step.value"> · </template>
              <span v-if="step.value" class="mono">{{ step.value }}</span>
            </p>
          </li>
        </ol>

        <div>
          <h2 class="subtitle">Expected result</h2>
          <p v-if="content.expected_result">
            {{ content.expected_result }}
            <span class="small muted">· {{ content.expected_source.replaceAll('_', ' ') }}</span>
          </p>
          <p v-else class="unknown">
            Not known yet. Edit the case to supply it; it is not guessed.
          </p>
        </div>
        <div>
          <h2 class="subtitle">Verification</h2>
          <p>{{ content.verification.description }}</p>
        </div>
        <div v-if="content.assumptions.length">
          <h2 class="subtitle">Unconfirmed assumptions</h2>
          <ul>
            <li v-for="item in content.assumptions" :key="item">{{ item }}</li>
          </ul>
        </div>
      </div>

      <section class="card stack">
        <h2 class="subtitle">Versions</h2>
        <p class="small muted">Every change is kept. The AI's original is never overwritten.</p>
        <ul class="list">
          <li v-for="version in versions" :key="version.version">
            <div class="grow">
              <strong>Version {{ version.version }}</strong>
              <span class="small muted">
                · {{ version.source === 'ai' ? 'AI' : 'person' }} · {{ version.change_reason }} ·
                {{ new Date(version.created_at).toLocaleString() }}
              </span>
            </div>
            <span v-if="version.is_original" class="tag">AI original</span>
            <span v-if="version.is_approved" class="tag">approved</span>
            <span v-if="version.is_current" class="tag">current</span>
            <BaseButton
              v-if="(shownVersion?.version ?? testCase.version) !== version.version"
              variant="ghost"
              @click="showVersion(version.version)"
            >
              View
            </BaseButton>
          </li>
        </ul>
      </section>
    </template>
  </section>
</template>

<style scoped>
.subtitle {
  margin: 0 0 var(--space-2);
  font-size: var(--text-md);
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
}

.steps {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  margin: 0;
  padding-left: var(--space-5);
}

ul {
  margin: 0;
  padding-left: var(--space-5);
}

ul.list {
  padding-left: 0;
}

.unknown {
  color: var(--color-warning);
  font-weight: 600;
}

.link {
  padding: 0;
  border: 0;
  background: none;
  color: inherit;
  font: inherit;
  text-decoration: underline;
  cursor: pointer;
}
</style>
