<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

import { ApiError } from '@/api/client'
import { casesApi, suitesApi } from '@/api/testing'
import StartExecutionDialog from '@/components/domain/StartExecutionDialog.vue'
import BaseButton from '@/components/ui/BaseButton.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import { toUserMessage } from '@/errors/messages'
import { CASE_STATUS_LABELS } from '@/pages/instructionStatus'
import { useAuthStore } from '@/stores/auth'
import { useNotificationsStore } from '@/stores/notifications'
import type { CaseSummary, Priority, Suite } from '@/types/api'

const props = defineProps<{ projectId: string; suiteId: string }>()

const auth = useAuthStore()
const notifications = useNotificationsStore()
const router = useRouter()
const org = computed(() => auth.organizationId ?? '')
const canApprove = computed(
  () => auth.role !== null && !['viewer', 'developer'].includes(auth.role),
)

const suite = ref<Suite | null>(null)
const allCases = ref<CaseSummary[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)
const busy = ref(false)
/** What stopped the last approval, per case. */
const blockers = ref<{ field: string | null; message: string }[]>([])
const toAdd = ref('')

const runOpen = ref(false)

async function started(executionId: string): Promise<void> {
  runOpen.value = false
  await router.push({ name: 'execution-live', params: { projectId: props.projectId, executionId } })
}

const editable = computed(() => auth.canContribute && suite.value?.status !== 'archived')
const candidates = computed(() => {
  const present = new Set(suite.value?.cases.map((c) => c.id))
  return allCases.value.filter((c) => !present.has(c.id))
})

async function load(): Promise<void> {
  try {
    ;[suite.value, allCases.value] = await Promise.all([
      suitesApi.get(org.value, props.projectId, props.suiteId),
      casesApi.list(org.value, props.projectId),
    ])
    document.title = suite.value.name
  } catch (error) {
    loadError.value = toUserMessage(error)
  } finally {
    loading.value = false
  }
}

async function act(action: () => Promise<Suite | void>, done?: string): Promise<void> {
  busy.value = true
  blockers.value = []
  try {
    const result = await action()
    if (result) suite.value = result
    if (done) notifications.success(done)
  } catch (error) {
    if (error instanceof ApiError && error.code === 'suite_not_ready') blockers.value = error.errors
    notifications.error(toUserMessage(error))
  } finally {
    busy.value = false
  }
}

const ids = () => suite.value?.cases.map((c) => c.id) ?? []
const setCases = (next: string[]) =>
  act(() => suitesApi.setCases(org.value, props.projectId, props.suiteId, next))

function move(index: number, delta: number): Promise<void> {
  const next = ids()
  const [id] = next.splice(index, 1)
  if (id) next.splice(index + delta, 0, id)
  return setCases(next)
}

const remove = (id: string) => setCases(ids().filter((existing) => existing !== id))

async function add(): Promise<void> {
  if (!toAdd.value) return
  await setCases([...ids(), toAdd.value])
  toAdd.value = ''
}

const setPriority = (event: Event) =>
  act(() =>
    suitesApi.update(org.value, props.projectId, props.suiteId, {
      priority: (event.target as HTMLSelectElement).value as Priority,
    }),
  )

const approve = () =>
  act(() => suitesApi.approve(org.value, props.projectId, props.suiteId), 'Suite approved.')

const deleteSuite = () =>
  act(async () => {
    if (!window.confirm(`Delete the suite "${suite.value?.name}"? Its test cases are kept.`)) return
    await suitesApi.remove(org.value, props.projectId, props.suiteId)
    await router.push({ name: 'suites', params: { projectId: props.projectId } })
  })

onMounted(load)
</script>

