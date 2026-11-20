<script setup lang="ts">
/** Organization-wide integrations: GitHub connections, CI API keys, and the browser (MCP) service. */
import { computed, onMounted, reactive, ref } from 'vue'

import { apiKeysApi, githubApi, organizationApi } from '@/api/admin'
import { projectsApi } from '@/api/projects'
import BaseButton from '@/components/ui/BaseButton.vue'
import BaseModal from '@/components/ui/BaseModal.vue'
import CodeViewer from '@/components/ui/CodeViewer.vue'
import DataTable, { type Column } from '@/components/ui/DataTable.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import FormField from '@/components/ui/FormField.vue'
import LoadingState from '@/components/ui/LoadingState.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import TabList from '@/components/ui/TabList.vue'
import { useAsync } from '@/composables/useAsync'
import { useFormErrors } from '@/composables/useFormErrors'
import { toUserMessage } from '@/errors/messages'
import { dateTime, label } from '@/pages/reportFormat'
import { useAuthStore } from '@/stores/auth'
import { useNotificationsStore } from '@/stores/notifications'
import type { ApiKey, GithubConnection } from '@/types/admin'
import type { Project } from '@/types/api'

const auth = useAuthStore()
const notifications = useNotificationsStore()
const org = computed(() => auth.organizationId ?? '')
const tabs = [
  { id: 'github', label: 'GitHub' },
  { id: 'ci', label: 'CI/CD' },
  { id: 'mcp', label: 'Browser (MCP)' },
]

const connections = useAsync(() => githubApi.connections(org.value))
const keys = useAsync(() => apiKeysApi.list(org.value))
const browser = useAsync(() => organizationApi.browser(org.value))
const projects = ref<Project[]>([])

// --- GitHub ---------------------------------------------------------------------------
const connectOpen = ref(false)
const connectErrors = useFormErrors()
const connecting = ref(false)
const connect = reactive({
  name: '',
  kind: 'token',
  token: '',
  app_id: '',
  private_key: '',
  installation_id: '',
})

async function submitConnection(): Promise<void> {
  connecting.value = true
  connectErrors.clear()
  try {
    await githubApi.connect(
      org.value,
      connect.kind === 'token'
        ? { name: connect.name, token: connect.token }
        : {
            name: connect.name,
            app_id: connect.app_id,
            private_key: connect.private_key,
            installation_id: Number(connect.installation_id),
          },
    )
    connectOpen.value = false
    notifications.success('Connected to GitHub.')
    await connections.run()
  } catch (error) {
    connectErrors.capture(error)
  } finally {
    connecting.value = false
    // Secrets do not stay in the page once sent.
    connect.token = ''
    connect.private_key = ''
  }
}

async function disconnect(connection: GithubConnection): Promise<void> {
  if (!window.confirm(`Remove "${connection.name}" and every repository link that uses it?`)) return
  try {
    await githubApi.disconnect(org.value, connection.id)
    await connections.run()
  } catch (error) {
    notifications.error(toUserMessage(error))
  }
}

// --- API keys -------------------------------------------------------------------------
const keyOpen = ref(false)
const keyErrors = useFormErrors()
const creatingKey = ref(false)
const createdKey = ref<string | null>(null)
const keyForm = reactive({ name: '', run: true, read: true, project_id: '', expires_in_days: 90 })
const keyColumns: Column[] = [
  { key: 'name', label: 'Name' },
  { key: 'key_prefix', label: 'Key begins' },
  { key: 'scopes', label: 'May' },
  { key: 'last_used_at', label: 'Last used' },
  { key: 'expires_at', label: 'Expires' },
  { key: 'state', label: 'State' },
  { key: 'actions', label: '' },
]

