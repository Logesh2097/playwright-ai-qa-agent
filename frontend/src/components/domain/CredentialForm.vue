<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'

import BaseButton from '@/components/ui/BaseButton.vue'
import FormField from '@/components/ui/FormField.vue'
import { useFormErrors } from '@/composables/useFormErrors'
import type { Credential, CredentialInput, CredentialKind } from '@/types/api'

/**
 * Secret values are write-only. This form never receives one from the server:
 * when editing, the secret fields start empty and are only sent if filled in.
 */
const props = defineProps<{
  /** Present when editing an existing credential. */
  credential?: Credential
  save: (input: Partial<CredentialInput>) => Promise<void>
}>()
const emit = defineEmits<{ cancel: [] }>()

interface FieldSpec {
  name: string
  label: string
  /** Shown as dots while typing. */
  masked: boolean
}

const KINDS: Record<CredentialKind, { label: string; fields: FieldSpec[] }> = {
  username_password: {
    label: 'Username and password',
    fields: [
      { name: 'username', label: 'Username', masked: false },
      { name: 'password', label: 'Password', masked: true },
    ],
  },
  basic_auth: {
    label: 'HTTP basic auth',
    fields: [
      { name: 'username', label: 'Username', masked: false },
      { name: 'password', label: 'Password', masked: true },
    ],
  },
  bearer_token: {
    label: 'Bearer token',
    fields: [{ name: 'token', label: 'Token', masked: true }],
  },
  api_key_header: {
    label: 'API key in a header',
    fields: [
      { name: 'header_name', label: 'Header name', masked: false },
      { name: 'value', label: 'Key', masked: true },
    ],
  },
  cookie: {
    label: 'Cookie',
    fields: [
      { name: 'name', label: 'Cookie name', masked: false },
      { name: 'value', label: 'Cookie value', masked: true },
    ],
  },
  totp_secret: {
    label: 'One-time code secret (TOTP)',
    fields: [{ name: 'secret', label: 'Secret', masked: true }],
  },
  secret_value: {
    label: 'Single secret value',
    fields: [{ name: 'value', label: 'Value', masked: true }],
  },
}

const existing = props.credential
const form = reactive({
  name: existing?.name ?? '',
  role_label: existing?.role_label ?? '',
  kind: existing?.kind ?? ('username_password' as CredentialKind),
  source: existing?.source ?? ('stored' as 'stored' | 'reference'),
  external_ref: existing?.external_ref ?? '',
  setup_instructions: existing?.setup_instructions ?? '',
  is_test_account: Boolean(existing),
})
// Kept apart from `form` so it is obvious what must be wiped.
const secret = reactive<Record<string, string>>({})

const fields = computed(() => KINDS[form.kind].fields)
const secretEntered = computed(() =>
  fields.value.some((field) => (secret[field.name] ?? '') !== ''),
)

function wipeSecret(): void {
  for (const key of Object.keys(secret)) delete secret[key]
}

watch(() => form.kind, wipeSecret)

const errors = useFormErrors()
const saving = ref(false)

async function submit(): Promise<void> {
  saving.value = true
  errors.clear()
  const input: Partial<CredentialInput> = {
    name: form.name,
    role_label: form.role_label,
    setup_instructions: form.setup_instructions || null,
  }
  if (!existing) {
    input.kind = form.kind
    input.is_test_account = form.is_test_account
  }
  if (form.source === 'reference') {
    if (!existing || form.external_ref !== existing.external_ref) {
      input.external_ref = form.external_ref
    }
  } else if (!existing || secretEntered.value) {
    input.secret = Object.fromEntries(
      fields.value.map((field) => [field.name, secret[field.name] ?? '']),
    )
  }
  try {
    await props.save(input)
    wipeSecret()
  } catch (error) {
    errors.capture(error)
  } finally {
    saving.value = false
  }
}

function cancel(): void {
  wipeSecret()
  emit('cancel')
}
</script>

<template>
  <!-- autocomplete off: a browser password manager must not save test credentials as the user's own -->
  <form class="stack" novalidate autocomplete="off" @submit.prevent="submit">
    <p v-if="errors.general.value" class="notice notice--danger" role="alert">
      {{ errors.general.value }}
    </p>

    <div class="form-grid">
      <FormField
        v-slot="field"
        label="Name"
        :error="errors.fields.name"
        hint="How this credential is referred to, e.g. customer-account"
      >
        <input :id="field.id" v-model.trim="form.name" class="input" maxlength="120" required />
      </FormField>
      <FormField
        v-slot="field"
        label="Role in tests"
        :error="errors.fields.role_label"
        hint="e.g. customer, admin"
      >
        <input
          :id="field.id"
          v-model.trim="form.role_label"
          class="input"
          maxlength="200"
          required
        />
      </FormField>
    </div>

    <div class="form-grid">
      <FormField v-slot="field" label="Type" :error="errors.fields.kind">
        <select :id="field.id" v-model="form.kind" class="input" :disabled="Boolean(existing)">
          <option v-for="(kind, value) in KINDS" :key="value" :value="value">
            {{ kind.label }}
          </option>
        </select>
      </FormField>
      <FormField v-slot="field" label="Where the secret is kept">
        <select :id="field.id" v-model="form.source" class="input">
          <option value="stored">Encrypted in QA Agent</option>
          <option value="reference">Elsewhere (reference only)</option>
        </select>
      </FormField>
    </div>

    <template v-if="form.source === 'stored'">
      <p v-if="existing" class="small muted">
        The current secret is stored encrypted and cannot be displayed. Fill in the fields below
        only if you want to replace it.
      </p>
      <div class="form-grid">
        <FormField
          v-for="spec in fields"
          :key="`${form.kind}-${spec.name}`"
          v-slot="field"
          :label="spec.label"
          :error="errors.fields.secret"
        >
          <input
            :id="field.id"
            v-model="secret[spec.name]"
            class="input"
            :type="spec.masked ? 'password' : 'text'"
            autocomplete="off"
            :autocapitalize="'off'"
            spellcheck="false"
            data-1p-ignore
            data-lpignore="true"
            :placeholder="existing ? 'Unchanged' : ''"
            :required="!existing"
          />
        </FormField>
      </div>
    </template>
    <FormField
      v-else
      v-slot="field"
      label="Reference"
      :error="errors.fields.external_ref"
      hint="Name of an environment variable on the test runner, written as env:VARIABLE_NAME. The value itself is never stored here."
    >
      <input
        :id="field.id"
        v-model.trim="form.external_ref"
        class="input mono"
        placeholder="env:STAGING_CUSTOMER_PASSWORD"
        :aria-invalid="field.invalid"
        required
      />
    </FormField>

    <FormField
      v-slot="field"
      label="Setup instructions"
      :error="errors.fields.setup_instructions"
      hint="What a tester should know about this account. Do not include the password."
    >
      <textarea
        :id="field.id"
        v-model.trim="form.setup_instructions"
        class="input"
        maxlength="4000"
      />
    </FormField>

    <FormField v-if="!existing" label="Confirmation" :error="errors.fields.is_test_account">
      <label class="checkbox">
        <input v-model="form.is_test_account" type="checkbox" />
        <span>
          This is a dedicated test account. It does not belong to a real customer or colleague.
        </span>
      </label>
    </FormField>

    <div class="cluster">
      <BaseButton type="submit" :loading="saving" :disabled="!existing && !form.is_test_account">
        {{ existing ? 'Save' : 'Store credential' }}
      </BaseButton>
      <BaseButton variant="ghost" @click="cancel">Cancel</BaseButton>
    </div>
  </form>
</template>
