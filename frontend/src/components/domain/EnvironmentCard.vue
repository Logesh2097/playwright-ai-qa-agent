<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { credentialsApi, environmentsApi, type EnvRef } from '@/api/projects'
import CredentialForm from '@/components/domain/CredentialForm.vue'
import BaseButton from '@/components/ui/BaseButton.vue'
import BaseModal from '@/components/ui/BaseModal.vue'
import StatusBadge from '@/components/ui/StatusBadge.vue'
import { toUserMessage } from '@/errors/messages'
import { useNotificationsStore } from '@/stores/notifications'
import type { AccessCheck, Credential, CredentialInput, Environment } from '@/types/api'

const props = defineProps<{
  environment: Environment
  envRef: EnvRef
  /** May the user change this environment and its credentials? Display only. */
  canEdit: boolean
  /** May the user see credential metadata at all? */
  canSeeCredentials: boolean
}>()
const emit = defineEmits<{ edit: [credentialNames: string[]]; delete: []; changed: [] }>()

const notifications = useNotificationsStore()

const credentials = ref<Credential[]>([])
const credentialsError = ref<string | null>(null)
const editing = ref<Credential | 'new' | null>(null)

const check = ref<AccessCheck | null>(null)
const checking = ref(false)

const kindTone = computed(() => (props.environment.kind === 'production' ? 'danger' : 'neutral'))

async function loadCredentials(): Promise<void> {
  if (!props.canSeeCredentials) return
  try {
    credentials.value = await credentialsApi.list(props.envRef)
    credentialsError.value = null
  } catch (error) {
    credentialsError.value = toUserMessage(error)
  }
}

async function saveCredential(input: Partial<CredentialInput>): Promise<void> {
  if (editing.value === 'new') {
    await credentialsApi.create(props.envRef, input as CredentialInput)
    notifications.success('Credential stored. Its secret can no longer be displayed.')
  } else if (editing.value) {
    await credentialsApi.update(props.envRef, editing.value.id, input)
    notifications.success('Credential updated.')
  }
  editing.value = null
  await loadCredentials()
  emit('changed')
}

async function removeCredential(credential: Credential): Promise<void> {
  if (!window.confirm(`Delete the credential "${credential.name}"? This cannot be undone.`)) return
  try {
    await credentialsApi.remove(props.envRef, credential.id)
    await loadCredentials()
    emit('changed')
  } catch (error) {
    notifications.error(toUserMessage(error))
  }
}

async function runCheck(): Promise<void> {
  checking.value = true
  check.value = null
  try {
    check.value = await environmentsApi.checkAccess(props.envRef)
  } catch (error) {
    notifications.error(toUserMessage(error))
  } finally {
    checking.value = false
  }
}

onMounted(loadCredentials)
</script>

