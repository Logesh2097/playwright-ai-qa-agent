<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

import { applicationsApi, environmentsApi, projectsApi, type AppRef } from '@/api/projects'
import ApplicationForm from '@/components/domain/ApplicationForm.vue'
import EnvironmentCard from '@/components/domain/EnvironmentCard.vue'
import EnvironmentForm from '@/components/domain/EnvironmentForm.vue'
import BaseButton from '@/components/ui/BaseButton.vue'
import BaseModal from '@/components/ui/BaseModal.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import { toUserMessage } from '@/errors/messages'
import { useAuthStore } from '@/stores/auth'
import { useNotificationsStore } from '@/stores/notifications'
import type {
  Application,
  ApplicationInput,
  Environment,
  EnvironmentInput,
  Project,
} from '@/types/api'

const props = defineProps<{ projectId: string; applicationId: string }>()

const auth = useAuthStore()
const notifications = useNotificationsStore()
const router = useRouter()

const appRef = computed<AppRef>(() => ({
  org: auth.organizationId ?? '',
  project: props.projectId,
  application: props.applicationId,
}))

const project = ref<Project | null>(null)
const application = ref<Application | null>(null)
const environments = ref<Environment[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)

const writable = computed(() => auth.canContribute && project.value?.status === 'active')

/** Mirrors the server rule so buttons are not offered that would only return 403. */
function canEdit(environment: Environment): boolean {
  return writable.value && (environment.kind !== 'production' || auth.isAdmin)
}

async function loadEnvironments(): Promise<void> {
  environments.value = await environmentsApi.list(appRef.value)
}

async function load(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    const [loadedProject, loadedApplication] = await Promise.all([
      projectsApi.get(appRef.value.org, props.projectId),
      applicationsApi.get(appRef.value),
    ])
    project.value = loadedProject
    application.value = loadedApplication
    document.title = loadedApplication.name
    await loadEnvironments()
  } catch (error) {
    loadError.value = toUserMessage(error)
  } finally {
    loading.value = false
  }
}

// --- application --------------------------------------------------------------------------

const dialog = ref<'application' | 'environment' | null>(null)

async function saveApplication(input: ApplicationInput): Promise<void> {
  application.value = await applicationsApi.update(appRef.value, input)
  dialog.value = null
}

async function deleteApplication(): Promise<void> {
  const name = application.value?.name
  if (
    !window.confirm(
      `Delete "${name}"? Its environments are removed and their stored credentials destroyed.`,
    )
  ) {
    return
  }
  try {
    await applicationsApi.remove(appRef.value)
    notifications.success('Application deleted.')
    await router.push({ name: 'project', params: { projectId: props.projectId } })
  } catch (error) {
    notifications.error(toUserMessage(error))
  }
}

// --- environments -------------------------------------------------------------------------

const editingEnvironment = ref<Environment | null>(null)
const editingCredentialNames = ref<string[]>([])

function openEnvironment(environment: Environment | null, credentialNames: string[] = []): void {
  editingEnvironment.value = environment
  editingCredentialNames.value = credentialNames
  dialog.value = 'environment'
}

async function saveEnvironment(input: EnvironmentInput): Promise<void> {
  if (editingEnvironment.value) {
    await environmentsApi.update(
      { ...appRef.value, environment: editingEnvironment.value.id },
      input,
    )
  } else {
    await environmentsApi.create(appRef.value, input)
  }
  dialog.value = null
  await loadEnvironments()
}

async function deleteEnvironment(environment: Environment): Promise<void> {
  if (
    !window.confirm(
      `Delete the environment "${environment.name}"? Its stored credentials are destroyed.`,
    )
  ) {
    return
  }
  try {
    await environmentsApi.remove({ ...appRef.value, environment: environment.id })
    await loadEnvironments()
  } catch (error) {
    notifications.error(toUserMessage(error))
  }
}

onMounted(load)
</script>

<template>
  <LoadingState v-if="loading" label="Loading application…" />
  <ErrorState
    v-else-if="loadError || !application || !project"
    :message="loadError ?? 'Application not found.'"
  />

  <section v-else class="stack stack--lg">
    <header class="stack">
      <RouterLink class="small" :to="{ name: 'project', params: { projectId } }">
        ← {{ project.name }}
      </RouterLink>
      <div class="cluster cluster--between">
        <div class="cluster">
          <h1>{{ application.name }}</h1>
          <span class="tag">{{ application.type }}</span>
          <span v-if="application.status === 'archived'" class="tag">archived</span>
        </div>
        <div v-if="writable" class="cluster">
          <BaseButton variant="secondary" @click="dialog = 'application'">Edit</BaseButton>
          <BaseButton variant="ghost" @click="deleteApplication">Delete</BaseButton>
        </div>
      </div>
      <p v-if="application.description" class="muted">{{ application.description }}</p>
    </header>

    <dl class="card facts">
      <div>
        <dt>Base URL</dt>
        <dd class="mono">{{ application.base_url }}</dd>
      </div>
      <div>
        <dt>Allowed domains</dt>
        <dd class="mono">{{ application.allowed_domains.join(', ') }}</dd>
      </div>
      <div>
        <dt>Sign-in</dt>
        <dd>{{ application.auth_method.replace('_', ' ') }}</dd>
      </div>
      <div v-if="application.framework">
        <dt>Framework</dt>
        <dd>{{ application.framework }}</dd>
      </div>
      <div>
        <dt>Default browser</dt>
        <dd>
          {{ application.default_browser_config.browser ?? 'chromium' }}
          <template v-if="application.default_browser_config.viewport">
            · {{ application.default_browser_config.viewport.width }}×{{
              application.default_browser_config.viewport.height
            }}
          </template>
        </dd>
      </div>
    </dl>

    <section class="stack">
      <div class="cluster cluster--between">
        <div>
          <h2>Environments</h2>
          <p class="small muted">
            Each environment has its own address, allowed origins and test credentials.
          </p>
        </div>
        <BaseButton v-if="writable" @click="openEnvironment(null)">Add environment</BaseButton>
      </div>
      <EmptyState
        v-if="environments.length === 0"
        title="No environments yet"
        message="Add the place where this application runs, such as staging."
      />
      <EnvironmentCard
        v-for="environment in environments"
        :key="environment.id"
        :environment="environment"
        :env-ref="{ ...appRef, environment: environment.id }"
        :can-edit="canEdit(environment)"
        :can-see-credentials="auth.canContribute"
        @edit="(names) => openEnvironment(environment, names)"
        @delete="deleteEnvironment(environment)"
        @changed="loadEnvironments"
      />
    </section>

    <BaseModal :open="dialog === 'application'" title="Edit application" @close="dialog = null">
      <ApplicationForm
        v-if="dialog === 'application'"
        :application="application"
        :save="saveApplication"
        @cancel="dialog = null"
      />
    </BaseModal>

    <BaseModal
      :open="dialog === 'environment'"
      :title="editingEnvironment ? 'Edit environment' : 'Add environment'"
      @close="dialog = null"
    >
      <EnvironmentForm
        v-if="dialog === 'environment'"
        :environment="editingEnvironment ?? undefined"
        :credential-names="editingCredentialNames"
        :is-admin="auth.isAdmin"
        :save="saveEnvironment"
        @cancel="dialog = null"
      />
    </BaseModal>
  </section>
</template>

<style scoped>
.facts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr));
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
  overflow-wrap: anywhere;
}
</style>