async function submitKey(): Promise<void> {
  creatingKey.value = true
  keyErrors.clear()
  try {
    const created = await apiKeysApi.create(org.value, {
      name: keyForm.name,
      scopes: [
        ...(keyForm.run ? ['executions:write'] : []),
        ...(keyForm.read ? ['executions:read'] : []),
      ],
      project_id: keyForm.project_id || null,
      expires_in_days: keyForm.expires_in_days,
    })
    createdKey.value = created.key ?? null
    keyOpen.value = false
    await keys.run()
  } catch (error) {
    keyErrors.capture(error)
  } finally {
    creatingKey.value = false
  }
}

async function revoke(key: ApiKey): Promise<void> {
  if (!window.confirm(`Revoke "${key.name}"? Pipelines using it stop working at once.`)) return
  try {
    await apiKeysApi.revoke(org.value, key.id)
    await keys.run()
  } catch (error) {
    notifications.error(toUserMessage(error))
  }
}

function keyState(key: ApiKey): string {
  if (key.revoked_at) return 'revoked'
  if (key.expires_at && new Date(key.expires_at) <= new Date()) return 'expired'
  return 'active'
}

const toolColumns: Column[] = [
  { key: 'name', label: 'Tool' },
  { key: 'category', label: 'Kind' },
  { key: 'availability', label: 'Availability' },
  { key: 'enabled', label: 'Enabled' },
]

onMounted(async () => {
  void browser.run()
  if (!auth.isAdmin) return
  void connections.run()
  void keys.run()
  try {
    projects.value = await projectsApi.list(org.value)
  } catch {
    projects.value = []
  }
})
</script>

<template>
  <section class="stack stack--lg">
    <header>
      <h1>Integrations</h1>
      <p class="muted">Connections to GitHub, keys for CI pipelines, and the browser service.</p>
    </header>

    <TabList v-slot="{ active }" :tabs="tabs" label="Integrations">
      <!-- GitHub -->
      <div v-if="active === 'github'" class="stack">
        <p v-if="!auth.isAdmin" class="notice">
          Only organization admins manage GitHub connections. Repositories linked to a project are
          shown on the project's own integrations page.
        </p>
        <template v-else>
          <div class="cluster cluster--between">
            <p class="muted small">
              A connection holds credentials for GitHub. Repositories are linked per project;
              nothing is triggered or written to GitHub until that is switched on for a repository.
            </p>
            <BaseButton @click="connectOpen = true">Connect to GitHub</BaseButton>
          </div>
          <LoadingState
            v-if="connections.loading.value && !connections.data.value"
            label="Loading…"
          />
          <ErrorState v-else-if="connections.error.value" :message="connections.error.value" />
          <ul v-else class="card list">
            <li v-if="(connections.data.value ?? []).length === 0" class="muted">
              No connections yet.
            </li>
            <li v-for="item in connections.data.value ?? []" :key="item.id">
              <span class="grow">
                <strong>{{ item.name }}</strong>
                <span class="small muted">
                  · {{ item.account }} ·
                  {{ item.auth === 'app' ? 'GitHub App' : 'access token' }}</span
                >
              </span>
              <StatusBadge :tone="item.status === 'active' ? 'success' : 'danger'">{{
                item.status
              }}</StatusBadge>
              <BaseButton variant="ghost" @click="disconnect(item)">Remove</BaseButton>
            </li>
          </ul>
        </template>
      </div>

      <!-- CI/CD -->
      <div v-else-if="active === 'ci'" class="stack">
        <p v-if="!auth.isAdmin" class="notice">Only organization admins manage API keys.</p>
        <template v-else>
          <div class="cluster cluster--between">
            <p class="muted small">
              A pipeline uses an API key to start approved tests and read the verified result. A key
              cannot approve anything and works only on the CI interface.
            </p>
            <BaseButton @click="keyOpen = true">Create API key</BaseButton>
          </div>
          <div v-if="createdKey" class="card stack" role="status">
            <strong>Copy this key now. It is not shown again.</strong>
            <CodeViewer :content="createdKey" label="API key" />
            <BaseButton variant="ghost" @click="createdKey = null">I have stored it</BaseButton>
          </div>
          <ErrorState v-if="keys.error.value" :message="keys.error.value" />
          <div v-else class="card">
            <DataTable
              :columns="keyColumns"
              :rows="keys.data.value ?? []"
              :row-key="(row) => row.id"
              caption="API keys"
              empty="No API keys yet."
            >
              <template #key_prefix="{ row }"
                ><span class="mono">{{ row.key_prefix.slice(-6) }}…</span></template
              >
              <template #scopes="{ row }">
                {{
                  row.scopes
                    .map((s) => (s === 'executions:write' ? 'start tests' : 'read results'))
                    .join(', ')
                }}
              </template>
              <template #last_used_at="{ row }">{{ dateTime(row.last_used_at) }}</template>
              <template #expires_at="{ row }">{{ dateTime(row.expires_at) }}</template>
              <template #state="{ row }">
                <StatusBadge :tone="keyState(row) === 'active' ? 'success' : 'neutral'">{{
                  keyState(row)
                }}</StatusBadge>
              </template>
              <template #actions="{ row }">
                <BaseButton v-if="keyState(row) === 'active'" variant="ghost" @click="revoke(row)"
                  >Revoke</BaseButton
                >
              </template>
            </DataTable>
          </div>
          <div class="card stack">
            <h2>Using a key in a pipeline</h2>
            <CodeViewer
              label="Shell"
              content="QA_AGENT_URL=https://your-qa-agent QA_AGENT_API_KEY=$SECRET \
python qa_agent_ci.py --project <project id> --environment <environment id> \
  --suite <suite id> --junit results.xml