<template>
  <article class="card stack">
    <header class="cluster cluster--between">
      <div class="cluster">
        <h3 class="title">{{ environment.name }}</h3>
        <StatusBadge :tone="kindTone">{{ environment.kind }}</StatusBadge>
        <span v-if="environment.status === 'disabled'" class="tag">disabled</span>
        <span v-if="environment.is_read_only" class="tag">read-only</span>
        <span v-if="environment.requires_approval" class="tag">needs approval</span>
        <span v-if="environment.allow_private_network" class="tag">private network</span>
      </div>
      <div class="cluster">
        <BaseButton v-if="canEdit" variant="secondary" :loading="checking" @click="runCheck">
          Check access
        </BaseButton>
        <BaseButton
          v-if="canEdit"
          variant="secondary"
          @click="
            emit(
              'edit',
              credentials.map((c) => c.name),
            )
          "
        >
          Edit
        </BaseButton>
        <BaseButton v-if="canEdit" variant="ghost" @click="emit('delete')">Delete</BaseButton>
      </div>
    </header>

    <dl class="facts">
      <div>
        <dt>Base URL</dt>
        <dd class="mono">{{ environment.base_url }}</dd>
      </div>
      <div v-if="environment.api_base_url">
        <dt>API</dt>
        <dd class="mono">{{ environment.api_base_url }}</dd>
      </div>
      <div>
        <dt>Allowed origins</dt>
        <dd>
          <span
            v-for="origin in environment.allowed_origins"
            :key="origin.origin"
            class="mono origin"
          >
            {{ origin.origin }}
          </span>
        </dd>
      </div>
      <div>
        <dt>Allowed actions</dt>
        <dd class="small">{{ environment.allowed_actions.join(', ').replaceAll('_', ' ') }}</dd>
      </div>
      <div v-if="environment.variables.length > 0">
        <dt>Variables</dt>
        <dd class="mono">
          <span v-for="variable in environment.variables" :key="variable.name" class="origin">
            {{ variable.name }}={{
              variable.secret_ref ? `@${variable.secret_ref}` : variable.value
            }}
          </span>
        </dd>
      </div>
    </dl>

    <div
      v-if="check"
      class="notice"
      :class="check.reachable ? 'notice--success' : 'notice--danger'"
    >
      <p>
        <strong>{{ check.reachable ? 'Reachable.' : 'Not reachable.' }}</strong>
        {{ check.error ?? `Answered with status ${check.status} in ${check.duration_ms} ms.` }}
      </p>
      <ol v-if="check.hops.length > 1" class="hops">
        <li v-for="(hop, index) in check.hops" :key="index" class="mono">
          {{ hop.url }} → {{ hop.status ?? `blocked (${hop.blocked_code})` }}
        </li>
      </ol>
    </div>

    <section v-if="canSeeCredentials" class="stack">
      <div class="cluster cluster--between">
        <h4 class="subtitle">Test credentials</h4>
        <BaseButton v-if="canEdit" variant="secondary" @click="editing = 'new'">
          Add credential
        </BaseButton>
      </div>
      <p v-if="credentialsError" class="notice notice--danger">{{ credentialsError }}</p>
      <p v-else-if="credentials.length === 0" class="small muted">
        No test accounts yet. Add one so tests can sign in to this environment.
      </p>
      <ul v-else class="list">
        <li v-for="credential in credentials" :key="credential.id">
          <div class="grow">
            <strong>{{ credential.name }}</strong>
            <span class="tag">{{ credential.role_label }}</span>
            <p class="small muted">
              {{ credential.kind.replaceAll('_', ' ') }} ·
              <template v-if="credential.source === 'stored'">
                encrypted ({{ credential.field_names.join(', ') }}) — values hidden
              </template>
              <template v-else>
                reference <span class="mono">{{ credential.external_ref }}</span>
              </template>
              <template v-if="credential.last_rotated_at">
                · rotated {{ new Date(credential.last_rotated_at).toLocaleDateString() }}
              </template>
            </p>
          </div>
          <BaseButton v-if="canEdit" variant="ghost" @click="editing = credential">
            Edit or rotate
          </BaseButton>
          <BaseButton v-if="canEdit" variant="ghost" @click="removeCredential(credential)">
            Delete
          </BaseButton>
        </li>
      </ul>
    </section>

    <BaseModal
      :open="editing !== null"
      :title="editing === 'new' ? 'Add test credential' : 'Edit credential'"
      @close="editing = null"
    >
      <!-- Re-created each time it opens, so no typed secret survives in component state. -->
      <CredentialForm
        v-if="editing !== null"
        :key="editing === 'new' ? 'new' : editing.id"
        :credential="editing === 'new' ? undefined : editing"
        :save="saveCredential"
        @cancel="editing = null"
      />
    </BaseModal>
  </article>
</template>

<style scoped>
.title {
  margin: 0;
  font-size: var(--text-lg);
}

.subtitle {
  margin: 0;
  font-size: var(--text-md);
}

.facts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr));
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

.origin {
  display: block;
}

.grow .tag {
  margin-left: var(--space-2);
}

.hops {
  margin: var(--space-2) 0 0;
  padding-left: var(--space-5);
}
</style>
