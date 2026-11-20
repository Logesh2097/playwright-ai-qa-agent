<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

import { applicationsApi, projectsApi } from '@/api/projects'
import ApplicationForm from '@/components/domain/ApplicationForm.vue'
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
import { useNotificationsStore } from '@/stores/notifications'
import { useProjectsStore } from '@/stores/projects'
import type {
  Application,
  ApplicationInput,
  OrgMember,
  Project,
  ProjectMember,
  ProjectRole,
} from '@/types/api'

const props = defineProps<{ projectId: string }>()

const auth = useAuthStore()
const projectsStore = useProjectsStore()
const notifications = useNotificationsStore()
const router = useRouter()
const org = computed(() => auth.organizationId ?? '')

const project = ref<Project | null>(null)
const applications = ref<Application[]>([])
const members = ref<ProjectMember[]>([])
const organizationMembers = ref<OrgMember[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)

const archived = computed(() => project.value?.status === 'archived')
const canManage = computed(() => Boolean(project.value?.can_manage))
const canEditContent = computed(() => auth.canContribute && !archived.value)

/** Organization members who could still be added to the project. */
const candidates = computed(() => {
  const present = new Set(members.value.map((member) => member.user_id))
  return organizationMembers.value.filter(
    (member) => member.status === 'active' && member.user_id && !present.has(member.user_id),
  )
})

async function load(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    const [loadedProject, loadedApplications, loadedMembers] = await Promise.all([
      projectsApi.get(org.value, props.projectId),
      applicationsApi.list(org.value, props.projectId),
      projectsApi.members(org.value, props.projectId),
    ])
    project.value = loadedProject
    applications.value = loadedApplications
    members.value = loadedMembers
    document.title = loadedProject.name
    if (loadedProject.can_manage) {
      organizationMembers.value = await projectsApi.organizationMembers(org.value)
    }
  } catch (error) {
    loadError.value = toUserMessage(error)
  } finally {
    loading.value = false
  }
}

function applyProject(updated: Project): void {
  project.value = updated
  projectsStore.upsert(updated)
}

// --- edit / archive / delete -------------------------------------------------------------

const dialog = ref<'edit' | 'delete' | 'application' | null>(null)
const errors = useFormErrors()
const saving = ref(false)
const editName = ref('')
const editDescription = ref('')
const confirmName = ref('')

function openEdit(): void {
  editName.value = project.value?.name ?? ''
  editDescription.value = project.value?.description ?? ''
  errors.clear()
  dialog.value = 'edit'
}

async function run(action: () => Promise<void>): Promise<void> {
  saving.value = true
  errors.clear()
  try {
    await action()
  } catch (error) {
    errors.capture(error)
    if (dialog.value === null) notifications.error(toUserMessage(error))
  } finally {
    saving.value = false
  }
}

const saveEdit = () =>
  run(async () => {
    applyProject(
      await projectsApi.update(org.value, props.projectId, {
        name: editName.value,
        description: editDescription.value || null,
      }),
    )
    dialog.value = null
  })

const toggleArchive = () =>
  run(async () => {
    const call = archived.value ? projectsApi.restore : projectsApi.archive
    applyProject(await call(org.value, props.projectId))
  })

const deleteProject = () =>
  run(async () => {
    await projectsApi.remove(org.value, props.projectId, confirmName.value)
    projectsStore.remove(props.projectId)
    notifications.success('Project deleted.')
    await router.push({ name: 'projects' })
  })

async function createApplication(input: ApplicationInput): Promise<void> {
  const created = await applicationsApi.create(org.value, props.projectId, input)
  dialog.value = null
  await router.push({
    name: 'application',
    params: { projectId: props.projectId, applicationId: created.id },
  })
}

// --- members -----------------------------------------------------------------------------

const newMember = ref('')

const addMember = () =>
  run(async () => {
    if (!newMember.value) return
    members.value = await projectsApi.addMember(
      org.value,
      props.projectId,
      newMember.value,
      'member',
    )
    newMember.value = ''
  })

const setRole = (member: ProjectMember, role: ProjectRole) =>
  run(async () => {
    members.value = await projectsApi.setMemberRole(
      org.value,
      props.projectId,
      member.user_id,
      role,
    )
  })