<template>
  <LoadingState v-if="loading" label="Loading suite…" />
  <ErrorState v-else-if="loadError || !suite" :message="loadError ?? 'Suite not found.'" />

  <section v-else class="stack stack--lg">
    <header class="stack">
      <RouterLink class="small" :to="{ name: 'suites', params: { projectId } }"
        >← Suites</RouterLink
      >
      <div class="cluster cluster--between">
        <div class="cluster">
          <h1>{{ suite.name }}</h1>
          <StatusBadge :tone="suite.status === 'approved' ? 'success' : 'neutral'">
            {{ suite.status }}
          </StatusBadge>
        </div>
        <div v-if="editable" class="cluster">
          <label class="cluster small">
            Priority
            <select class="input narrow" :value="suite.priority" @change="setPriority">
              <option value="p0">P0</option>
              <option value="p1">P1</option>
              <option value="p2">P2</option>
              <option value="p3">P3</option>
            </select>
          </label>
          <BaseButton
            v-if="canApprove && suite.status !== 'approved'"
            :loading="busy"
            :disabled="suite.cases.length === 0"
            @click="approve"
          >
            Approve suite
          </BaseButton>
          <BaseButton
            v-if="auth.canContribute && suite.status === 'approved'"
            @click="runOpen = true"
          >
            Run suite
          </BaseButton>
          <BaseButton variant="ghost" :loading="busy" @click="deleteSuite">Delete</BaseButton>
        </div>
      </div>
      <p class="muted">
        <template v-if="suite.feature">Feature: {{ suite.feature }} · </template>
        {{ suite.source === 'ai_generated' ? 'Generated from an instruction' : 'Created by hand' }}
        <template v-if="suite.description"> · {{ suite.description }}</template>
      </p>
      <p v-if="suite.status === 'approved'" class="notice notice--success">
        Approved on {{ new Date(suite.approved_at ?? '').toLocaleDateString() }}. The approved
        version of each case is recorded. Adding, removing or reordering cases makes it a draft
        again.
      </p>
      <div v-if="blockers.length > 0" class="notice notice--danger" role="alert">
        <p><strong>Nothing was approved.</strong> These cases are not ready:</p>
        <ul>
          <li v-for="(blocker, index) in blockers" :key="index">
            {{ blocker.field }}: {{ blocker.message }}
          </li>
        </ul>
      </div>
    </header>

    <section class="card stack">
      <h2>Cases, in execution order</h2>
      <p v-if="suite.cases.length === 0" class="small muted">This suite has no test cases yet.</p>
      <ol v-else class="list ordered">
        <li v-for="(item, index) in suite.cases" :key="item.id">
          <span class="position">{{ item.position }}</span>
          <div class="grow">
            <RouterLink :to="{ name: 'case', params: { projectId, caseId: item.id } }">
              {{ item.key }} · {{ item.title }}
            </RouterLink>
            <p class="small muted">
              {{ item.category.replace('_', ' ') }} · {{ item.priority.toUpperCase() }}
              <template v-if="item.approved_version">
                · approved version {{ item.approved_version }}
              </template>
              <template v-if="item.outdated">
                · <strong>changed since approval (now version {{ item.current_version }})</strong>
              </template>
            </p>
          </div>
          <StatusBadge :tone="item.status === 'approved' ? 'success' : 'neutral'">
            {{ CASE_STATUS_LABELS[item.status] }}
          </StatusBadge>
          <template v-if="editable">
            <BaseButton variant="ghost" :disabled="index === 0 || busy" @click="move(index, -1)">
              Up
            </BaseButton>
            <BaseButton
              variant="ghost"
              :disabled="index === suite.cases.length - 1 || busy"
              @click="move(index, 1)"
            >
              Down
            </BaseButton>
            <BaseButton variant="ghost" :disabled="busy" @click="remove(item.id)"
              >Remove</BaseButton
            >
          </template>
        </li>
      </ol>

      <form v-if="editable && candidates.length > 0" class="cluster" @submit.prevent="add">
        <label class="grow">
          <span class="visually-hidden">Test case to add</span>
          <select v-model="toAdd" class="input">
            <option value="" disabled>Add a test case…</option>
            <option v-for="item in candidates" :key="item.id" :value="item.id">
              {{ item.key }} · {{ item.title }}
            </option>
          </select>
        </label>
        <BaseButton type="submit" variant="secondary" :disabled="!toAdd" :loading="busy"
          >Add</BaseButton
        >
      </form>
    </section>
    <StartExecutionDialog
      :open="runOpen"
      :org="org"
      :project-id="projectId"
      :suite-id="suiteId"
      @close="runOpen = false"
      @started="started"
    />
  </section>
</template>

<style scoped>
.ordered {
  counter-reset: none;
}

.position {
  flex: none;
  width: 1.75rem;
  color: var(--color-text-muted);
  font-variant-numeric: tabular-nums;
  text-align: right;
}

.narrow {
  width: auto;
  min-height: 2rem;
}

.notice ul {
  margin: var(--space-2) 0 0;
  padding-left: var(--space-5);
}
</style>
