<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import { authApi } from '@/api/auth'
import BaseButton from '@/components/ui/BaseButton.vue'
import FormField from '@/components/ui/FormField.vue'
import { useFormErrors } from '@/composables/useFormErrors'

const route = useRoute()
const errors = useFormErrors()
const token = computed(() => (typeof route.query.token === 'string' ? route.query.token : ''))
const password = ref('')
const confirmation = ref('')
const submitting = ref(false)
const done = ref(false)
const mismatch = computed(() => confirmation.value !== '' && confirmation.value !== password.value)

async function submit(): Promise<void> {
  if (mismatch.value) return
  submitting.value = true
  errors.clear()
  try {
    await authApi.resetPassword(token.value, password.value)
    done.value = true
  } catch (error) {
    errors.capture(error)
  } finally {
    submitting.value = false
    password.value = ''
    confirmation.value = ''
  }
}
</script>

<template>
  <div v-if="!token" class="stack">
    <h1>This link is not complete</h1>
    <p>Open the link from the email again, or ask for a new one.</p>
    <RouterLink :to="{ name: 'forgot-password' }">Request a new link</RouterLink>
  </div>
  <div v-else-if="done" class="stack">
    <h1>Password changed</h1>
    <p>Every session of your account has been signed out. Sign in with the new password.</p>
    <RouterLink :to="{ name: 'login' }">Sign in</RouterLink>
  </div>
  <form v-else class="stack" novalidate @submit.prevent="submit">
    <h1>Choose a new password</h1>
    <p v-if="errors.general.value" class="notice notice--danger" role="alert">
      {{ errors.general.value }}
      <RouterLink :to="{ name: 'forgot-password' }">Request a new link</RouterLink>
    </p>
    <FormField
      v-slot="field"
      label="New password"
      :error="errors.fields.new_password ?? errors.fields.password"
      hint="At least 12 characters. Not your name or email address."
    >
      <input
        :id="field.id"
        v-model="password"
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
        v-model="confirmation"
        class="input"
        type="password"
        autocomplete="new-password"
        required
      />
    </FormField>
    <BaseButton type="submit" :loading="submitting" :disabled="!password || mismatch">
      Change password
    </BaseButton>
  </form>
</template>
