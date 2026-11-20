<script setup lang="ts">
import { ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import BaseButton from '@/components/ui/BaseButton.vue'
import FormField from '@/components/ui/FormField.vue'
import { useFormErrors } from '@/composables/useFormErrors'
import { safeRedirect } from '@/router/redirect'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const errors = useFormErrors()

const email = ref('')
const password = ref('')
const submitting = ref(false)

async function submit(): Promise<void> {
  submitting.value = true
  errors.clear()
  try {
    await auth.login(email.value, password.value)
    await router.replace(safeRedirect(route.query.redirect))
  } catch (error) {
    errors.capture(error)
    password.value = ''
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <form class="stack" novalidate @submit.prevent="submit">
    <h1>Sign in</h1>
    <p v-if="errors.general.value" class="notice notice--danger" role="alert">
      {{ errors.general.value }}
    </p>
    <FormField v-slot="field" label="Email">
      <input
        :id="field.id"
        v-model.trim="email"
        class="input"
        type="email"
        autocomplete="username"
        required
      />
    </FormField>
    <FormField v-slot="field" label="Password">
      <input
        :id="field.id"
        v-model="password"
        class="input"
        type="password"
        autocomplete="current-password"
        required
      />
    </FormField>
    <BaseButton type="submit" :loading="submitting">Sign in</BaseButton>
    <p class="small">
      <RouterLink :to="{ name: 'forgot-password' }">Forgot your password?</RouterLink>
    </p>
    <p class="small muted">
      New here? <RouterLink :to="{ name: 'register' }">Create an account</RouterLink>
    </p>
  </form>
</template>
