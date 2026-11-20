<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import BaseButton from '@/components/ui/BaseButton.vue'
import FormField from '@/components/ui/FormField.vue'
import { useFormErrors } from '@/composables/useFormErrors'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const errors = useFormErrors()

// An invitation link carries ?token=...; the account then joins that organization.
const inviteToken = computed(() =>
  typeof route.query.token === 'string' ? route.query.token : undefined,
)

const fullName = ref('')
const email = ref('')
const organizationName = ref('')
const password = ref('')
const submitting = ref(false)

async function submit(): Promise<void> {
  submitting.value = true
  errors.clear()
  try {
    await auth.register({
      full_name: fullName.value,
      email: email.value,
      password: password.value,
      ...(inviteToken.value
        ? { invite_token: inviteToken.value }
        : { organization_name: organizationName.value }),
    })
    await router.replace({ name: 'home' })
  } catch (error) {
    errors.capture(error)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <form class="stack" novalidate @submit.prevent="submit">
    <h1>{{ inviteToken ? 'Accept invitation' : 'Create your account' }}</h1>
    <p v-if="errors.general.value" class="notice notice--danger" role="alert">
      {{ errors.general.value }}
    </p>
    <FormField v-slot="field" label="Your name" :error="errors.fields.full_name">
      <input :id="field.id" v-model.trim="fullName" class="input" autocomplete="name" required />
    </FormField>
    <FormField
      v-slot="field"
      label="Work email"
      :error="errors.fields.email"
      :hint="inviteToken ? 'Use the address the invitation was sent to.' : undefined"
    >
      <input
        :id="field.id"
        v-model.trim="email"
        class="input"
        type="email"
        autocomplete="username"
        required
      />
    </FormField>
    <FormField
      v-if="!inviteToken"
      v-slot="field"
      label="Organization name"
      :error="errors.fields.organization_name"
    >
      <input :id="field.id" v-model.trim="organizationName" class="input" required />
    </FormField>
    <FormField
      v-slot="field"
      label="Password"
      :error="errors.fields.password"
      hint="At least 12 characters. A phrase of several words works well."
    >
      <input
        :id="field.id"
        v-model="password"
        class="input"
        type="password"
        autocomplete="new-password"
        :aria-invalid="field.invalid"
        required
      />
    </FormField>
    <BaseButton type="submit" :loading="submitting">Create account</BaseButton>
    <p class="small muted">
      Already registered? <RouterLink :to="{ name: 'login' }">Sign in</RouterLink>
    </p>
  </form>
</template>
