<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

import { applicationsApi, projectsApi } from '@/api/projects'
import { suitesApi } from '@/api/testing'
import BaseButton from '@/components/ui/BaseButton.vue'
import BaseModal from '@/components/ui/BaseModal.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import FormField from '@/components/ui/FormField.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import { useFormErrors } from '@/composables/useFormErrors'
import { toUserMessage } from '@/errors/messages'
import { useAuthStore } from '@/stores/auth'
import type { Application, Priority, Project, Suite } from '@/types/api'

const props = defineProps<{ projectId: string }>()

const auth = useAuthStore()
const router = useRouter()
const org = computed(() => auth.organizationId ?? '')

const project = ref<Project | null>(null)
const suites = ref<Suite[]>([])
const applications = ref<Application[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)

/** Suites grouped under their feature, in the order the server returns them. */
const byFeature = computed(() => {
  const groups = new Map<string, Suite[]>()
  for (const suite of suites.value) {
    const key = suite.feature ?? 'No feature'
    groups.set(key, [...(groups.get(key) ?? []), suite])
  }
  return [...groups.entries()]
})

async function load(): Promise<void> {
  try {
    ;[project.value, suites.value, applications.value] = await Promise.all([
      projectsApi.get(org.value, props.projectId),
      suitesApi.list(org.value, props.projectId),
      applicationsApi.list(org.value, props.projectId),
    ])
  } catch (error) {
    loadError.value = toUserMessage(error)
  } finally {
    loading.value = false
  }
}

const open = ref(false)
const saving = ref(false)
const errors = useFormErrors()
const form = reactive({ name: '', feature: '', priority: 'p2' as Priority, application_id: '' })

function openForm(): void {
  errors.clear()
  Object.assign(form, {
    name: '',
    feature: '',
    priority: 'p2',
    application_id: applications.value[0]?.id ?? '',
  })
  open.value = true
}

async function submit(): Promise<void> {
  saving.value = true
  try {
    const created = await suitesApi.create(org.value, props.projectId, {
      name: form.name,
      application_id: form.application_id,
      feature: form.feature || null,
      priority: form.priority,
    })
    await router.push({
      name: 'suite',
      params: { projectId: props.projectId, suiteId: created.id },
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
  <LoadingState v-if="loading" label="Loading suites…" />
  <ErrorState v-else-if="loadError || !project" :message="loadError ?? 'Project not found.'" />

  <section v-else class="stack stack--lg">
    <header class="stack">
      <RouterLink class="small" :to="{ name: 'project', params: { projectId } }">
        ← {{ project.name }}
      </RouterLink>
      <div class="cluster cluster--between">
        <div>
          <h1>Test suites</h1>
          <p class="muted">Ordered sets of test cases, grouped by feature.</p>
        </div>
        <BaseButton
          v-if="auth.canContribute && project.status === 'active' && applications.length > 0"
          @click="openForm"
        >
          New suite
        </BaseButton>
      </div>
    </header>

    <EmptyState
      v-if="suites.length === 0"
      title="No suites yet"
      message="Generating test cases from an instruction creates a suite. You can also create one by hand."
    />
    <section v-for="[feature, group] in byFeature" v-else :key="feature" class="stack">
      <h2>{{ feature }}</h2>
      <ul class="card list">
        <li v-for="suite in group" :key="suite.id">
          <RouterLink
            class="grow"
            :to="{ name: 'suite', params: { projectId, suiteId: suite.id } }"
          >
            {{ suite.name }}
          </RouterLink>
          <span class="small muted">{{ suite.cases.length }} cases</span>
          <span class="tag">{{ suite.priority.toUpperCase() }}</span>
          <StatusBadge :tone="suite.status === 'approved' ? 'success' : 'neutral'">
            {{ suite.status }}
          </StatusBadge>
        </li>
      </ul>
    </section>

    <BaseModal :open="open" title="New test suite" @close="open = false">
      <form class="stack" novalidate @submit.prevent="submit">
        <p v-if="errors.general.value" class="notice notice--danger" role="alert">
          {{ errors.general.value }}
        </p>
        <FormField v-slot="field" label="Name" :error="errors.fields.name">
          <input :id="field.id" v-model.trim="form.name" class="input" maxlength="160" required />
        </FormField>
        <div class="form-grid">
          <FormField v-slot="field" label="Feature" hint="Suites are grouped by feature.">
            <input :id="field.id" v-model.trim="form.feature" class="input" maxlength="160" />
          </FormField>
          <FormField v-slot="field" label="Execution priority">
            <select :id="field.id" v-model="form.priority" class="input">
              <option value="p0">P0 — runs first</option>
              <option value="p1">P1</option>
              <option value="p2">P2</option>
              <option value="p3">P3</option>
            </select>
          </FormField>
          <FormField v-slot="field" label="Application">
            <select :id="field.id" v-model="form.application_id" class="input">
              <option v-for="app in applications" :key="app.id" :value="app.id">
                {{ app.name }}
              </option>
            </select>
          </FormField>
        </div>
        <div class="cluster">
          <BaseButton type="submit" :loading="saving">Create suite</BaseButton>
          <BaseButton variant="ghost" @click="open = false">Cancel</BaseButton>
        </div>
      </form>
    </BaseModal>
  </section>
</template>
