<script setup lang="ts">
/**
 * Starts an execution of approved work: one suite, or chosen cases, in one environment.
 * What may run is decided by the server; this only collects the choice.
 */
import { computed, reactive, ref, watch } from 'vue'

import { executionsApi } from '@/api/admin'
import { ApiError } from '@/api/client'
import { applicationsApi, environmentsApi } from '@/api/projects'
import { suitesApi } from '@/api/testing'
import BaseButton from '@/components/ui/BaseButton.vue'
import BaseModal from '@/components/ui/BaseModal.vue'
import FormField from '@/components/ui/FormField.vue'
import { useFormErrors } from '@/composables/useFormErrors'
import { toUserMessage } from '@/errors/messages'
import type { CaptureMode } from '@/types/admin'
import type { Environment, Suite } from '@/types/api'

const props = defineProps<{
  open: boolean
  org: string
  projectId: string
  /** Preselects a suite, e.g. when opened from the suite's own page. */
  suiteId?: string
  caseIds?: string[]
}>()
const emit = defineEmits<{ close: []; started: [executionId: string] }>()

const errors = useFormErrors()
const loading = ref(false)
const loadError = ref<string | null>(null)
const submitting = ref(false)
const environments = ref<(Environment & { application: string })[]>([])
const suites = ref<Suite[]>([])
const form = reactive({
  environment_id: '',
  suite_id: '',
  browser: 'chromium' as 'chromium' | 'firefox' | 'webkit',
  attempts: 1,
  screenshots: 'on_failure' as CaptureMode,
  video: 'off' as CaptureMode,
  healing: 'off' as 'off' | 'propose' | 'recover',
})

/** Sensitive steps the server found, when it asked for approval before running them. */
const sensitive = ref<{ field: string | null; message: string }[]>([])
const approveSensitive = ref(false)

const approvedSuites = computed(() => suites.value.filter((suite) => suite.status === 'approved'))
const environment = computed(() => environments.value.find((e) => e.id === form.environment_id))
const byCases = computed(() => (props.caseIds?.length ?? 0) > 0)
const ready = computed(() => form.environment_id !== '' && (byCases.value || form.suite_id !== ''))

async function load(): Promise<void> {
  sensitive.value = []
  approveSensitive.value = false
  loading.value = true
  loadError.value = null
  errors.clear()
  try {
    const [applications, allSuites] = await Promise.all([
      applicationsApi.list(props.org, props.projectId),
      suitesApi.list(props.org, props.projectId),
    ])
    const lists = await Promise.all(
      applications.map(async (application) =>
        (
          await environmentsApi.list({
            org: props.org,
            project: props.projectId,
            application: application.id,
          })
        ).map((item) => ({ ...item, application: application.name })),
      ),
    )
    environments.value = lists.flat().filter((item) => item.status === 'active')
    suites.value = allSuites
    form.environment_id ||= environments.value[0]?.id ?? ''
    form.suite_id = props.suiteId ?? form.suite_id
  } catch (error) {
    loadError.value = toUserMessage(error)
  } finally {
    loading.value = false
  }
}

async function submit(): Promise<void> {
  submitting.value = true
  errors.clear()
  try {
    const started = await executionsApi.start(props.org, props.projectId, {
      environment_id: form.environment_id,
      ...(byCases.value ? { case_ids: props.caseIds } : { suite_id: form.suite_id }),
      config: {
        browser: form.browser,
        retry: { max_attempts: form.attempts },
        evidence: { screenshots: form.screenshots, video: form.video },
        healing: { mode: form.healing },
      },
      ...(approveSensitive.value ? { approve_sensitive_actions: true } : {}),
    })
    emit('started', started.id)
  } catch (error) {
    if (error instanceof ApiError && error.code === 'sensitive_approval_required') {
      // Not a failure: the run contains steps a person has to approve first.
      sensitive.value = error.errors
      approveSensitive.value = false
      return
    }
    errors.capture(error)
  } finally {
    submitting.value = false
  }
}

watch(
  () => props.open,
  (open) => {
    if (open) void load()
  },
  { immediate: true },
)
</script>