# exit 0 passed · 1 failed · 2 not a verified pass · 3 did not finish · 4 could not run"
            />
          </div>
        </template>
      </div>

      <!-- MCP -->
      <div v-else class="stack">
        <LoadingState v-if="browser.loading.value && !browser.data.value" label="Checking…" />
        <ErrorState
          v-else-if="browser.error.value || !browser.data.value"
          :message="browser.error.value ?? 'Unavailable.'"
        />
        <template v-else>
          <div class="card cluster cluster--between">
            <div>
              <strong>Playwright MCP browser service</strong>
              <p class="small muted">
                Used for connection and login checks and for the read-only look at an application
                before tests are written. Test runs use the deterministic engine.
              </p>
            </div>
            <StatusBadge :tone="browser.data.value.configured ? 'success' : 'neutral'">
              {{ browser.data.value.configured ? 'configured' : 'not configured' }}
            </StatusBadge>
          </div>
          <div v-if="browser.data.value.connection" class="card">
            <CodeViewer
              :content="JSON.stringify(browser.data.value.connection, null, 2)"
              label="Connection"
            />
          </div>
          <div class="card stack">
            <h2>Tools</h2>
            <p class="small muted">
              Tools are allowed by rule, not by default.
              {{
                browser.data.value.restricted
                  ? 'This organization has narrowed the list further.'
                  : ''
              }}
            </p>
            <DataTable
              :columns="toolColumns"
              :rows="browser.data.value.tools"
              :row-key="(row) => row.name"
              caption="Browser tools and whether they may be used"
              empty="No tools are known."
            >
              <template #name="{ row }"
                ><span class="mono">{{ row.name }}</span></template
              >
              <template #category="{ row }">{{ label(row.category) }}</template>
              <template #availability="{ row }">
                <StatusBadge
                  :tone="
                    row.availability === 'available'
                      ? 'success'
                      : row.availability === 'forbidden'
                        ? 'danger'
                        : 'warning'
                  "
                >
                  {{ label(row.availability) }}
                </StatusBadge>
              </template>
              <template #enabled="{ row }">{{ row.enabled ? 'yes' : 'no' }}</template>
            </DataTable>
          </div>
        </template>
      </div>
    </TabList>

    <BaseModal :open="connectOpen" title="Connect to GitHub" @close="connectOpen = false">
      <form class="stack" novalidate autocomplete="off" @submit.prevent="submitConnection">
        <p v-if="connectErrors.general.value" class="notice notice--danger" role="alert">
          {{ connectErrors.general.value }}
        </p>
        <FormField v-slot="field" label="Name" :error="connectErrors.fields.name">
          <input
            :id="field.id"
            v-model.trim="connect.name"
            class="input"
            maxlength="120"
            required
          />
        </FormField>
        <FormField v-slot="field" label="Authentication">
          <select :id="field.id" v-model="connect.kind" class="input">
            <option value="token">Fine-grained personal access token</option>
            <option value="app">GitHub App installation</option>
          </select>
        </FormField>
        <FormField
          v-if="connect.kind === 'token'"
          v-slot="field"
          label="Token"
          :error="connectErrors.fields.token"
          hint="Grant only: Metadata, Contents and Pull requests (read). Add Commit statuses and Pull requests (write) only if results are to be written back."
        >
          <input
            :id="field.id"
            v-model="connect.token"
            class="input"
            type="password"
            autocomplete="off"
            required
          />
        </FormField>
        <template v-else>
          <FormField v-slot="field" label="App ID" :error="connectErrors.fields.app_id">
            <input :id="field.id" v-model.trim="connect.app_id" class="input" required />
          </FormField>
          <FormField
            v-slot="field"
            label="Installation ID"
            :error="connectErrors.fields.installation_id"
          >
            <input
              :id="field.id"
              v-model.trim="connect.installation_id"
              class="input"
              inputmode="numeric"
              required
            />
          </FormField>
          <FormField
            v-slot="field"
            label="Private key (PEM)"
            :error="connectErrors.fields.private_key"
          >
            <textarea
              :id="field.id"
              v-model="connect.private_key"
              class="input"
              rows="5"
              autocomplete="off"
              required
            />
          </FormField>
        </template>
        <p class="small muted">
          The credentials are checked with GitHub, stored encrypted and never shown again.
        </p>
        <div class="cluster">
          <BaseButton type="submit" :loading="connecting">Connect</BaseButton>
          <BaseButton variant="ghost" @click="connectOpen = false">Cancel</BaseButton>
        </div>
      </form>
    </BaseModal>

    <BaseModal :open="keyOpen" title="Create API key" @close="keyOpen = false">
      <form class="stack" novalidate @submit.prevent="submitKey">
        <p v-if="keyErrors.general.value" class="notice notice--danger" role="alert">
          {{ keyErrors.general.value }}
        </p>
        <FormField
          v-slot="field"
          label="Name"
          :error="keyErrors.fields.name"
          hint="For example the pipeline that will use it."
        >
          <input
            :id="field.id"
            v-model.trim="keyForm.name"
            class="input"
            maxlength="120"
            required
          />
        </FormField>
        <fieldset class="stack">
          <legend class="small">What the key may do</legend>
          <label class="checkbox"
            ><input v-model="keyForm.run" type="checkbox" /> Start approved tests</label
          >
          <label class="checkbox"
            ><input v-model="keyForm.read" type="checkbox" /> Read results</label
          >
        </fieldset>
        <FormField v-slot="field" label="Project" hint="Limit the key to one project.">
          <select :id="field.id" v-model="keyForm.project_id" class="input">
            <option value="">Every project</option>
            <option v-for="project in projects" :key="project.id" :value="project.id">
              {{ project.name }}
            </option>
          </select>
        </FormField>
        <FormField v-slot="field" label="Expires after">
          <select :id="field.id" v-model.number="keyForm.expires_in_days" class="input">
            <option :value="30">30 days</option>
            <option :value="90">90 days</option>
            <option :value="365">1 year</option>
          </select>
        </FormField>
        <div class="cluster">
          <BaseButton
            type="submit"
            :loading="creatingKey"
            :disabled="!keyForm.name || (!keyForm.run && !keyForm.read)"
          >
            Create key
          </BaseButton>
          <BaseButton variant="ghost" @click="keyOpen = false">Cancel</BaseButton>
        </div>
      </form>
    </BaseModal>
  </section>
</template>
