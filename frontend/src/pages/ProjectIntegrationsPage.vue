<script setup lang="ts">
/** GitHub repositories linked to one project, and what each link is allowed to do. */
import { computed, onMounted, reactive, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { githubApi } from '@/api/admin'
import { applicationsApi, environmentsApi } from '@/api/projects'
import { suitesApi } from '@/api/testing'
import BaseButton from '@/components/ui/BaseButton.vue'
import BaseModal from '@/components/ui/BaseModal.vue'
import CodeViewer from '@/components/ui/CodeViewer.vue'
import DataTable, { type Column } from '@/components/ui/DataTable.vue'
import EmptyState from '@/components/ui/EmptyState.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import FormField from '@/components/ui/FormField.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import { useFormErrors } from '@/composables/useFormErrors'
import { toUserMessage } from '@/errors/messages'
import { dateTime, label } from '@/pages/reportFormat'
import { useAuthStore } from '@/stores/auth'
import { useNotificationsStore } from '@/stores/notifications'
import type {
  GithubAvailableRepository,
  GithubConnection,
  GithubDelivery,
  GithubLink,
} from '@/types/admin'
import type { Suite } from '@/types/api'

const props = defineProps<{ projectId: string }>()
const auth = useAuthStore()
const notifications = useNotificationsStore()
const org = computed(() => auth.organizationId ?? '')

const links = ref<GithubLink[]>([])
const suites = ref<Suite[]>([])
const environments = ref<{ id: string; name: string }[]>([])
const connections = ref<GithubConnection[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)
const secret = ref<{ repository: string; url: string; value: string } | null>(null)
const deliveries = reactive(new Map<string, GithubDelivery[]>())

const approvedSuites = computed(() => suites.value.filter((suite) => suite.status === 'approved'))
const origin = window.location.origin

async function load(): Promise<void> {
  try {
    const [found, allSuites, applications] = await Promise.all([
      githubApi.links(org.value, props.projectId),
      suitesApi.list(org.value, props.projectId),
      applicationsApi.list(org.value, props.projectId),
    ])
    links.value = found
    suites.value = allSuites
    const lists = await Promise.all(
      applications.map(async (application) =>
        (
          await environmentsApi.list({
            org: org.value,
            project: props.projectId,
            application: application.id,
          })
        ).map((item) => ({ id: item.id, name: `${application.name} — ${item.name}` })),
      ),
    )
    environments.value = lists.flat()
    if (auth.isAdmin) connections.value = await githubApi.connections(org.value)
  } catch (error) {
    loadError.value = toUserMessage(error)
  } finally {
    loading.value = false
  }
}

// --- linking --------------------------------------------------------------------------
const linkOpen = ref(false)
const linkErrors = useFormErrors()
const linking = ref(false)
const available = ref<GithubAvailableRepository[]>([])
const linkForm = reactive({ connection_id: '', full_name: '' })

async function openLink(): Promise<void> {
  linkErrors.clear()
  linkForm.connection_id = connections.value[0]?.id ?? ''
  linkForm.full_name = ''
  linkOpen.value = true
  await loadAvailable()
}

async function loadAvailable(): Promise<void> {
  available.value = []
  if (!linkForm.connection_id) return
  try {
    available.value = (await githubApi.available(org.value, linkForm.connection_id)).filter(
      (r) => !r.archived,
    )
  } catch (error) {
    linkErrors.capture(error)
  }
}

async function submitLink(): Promise<void> {
  linking.value = true
  linkErrors.clear()
  try {
    const created = await githubApi.link(
      org.value,
      props.projectId,
      linkForm.connection_id,
      linkForm.full_name,
    )
    secret.value = {
      repository: created.full_name,
      url: origin + created.webhook_url,
      value: created.webhook_secret ?? '',
    }
    linkOpen.value = false
    await load()
  } catch (error) {
    linkErrors.capture(error)
  } finally {
    linking.value = false
  }
}

// --- settings of one link ---------------------------------------------------------------
const saving = ref<string | null>(null)

async function save(link: GithubLink, changes: Partial<GithubLink>): Promise<void> {
  saving.value = link.id
  try {
    const updated = await githubApi.configure(org.value, props.projectId, link.id, changes)
    links.value = links.value.map((item) => (item.id === updated.id ? updated : item))
  } catch (error) {
    notifications.error(toUserMessage(error))
    await load()
  } finally {
    saving.value = null
  }
}

function toggleSuite(link: GithubLink, suiteId: string, on: boolean): void {
  const next = on ? [...link.suite_ids, suiteId] : link.suite_ids.filter((id) => id !== suiteId)
  void save(link, { suite_ids: next })
}

async function rotate(link: GithubLink): Promise<void> {
  if (
    !window.confirm(
      'Replace the webhook secret? Deliveries signed with the old one will be refused.',
    )
  )
    return
  try {
    const updated = await githubApi.rotate(org.value, props.projectId, link.id)
    secret.value = {
      repository: updated.full_name,
      url: origin + updated.webhook_url,
      value: updated.webhook_secret ?? '',
    }
  } catch (error) {
    notifications.error(toUserMessage(error))
  }
}

async function unlink(link: GithubLink): Promise<void> {
  if (!window.confirm(`Unlink ${link.full_name}? Its webhook stops working.`)) return
  try {
    await githubApi.unlink(org.value, props.projectId, link.id)
    await load()
  } catch (error) {
    notifications.error(toUserMessage(error))
  }
}

async function showDeliveries(link: GithubLink): Promise<void> {
  if (deliveries.has(link.id)) {
    deliveries.delete(link.id)
    return
  }
  try {
    deliveries.set(link.id, await githubApi.deliveries(org.value, props.projectId, link.id))
  } catch (error) {
    notifications.error(toUserMessage(error))
  }
}

const deliveryColumns: Column[] = [
  { key: 'received_at', label: 'Received' },
  { key: 'event', label: 'Event' },
  { key: 'outcome', label: 'Outcome' },
  { key: 'reason', label: 'Reason' },
]

onMounted(load)
</script>

<template>
  <LoadingState v-if="loading" label="Loading integrations…" />
  <ErrorState v-else-if="loadError" :message="loadError" />
  <section v-else class="stack stack--lg">
    <header class="stack">
      <RouterLink class="small" :to="{ name: 'project', params: { projectId } }"
        >← Project</RouterLink
      >
      <div class="cluster cluster--between">
        <div>
          <h1>GitHub repositories</h1>
          <p class="muted">
            Tests start, and results are written to GitHub, only for repositories linked here and
            only where the switches below are on.
          </p>
        </div>
        <BaseButton v-if="auth.isAdmin && connections.length > 0" @click="openLink"
          >Link a repository</BaseButton
        >
      </div>
      <p v-if="auth.isAdmin && connections.length === 0" class="notice">
        Connect to GitHub first, on the
        <RouterLink :to="{ name: 'integrations' }">Integrations</RouterLink> page.
      </p>
    </header>

    <div v-if="secret" class="card stack" role="status">
      <strong>Webhook for {{ secret.repository }}. The secret is shown only now.</strong>
      <p class="small muted">
        On GitHub: repository → Settings → Webhooks → Add webhook. Content type
        <code>application/json</code>; events “Pull requests” and, if wanted, “Pushes”.
      </p>
      <CodeViewer :content="secret.url" label="Payload URL" />
      <CodeViewer :content="secret.value" label="Secret" />
      <BaseButton variant="ghost" @click="secret = null">I have entered it</BaseButton>
    </div>

    <EmptyState
      v-if="links.length === 0"
      title="No repository linked"
      message="Link a repository to test its pull requests."
    />

    <article v-for="link in links" :key="link.id" class="card stack">
      <div class="cluster cluster--between">
        <div>
          <h2>{{ link.full_name }}</h2>
          <span class="small muted"
            >default branch {{ link.default_branch }} ·
            {{ link.private ? 'private' : 'public' }}</span
          >
        </div>
        <StatusBadge
          :tone="link.enabled && (link.on_pull_request || link.on_push) ? 'success' : 'neutral'"
        >
          {{
            !link.enabled
              ? 'switched off'
              : link.on_pull_request || link.on_push
                ? 'triggers on'
                : 'no trigger on'
          }}
        </StatusBadge>
      </div>

      <fieldset class="settings" :disabled="!auth.isAdmin || saving === link.id">
        <FormField v-slot="field" label="Environment to test against">
          <select
            :id="field.id"
            class="input"
            :value="link.environment_id ?? ''"
            @change="
              save(link, { environment_id: ($event.target as HTMLSelectElement).value || null })
            "
          >
            <option value="">Not chosen</option>
            <option v-for="item in environments" :key="item.id" :value="item.id">
              {{ item.name }}
            </option>
          </select>
        </FormField>
        <FormField
          v-slot="field"
          label="Branches"
          hint="Comma-separated. Names or patterns such as release/*."
        >
          <input
            :id="field.id"
            class="input"
            :value="link.branches.join(', ')"
            @change="
              save(link, {
                branches: ($event.target as HTMLInputElement).value
                  .split(',')
                  .map((b) => b.trim())
                  .filter(Boolean),
              })
            "
          />
        </FormField>
        <div class="stack">
          <span class="small"><strong>Suites of a full run</strong> (approved suites only)</span>
          <p v-if="approvedSuites.length === 0" class="small muted">
            This project has no approved suite yet.
          </p>
          <label v-for="suite in approvedSuites" :key="suite.id" class="checkbox">
            <input
              type="checkbox"
              :checked="link.suite_ids.includes(suite.id)"
              @change="toggleSuite(link, suite.id, ($event.target as HTMLInputElement).checked)"
            />
            {{ suite.name }}
          </label>
        </div>
        <div class="stack">
          <span class="small"><strong>Start tests when</strong></span>
          <label class="checkbox">
            <input
              type="checkbox"
              :checked="link.on_pull_request"
              @change="save(link, { on_pull_request: ($event.target as HTMLInputElement).checked })"
            />
            a pull request is opened or gets new commits
          </label>
          <label class="checkbox">
            <input
              type="checkbox"
              :checked="link.on_push"
              @change="save(link, { on_push: ($event.target as HTMLInputElement).checked })"
            />
            commits are pushed to one of the branches
          </label>
          <span class="small"><strong>Write to GitHub</strong> (off unless ticked)</span>
          <label class="checkbox">
            <input
              type="checkbox"
              :checked="link.status_enabled"
              @change="save(link, { status_enabled: ($event.target as HTMLInputElement).checked })"
            />
            a commit status per suite
          </label>
          <label class="checkbox">
            <input
              type="checkbox"
              :checked="link.comment_enabled"
              @change="save(link, { comment_enabled: ($event.target as HTMLInputElement).checked })"
            />
            one comment on the pull request
          </label>
          <label class="checkbox">
            <input
              type="checkbox"
              :checked="link.enabled"
              @change="save(link, { enabled: ($event.target as HTMLInputElement).checked })"
            />
            link switched on
          </label>
        </div>
      </fieldset>
      <p v-if="link.path_rules.length > 0" class="small muted">
        {{ link.path_rules.length }} path rule(s); selection mode {{ label(link.selection_mode) }}.
        A run is narrowed only when every changed file is covered by a rule.
      </p>

      <div class="cluster">
        <BaseButton variant="ghost" @click="showDeliveries(link)">
          {{ deliveries.has(link.id) ? 'Hide deliveries' : 'Recent deliveries' }}
        </BaseButton>
        <template v-if="auth.isAdmin">
          <BaseButton variant="ghost" @click="rotate(link)">New webhook secret</BaseButton>
          <BaseButton variant="ghost" @click="unlink(link)">Unlink</BaseButton>
        </template>
      </div>
      <DataTable
        v-if="deliveries.has(link.id)"
        :columns="deliveryColumns"
        :rows="deliveries.get(link.id) ?? []"
        :row-key="(row) => row.id"
        caption="Recent webhook deliveries"
        empty="No delivery has been received."
      >
        <template #received_at="{ row }">{{ dateTime(row.received_at) }}</template>
        <template #event="{ row }"
          >{{ row.event }}<template v-if="row.action"> · {{ row.action }}</template></template
        >
        <template #outcome="{ row }">
          <StatusBadge
            :tone="
              row.outcome === 'processed'
                ? 'success'
                : row.outcome === 'failed'
                  ? 'danger'
                  : 'neutral'
            "
            >{{ row.outcome }}</StatusBadge
          >
        </template>
      </DataTable>
    </article>

    <BaseModal :open="linkOpen" title="Link a repository" @close="linkOpen = false">
      <form class="stack" novalidate @submit.prevent="submitLink">
        <p v-if="linkErrors.general.value" class="notice notice--danger" role="alert">
          {{ linkErrors.general.value }}
        </p>
        <FormField v-slot="field" label="Connection">
          <select
            :id="field.id"
            v-model="linkForm.connection_id"
            class="input"
            @change="loadAvailable"
          >
            <option v-for="item in connections" :key="item.id" :value="item.id">
              {{ item.name }} ({{ item.account }})
            </option>
          </select>
        </FormField>
        <FormField
          v-slot="field"
          label="Repository"
          :error="linkErrors.fields.full_name"
          hint="Only repositories the connection can see."
        >
          <select :id="field.id" v-model="linkForm.full_name" class="input" required>
            <option value="" disabled>Choose a repository</option>
            <option v-for="item in available" :key="item.id" :value="item.full_name">
              {{ item.full_name }}
            </option>
          </select>
        </FormField>
        <div class="cluster">
          <BaseButton type="submit" :loading="linking" :disabled="!linkForm.full_name"
            >Link</BaseButton
          >
          <BaseButton variant="ghost" @click="linkOpen = false">Cancel</BaseButton>
        </div>
      </form>
    </BaseModal>
  </section>
</template>

<style scoped>
.settings {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(17rem, 1fr));
  gap: var(--space-4) var(--space-5);
  margin: 0;
  padding: 0;
  border: 0;
}
</style>