const removeMember = (member: ProjectMember) =>
  run(async () => {
    const self = member.user_id === auth.user?.id
    const question = self
      ? 'Leave this project? You will lose access to it.'
      : `Remove ${member.full_name} from this project?`
    if (!window.confirm(question)) return
    await projectsApi.removeMember(org.value, props.projectId, member.user_id)
    if (self && !auth.isAdmin) {
      projectsStore.remove(props.projectId)
      await router.push({ name: 'projects' })
      return
    }
    members.value = members.value.filter((item) => item.user_id !== member.user_id)
  })

onMounted(load)
</script>

<template>
  <LoadingState v-if="loading" label="Loading project…" />
  <ErrorState v-else-if="loadError || !project" :message="loadError ?? 'Project not found.'">
    <RouterLink :to="{ name: 'projects' }">Back to projects</RouterLink>
  </ErrorState>

  <section v-else class="stack stack--lg">
    <header class="stack">
      <RouterLink class="small" :to="{ name: 'projects' }">← Projects</RouterLink>
      <div class="cluster cluster--between">
        <div class="cluster">
          <h1>{{ project.name }}</h1>
          <StatusBadge v-if="archived" tone="neutral">Archived</StatusBadge>
        </div>
        <div v-if="canManage" class="cluster">
          <BaseButton v-if="!archived" variant="secondary" @click="openEdit">Edit</BaseButton>
          <BaseButton variant="secondary" :loading="saving" @click="toggleArchive">
            {{ archived ? 'Restore' : 'Archive' }}
          </BaseButton>
          <BaseButton
            v-if="archived && auth.isAdmin"
            variant="ghost"
            @click="((confirmName = ''), errors.clear(), (dialog = 'delete'))"
          >
            Delete…
          </BaseButton>
        </div>
      </div>
      <p v-if="project.description" class="muted">{{ project.description }}</p>
      <p class="small muted">
        Created {{ new Date(project.created_at).toLocaleDateString() }} · updated
        {{ new Date(project.updated_at).toLocaleDateString() }}
      </p>
      <p v-if="archived" class="notice">
        This project is archived and read-only. Restore it to make changes.
      </p>
    </header>

    <nav class="cluster" aria-label="Testing">
      <RouterLink
        class="card card--link shortcut"
        :to="{ name: 'instructions', params: { projectId } }"
      >
        <strong>Test instructions</strong>
        <span class="small muted">Describe what to test; review the AI's analysis and cases</span>
      </RouterLink>
      <RouterLink class="card card--link shortcut" :to="{ name: 'suites', params: { projectId } }">
        <strong>Test suites</strong>
        <span class="small muted">Organise, order and approve test cases</span>
      </RouterLink>
      <RouterLink
        class="card card--link shortcut"
        :to="{ name: 'executions', params: { projectId } }"
      >
        <strong>Executions and reports</strong>
        <span class="small muted">Run history, test reports, evidence and comparisons</span>
      </RouterLink>
      <RouterLink
        class="card card--link shortcut"
        :to="{ name: 'dashboard', params: { projectId } }"
      >
        <strong>Dashboard</strong>
        <span class="small muted">Pass rate, flaky tests and trends</span>
      </RouterLink>
      <RouterLink
        class="card card--link shortcut"
        :to="{ name: 'heal-proposals', params: { projectId } }"
      >
        <strong>Replacement locators</strong>
        <span class="small muted">Review what the engine found for elements that moved</span>
      </RouterLink>
      <RouterLink
        class="card card--link shortcut"
        :to="{ name: 'project-integrations', params: { projectId } }"
      >
        <strong>GitHub</strong>
        <span class="small muted">Linked repositories and pull request testing</span>
      </RouterLink>
    </nav>

    <section class="stack">
      <div class="cluster cluster--between">
        <h2>Applications</h2>
        <BaseButton v-if="canEditContent" @click="dialog = 'application'">
          Register application
        </BaseButton>
      </div>
      <EmptyState
        v-if="applications.length === 0"
        title="No applications registered"
        message="Register the web application or API you want to test."
      />
      <div v-else class="grid">
        <RouterLink
          v-for="application in applications"
          :key="application.id"
          class="card card--link stack"
          :to="{
            name: 'application',
            params: { projectId: project.id, applicationId: application.id },
          }"
        >
          <div class="cluster cluster--between">
            <h3 class="app-name">{{ application.name }}</h3>
            <span class="tag">{{ application.type }}</span>
          </div>
          <p class="mono muted">{{ application.base_url }}</p>
          <p class="small muted">
            {{ application.environment_count }}
            {{ application.environment_count === 1 ? 'environment' : 'environments' }}
            <template v-if="application.status === 'archived'"> · archived</template>
          </p>
        </RouterLink>
      </div>
    </section>

    <section class="card stack">
      <h2>Members</h2>
      <p class="small muted">
        Members can see this project. What they can change depends on their organization role;
        managers can also edit the project and this list. Organization admins have access to every
        project.
      </p>
      <ul class="list">
        <li v-for="member in members" :key="member.user_id">
          <div class="grow">
            <strong>{{ member.full_name }}</strong>
            <p class="small muted">
              {{ member.email }} · {{ member.organization_role.replace('_', ' ') }}
            </p>
          </div>
          <select
            v-if="canManage && !archived"
            class="input role"
            :value="member.role"
            :aria-label="`Project role of ${member.full_name}`"
            @change="setRole(member, ($event.target as HTMLSelectElement).value as ProjectRole)"
          >
            <option value="member">Member</option>
            <option value="manager">Manager</option>
          </select>
          <span v-else class="tag">{{ member.role }}</span>
          <BaseButton
            v-if="canManage || member.user_id === auth.user?.id"
            variant="ghost"
            @click="removeMember(member)"
          >
            {{ member.user_id === auth.user?.id ? 'Leave' : 'Remove' }}
          </BaseButton>
        </li>
      </ul>
      <form
        v-if="canManage && !archived && candidates.length > 0"
        class="cluster"
        @submit.prevent="addMember"
      >
        <label class="grow">
          <span class="visually-hidden">Person to add</span>
          <select v-model="newMember" class="input">
            <option value="" disabled>Add someone from the organization…</option>
            <option v-for="person in candidates" :key="person.id" :value="person.user_id ?? ''">
              {{ person.full_name }} ({{ person.email }})
            </option>
          </select>
        </label>
        <BaseButton type="submit" variant="secondary" :disabled="!newMember" :loading="saving">
          Add
        </BaseButton>
      </form>
    </section>

    <BaseModal :open="dialog === 'edit'" title="Edit project" @close="dialog = null">
      <form class="stack" novalidate @submit.prevent="saveEdit">
        <p v-if="errors.general.value" class="notice notice--danger" role="alert">
          {{ errors.general.value }}
        </p>
        <FormField v-slot="field" label="Name" :error="errors.fields.name">
          <input :id="field.id" v-model.trim="editName" class="input" maxlength="120" required />
        </FormField>
        <FormField v-slot="field" label="Description" :error="errors.fields.description">
          <textarea :id="field.id" v-model.trim="editDescription" class="input" maxlength="2000" />
        </FormField>
        <div class="cluster">
          <BaseButton type="submit" :loading="saving">Save changes</BaseButton>
          <BaseButton variant="ghost" @click="dialog = null">Cancel</BaseButton>
        </div>
      </form>
    </BaseModal>

    <BaseModal :open="dialog === 'delete'" title="Delete project" @close="dialog = null">
      <form class="stack" novalidate @submit.prevent="deleteProject">
        <p class="notice notice--danger">
          This removes the project with its applications and environments, and permanently destroys
          the test credentials stored for them.
        </p>
        <p v-if="errors.general.value" class="notice notice--danger" role="alert">
          {{ errors.general.value }}
        </p>
        <FormField
          v-slot="field"
          :label="`Type the project name to confirm: ${project.name}`"
          :error="errors.fields.confirm_name"
        >
          <input :id="field.id" v-model="confirmName" class="input" autocomplete="off" />
        </FormField>
        <div class="cluster">
          <BaseButton type="submit" :loading="saving" :disabled="confirmName !== project.name">
            Delete project
          </BaseButton>
          <BaseButton variant="ghost" @click="dialog = null">Cancel</BaseButton>
        </div>
      </form>
    </BaseModal>

    <BaseModal :open="dialog === 'application'" title="Register application" @close="dialog = null">
      <ApplicationForm
        v-if="dialog === 'application'"
        :save="createApplication"
        @cancel="dialog = null"
      />
    </BaseModal>
  </section>
</template>

<style scoped>
.app-name {
  margin: 0;
  font-size: var(--text-lg);
}

.shortcut {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: var(--space-1);
  min-width: 14rem;
  padding: var(--space-4);
}

.role {
  width: auto;
  min-height: 2rem;
}
</style>
