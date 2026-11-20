<script setup lang="ts">
/** The signed-in person's profile and security, and the organization's members, roles and audit log. */
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { organizationApi } from '@/api/admin'
import { authApi } from '@/api/auth'
import BaseButton from '@/components/ui/BaseButton.vue'
import BaseModal from '@/components/ui/BaseModal.vue'
import CodeViewer from '@/components/ui/CodeViewer.vue'
import DataTable, { type Column } from '@/components/ui/DataTable.vue'
import ErrorState from '@/components/ui/ErrorState.vue'
import FilterBar from '@/components/ui/FilterBar.vue'
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
import type { AuditEntry, Member } from '@/types/admin'
import type { OrgRole } from '@/types/api'

const auth = useAuthStore()
const router = useRouter()
const notifications = useNotificationsStore()
const org = computed(() => auth.organizationId ?? '')
const ROLES: OrgRole[] = ['owner', 'admin', 'qa_engineer', 'developer', 'viewer']
const tabs = computed(() => [
  { id: 'profile', label: 'Profile' },
  { id: 'security', label: 'Security' },
  { id: 'organization', label: 'Organization' },
  { id: 'roles', label: 'Roles and permissions' },
  ...(auth.isAdmin ? [{ id: 'audit', label: 'Audit log' }] : []),
])

// --- security -------------------------------------------------------------------------
const passwordErrors = useFormErrors()
const password = reactive({ current: '', next: '', repeat: '' })
const changing = ref(false)
const mismatch = computed(() => password.repeat !== '' && password.repeat !== password.next)

async function changePassword(): Promise<void> {
  if (mismatch.value) return
  changing.value = true
  passwordErrors.clear()
  try {
    // The server ends every session and answers with a new one for this browser.
    auth.applySession(await authApi.changePassword(password.current, password.next))
    notifications.success('Password changed. Every other session has been signed out.')
  } catch (error) {
    passwordErrors.capture(error)
  } finally {
    changing.value = false
    Object.assign(password, { current: '', next: '', repeat: '' })
  }
}

async function signOutEverywhere(): Promise<void> {
  if (!window.confirm('Sign out of every device, including this one?')) return
  try {
    await authApi.logoutAll()
  } catch (error) {
    notifications.error(toUserMessage(error))
    return
  }
  await auth.logout().catch(() => undefined)
  await router.push({ name: 'login' })
}

// --- organization ---------------------------------------------------------------------
const organization = useAsync(() => organizationApi.get(org.value))
const members = useAsync(() => organizationApi.members(org.value))
const orgName = ref('')
const renaming = ref(false)
const inviteOpen = ref(false)
const inviteErrors = useFormErrors()
const inviting = ref(false)
const invite = reactive({ email: '', role: 'developer' as OrgRole })
const memberColumns: Column[] = [
  { key: 'person', label: 'Member' },
  { key: 'role', label: 'Role' },
  { key: 'status', label: 'Status' },
  { key: 'actions', label: '' },
]

async function rename(): Promise<void> {
  renaming.value = true
  try {
    await organizationApi.rename(org.value, orgName.value)
    notifications.success('Organization renamed.')
    await Promise.all([organization.run(), auth.reloadProfile()])
  } catch (error) {
    notifications.error(toUserMessage(error))
  } finally {
    renaming.value = false
  }
}

async function sendInvite(): Promise<void> {
  inviting.value = true
  inviteErrors.clear()
  try {
    await organizationApi.invite(org.value, invite.email, invite.role)
    inviteOpen.value = false
    invite.email = ''
    notifications.success('Invitation sent.')
    await members.run()
  } catch (error) {
    inviteErrors.capture(error)
  } finally {
    inviting.value = false
  }
}

async function setRole(member: Member, role: OrgRole): Promise<void> {
  try {
    await organizationApi.setRole(org.value, member.id, role)
  } catch (error) {
    notifications.error(toUserMessage(error))
  }
  await members.run()
}

