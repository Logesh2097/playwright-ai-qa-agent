<script setup lang="ts">
import { reactive, ref } from 'vue'

import BaseButton from '@/components/ui/BaseButton.vue'
import FormField from '@/components/ui/FormField.vue'
import { useFormErrors } from '@/composables/useFormErrors'
import type { Application, ApplicationInput, AuthMethod, BrowserName } from '@/types/api'

const props = defineProps<{
  /** Present when editing. */
  application?: Application
  /** Performs the save; the form shows whatever error it throws. */
  save: (input: ApplicationInput) => Promise<void>
}>()
const emit = defineEmits<{ cancel: [] }>()

const AUTH_METHODS: Record<AuthMethod, string> = {
  none: 'No sign-in',
  form_login: 'Login form',
  basic_auth: 'HTTP basic auth',
  bearer_token: 'Bearer token',
  api_key: 'API key',
  sso: 'Single sign-on',
  other: 'Other',
}
const BROWSERS: BrowserName[] = ['chromium', 'firefox', 'webkit']

const existing = props.application
const form = reactive({
  name: existing?.name ?? '',
  description: existing?.description ?? '',
  base_url: existing?.base_url ?? '',
  type: existing?.type ?? ('web' as const),
  framework: existing?.framework ?? '',
  auth_method: existing?.auth_method ?? ('none' as AuthMethod),
  browser: existing?.default_browser_config.browser ?? ('chromium' as BrowserName),
  width: existing?.default_browser_config.viewport?.width ?? 1440,
  height: existing?.default_browser_config.viewport?.height ?? 900,
  // One host name per line.
  allowed_domains: (existing?.allowed_domains ?? []).join('\n'),
})

const errors = useFormErrors()
const saving = ref(false)

async function submit(): Promise<void> {
  saving.value = true
  errors.clear()
  const domains = form.allowed_domains
    .split(/[\s,]+/)
    .map((entry) => entry.trim())
    .filter(Boolean)
  try {
    await props.save({
      name: form.name,
      description: form.description || null,
      base_url: form.base_url,
      type: form.type,
      framework: form.framework || null,
      auth_method: form.auth_method,
      default_browser_config: {
        browser: form.browser,
        viewport: { width: Number(form.width), height: Number(form.height) },
      },
      // Left empty on creation, the server uses the base URL's host.
      ...(domains.length > 0 ? { allowed_domains: domains } : {}),
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

    <div class="form-grid">
      <FormField v-slot="field" label="Name" :error="errors.fields.name">
        <input :id="field.id" v-model.trim="form.name" class="input" maxlength="120" required />
      </FormField>
      <FormField v-slot="field" label="Type" :error="errors.fields.type">
        <select :id="field.id" v-model="form.type" class="input">
          <option value="web">Web application</option>
          <option value="api">API</option>
        </select>
      </FormField>
    </div>

    <FormField
      v-slot="field"
      label="Base URL"
      :error="errors.fields.base_url"
      hint="The address people normally open in a browser."
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
      label="Allowed domains"
      :error="errors.fields.allowed_domains"
      hint="One host name per line. Each covers its subdomains. Tests can never leave these domains. Leave empty to use the base URL's host."
    >
      <textarea
        :id="field.id"
        v-model="form.allowed_domains"
        class="input mono"
        rows="3"
        placeholder="example.com"
        :aria-invalid="field.invalid"
      />
    </FormField>

    <FormField v-slot="field" label="Description" :error="errors.fields.description">
      <textarea :id="field.id" v-model.trim="form.description" class="input" maxlength="2000" />
    </FormField>

    <div class="form-grid">
      <FormField
        v-slot="field"
        label="Framework"
        :error="errors.fields.framework"
        hint="Optional, e.g. Vue 3 + Laravel"
      >
        <input :id="field.id" v-model.trim="form.framework" class="input" maxlength="200" />
      </FormField>
      <FormField v-slot="field" label="How users sign in" :error="errors.fields.auth_method">
        <select :id="field.id" v-model="form.auth_method" class="input">
          <option v-for="(label, method) in AUTH_METHODS" :key="method" :value="method">
            {{ label }}
          </option>
        </select>
      </FormField>
    </div>

    <fieldset class="fieldset">
      <legend>Default browser</legend>
      <div class="form-grid">
        <FormField v-slot="field" label="Browser">
          <select :id="field.id" v-model="form.browser" class="input">
            <option v-for="browser in BROWSERS" :key="browser" :value="browser">
              {{ browser }}
            </option>
          </select>
        </FormField>
        <FormField
          v-slot="field"
          label="Viewport width"
          :error="errors.fields.default_browser_config"
        >
          <input
            :id="field.id"
            v-model.number="form.width"
            class="input"
            type="number"
            min="240"
            max="7680"
          />
        </FormField>
        <FormField v-slot="field" label="Viewport height">
          <input
            :id="field.id"
            v-model.number="form.height"
            class="input"
            type="number"
            min="240"
            max="4320"
          />
        </FormField>
      </div>
    </fieldset>

    <div class="cluster">
      <BaseButton type="submit" :loading="saving">
        {{ existing ? 'Save changes' : 'Register application' }}
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
</style>
