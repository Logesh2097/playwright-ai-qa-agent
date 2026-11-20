<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

import { applicationsApi, environmentsApi, projectsApi } from '@/api/projects'
import { instructionsApi } from '@/api/testing'
import BaseButton from '@/components/ui/BaseButton.vue'
import BaseModal from '@/components/ui/BaseModal.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import FormField from '@/components/ui/FormField.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import { useFormErrors } from '@/composables/useFormErrors'
import { toUserMessage } from '@/errors/messages'
import { STATUS_LABELS, statusTone } from '@/pages/instructionStatus'
import { useAuthStore } from '@/stores/auth'
import type { Application, Environment, Instruction, Project } from '@/types/api'

const props = defineProps<{ projectId: string }>()

const auth = useAuthStore()
const router = useRouter()
const org = computed(() => auth.organizationId ?? '')

const project = ref<Project | null>(null)
const instructions = ref<Instruction[]>([])
const applications = ref<Application[]>([])
const environments = ref<Environment[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)

const canSubmit = computed(() => auth.canContribute && project.value?.status === 'active')

async function load(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    ;[project.value, instructions.value, applications.value] = await Promise.all([
      projectsApi.get(org.value, props.projectId),
      instructionsApi.list(org.value, props.projectId),
      applicationsApi.list(org.value, props.projectId),
    ])
  } catch (error) {
    loadError.value = toUserMessage(error)
  } finally {
    loading.value = false
  }
}

// --- submit ---------------------------------------------------------------------------------

const open = ref(false)
const saving = ref(false)
const errors = useFormErrors()
const form = reactive({ application_id: '', environment_id: '', text: '', rules: '' })

watch(
  () => form.application_id,
  async (applicationId) => {
    form.environment_id = ''
    environments.value = applicationId
      ? await environmentsApi.list({
          org: org.value,
          project: props.projectId,
          application: applicationId,
        })
      : []
    form.environment_id = environments.value[0]?.id ?? ''
  },
)

function openForm(): void {
  errors.clear()
  form.text = ''
  form.rules = ''
  form.application_id = applications.value[0]?.id ?? ''
  open.value = true
}

async function submit(): Promise<void> {
  saving.value = true
  errors.clear()
  try {
    const created = await instructionsApi.submit(org.value, props.projectId, {
      application_id: form.application_id,
      environment_id: form.environment_id,
      text: form.text,
      test_types: ['ui'],
      options: {
        // "name = value" per line
        rules: form.rules
          .split('\n')
          .map((line) => line.trim())
          .filter((line) => line.includes('='))
          .map((line) => {
            const at = line.indexOf('=')
            return { name: line.slice(0, at).trim(), value: line.slice(at + 1).trim() }
          }),
      },
    })
    open.value = false
    await router.push({
      name: 'instruction',
      params: { projectId: props.projectId, instructionId: created.id },
    })
  } catch (error) {
    errors.capture(error)
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <LoadingState v-if="loading" label="Loading instructions…" />
  <ErrorState v-else-if="loadError || !project" :message="loadError ?? 'Project not found.'" />

  <section v-else class="stack stack--lg">
    <header class="stack">
      <RouterLink class="small" :to="{ name: 'project', params: { projectId } }">
        ← {{ project.name }}
      </RouterLink>
      <div class="cluster cluster--between">
        <div>
          <h1>Test instructions</h1>
          <p class="muted">
            Describe what to test in plain language. The AI turns it into a requirement you review.
          </p>
        </div>
        <BaseButton v-if="canSubmit && applications.length > 0" @click="openForm">
          New instruction
        </BaseButton>
      </div>
    </header>

    <EmptyState
      v-if="applications.length === 0"
      title="Register an application first"
      message="An instruction is always aimed at one application and one of its environments."
    />
    <EmptyState
      v-else-if="instructions.length === 0"
      title="No instructions yet"
      message="For example: “Test the customer login. Verify valid login, invalid password and empty email.”"
    />
    <ul v-else class="card list">
      <li v-for="instruction in instructions" :key="instruction.id">
        <RouterLink
          class="grow title"
          :to="{ name: 'instruction', params: { projectId, instructionId: instruction.id } }"
        >
          {{ instruction.analysis?.feature_name ?? instruction.text }}
        </RouterLink>
        <span class="small muted">{{ new Date(instruction.created_at).toLocaleDateString() }}</span>
        <StatusBadge :tone="statusTone(instruction.status)">
          {{ STATUS_LABELS[instruction.status] }}
        </StatusBadge>
      </li>
    </ul>

    <BaseModal :open="open" title="New test instruction" @close="open = false">
      <form class="stack" novalidate @submit.prevent="submit">
        <p v-if="errors.general.value" class="notice notice--danger" role="alert">
          {{ errors.general.value }}
        </p>
        <div class="form-grid">
          <FormField v-slot="field" label="Application" :error="errors.fields.application_id">
            <select :id="field.id" v-model="form.application_id" class="input">
              <option v-for="app in applications" :key="app.id" :value="app.id">
                {{ app.name }}
              </option>
            </select>
          </FormField>
          <FormField v-slot="field" label="Environment" :error="errors.fields.environment_id">
            <select :id="field.id" v-model="form.environment_id" class="input">
              <option v-for="env in environments" :key="env.id" :value="env.id">
                {{ env.name }} ({{ env.kind }})
              </option>
            </select>
          </FormField>
        </div>
        <FormField
          v-slot="field"
          label="What should be tested?"
          :error="errors.fields.text"
          hint="Say what should happen where you know it. Results that are not stated will be asked about, not guessed."
        >
          <textarea
            :id="field.id"
            v-model="form.text"
            class="input"
            rows="5"
            maxlength="4000"
            :aria-invalid="field.invalid"
            required
          />
        </FormField>
        <FormField
          v-slot="field"
          label="Known rules and values"
          hint="Optional, one per line as name = value. Example: minimum_bill_amount = 500"
        >
          <textarea :id="field.id" v-model="form.rules" class="input mono" rows="2" />
        </FormField>
        <div class="cluster">
          <BaseButton type="submit" :loading="saving" :disabled="!form.environment_id">
            Analyse
          </BaseButton>
          <BaseButton variant="ghost" @click="open = false">Cancel</BaseButton>
        </div>
      </form>
    </BaseModal>
  </section>
</template>

<style scoped>
.title {
  overflow: hidden;
  color: var(--color-text);
  font-weight: 600;
  text-decoration: none;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.title:hover {
  color: var(--color-accent);
}
</style>
