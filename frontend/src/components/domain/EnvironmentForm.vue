<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'

import BaseButton from '@/components/ui/BaseButton.vue'
import FormField from '@/components/ui/FormField.vue'
import { useFormErrors } from '@/composables/useFormErrors'
import type {
  AuthMethod,
  BrowserAction,
  Environment,
  EnvironmentInput,
  EnvironmentKind,
  EnvironmentVariable,
} from '@/types/api'

const props = defineProps<{
  /** Present when editing. */
  environment?: Environment
  /** Names of this environment's credentials, for references. */
  credentialNames?: string[]
  /** Whether the user may change admin-only settings. The server decides in the end. */
  isAdmin: boolean
  save: (input: EnvironmentInput) => Promise<void>
}>()
const emit = defineEmits<{ cancel: [] }>()

const KINDS: Record<EnvironmentKind, string> = {
  local: 'Local',
  development: 'Development',
  testing: 'Testing',
  staging: 'Staging',
  production: 'Production',
  other: 'Other',
}
const ACTIONS: Record<BrowserAction, string> = {
  navigate: 'Open pages',
  read: 'Read page content',
  click: 'Click',
  type: 'Type into fields',
  select: 'Choose options',
  upload: 'Upload files',
  download: 'Download files',
  api_read: 'API requests that read',
  api_write: 'API requests that change data',
}
const READ_ONLY: BrowserAction[] = ['navigate', 'read', 'api_read']
const AUTH_METHODS: AuthMethod[] = [
  'none',
  'form_login',
  'basic_auth',
  'bearer_token',
  'api_key',
  'sso',
  'other',
]

/** Variables are edited as text: NAME=value, or NAME=@credential-name for a secret. */
function variablesToText(variables: EnvironmentVariable[]): string {
  return variables
    .map((v) => `${v.name}=${v.secret_ref ? `@${v.secret_ref}` : (v.value ?? '')}`)
    .join('\n')
}

function textToVariables(text: string): EnvironmentVariable[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const separator = line.indexOf('=')
      const name = separator === -1 ? line : line.slice(0, separator).trim()
      const rest = separator === -1 ? '' : line.slice(separator + 1).trim()
      return rest.startsWith('@') ? { name, secret_ref: rest.slice(1) } : { name, value: rest }
    })
}

const existing = props.environment
const extraOrigins = (existing?.allowed_origins ?? []).filter(
  (origin) => origin.purpose !== 'app' && origin.purpose !== 'api',
)

const form = reactive({
  name: existing?.name ?? '',
  kind: existing?.kind ?? ('staging' as EnvironmentKind),
  base_url: existing?.base_url ?? '',
  api_base_url: existing?.api_base_url ?? '',
  additional_origins: extraOrigins.map((origin) => origin.origin).join('\n'),
  variables: variablesToText(existing?.variables ?? []),
  auth_method: existing?.auth_config.method ?? ('none' as AuthMethod),
  login_url: existing?.auth_config.login_url ?? '',
  default_credential: existing?.auth_config.default_credential ?? '',
  auth_notes: existing?.auth_config.notes ?? '',
  is_read_only: existing?.is_read_only ?? false,
  requires_approval: existing?.requires_approval ?? false,
  allow_private_network: existing?.allow_private_network ?? false,
  allowed_actions: [
    ...(existing?.allowed_actions ?? (Object.keys(ACTIONS) as BrowserAction[])),
  ] as BrowserAction[],
  status: existing?.status ?? ('active' as const),
})

const isProduction = computed(() => form.kind === 'production')
const adminOnly = computed(() => isProduction.value && !props.isAdmin)

// Production starts out as safe as possible; an admin can relax it deliberately.
watch(
  () => form.kind,
  (kind) => {
    if (kind === 'production' && !existing) {
      form.is_read_only = true
      form.requires_approval = true
    }
  },
)
watch(
  () => form.is_read_only,
  (readOnly) => {
    if (readOnly) form.allowed_actions = form.allowed_actions.filter((a) => READ_ONLY.includes(a))
  },
  { immediate: true },
)

const errors = useFormErrors()
const saving = ref(false)