async function remove(member: Member): Promise<void> {
  if (!window.confirm(`Remove ${member.email} from the organization? Their access ends at once.`))
    return
  try {
    await organizationApi.removeMember(org.value, member.id)
  } catch (error) {
    notifications.error(toUserMessage(error))
  }
  await members.run()
}

// --- roles ----------------------------------------------------------------------------
const roles = useAsync(() => organizationApi.roles())
const permissions = computed(() =>
  [...new Set((roles.data.value ?? []).flatMap((r) => r.permissions))].sort(),
)

// --- audit ----------------------------------------------------------------------------
const audit = ref<AuditEntry[]>([])
const auditCursor = ref<string | null>(null)
const auditLoading = ref(false)
const auditError = ref<string | null>(null)
const auditAction = ref('')
const opened = ref<AuditEntry | null>(null)
const auditColumns: Column[] = [
  { key: 'created_at', label: 'When' },
  { key: 'action', label: 'Action' },
  { key: 'actor', label: 'By' },
  { key: 'resource', label: 'On' },
  { key: 'details', label: '' },
]

async function loadAudit(reset: boolean): Promise<void> {
  auditLoading.value = true
  auditError.value = null
  try {
    const page = await organizationApi.auditLogs(org.value, {
      limit: 50,
      action: auditAction.value || undefined,
      cursor: reset ? undefined : (auditCursor.value ?? undefined),
    })
    audit.value = reset ? page.items : [...audit.value, ...page.items]
    auditCursor.value = page.next_cursor
  } catch (error) {
    auditError.value = toUserMessage(error)
  } finally {
    auditLoading.value = false
  }
}

onMounted(async () => {
  void roles.run()
  void members.run()
  const found = await organization.run()
  orgName.value = found?.name ?? ''
  if (auth.isAdmin) void loadAudit(true)
})
</script>

