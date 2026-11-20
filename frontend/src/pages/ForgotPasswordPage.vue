<script setup lang="ts">
import { ref } from 'vue'
import { RouterLink } from 'vue-router'

import { authApi } from '@/api/auth'
import BaseButton from '@/components/ui/BaseButton.vue'
import FormField from '@/components/ui/FormField.vue'
import { useFormErrors } from '@/composables/useFormErrors'

const errors = useFormErrors()
const email = ref('')
const submitting = ref(false)
const sent = ref(false)

async function submit(): Promise<void> {
  submitting.value = true
  errors.clear()
  try {
    await authApi.forgotPassword(email.value)
    sent.value = true
  } catch (error) {
    errors.capture(error)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div v-if="sent" class="stack">
    <h1>Check your email</h1>
    <!-- The same message whether or not the address has an account. -->
    <p>
      If an account exists for <strong>{{ email }}</strong>, a link to choose a new password has
      been sent to it. The link works once and for a limited time.
    </p>
    <RouterLink :to="{ name: 'login' }">Back to sign in</RouterLink>
  </div>
  <form v-else class="stack" novalidate @submit.prevent="submit">
    <h1>Reset your password</h1>
    <p class="muted small">Enter the email address of your account.</p>
    <p v-if="errors.general.value" class="notice notice--danger" role="alert">
      {{ errors.general.value }}
    </p>
    <FormField v-slot="field" label="Email" :error="errors.fields.email">
      <input
        :id="field.id"
        v-model.trim="email"
        class="input"
        type="email"
        autocomplete="username"
        required
      />
    </FormField>
    <BaseButton type="submit" :loading="submitting" :disabled="!email">Send reset link</BaseButton>
    <p class="small muted"><RouterLink :to="{ name: 'login' }">Back to sign in</RouterLink></p>
  </form>
</template>