<template>
  <BaseModal :open="open" title="Start an execution" @close="emit('close')">
    <p v-if="loading" class="muted">Loading environments and suites…</p>
    <p v-else-if="loadError" class="notice notice--danger" role="alert">{{ loadError }}</p>
    <p v-else-if="environments.length === 0" class="notice">
      This project has no active environment. Add one to an application first.
    </p>
    <form v-else class="stack" novalidate @submit.prevent="submit">
      <p v-if="errors.general.value" class="notice notice--danger" role="alert">
        {{ errors.general.value }}
      </p>
      <FormField v-slot="field" label="Environment" :error="errors.fields.environment_id">
        <select :id="field.id" v-model="form.environment_id" class="input" required>
          <option v-for="item in environments" :key="item.id" :value="item.id">
            {{ item.application }} — {{ item.name }} ({{ item.kind }})
          </option>
        </select>
      </FormField>
      <p v-if="environment?.requires_approval" class="notice">
        This environment requires approval: tests that change data or sign in can only be started by
        an organization admin.
      </p>
      <p v-if="environment?.is_read_only" class="notice">
        This environment is read-only: tests that change data will be blocked, not run.
      </p>

      <p v-if="byCases" class="small">Runs {{ caseIds?.length }} selected test case(s).</p>
      <FormField
        v-else
        v-slot="field"
        label="Test suite"
        :error="errors.fields.suite_id"
        hint="Only approved suites can be run."
      >
        <select :id="field.id" v-model="form.suite_id" class="input" required>
          <option value="" disabled>Choose a suite</option>
          <option v-for="suite in approvedSuites" :key="suite.id" :value="suite.id">
            {{ suite.name }} ({{ suite.cases.length }} cases)
          </option>
        </select>
      </FormField>

      <div class="form-grid">
        <FormField v-slot="field" label="Browser">
          <select :id="field.id" v-model="form.browser" class="input">
            <option value="chromium">Chromium</option>
            <option value="firefox">Firefox</option>
            <option value="webkit">WebKit</option>
          </select>
        </FormField>
        <FormField
          v-slot="field"
          label="Attempts per test"
          hint="A test that passes only on a later attempt is reported as flaky."
        >
          <select :id="field.id" v-model.number="form.attempts" class="input">
            <option :value="1">1</option>
            <option :value="2">2</option>
            <option :value="3">3</option>
          </select>
        </FormField>
        <FormField v-slot="field" label="Screenshots">
          <select :id="field.id" v-model="form.screenshots" class="input">
            <option value="on_failure">On failure</option>
            <option value="always">After every step</option>
            <option value="off">Off</option>
          </select>
        </FormField>
        <FormField v-slot="field" label="Video">
          <select :id="field.id" v-model="form.video" class="input">
            <option value="off">Off</option>
            <option value="on_failure">On failure</option>
            <option value="always">Always</option>
          </select>
        </FormField>
        <FormField
          v-slot="field"
          label="When an element is not found"
          hint="A test that passes with a replacement is marked needs review, not passed."
        >
          <select :id="field.id" v-model="form.healing" class="input">
            <option value="off">Fail the step</option>
            <option value="propose">Propose a replacement</option>
            <option value="recover">Use a safe replacement</option>
          </select>
        </FormField>
      </div>
      <div v-if="sensitive.length > 0" class="notice stack" role="alert">
        <strong>This run contains sensitive steps.</strong>
        <span>
          They delete data, pay, send messages or change configuration, and cannot be undone by
          running the test again. They are never run against production.
        </span>
        <ul class="sensitive">
          <li v-for="(item, index) in sensitive" :key="index">
            <strong>{{ item.field }}</strong> — {{ item.message }}
          </li>
        </ul>
        <label class="checkbox">
          <input v-model="approveSensitive" type="checkbox" />
          I have reviewed these steps and approve them for this execution.
        </label>
      </div>
      <div class="cluster">
        <BaseButton
          type="submit"
          :loading="submitting"
          :disabled="!ready || (sensitive.length > 0 && !approveSensitive)"
        >
          {{ sensitive.length > 0 ? 'Approve and start' : 'Start' }}
        </BaseButton>
        <BaseButton variant="ghost" @click="emit('close')">Cancel</BaseButton>
      </div>
    </form>
  </BaseModal>
</template>

<style scoped>
.sensitive {
  margin: 0;
  padding-left: var(--space-5);
}
</style>