<template>
  <section class="stack stack--lg">
    <header>
      <h1>Settings</h1>
      <p class="muted">
        Your account and the organization {{ auth.membership?.organization_name }}.
      </p>
    </header>

    <TabList v-slot="{ active }" :tabs="tabs" label="Settings">
      <div v-if="active === 'profile'" class="card stack">
        <h2>Profile</h2>
        <dl class="facts">
          <div>
            <dt>Name</dt>
            <dd>{{ auth.user?.full_name }}</dd>
          </div>
          <div>
            <dt>Email</dt>
            <dd>{{ auth.user?.email }}</dd>
          </div>
          <div>
            <dt>Member since</dt>
            <dd>{{ dateTime(auth.user?.created_at) }}</dd>
          </div>
          <div>
            <dt>Last sign-in</dt>
            <dd>{{ dateTime(auth.user?.last_login_at) }}</dd>
          </div>
          <div>
            <dt>Role in this organization</dt>
            <dd>{{ label(auth.role) }}</dd>
          </div>
        </dl>
        <h3>Organizations</h3>
        <ul class="list">
          <li v-for="item in auth.memberships" :key="item.organization_id">
            <span class="grow">{{ item.organization_name }}</span>
            <span class="tag">{{ label(item.role) }}</span>
          </li>
        </ul>
      </div>

      <div v-else-if="active === 'security'" class="stack">
        <form class="card stack narrow" novalidate @submit.prevent="changePassword">
          <h2>Change password</h2>
          <p v-if="passwordErrors.general.value" class="notice notice--danger" role="alert">
            {{ passwordErrors.general.value }}
          </p>
          <FormField
            v-slot="field"
            label="Current password"
            :error="passwordErrors.fields.current_password"
          >
            <input
              :id="field.id"
              v-model="password.current"
              class="input"
              type="password"
              autocomplete="current-password"
              required
            />
          </FormField>
          <FormField
            v-slot="field"
            label="New password"
            :error="passwordErrors.fields.new_password"
            hint="At least 12 characters. Not your name or email address."
          >
            <input
              :id="field.id"
              v-model="password.next"
              class="input"
              type="password"
              autocomplete="new-password"
              required
            />
          </FormField>
          <FormField
            v-slot="field"
            label="Repeat the new password"
            :error="mismatch ? 'The two passwords are not the same.' : undefined"
          >
            <input
              :id="field.id"
              v-model="password.repeat"
              class="input"
              type="password"
              autocomplete="new-password"
              required
            />
          </FormField>
          <p class="small muted">Changing the password signs out every other session.</p>
          <BaseButton
            type="submit"
            :loading="changing"
            :disabled="!password.current || !password.next || mismatch"
            >Change password</BaseButton
          >
        </form>
        <div class="card stack">
          <h2>Sessions</h2>
          <p class="small muted">
            Sign-in sessions last a limited time and are renewed in the background. If a device was
            lost, end every session.
          </p>
          <div>
            <BaseButton variant="ghost" @click="signOutEverywhere">Sign out everywhere</BaseButton>
          </div>
        </div>
        <div class="card stack">
          <h2>How this installation protects access</h2>
          <ul class="plain small">
            <li>Passwords are stored only as salted hashes; nobody can read them back.</li>
            <li>
              Test credentials and integration secrets are encrypted and never shown after saving.
            </li>
            <li>Every permission is checked on the server for every request.</li>
            <li>
              Evidence files are served only to people who can see the project they belong to.
            </li>
          </ul>
        </div>
      </div>

      <div v-else-if="active === 'organization'" class="stack">
        <LoadingState
          v-if="organization.loading.value && !organization.data.value"
          label="Loading…"
        />
        <ErrorState v-else-if="organization.error.value" :message="organization.error.value" />
        <form v-else class="card stack narrow" @submit.prevent="rename">
          <h2>Organization</h2>
          <FormField v-slot="field" label="Name">
            <input
              :id="field.id"
              v-model.trim="orgName"
              class="input"
              maxlength="120"
              :disabled="!auth.isAdmin"
              required
            />
          </FormField>
          <p class="small muted">
            Identifier: <span class="mono">{{ organization.data.value?.slug }}</span>
          </p>
          <div v-if="auth.isAdmin">
            <BaseButton
              type="submit"
              :loading="renaming"
              :disabled="!orgName || orgName === organization.data.value?.name"
              >Save</BaseButton
            >
          </div>
        </form>
        <div class="card stack">
          <div class="cluster cluster--between">
            <h2>Members</h2>
            <BaseButton v-if="auth.isAdmin" @click="inviteOpen = true">Invite someone</BaseButton>
          </div>
          <ErrorState v-if="members.error.value" :message="members.error.value" />
          <DataTable
            v-else
            :columns="memberColumns"
            :rows="members.data.value ?? []"
            :row-key="(row) => row.id"
            caption="Members of the organization"
          >
            <template #person="{ row }">
              <strong>{{ row.full_name ?? row.email }}</strong>
              <span v-if="row.full_name" class="small muted"> {{ row.email }}</span>
            </template>
            <template #role="{ row }">
              <select
                v-if="auth.isAdmin && row.role !== 'owner' && row.user_id !== auth.user?.id"
                class="input role"
                :value="row.role"
                :aria-label="`Role of ${row.email}`"
                @change="setRole(row, ($event.target as HTMLSelectElement).value as OrgRole)"
              >
                <option
                  v-for="role in ROLES.filter((r) => r !== 'owner')"
                  :key="role"
                  :value="role"
                >
                  {{ label(role) }}
                </option>
              </select>
              <span v-else>{{ label(row.role) }}</span>
            </template>
            <template #status="{ row }">
              <StatusBadge
                :tone="
                  row.status === 'active'
                    ? 'success'
                    : row.status === 'invited'
                      ? 'warning'
                      : 'neutral'
                "
                >{{ row.status }}</StatusBadge
              >
            </template>
            <template #actions="{ row }">
              <BaseButton
                v-if="auth.isAdmin && row.role !== 'owner' && row.user_id !== auth.user?.id"
                variant="ghost"
                @click="remove(row)"
                >Remove</BaseButton
              >
            </template>
          </DataTable>
        </div>
      </div>

      <div v-else-if="active === 'roles'" class="card stack">
        <h2>Roles and permissions</h2>
        <p class="small muted">
          This is the table the server enforces. Seeing a project also needs membership of that
          project, except for roles that may access all projects.
        </p>
        <ErrorState v-if="roles.error.value" :message="roles.error.value" />
        <div v-else class="table-wrap">
          <table class="table">
            <caption class="visually-hidden">
              Which role holds which permission
            </caption>
            <thead>
              <tr>
                <th scope="col">Permission</th>
                <th v-for="role in roles.data.value ?? []" :key="role.role" scope="col">
                  {{ label(role.role) }}
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="permission in permissions" :key="permission">
                <th scope="row" class="mono">{{ permission }}</th>
                <td v-for="role in roles.data.value ?? []" :key="role.role">
                  <span v-if="role.permissions.includes(permission)" aria-label="yes">●</span>
                  <span v-else class="muted" aria-label="no">–</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div v-else class="card stack">
        <h2>Audit log</h2>
        <FilterBar>
          <label class="cluster small">
            Action
            <input
              v-model.trim="auditAction"
              class="input"
              placeholder="e.g. execution.start"
              @change="loadAudit(true)"
            />
          </label>
        </FilterBar>
        <ErrorState v-if="auditError" :message="auditError" />
        <DataTable
          v-else
          :columns="auditColumns"
          :rows="audit"
          :row-key="(row) => row.id"
          caption="Recorded actions, newest first"
          empty="No entries."
        >
          <template #created_at="{ row }">{{ dateTime(row.created_at) }}</template>
          <template #action="{ row }"
            ><span class="mono">{{ row.action }}</span></template
          >
          <template #actor="{ row }"
            >{{ label(row.actor_type)
            }}<span v-if="row.ip" class="small muted"> · {{ row.ip }}</span></template
          >
          <template #resource="{ row }">{{ label(row.resource_type) }}</template>
          <template #details="{ row }"
            ><BaseButton variant="ghost" @click="opened = row">Details</BaseButton></template
          >
        </DataTable>
        <div v-if="auditCursor">
          <BaseButton variant="ghost" :loading="auditLoading" @click="loadAudit(false)"
            >Load older entries</BaseButton
          >
        </div>
      </div>
    </TabList>

    <BaseModal :open="inviteOpen" title="Invite someone" @close="inviteOpen = false">
      <form class="stack" novalidate @submit.prevent="sendInvite">
        <p v-if="inviteErrors.general.value" class="notice notice--danger" role="alert">
          {{ inviteErrors.general.value }}
        </p>
        <FormField v-slot="field" label="Email" :error="inviteErrors.fields.email">
          <input :id="field.id" v-model.trim="invite.email" class="input" type="email" required />
        </FormField>
        <FormField v-slot="field" label="Role" :error="inviteErrors.fields.role">
          <select :id="field.id" v-model="invite.role" class="input">
            <option v-for="role in ROLES.filter((r) => r !== 'owner')" :key="role" :value="role">
              {{ label(role) }}
            </option>
          </select>
        </FormField>
        <div class="cluster">
          <BaseButton type="submit" :loading="inviting" :disabled="!invite.email"
            >Send invitation</BaseButton
          >
          <BaseButton variant="ghost" @click="inviteOpen = false">Cancel</BaseButton>
        </div>
      </form>
    </BaseModal>

    <BaseModal :open="opened !== null" title="Audit entry" @close="opened = null">
      <CodeViewer
        v-if="opened"
        :content="JSON.stringify(opened, null, 2)"
        label="Entry"
        :max-lines="60"
      />
    </BaseModal>
  </section>
</template>

<style scoped>
.facts {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(14rem, 1fr));
  gap: var(--space-3) var(--space-5);
  margin: 0;
}

.facts dt {
  color: var(--color-text-muted);
  font-size: var(--text-xs);
  font-weight: 600;
}

.facts dd {
  margin: 0;
  overflow-wrap: anywhere;
}

.narrow {
  max-width: 32rem;
}

.role {
  width: auto;
  min-height: 2rem;
}

.plain {
  margin: 0;
  padding-left: var(--space-5);
}
</style>