async function submit(): Promise<void> {
  saving.value = true
  errors.clear()
  try {
    await props.save({
      name: form.name,
      kind: form.kind,
      base_url: form.base_url,
      api_base_url: form.api_base_url || null,
      additional_origins: form.additional_origins
        .split(/\s+/)
        .filter(Boolean)
        .map((url) => ({ url, purpose: 'auth_provider' as const })),
      variables: textToVariables(form.variables),
      auth_config: {
        method: form.auth_method,
        login_url: form.login_url || null,
        default_credential: form.default_credential || null,
        notes: form.auth_notes || null,
      },
      allowed_actions: form.allowed_actions,
      is_read_only: form.is_read_only,
      requires_approval: form.requires_approval,
      allow_private_network: form.allow_private_network,
      ...(existing ? { status: form.status } : {}),
    })
  } catch (error) {
    errors.capture(error)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <form class="stack" novalidate @submit.prevent="submit">
    <p v-if="errors.general.value" class="notice notice--danger" role="alert">
      {{ errors.general.value }}
    </p>
    <p v-if="adminOnly" class="notice">
      Production environments can only be created or changed by an organization admin.
    </p>

    <div class="form-grid">
      <FormField v-slot="field" label="Name" :error="errors.fields.name">
        <input :id="field.id" v-model.trim="form.name" class="input" maxlength="120" required />
      </FormField>
      <FormField v-slot="field" label="Kind" :error="errors.fields.kind">
        <select :id="field.id" v-model="form.kind" class="input">
          <option v-for="(label, value) in KINDS" :key="value" :value="value">{{ label }}</option>
        </select>
      </FormField>
      <FormField v-if="existing" v-slot="field" label="Status">
        <select :id="field.id" v-model="form.status" class="input">
          <option value="active">Active</option>
          <option value="disabled">Disabled</option>
        </select>
      </FormField>
    </div>

    <FormField
      v-slot="field"
      label="Base URL"
      :error="errors.fields.base_url"
      :hint="isProduction ? 'Production must use https.' : undefined"
    >
      <input
        :id="field.id"
        v-model.trim="form.base_url"
        class="input"
        type="url"
        inputmode="url"
        placeholder="https://"
        :aria-invalid="field.invalid"
        required
      />
    </FormField>
    <FormField
      v-slot="field"
      label="API base URL"
      :error="errors.fields.api_base_url"
      hint="Optional. Only if the API lives on a different address."
    >
      <input
        :id="field.id"
        v-model.trim="form.api_base_url"
        class="input"
        type="url"
        inputmode="url"
        :aria-invalid="field.invalid"
      />
    </FormField>
    <FormField
      v-slot="field"
      label="Other origins tests may visit"
      :error="errors.fields.additional_origins"
      hint="One URL per line, e.g. a separate sign-in host. Anything not listed is blocked, including redirects."
    >
      <textarea
        :id="field.id"
        v-model="form.additional_origins"
        class="input mono"
        rows="2"
        :aria-invalid="field.invalid"
      />
    </FormField>

    <fieldset class="fieldset stack">
      <legend>Safety</legend>
      <label class="checkbox">
        <input v-model="form.is_read_only" type="checkbox" />
        <span>Read-only: tests may look but not change anything</span>
      </label>
      <label class="checkbox">
        <input v-model="form.requires_approval" type="checkbox" />
        <span>Every test run needs approval first</span>
      </label>
      <label class="checkbox">
        <input v-model="form.allow_private_network" type="checkbox" :disabled="!isAdmin" />
        <span>
          Allow localhost and private network addresses
          <span class="muted">— admins only; recorded in the audit log</span>
        </span>
      </label>
      <p v-if="form.allow_private_network" class="notice">
        Tests for this environment will be able to reach machines on the platform's internal
        network. Only enable this for applications that really live there.
      </p>

      <FormField label="Allowed actions" :error="errors.fields.allowed_actions">
        <div class="actions">
          <label v-for="(label, action) in ACTIONS" :key="action" class="checkbox">
            <input
              v-model="form.allowed_actions"
              type="checkbox"
              :value="action"
              :disabled="
                action === 'navigate' || (form.is_read_only && !READ_ONLY.includes(action))
              "
            />
            <span>{{ label }}</span>
          </label>
        </div>
      </FormField>
    </fieldset>

    <fieldset class="fieldset stack">
      <legend>Sign-in</legend>
      <div class="form-grid">
        <FormField v-slot="field" label="Method">
          <select :id="field.id" v-model="form.auth_method" class="input">
            <option v-for="method in AUTH_METHODS" :key="method" :value="method">
              {{ method.replace('_', ' ') }}
            </option>
          </select>
        </FormField>
        <FormField v-if="existing" v-slot="field" label="Default test account">
          <select :id="field.id" v-model="form.default_credential" class="input">
            <option value="">None</option>
            <option v-for="name in credentialNames ?? []" :key="name" :value="name">
              {{ name }}
            </option>
          </select>
        </FormField>
      </div>
      <FormField v-slot="field" label="Login page URL" :error="errors.fields.auth_config">
        <input
          :id="field.id"
          v-model.trim="form.login_url"
          class="input"
          type="url"
          :aria-invalid="field.invalid"
        />
      </FormField>
      <FormField
        v-slot="field"
        label="Notes for testers"
        hint="Never write passwords here. Store them as credentials."
      >
        <textarea :id="field.id" v-model.trim="form.auth_notes" class="input" maxlength="4000" />
      </FormField>
    </fieldset>

    <FormField
      v-slot="field"
      label="Variables"
      :error="errors.fields.variables"
      hint="One per line: NAME=value. For anything secret, save a credential and write NAME=@credential-name."
    >
      <textarea
        :id="field.id"
        v-model="form.variables"
        class="input mono"
        rows="3"
        placeholder="FEATURE_FLAG=on"
        :aria-invalid="field.invalid"
      />
    </FormField>

    <div class="cluster">
      <BaseButton type="submit" :loading="saving" :disabled="adminOnly">
        {{ existing ? 'Save changes' : 'Add environment' }}
      </BaseButton>
      <BaseButton variant="ghost" @click="emit('cancel')">Cancel</BaseButton>
    </div>
  </form>
</template>

<style scoped>
.fieldset {
  margin: 0;
  padding: var(--space-4);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
}

.fieldset legend {
  padding: 0 var(--space-2);
  font-size: var(--text-sm);
  font-weight: 600;
}

.actions {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(13rem, 1fr));
  gap: var(--space-2);
}
</style>
